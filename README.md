# Codex Chat Metadata Repair

A small portable Python script for repairing **active Codex chats** that still exist on disk but no longer show up correctly because their display metadata was lost.

The script targets this failure mode:

- `title` is empty
- `preview` is empty
- `first_user_message` is empty
- `thread_source` is missing for active Codex Desktop / VS Code chats
- `session_index.jsonl` is missing a current name entry
- an active rollout JSONL file is unusually large because it contains embedded
  base64 screenshots or other huge strings, causing Codex to skip or fail to
  read the thread until the app is restarted
- an active rollout contains an invalid `image_url` value, such as a local file
  path, a malformed value, or a previous placeholder written by an older version
  of this script, causing API errors such as
  `Invalid 'input[n].content[m].image_url'`

It creates backups before modifying anything.

Recent Codex Desktop builds may use either of these local SQLite layouts:

- `~/.codex/state_5.sqlite`
- `~/.codex/sqlite/state_5.sqlite`

The script checks and repairs both when present. This matters after app updates,
because repairing only the old path can make chats appear briefly and then
disappear again when Codex reloads state from the newer SQLite location.

## What It Does Not Do

This tool is intentionally conservative.

- It does not unarchive chats.
- It does not move rollout files between `sessions` and `archived_sessions`.
- It does not delete chats.
- It does not repair remote/cloud history.
- It does not modify Codex source code.
- It does not remove rollout records. For unusually large active rollouts, it
  replaces very large embedded strings with placeholder text. For image content,
  it replaces the whole image block with a text placeholder so Codex does not
  later send an invalid `image_url` to the API.

If a chat is archived, missing from disk, or absent from `state_5.sqlite`, this script is not the right repair.

## Requirements

- Python 3.10 or newer
- Local Codex data stored in the default `~/.codex` directory, or a custom directory passed with `--codex-home`

No third-party Python packages are required.

## Self-Test

Before touching your real Codex data, you can run the built-in portable self-test. It creates a temporary fake `.codex` directory, repairs one broken active chat, verifies that one healthy chat and one archived chat are untouched, then deletes the temporary directory.

macOS / Linux:

```bash
python3 repair_codex_chat_metadata.py --self-test
```

Windows PowerShell:

```powershell
py .\repair_codex_chat_metadata.py --self-test
```

Expected final line:

```text
self_test=passed
```

## Before You Run It

Fully quit Codex first.

On macOS, use `Cmd+Q` and wait a few seconds. On Windows, close Codex and make sure no Codex background process is still running.

This matters because Codex may keep sidebar/thread state in memory and write it back later.

## Dry Run

Use dry-run first to see what would be repaired.

macOS / Linux:

```bash
python3 repair_codex_chat_metadata.py --dry-run
```

Windows PowerShell:

```powershell
py .\repair_codex_chat_metadata.py --dry-run
```

`--dry-run` does not change files. It only prints matching active chats.
It also reports any large active rollouts that would be sanitized and any
existing `session_index.jsonl` title entries that would be refreshed.

## Repair

macOS / Linux:

```bash
python3 repair_codex_chat_metadata.py
```

Windows PowerShell:

```powershell
py .\repair_codex_chat_metadata.py
```

After it finishes, open Codex again.

If Codex was already running, fully quit and reopen it. Codex can keep a stale
thread index in memory, so repairs may not appear until restart.

## Custom Codex Home

By default the script uses:

```text
~/.codex
```

You can override that with:

```bash
python3 repair_codex_chat_metadata.py --codex-home /path/to/.codex
```

On Windows:

```powershell
py .\repair_codex_chat_metadata.py --codex-home "$env:USERPROFILE\.codex"
```

You can also set `CODEX_HOME`.

## Backups

Before a real repair, the script creates a backup directory:

```text
~/.codex/recovery_backups/quick_chat_metadata_fix_YYYYMMDDTHHMMSSZ/
```

It backs up:

