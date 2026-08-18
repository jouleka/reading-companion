# Local library to hosted migration

This procedure never writes to the local library. Run PostgreSQL migrations first and choose the
internal `users.id` that will own every imported record. `DATABASE_URL` must identify the restricted
migration/operator role that can bypass tenant RLS for this offline procedure—not the web tenant role.
Keep DSNs and storage keys in environment variables; do not paste them into command history or reports.

## 1. Create and retain verified backups

Use a new, empty directory outside `DATA_DIR`:

```bash
cd backend
python -m app.hosted.local_migration backup \
  --data-dir /path/to/local-data \
  --backup-dir /path/to/cutover-backups
```

The command uses SQLite online backup, verifies every archive, and stops on outstanding cost
reservations or any identity/hash/frontier mismatch. Copy the resulting `.rcbackup` files to the
normal backup destination before cutover. Do not delete them after import.

## 2. Read-only plan

```bash
python -m app.hosted.local_migration plan \
  --owner 00000000-0000-0000-0000-000000000000 \
  --dsn-env DATABASE_URL \
  --archive /path/to/cutover-backups/BOOK.rcbackup
```

Repeat `--archive` for the full library. The command verifies the archive, confirms the selected owner
exists, and prints only identifiers, checksums, and counts. It writes neither database nor storage.
Review the reported book count, atom/chapter count, durable receipts, reading state, memory tables, and
cost rows before proceeding.

## 3. Database backup and apply

Take the deployment's normal PostgreSQL snapshot. For a logical backup, the equivalent operation is:

```bash
pg_dump --format=custom --file=/secure/path/pre-local-import.dump "$DATABASE_URL"
```

Then apply the exact plans:

```bash
python -m app.hosted.local_migration apply \
  --owner 00000000-0000-0000-0000-000000000000 \
  --dsn-env DATABASE_URL \
  --archive /path/to/cutover-backups/BOOK.rcbackup
```

It is safe to rerun the same command. A crash after object upload resumes from the matching encrypted
object; a completed book reports `already-complete`. Different content for an imported local book is
rejected rather than overwritten.

## 4. Verify and smoke test

The apply command fails unless hosted counts, EPUB SHA-256/size, reading bookmark/CFI/epoch, and all
memory reveal/invalid/retraction boundaries match the archive. Also sign in as the selected owner and
verify the shelf, resume location, chapter progress, one bounded structured-memory view, and one vector
query. Another owner must see none of the imported identifiers.

## 5. Rollback

Stop application/worker writes for the selected books, then run with the same owner and archives:

```bash
python -m app.hosted.local_migration rollback \
  --owner 00000000-0000-0000-0000-000000000000 \
  --dsn-env DATABASE_URL \
  --archive /path/to/cutover-backups/BOOK.rcbackup
```

Multi-book rollback runs in reverse archive order. It removes only rows whose stored source and plan
checksums match, then deletes the encrypted source object. Rerun after an interrupted object deletion.
If broader hosted writes occurred after cutover, restore the pre-import PostgreSQL snapshot instead of
using selective rollback. The local library and retained `.rcbackup` files are the recovery source and
remain unchanged.
