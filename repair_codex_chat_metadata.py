#!/usr/bin/env python3
"""Repair visible metadata for active Codex chats.

This script fixes active, non-archived chats whose display metadata was lost
(`title`, `preview`, or `first_user_message` is empty). It also repairs missing
`thread_source` for active VS Code/Codex Desktop threads and appends current
names to `session_index.jsonl`.

It intentionally does not unarchive chats and does not move rollout files.
Close Codex before running it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from datetime import timezone
from pathlib import Path


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


def backup_files(codex_home: Path, backup_dir: Path) -> None:
    backup_dir.mkdir(parents=True, exist_ok=False)
    for name in (
        "state_5.sqlite",
        "state_5.sqlite-wal",
        "state_5.sqlite-shm",
        "session_index.jsonl",
    ):
        source = codex_home / name
        if source.exists():
            shutil.copy2(source, backup_dir / name)


def append_session_index(codex_home: Path, thread_id: str, title: str) -> None:
    entry = {
        "id": thread_id,
        "thread_name": title,
        "updated_at": now_rfc3339(),
    }
    with (codex_home / "session_index.jsonl").open("a", encoding="utf-8") as file:
        file.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")


def repair_rollout_header(
    rollout_path: Path,
    backup_dir: Path,
    thread_id: str,
    dry_run: bool,
) -> bool:
    if not rollout_path.exists():
        return False
    try:
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
        rollout_backup = backup_dir / "rollouts" / f"{thread_id}.jsonl"
        rollout_backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(rollout_path, rollout_backup)
        payload["thread_source"] = "user"
        lines[0] = (
            json.dumps(first_obj, ensure_ascii=False, separators=(",", ":")) + "\n"
        )
        rollout_path.write_text("".join(lines), encoding="utf-8")
    return True


def repair(codex_home: Path, dry_run: bool) -> int:
    db_path = codex_home / "state_5.sqlite"
    if not db_path.exists():
        raise FileNotFoundError(f"state database not found: {db_path}")

    backup_dir = (
        codex_home / "recovery_backups" / f"quick_chat_metadata_fix_{utc_stamp()}"
    )
    if not dry_run:
        backup_files(codex_home, backup_dir)

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
        rollout_path = Path(row["rollout_path"])
        text = first_user_text(rollout_path, codex_home)
        title = row["title"] or make_title(text, row["id"])
        preview = row["preview"] or compact_preview(text or title)
        first_message = row["first_user_message"] or (
            clean_text(text)[:2000] if text else preview
        )
        thread_source = row["thread_source"] or (
            "user" if row["source"] == "vscode" else row["thread_source"]
        )

        print(f"{'would fix' if dry_run else 'fixing'} {row['id']}: {title}")

        if not dry_run:
            conn.execute(
                """
                update threads
                set title = ?, preview = ?, first_user_message = ?, thread_source = ?
                where id = ?
                """,
                (title, preview, first_message, thread_source, row["id"]),
            )
            append_session_index(codex_home, row["id"], title)

        if row["source"] == "vscode" and repair_rollout_header(
            rollout_path,
            backup_dir,
            row["id"],
            dry_run,
        ):
            rollout_headers_fixed += 1
        fixed += 1

    if dry_run:
        conn.close()
        print(f"would_fix={fixed}")
        return fixed

    conn.commit()
    conn.close()
    print(f"fixed={fixed}")
    print(f"rollout_headers_fixed={rollout_headers_fixed}")
    print(f"backup={backup_dir}")
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
        conn.commit()
        conn.close()
        (codex_home / "session_index.jsonl").write_text("", encoding="utf-8")

        dry_count = repair(codex_home, dry_run=True)
        if dry_count != 1:
            raise AssertionError(f"dry run should find 1 thread, found {dry_count}")

        fixed_count = repair(codex_home, dry_run=False)
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

        print("self_test=passed")


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
        "--self-test",
        action="store_true",
        help="Run a portable synthetic repair test and exit.",
    )
    args = parser.parse_args()

    try:
        if args.self_test:
            run_self_test()
        else:
            repair(args.codex_home.expanduser(), args.dry_run)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
