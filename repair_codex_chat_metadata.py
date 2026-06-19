#!/usr/bin/env python3
"""Repair visible metadata for active Codex chats.

This script fixes active, non-archived chats whose display metadata was lost
(`title`, `preview`, or `first_user_message` is empty). It also repairs missing
`thread_source` for active VS Code/Codex Desktop threads and appends current
names to `session_index.jsonl`.

It can also sanitize unusually large rollout JSONL files by replacing very large
embedded strings, such as base64 screenshots, with small placeholders. This keeps
the rollout valid while making it possible for Codex to index/read the thread
again after a restart.

It intentionally does not unarchive chats and does not move rollout files.
Close Codex before running it.
"""

import argparse
import json
import os
import plistlib
import re
import select
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path


DEFAULT_LARGE_ROLLOUT_BYTES = 50 * 1024 * 1024
DEFAULT_LARGE_STRING_CHARS = 20_000
BROKEN_IMAGE_URL_PLACEHOLDER = b'"image_url":"[omitted large string by codex-chat-metadata-repair:'
LAUNCH_AGENT_LABEL = "com.codex-chat-metadata-repair"


@dataclass(frozen=True)
class RepairOptions:
    dry_run: bool
    large_rollout_bytes: int
    large_string_chars: int
    skip_large_rollout_sanitize: bool
    skip_session_index_refresh: bool
    metadata_only: bool
    thread_ids: tuple[str, ...]


def default_codex_home() -> Path:
    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".codex"


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def clean_text(text: str | None) -> str:
    text = (text or "").replace("\r", "\n").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def compact_preview(text: str, limit: int = 240) -> str:
    return " ".join(clean_text(text).split())[:limit]


def attachment_paths_from_message(message: str, codex_home: Path) -> list[Path]:
    candidates: list[Path] = []

    # Portable match for explicit pasted-text attachment paths on Unix and Windows.
    for match in re.findall(
        r"([A-Za-z]:[^\n\r\"']*?pasted-text\.txt|/[^\n\r\"']*?pasted-text\.txt)",
        message,
    ):
        candidates.append(Path(match.strip()))

    # Rollouts often mention attachments by filename only in a markdown preamble.
    # If no absolute path was parseable, scan .codex/attachments newest-first.
    if "pasted text" in message.lower() or "pasted-text.txt" in message.lower():
        attachments = codex_home / "attachments"
        if attachments.exists():
            candidates.extend(
                sorted(
                    attachments.glob("*/pasted-text.txt"),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )[:20]
            )

    unique: list[Path] = []
    seen = set()
    for path in candidates:
        key = str(path)
        if key not in seen:
            unique.append(path)
            seen.add(key)
    return unique


def first_user_text(rollout_path: Path, codex_home: Path) -> str:
    if not rollout_path.exists():
        return ""

    try:
        lines = rollout_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""

    fallback_message = ""
    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        payload = obj.get("payload") or {}
        message = ""
        if obj.get("type") == "event_msg" and payload.get("type") == "user_message":
            message = payload.get("message") or ""
        elif obj.get("type") == "user_message":
            message = payload.get("message") or ""

        message = clean_text(message)
        if not message:
            continue

        if not fallback_message:
            fallback_message = message

        for path in attachment_paths_from_message(message, codex_home):
            if path.exists():
                try:
                    text = clean_text(
                        path.read_text(encoding="utf-8", errors="replace")
                    )
                except OSError:
                    continue
                if text:
                    return text

        # Ignore the generic attachment wrapper if we could not resolve the file.
        if "The attached pasted text file(s) contain the user's request" not in message:
            return message

    return fallback_message


def make_title(text: str, fallback: str) -> str:
    for line in clean_text(text).splitlines():
        line = line.strip(" #-\t")
        if not line:
            continue
        if line.lower().startswith("files mentioned by the user"):
            continue
        return line[:90]
    return fallback


@dataclass(frozen=True)
class KnownMetadata:
    title: str
    preview: str
    first_user_message: str
    thread_source: str


def state_db_paths(codex_home: Path) -> list[Path]:
    candidates = [
        codex_home / "state_5.sqlite",
        codex_home / "sqlite" / "state_5.sqlite",
    ]
    paths: list[Path] = []
    seen = set()
    for path in candidates:
        if not path.exists():
            continue
        key = str(path.resolve())
        if key not in seen:
            paths.append(path)
            seen.add(key)
    return paths


def backup_files(codex_home: Path, db_paths: list[Path], backup_dir: Path) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    for db_path in db_paths:
        relative = db_path.relative_to(codex_home)
        for source in (
            db_path,
            db_path.with_name(f"{db_path.name}-wal"),
            db_path.with_name(f"{db_path.name}-shm"),
        ):
            if source.exists():
                target = backup_dir / relative.parent / source.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

    for source in (codex_home / "session_index.jsonl",):
        if source.exists():
            shutil.copy2(source, backup_dir / source.name)