- `state_5.sqlite`
- `state_5.sqlite-wal`
- `state_5.sqlite-shm`
- `sqlite/state_5.sqlite`
- `sqlite/state_5.sqlite-wal`
- `sqlite/state_5.sqlite-shm`
- `session_index.jsonl`
- any rollout JSONL file whose `session_meta.thread_source` is updated
- any rollout JSONL file sanitized because it was unusually large

## How It Repairs Metadata

For each active non-archived thread with missing display metadata, the script:

1. Reads the thread row from each available local `state_5.sqlite` location.
2. Reads the linked rollout JSONL file.
3. Extracts the first user message.
4. If the first user message points to a pasted-text attachment, reads that attachment.
5. Generates a title and preview.
6. Updates `title`, `preview`, `first_user_message`, and missing `thread_source`.
7. Uses good metadata from one SQLite layout to repair the other when possible.
8. Appends the repaired title to `session_index.jsonl`.
9. Adds `thread_source: "user"` to active VS Code / Codex Desktop rollout headers when missing.
10. Refreshes existing `session_index.jsonl` title entries for active threads.
11. Sanitizes unusually large active rollout JSONL files by replacing strings
    longer than 20,000 characters with placeholder text.
12. Rewrites invalid or oversized image blocks to text placeholders, including
    local file paths, malformed `image_url` values, and already-broken
    placeholder `image_url` values from older script versions.

By default, rollout sanitization only runs for active rollout files at or above
50 MB, or for active rollout files that contain image URL records that would be
invalid when sent back to the API. You can tune or disable this:

```bash
python3 repair_codex_chat_metadata.py --large-rollout-bytes 104857600
python3 repair_codex_chat_metadata.py --large-string-chars 50000
python3 repair_codex_chat_metadata.py --thread-id THREAD_ID
python3 repair_codex_chat_metadata.py --skip-large-rollout-sanitize
python3 repair_codex_chat_metadata.py --skip-session-index-refresh
```

Windows PowerShell:

```powershell
py .\repair_codex_chat_metadata.py --large-rollout-bytes 104857600
py .\repair_codex_chat_metadata.py --large-string-chars 50000
py .\repair_codex_chat_metadata.py --thread-id THREAD_ID
py .\repair_codex_chat_metadata.py --skip-large-rollout-sanitize
py .\repair_codex_chat_metadata.py --skip-session-index-refresh
```

Use `--thread-id` when one known chat is broken and you do not want to scan or
sanitize every active rollout. The flag may be passed more than once.

## Expected Output

Example dry-run output:

```text
database=/Users/example/.codex/state_5.sqlite
would fix THREAD_ID: Build Android app
would_fix=1
total_would_fix=1
```

Example repair output:

```text
database=/Users/example/.codex/state_5.sqlite
fixing THREAD_ID: Build Android app
fixed=1
rollout_headers_fixed=1
total_session_index_entries_refreshed=0
total_large_rollouts_sanitized=0
total_fixed=1
total_rollout_headers_fixed=1
backup=/Users/example/.codex/recovery_backups/quick_chat_metadata_fix_20260611T103851Z
```

## Troubleshooting

If `would_fix=0`, the missing chat is probably not affected by this metadata bug. Check whether it is archived, whether its rollout file exists, and whether it has a row in `state_5.sqlite`.

If Codex still does not show the repaired chat, fully quit Codex again and restart it. In some cases the running app keeps old sidebar state in memory.

If a chat appears briefly and disappears, or exists in SQLite but `thread/read`
acts like it does not exist, check for a huge rollout JSONL. This version can
sanitize those files while preserving a full backup.

If chats appear for a second and then disappear after an app update, make sure
you are running the latest version of this script. Older versions repaired only
`~/.codex/state_5.sqlite`, while newer Codex Desktop versions can load from
`~/.codex/sqlite/state_5.sqlite`.

If a title looks wrong, edit it directly in `state_5.sqlite` or rerun the script after restoring from backup and adjusting the rollout/attachment content.
