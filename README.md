# Codex Chat Metadata Repair

A small portable Python script for repairing **active Codex chats** that still exist on disk but no longer show up correctly because their display metadata was lost.

The script targets this failure mode:

- `title` is empty
- `preview` is empty
- `first_user_message` is empty
- `thread_source` is missing for active Codex Desktop / VS Code chats
- `session_index.jsonl` is missing a current name entry

It creates backups before modifying anything.

## What It Does Not Do

This tool is intentionally conservative.

- It does not unarchive chats.
- It does not move rollout files between `sessions` and `archived_sessions`.
- It does not delete chats.
- It does not repair remote/cloud history.
- It does not modify Codex source code.

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
- `session_index.jsonl`
- any rollout JSONL file whose `session_meta.thread_source` is updated

## How It Repairs Metadata

For each active non-archived thread with missing display metadata, the script:

1. Reads the thread row from `state_5.sqlite`.
2. Reads the linked rollout JSONL file.
3. Extracts the first user message.
4. If the first user message points to a pasted-text attachment, reads that attachment.
5. Generates a title and preview.
6. Updates `title`, `preview`, `first_user_message`, and missing `thread_source`.
7. Appends the repaired title to `session_index.jsonl`.
8. Adds `thread_source: "user"` to active VS Code / Codex Desktop rollout headers when missing.

## Expected Output

Example dry-run output:

```text
would fix THREAD_ID: Build Android app
would_fix=1
```

Example repair output:

```text
fixing THREAD_ID: Build Android app
fixed=1
rollout_headers_fixed=1
backup=/Users/example/.codex/recovery_backups/quick_chat_metadata_fix_20260611T103851Z
```

## Troubleshooting

If `would_fix=0`, the missing chat is probably not affected by this metadata bug. Check whether it is archived, whether its rollout file exists, and whether it has a row in `state_5.sqlite`.

If Codex still does not show the repaired chat, fully quit Codex again and restart it. In some cases the running app keeps old sidebar state in memory.

If a title looks wrong, edit it directly in `state_5.sqlite` or rerun the script after restoring from backup and adjusting the rollout/attachment content.