def append_session_index(codex_home: Path, thread_id: str, title: str) -> None:
    entry = {
        "id": thread_id,
        "thread_name": title,
        "updated_at": now_rfc3339(),
    }
    with (codex_home / "session_index.jsonl").open("a", encoding="utf-8") as file:
        file.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")


def backup_rollout(rollout_path: Path, backup_dir: Path, thread_id: str) -> None:
    rollout_backup = backup_dir / "rollouts" / f"{thread_id}.jsonl"
    rollout_backup.parent.mkdir(parents=True, exist_ok=True)
    if not rollout_backup.exists():
        shutil.copy2(rollout_path, rollout_backup)


class BackupManager:
    def __init__(self, codex_home: Path, db_paths: list[Path], backup_dir: Path):
        self.codex_home = codex_home
        self.db_paths = db_paths
        self.backup_dir = backup_dir
        self._state_files_backed_up = False

    def backup_state_files(self) -> None:
        if self._state_files_backed_up:
            return
        backup_files(self.codex_home, self.db_paths, self.backup_dir)
        self._state_files_backed_up = True

    def backup_rollout(self, rollout_path: Path, thread_id: str) -> None:
        backup_rollout(rollout_path, self.backup_dir, thread_id)

    def created_backup(self) -> bool:
        return self.backup_dir.exists()


def repair_rollout_header(
    rollout_path: Path,
    backups: BackupManager,
    thread_id: str,
    dry_run: bool,
) -> bool:
    if not rollout_path.exists():
        return False
    try:
        original_stat = rollout_path.stat()
        lines = rollout_path.read_text(encoding="utf-8", errors="replace").splitlines(
            keepends=True
        )
    except OSError:
        return False
    if not lines:
        return False

    try:
        first_obj = json.loads(lines[0])
    except json.JSONDecodeError:
        return False

    payload = first_obj.get("payload")
    if first_obj.get("type") != "session_meta" or not isinstance(payload, dict):
        return False
    if payload.get("thread_source") == "user":
        return False

    if not dry_run:
        backups.backup_rollout(rollout_path, thread_id)
        payload["thread_source"] = "user"
        lines[0] = (
            json.dumps(first_obj, ensure_ascii=False, separators=(",", ":")) + "\n"
        )
        current_stat = rollout_path.stat()
        if (
            current_stat.st_ino != original_stat.st_ino
            or current_stat.st_size != original_stat.st_size
            or current_stat.st_mtime_ns != original_stat.st_mtime_ns
        ):
            print(f"skipped changed rollout header {thread_id}")
            return False
        rollout_path.write_text("".join(lines), encoding="utf-8")
    return True


@dataclass(frozen=True)
class ActiveThread:
    thread_id: str
    title: str
    rollout_path: Path


def load_active_threads(db_paths: list[Path]) -> dict[str, ActiveThread]:
    threads: dict[str, ActiveThread] = {}
    for db_path in db_paths:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                select id, title, rollout_path
                from threads
                where archived = 0
                  and coalesce(title, '') <> ''
                  and coalesce(rollout_path, '') <> ''
                """
            )
        except sqlite3.Error:
            conn.close()
            continue

        for row in rows:
            threads[row["id"]] = ActiveThread(
                thread_id=row["id"],
                title=row["title"],
                rollout_path=Path(row["rollout_path"]),
            )
        conn.close()
    return threads


def shrink_large_strings(value: object, max_chars: int) -> tuple[object, int]:
    if isinstance(value, str):
        if len(value) > max_chars:
            return (
                f"[omitted large string by codex-chat-metadata-repair: {len(value)} chars]",
                1,
            )
        return value, 0
    if isinstance(value, list):
        changed = 0
        shrunk_items = []
        for item in value:
            shrunk, item_changed = shrink_large_strings(item, max_chars)
            changed += item_changed
            shrunk_items.append(shrunk)
        return shrunk_items, changed
    if isinstance(value, dict):
        changed = 0
        shrunk_dict = {}
        for key, item in value.items():
            shrunk, item_changed = shrink_large_strings(item, max_chars)
            changed += item_changed
            shrunk_dict[key] = shrunk
        return shrunk_dict, changed
    return value, 0


def image_url_needs_replacement(value: object, max_chars: int) -> bool:
    if not isinstance(value, str):
        return True
    if len(value) > max_chars:
        return True
    if value.startswith("[omitted large string by codex-chat-metadata-repair:"):
        return True
    return not (
        value.startswith("https://")
        or value.startswith("http://")
        or value.startswith("data:image/")
    )


def image_url_is_invalid(value: object) -> bool:
    if not isinstance(value, str):
        return True
    if value.startswith("[omitted large string by codex-chat-metadata-repair:"):
        return True
    return not (
        value.startswith("https://")
        or value.startswith("http://")
        or value.startswith("data:image/")
    )


def sanitize_rollout_value(value: object, max_chars: int) -> tuple[object, int]:
    if isinstance(value, dict):
        image_url = value.get("image_url")
        if "image_url" in value and image_url_needs_replacement(image_url, max_chars):
            image_url_len = len(image_url) if isinstance(image_url, str) else 0
            image_type = value.get("type") or "image"
            return (
                {
                    "type": "input_text",
                    "text": (
                        "[omitted image by codex-chat-metadata-repair: "
                        f"type={image_type} image_url_chars={image_url_len}]"
                    ),
                },
                1,
            )

        changed = 0
        sanitized_dict = {}
        for key, item in value.items():
            sanitized, item_changed = sanitize_rollout_value(item, max_chars)
            changed += item_changed
            sanitized_dict[key] = sanitized
        return sanitized_dict, changed

    if isinstance(value, list):
        changed = 0
        sanitized_items = []
        for item in value:
            sanitized, item_changed = sanitize_rollout_value(item, max_chars)
            changed += item_changed
            sanitized_items.append(sanitized)
        return sanitized_items, changed

    return shrink_large_strings(value, max_chars)


def value_has_invalid_image_url(value: object) -> bool:
    if isinstance(value, dict):
        if "image_url" in value and image_url_is_invalid(value.get("image_url")):
            return True
        return any(value_has_invalid_image_url(item) for item in value.values())

    if isinstance(value, list):
        return any(value_has_invalid_image_url(item) for item in value)

    return False


def rollout_needs_sanitize(
    rollout_path: Path,
    min_bytes: int,
) -> bool:
    try:
        if not rollout_path.exists():
            return False
        if rollout_path.stat().st_size >= min_bytes:
            return True

        has_image_url = False
        with rollout_path.open("rb") as source:
            previous = b""
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                window = previous + chunk
                if BROKEN_IMAGE_URL_PLACEHOLDER in window:
                    return True
                if b'"image_url"' in window:
                    has_image_url = True
                previous = window[-len(BROKEN_IMAGE_URL_PLACEHOLDER) :]
        if not has_image_url:
            return False

        with rollout_path.open("rb") as source:
            for raw in source:
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if value_has_invalid_image_url(obj):
                    return True
        return False
    except OSError:
        return False


def sanitize_large_rollout(
    rollout_path: Path,
    backups: BackupManager,
    thread_id: str,
    max_string_chars: int,
    dry_run: bool,
) -> tuple[bool, int, int, int]:
    changed_records = 0
    changed_strings = 0
    original_bytes = 0
    new_bytes = 0

    try:
        original_stat = rollout_path.stat()
        source = rollout_path.open("rb")
    except OSError:
        return False, 0, 0, 0

    with source:
        if dry_run:
            for raw in source:
                original_bytes += len(raw)
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    new_bytes += len(raw)
                    continue
                shrunk, line_changed_strings = sanitize_rollout_value(
                    obj,
                    max_string_chars,
                )
                if line_changed_strings:
                    changed_records += 1
                    changed_strings += line_changed_strings
                out = json.dumps(
                    shrunk,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ) + "\n"
                new_bytes += len(out.encode("utf-8"))
            return (
                changed_records > 0,
                changed_records,
                changed_strings,
                original_bytes - new_bytes,
            )

        fd, tmp_name = tempfile.mkstemp(
            prefix=f"{rollout_path.name}.", suffix=".tmp", dir=rollout_path.parent
        )
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            with tmp_path.open("w", encoding="utf-8") as target:
                for raw in source:
                    original_bytes += len(raw)
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        text = raw.decode("utf-8", errors="replace")
                        target.write(text)
                        new_bytes += len(text.encode("utf-8"))
                        continue

                    shrunk, line_changed_strings = sanitize_rollout_value(
                        obj,
                        max_string_chars,
                    )
                    if line_changed_strings:
                        changed_records += 1
                        changed_strings += line_changed_strings
                    out = json.dumps(
                        shrunk,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ) + "\n"
                    target.write(out)
                    new_bytes += len(out.encode("utf-8"))

            if changed_records:
                with tmp_path.open(encoding="utf-8") as validate:
                    for line in validate:
                        json.loads(line)
                backups.backup_rollout(rollout_path, thread_id)
                current_stat = rollout_path.stat()
                if (
                    current_stat.st_ino != original_stat.st_ino
                    or current_stat.st_size != original_stat.st_size
                    or current_stat.st_mtime_ns != original_stat.st_mtime_ns
                ):
                    print(f"skipped changed rollout {thread_id}")
                    tmp_path.unlink(missing_ok=True)
                    return False, 0, 0, 0
                shutil.copystat(rollout_path, tmp_path)
                tmp_path.replace(rollout_path)
            else:
                tmp_path.unlink(missing_ok=True)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    return changed_records > 0, changed_records, changed_strings, original_bytes - new_bytes


def sanitize_large_rollouts(
    active_threads: dict[str, ActiveThread],
    backups: BackupManager,
    options: RepairOptions,
) -> int:
    if options.skip_large_rollout_sanitize:
        return 0

    fixed = 0
    for thread in active_threads.values():
        if not rollout_needs_sanitize(
            thread.rollout_path,
            options.large_rollout_bytes,
        ):
            continue
        changed, changed_records, changed_strings, saved_bytes = sanitize_large_rollout(
            thread.rollout_path,
            backups,
            thread.thread_id,
            options.large_string_chars,
            options.dry_run,
        )
        if not changed:
            continue
        action = "would sanitize" if options.dry_run else "sanitized"
        print(
            f"{action} rollout {thread.thread_id}: "
            f"records={changed_records} strings={changed_strings} "
            f"bytes_saved={max(saved_bytes, 0)}"
        )
        fixed += 1
    return fixed


def refresh_session_index(
    codex_home: Path,
    active_threads: dict[str, ActiveThread],
    backups: BackupManager,
    options: RepairOptions,
) -> int:
    if options.skip_session_index_refresh:
        return 0

    index_path = codex_home / "session_index.jsonl"
    if not index_path.exists():
        return 0

    try:
        lines = index_path.read_text(encoding="utf-8", errors="replace").splitlines(
            keepends=True
        )
    except OSError:
        return 0

    changed = 0
    refreshed: list[str] = []
    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            refreshed.append(line)
            continue

        thread = active_threads.get(obj.get("id"))
        if thread and obj.get("thread_name") != thread.title:
            obj["thread_name"] = thread.title
            obj["updated_at"] = now_rfc3339()
            line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
            changed += 1
        refreshed.append(line)

    if changed and not options.dry_run:
        backups.backup_state_files()
        index_path.write_text("".join(refreshed), encoding="utf-8")

    if changed:
        print(
            f"{'would refresh' if options.dry_run else 'refreshed'} "
            f"session_index_entries={changed}"
        )
    return changed


def load_known_metadata(db_paths: list[Path]) -> dict[str, KnownMetadata]:
    known: dict[str, KnownMetadata] = {}
    for db_path in db_paths:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                select id, title, preview, first_user_message, source, thread_source
                from threads
                where archived = 0
                """
            )
        except sqlite3.Error:
            conn.close()
            continue

        for row in rows:
            title = row["title"] or ""
            preview = row["preview"] or ""
            first_user_message = row["first_user_message"] or ""
            thread_source = row["thread_source"] or ""
            if row["source"] == "vscode" and not thread_source:
                thread_source = "user"
            if not title or not preview or not first_user_message:
                continue
            known[row["id"]] = KnownMetadata(
                title=title,
                preview=preview,
                first_user_message=first_user_message,
                thread_source=thread_source,
            )
        conn.close()
    return known


def repair_one_db(
    codex_home: Path,
    db_path: Path,
    backups: BackupManager,
    known: dict[str, KnownMetadata],
    options: RepairOptions,
) -> tuple[int, int]:
    print(f"database={db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = list(
        conn.execute(
            """
            select id, title, preview, first_user_message, rollout_path, source, thread_source
            from threads
            where archived = 0
              and (
                coalesce(title, '') = ''
                or coalesce(preview, '') = ''
                or coalesce(first_user_message, '') = ''
                or (source = 'vscode' and coalesce(thread_source, '') = '')
              )
            """
        )
    )

    fixed = 0
    rollout_headers_fixed = 0
    for row in rows:
        if options.thread_ids and row["id"] not in options.thread_ids:
            continue

        rollout_path = Path(row["rollout_path"])
        text = first_user_text(rollout_path, codex_home)
        fallback = known.get(row["id"])
        title = (
            row["title"]
            or (make_title(fallback.title, row["id"]) if fallback else "")
            or make_title(text, row["id"])
        )
        preview = (
            row["preview"]
            or (fallback.preview if fallback else "")
            or compact_preview(text or title)
        )
        first_message = row["first_user_message"] or (
            fallback.first_user_message
            if fallback
            else clean_text(text)[:2000] if text else preview
        )
        thread_source = (
            row["thread_source"]
            or (fallback.thread_source if fallback else "")
            or ("user" if row["source"] == "vscode" else row["thread_source"])
        )

        print(
            f"{'would fix' if options.dry_run else 'fixing'} "
            f"{row['id']}: {compact_preview(title, 120)}"
        )

        if not options.dry_run:
            backups.backup_state_files()
            conn.execute(
                """
                update threads
                set title = ?, preview = ?, first_user_message = ?, thread_source = ?
                where id = ?
                """,
                (title, preview, first_message, thread_source, row["id"]),
            )
            if not options.metadata_only:
                append_session_index(codex_home, row["id"], title)

        if (
            not options.metadata_only
            and row["source"] == "vscode"
            and repair_rollout_header(
                rollout_path,
                backups,
                row["id"],
                options.dry_run,
            )
        ):
            rollout_headers_fixed += 1
        fixed += 1

    if options.dry_run:
        conn.close()
        print(f"would_fix={fixed}")
        return fixed, rollout_headers_fixed

    conn.commit()
    conn.close()
    print(f"fixed={fixed}")
    print(f"rollout_headers_fixed={rollout_headers_fixed}")
    return fixed, rollout_headers_fixed


def repair(codex_home: Path, options: RepairOptions) -> int:
    db_paths = state_db_paths(codex_home)
    if not db_paths:
        raise FileNotFoundError(
            f"state database not found under {codex_home} or {codex_home / 'sqlite'}"
        )

    backup_dir = (
        codex_home / "recovery_backups" / f"quick_chat_metadata_fix_{utc_stamp()}"
    )
    backups = BackupManager(codex_home, db_paths, backup_dir)

    known = load_known_metadata(db_paths)
    active_threads = load_active_threads(db_paths)
    if options.thread_ids:
        active_threads = {
            thread_id: thread
            for thread_id, thread in active_threads.items()
            if thread_id in options.thread_ids
        }

    fixed = 0
    rollout_headers_fixed = 0
    for db_path in db_paths:
        db_fixed, db_rollouts_fixed = repair_one_db(
            codex_home, db_path, backups, known, options
        )
        fixed += db_fixed
        rollout_headers_fixed += db_rollouts_fixed

    index_entries_refreshed = 0
    large_rollouts_sanitized = 0
    if not options.metadata_only:
        index_entries_refreshed = refresh_session_index(
            codex_home,
            active_threads,
            backups,
            options,
        )
        large_rollouts_sanitized = sanitize_large_rollouts(
            active_threads,
            backups,
            options,
        )

    if options.dry_run:
        print(f"total_would_fix={fixed}")
        print(f"total_would_refresh_session_index_entries={index_entries_refreshed}")
        print(f"total_would_sanitize_large_rollouts={large_rollouts_sanitized}")
        return fixed

    print(f"total_fixed={fixed}")
    print(f"total_rollout_headers_fixed={rollout_headers_fixed}")
    print(f"total_session_index_entries_refreshed={index_entries_refreshed}")
    print(f"total_large_rollouts_sanitized={large_rollouts_sanitized}")
    if backups.created_backup():
        print(f"backup={backup_dir}")
    else:
        print("backup=not_created_no_changes")
    return fixed


def run_self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="codex-chat-metadata-repair-") as tmp:
        codex_home = Path(tmp) / ".codex"
        sessions = codex_home / "sessions" / "2026" / "01" / "01"
        attachments = codex_home / "attachments" / "fixture"
        sessions.mkdir(parents=True)
        attachments.mkdir(parents=True)

        pasted_text = attachments / "pasted-text.txt"
        pasted_text.write_text(
            "Build Android app\n\nDetailed request body.",
            encoding="utf-8",
        )

        rollout = sessions / "rollout-test.jsonl"
        rollout.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "timestamp": "2026-01-01T00:00:00Z",
                            "type": "session_meta",
                            "payload": {
                                "id": "thread-1",
                                "cwd": str(Path(tmp) / "project"),
                                "source": "vscode",
                            },
                        },
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        {
                            "timestamp": "2026-01-01T00:00:01Z",
                            "type": "event_msg",
                            "payload": {
                                "type": "user_message",
                                "message": (
                                    "# Files mentioned by the user:\n\n"
                                    f"## Pasted text.txt: {pasted_text}\n\n"
                                    "The attached pasted text file(s) contain the user's request."
                                ),
                            },
                        },
                        separators=(",", ":"),
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        archived_rollout = sessions / "rollout-archived.jsonl"
        archived_rollout.write_text(
            json.dumps(
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "id": "thread-archived",
                        "cwd": str(Path(tmp) / "project"),
                        "source": "vscode",
                    },
                },
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )

        large_rollout = sessions / "rollout-large.jsonl"
        large_rollout.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "timestamp": "2026-01-01T00:00:00Z",
                            "type": "session_meta",
                            "payload": {
                                "id": "thread-large",
                                "cwd": str(Path(tmp) / "project"),
                                "source": "vscode",
                                "thread_source": "user",
                            },
                        },
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        {
                            "timestamp": "2026-01-01T00:00:01Z",
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_image",
                                        "image_url": "data:image/png;base64,"
                                        + ("x" * 5000),
                                        "detail": "high",
                                    }
                                ],
                            },
                        },
                        separators=(",", ":"),
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        bad_image_rollout = sessions / "rollout-bad-image.jsonl"
        bad_image_rollout.write_text(
            json.dumps(
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_image",
                                "image_url": (
                                    "[omitted large string by "
                                    "codex-chat-metadata-repair: 5000 chars]"
                                ),
                                "detail": "high",
                            }
                        ],
                    },
                },
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )

        invalid_image_url_rollout = sessions / "rollout-invalid-image-url.jsonl"
        invalid_image_url_rollout.write_text(
            json.dumps(
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_image",
                                "image_url": "/tmp/local-screenshot.png",
                                "detail": "high",
                            }
                        ],
                    },
                },
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )

        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        conn.execute(
            """
            create table threads (
                id text primary key,
                title text,
                preview text,
                first_user_message text,
                rollout_path text,
                source text,
                thread_source text,
                archived integer
            )
            """
        )
        conn.execute(
            "insert into threads values (?, ?, ?, ?, ?, ?, ?, ?)",
            ("thread-1", "", "", "", str(rollout), "vscode", None, 0),
        )
        conn.execute(
            "insert into threads values (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "thread-ok",
                "Already ok",
                "Preview",
                "First",
                str(rollout),
                "vscode",
                "user",
                0,
            ),
        )
        conn.execute(
            "insert into threads values (?, ?, ?, ?, ?, ?, ?, ?)",
            ("thread-archived", "", "", "", str(archived_rollout), "vscode", None, 1),
        )
        conn.execute(
            "insert into threads values (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "thread-large",
                "Large rollout",
                "Large preview",
                "Large first message",
                str(large_rollout),
                "vscode",
                "user",
                0,
            ),
        )
        conn.execute(
            "insert into threads values (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "thread-bad-image",
                "Bad image rollout",
                "Bad image preview",
                "Bad image first message",
                str(bad_image_rollout),
                "vscode",
                "user",
                0,
            ),
        )
        conn.execute(
            "insert into threads values (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "thread-invalid-image-url",
                "Invalid image URL rollout",
                "Invalid image URL preview",
                "Invalid image URL first message",
                str(invalid_image_url_rollout),
                "vscode",
                "user",
                0,
            ),
        )
        conn.commit()
        conn.close()
        (codex_home / "session_index.jsonl").write_text("", encoding="utf-8")

        dry_options = RepairOptions(
            dry_run=True,
            large_rollout_bytes=1000,
            large_string_chars=100,
            skip_large_rollout_sanitize=False,
            skip_session_index_refresh=False,
            metadata_only=False,
            thread_ids=(),
        )
        metadata_only_options = RepairOptions(
            dry_run=False,
            large_rollout_bytes=1000,
            large_string_chars=100,
            skip_large_rollout_sanitize=False,
            skip_session_index_refresh=False,
            metadata_only=True,
            thread_ids=(),
        )
        repair_options = RepairOptions(
            dry_run=False,
            large_rollout_bytes=1000,
            large_string_chars=100,
            skip_large_rollout_sanitize=False,
            skip_session_index_refresh=False,
            metadata_only=False,
            thread_ids=(),
        )

        dry_count = repair(codex_home, dry_options)
        if dry_count != 1:
            raise AssertionError(f"dry run should find 1 thread, found {dry_count}")

        rollout_before = rollout.read_bytes()
        index_before = (codex_home / "session_index.jsonl").read_bytes()
        metadata_fixed_count = repair(codex_home, metadata_only_options)
        if metadata_fixed_count != 1:
            raise AssertionError(
                f"metadata-only repair should fix 1 thread, fixed {metadata_fixed_count}"
            )
        if rollout.read_bytes() != rollout_before:
            raise AssertionError("metadata-only repair changed a rollout")
        if (codex_home / "session_index.jsonl").read_bytes() != index_before:
            raise AssertionError("metadata-only repair changed the session index")

        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        conn.execute(
            """
            update threads
            set title = '', preview = '', first_user_message = '', thread_source = null
            where id = 'thread-1'
            """
        )
        conn.commit()
        conn.close()

        fixed_count = repair(codex_home, repair_options)
        if fixed_count != 1:
            raise AssertionError(f"repair should fix 1 thread, fixed {fixed_count}")

        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        rows = {
            row[0]: row
            for row in conn.execute(
                "select id, title, preview, first_user_message, thread_source, archived from threads"
            )
        }
        conn.close()

        repaired = rows["thread-1"]
        if repaired[1] != "Build Android app":
            raise AssertionError(f"unexpected title: {repaired[1]}")
        if not repaired[2] or not repaired[3]:
            raise AssertionError("preview and first_user_message should be populated")
        if repaired[4] != "user" or repaired[5] != 0:
            raise AssertionError(
                "active thread source/archive state was not repaired correctly"
            )

        archived = rows["thread-archived"]
        if archived[1] or archived[4] or archived[5] != 1:
            raise AssertionError("archived thread should not be modified")

        ok = rows["thread-ok"]
        if ok[1] != "Already ok" or ok[2] != "Preview" or ok[3] != "First":
            raise AssertionError("healthy thread should not be modified")

        header = json.loads(rollout.read_text(encoding="utf-8").splitlines()[0])
        if header["payload"].get("thread_source") != "user":
            raise AssertionError("rollout header thread_source was not repaired")

        index = (codex_home / "session_index.jsonl").read_text(encoding="utf-8")
        if "Build Android app" not in index:
            raise AssertionError("session_index.jsonl was not updated")

        large_text = large_rollout.read_text(encoding="utf-8")
        if "x" * 5000 in large_text:
            raise AssertionError("large rollout image data was not sanitized")
        if '"image_url"' in large_text:
            raise AssertionError("large rollout should not keep an invalid image_url")
        if "omitted image by codex-chat-metadata-repair" not in large_text:
            raise AssertionError("large rollout image placeholder is missing")

        bad_image_text = bad_image_rollout.read_text(encoding="utf-8")
        if '"image_url"' in bad_image_text:
            raise AssertionError("bad image rollout image_url was not removed")
        if "omitted image by codex-chat-metadata-repair" not in bad_image_text:
            raise AssertionError("bad image rollout placeholder is missing")

        invalid_image_url_text = invalid_image_url_rollout.read_text(encoding="utf-8")
        if '"image_url"' in invalid_image_url_text:
            raise AssertionError("invalid image URL rollout image_url was not removed")
        if "omitted image by codex-chat-metadata-repair" not in invalid_image_url_text:
            raise AssertionError("invalid image URL placeholder is missing")

        print("self_test=passed")


def macos_codex_process_ids() -> set[int]:
    result = subprocess.run(
        ["/usr/bin/pgrep", "-x", "Codex"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode == 1:
        return set()
    if result.returncode != 0:
        raise RuntimeError(f"pgrep failed: {result.stderr.strip()}")
    return {int(value) for value in result.stdout.split()}


def codex_is_running() -> bool:
    if sys.platform == "darwin":
        return bool(macos_codex_process_ids())

    if sys.platform == "win32":
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Codex.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"tasklist failed: {result.stderr.strip()}")
        return '"Codex.exe"' in result.stdout

    result = subprocess.run(
        ["pgrep", "-x", "codex"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(f"pgrep failed: {result.stderr.strip()}")
    return result.returncode == 0


def wait_for_macos_codex_exit() -> None:
    if sys.platform != "darwin":
        raise RuntimeError("--wait-for-codex-exit is only supported on macOS")

    # Give the app process time to appear after a state-file launch event.
    time.sleep(1)
    process_ids = macos_codex_process_ids()

    if not process_ids:
        return

    watched_process_ids: set[int] = set()
    queue = select.kqueue()
    try:
        for process_id in process_ids:
            event = select.kevent(
                process_id,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                fflags=select.KQ_NOTE_EXIT,
            )
            try:
                queue.control([event], 0, 0)
            except ProcessLookupError:
                continue
            watched_process_ids.add(process_id)

        if watched_process_ids:
            process_list = ",".join(map(str, sorted(watched_process_ids)))
            print(f"waiting_for_codex_exit={process_list}", flush=True)
        while watched_process_ids:
            for event in queue.control(None, len(watched_process_ids), None):
                watched_process_ids.discard(event.ident)
    finally:
        queue.close()


def install_macos_launch_agent(
    script_path: Path,
    codex_home: Path,
    interval_seconds: int,
) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("--install-macos-launch-agent is only supported on macOS")
    if interval_seconds and interval_seconds < 60:
        raise ValueError("--launch-agent-interval must be at least 60 seconds")

    launch_agents_dir = Path.home() / "Library" / "LaunchAgents"
    logs_dir = Path.home() / "Library" / "Logs"
    plist_path = launch_agents_dir / f"{LAUNCH_AGENT_LABEL}.plist"
    script_path = script_path.resolve()
    codex_home = codex_home.expanduser().resolve()

    launch_agents_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    watch_paths = [
        codex_home / "state_5.sqlite",
        codex_home / "state_5.sqlite-wal",
        codex_home / "sqlite" / "state_5.sqlite",
        codex_home / "sqlite" / "state_5.sqlite-wal",
        codex_home / "session_index.jsonl",
    ]

    plist = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            sys.executable or "/usr/bin/python3",
            str(script_path),
            "--codex-home",
            str(codex_home),
            "--wait-for-codex-exit",
            "--metadata-only",
        ],
        "RunAtLoad": True,
        "WatchPaths": [str(path) for path in watch_paths],
        "StandardOutPath": str(logs_dir / f"{LAUNCH_AGENT_LABEL}.log"),
        "StandardErrorPath": str(logs_dir / f"{LAUNCH_AGENT_LABEL}.err.log"),
    }
    if interval_seconds:
        plist["StartInterval"] = interval_seconds

    with plist_path.open("wb") as file:
        plistlib.dump(plist, file)

    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootout", domain, str(plist_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    bootstrap = subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if bootstrap.returncode != 0:
        message = bootstrap.stderr.strip() or bootstrap.stdout.strip()
        raise RuntimeError(f"launchctl bootstrap failed: {message}")

    subprocess.run(
        ["launchctl", "kickstart", "-k", f"{domain}/{LAUNCH_AGENT_LABEL}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )

    print(f"launch_agent_installed={plist_path}")
    launch_agent_mode = (
        "after_codex_exit_and_interval"
        if interval_seconds
        else "after_codex_exit"
    )
    print(f"launch_agent_mode={launch_agent_mode}")
    if interval_seconds:
        print(f"launch_agent_interval_seconds={interval_seconds}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Repair active Codex chat display metadata."
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=default_codex_home(),
        help="Path to Codex home directory. Defaults to CODEX_HOME or ~/.codex.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Report what would be changed."
    )
    parser.add_argument(
        "--large-rollout-bytes",
        type=int,
        default=DEFAULT_LARGE_ROLLOUT_BYTES,
        help=(
            "Sanitize active rollout JSONL files at or above this size. "
            f"Default: {DEFAULT_LARGE_ROLLOUT_BYTES}."
        ),
    )
    parser.add_argument(
        "--large-string-chars",
        type=int,
        default=DEFAULT_LARGE_STRING_CHARS,
        help=(
            "Replace strings longer than this inside large rollouts. "
            f"Default: {DEFAULT_LARGE_STRING_CHARS}."
        ),
    )
    parser.add_argument(
        "--skip-large-rollout-sanitize",
        action="store_true",
        help="Do not sanitize unusually large active rollout JSONL files.",
    )
    parser.add_argument(
        "--skip-session-index-refresh",
        action="store_true",
        help="Do not refresh existing session_index.jsonl title entries.",
    )
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help=(
            "Only update SQLite metadata. Do not write rollout JSONL or "
            "session_index.jsonl files."
        ),
    )
    parser.add_argument(
        "--wait-for-codex-exit",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--thread-id",
        action="append",
        default=[],
        help=(
            "Limit repairs to one active thread id. "
            "May be passed more than once."
        ),
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run a portable synthetic repair test and exit.",
    )
    parser.add_argument(
        "--install-macos-launch-agent",
        action="store_true",
        help=(
            "Install a macOS LaunchAgent that runs this repair script "
            "when Codex state changes."
        ),
    )
    parser.add_argument(
        "--launch-agent-interval",
        type=int,
        default=0,
        help=(
            "Optional periodic LaunchAgent repair interval in seconds. "
            "Default: 0, disabled."
        ),
    )
    args = parser.parse_args()

    try:
        if args.self_test:
            run_self_test()
        elif args.install_macos_launch_agent:
            install_macos_launch_agent(
                Path(__file__),
                args.codex_home,
                args.launch_agent_interval,
            )
        else:
            if args.wait_for_codex_exit:
                wait_for_macos_codex_exit()
            if not args.dry_run and not args.metadata_only and codex_is_running():
                raise RuntimeError(
                    "Codex is running. Fully quit Codex before a full repair, "
                    "or use --metadata-only."
                )
            options = RepairOptions(
                dry_run=args.dry_run,
                large_rollout_bytes=args.large_rollout_bytes,
                large_string_chars=args.large_string_chars,
                skip_large_rollout_sanitize=args.skip_large_rollout_sanitize,
                skip_session_index_refresh=args.skip_session_index_refresh,
                metadata_only=args.metadata_only,
                thread_ids=tuple(args.thread_id),
            )
            repair(args.codex_home.expanduser(), options)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
