# ADR 0009 — Per-book backup, portable export, and data-loss recovery (LIT-24)

**Status:** Accepted (2026-07-13)  
**Ticket:** LIT-24
**Builds on:** [ADR 0002](0002-bitemporal-schema-and-dal.md) (per-book memory + global catalog),
[ADR 0007](0007-backend-service-architecture.md) (sole-owned SQLite connections), and D23/D24
(durable receipts and shelf incarnations).

## Context

The derived story memory is user value that can be expensive to re-create and impossible to recover
without the source EPUB. One logical book spans immutable files (`source.epub`, `atoms.json`), a
per-book `memory.db`, and book-scoped rows in the global `catalog.db`. Copying live SQLite files is not
safe in WAL mode, and copying only `memory.db` loses reading state, costs, catalog incarnation, and
the evidence that defines the durable spoiler frontier.

LIT-24 also owns a portable representation for a future hosted importer and a recovery procedure that
does not "repair" corruption by inventing completion state.

## Decision

### One versioned per-book archive with two representations

`python -m app.lifecycle backup` produces a versioned `.rcbackup` ZIP containing exactly:

```text
manifest.json
export.json
files/atoms.json
files/source.epub
snapshot/catalog.db
snapshot/memory.db
```

The catalog snapshot is reduced to the selected book and preserves its complete `books`,
`reading_state`, and `cost_ledger` rows, including the shelf incarnation. The memory snapshot is the
complete current-schema per-book database, including raw text, model pins, vectors, and LIT-7
completion receipts. `export.json` is a table/column-described, typed JSON encoding of those same rows;
binary values use an explicit base64 wrapper. It is the portable/hosted migration seam, while the
SQLite snapshots are the exact local recovery path.

Every archive member has a SHA-256 and byte length in `manifest.json`. The published archive is
re-opened and fully verified before an atomic rename makes it visible. It is created mode `0600` on
POSIX because it contains the user's full EPUB and retained raw prose. It contains no environment,
credentials, process configuration, or logs.

### Consistent online snapshots

Databases are copied with SQLite's online backup API, never by copying a live `.db`/WAL pair. The
catalog snapshot is taken first, then the append-only memory snapshot. This ordering means catalog
ingest progress cannot describe a memory state newer than the later memory snapshot. Both snapshots
are normalized to rollback-journal files inside the archive.

The immutable EPUB and atom manifest are copied with before/copy/after hashes. The completed stage is
accepted only when all of these agree:

- `PRAGMA integrity_check` is `ok` and `foreign_key_check` is empty for both databases;
- catalog, book metadata, manifest, canonical `db_path`, and EPUB SHA-256 name the same book;
- schema versions agree and are the current importable schema;
- manifest ordinals/version and live chapter keys agree;
- every live chapter has one contiguous, content-hash-valid completion receipt;
- catalog `ingest_progress` does not exceed that durable frontier; and
- extraction cost rows match the corresponding durable receipt payloads exactly.

An interrupted/failed backup removes its private stage and partial archive; it never leaves the named
destination behind.

### Verified staging restore, explicit collisions, retained rollback

Restore first verifies the whole archive, builds a sibling staging data directory, and verifies the
materialized result again. The default path uses SQLite snapshots. `--portable` instead reconstructs
both databases from `export.json`, exercising the future hosted/import representation.

The stage is published with a same-filesystem directory rename:

- a nonexistent target is created atomically;
- an existing target fails closed unless `--replace` is explicit;
- replacement is allowed only for an empty directory or a one-book directory containing this same
  book; different catalog books, orphan book directories, and unknown top-level data are refused;
- the previous directory is renamed to a unique `.rollback-*` sibling and deliberately retained;
- if stage publication fails, the old directory is renamed back immediately.

The app lifespan holds a cross-process lock beside `DATA_DIR`. Restore takes the same lock, so a
post-LIT-24 service must be stopped before any directory replacement. Backup remains online and does
not take that exclusive lock.

### Corruption and loss recovery ladder

1. Run `verify` on one or more archives; never overwrite the suspect data.
2. Restore a verified archive to a new target and smoke-test it there.
3. Only after owner approval, stop the service and atomically switch/replace, retaining rollback.
4. If no good archive exists but `source.epub` is intact, re-import into a fresh directory and pay the
   full extraction cost again. Preserve the corrupt database as evidence.
5. Never manufacture LIT-7 receipts, lower integrity checks, or mutate a legacy/corrupt database to
   make it appear complete.

### Local-to-hosted migration

The hosted importer consumes the versioned `export.json` plus `source.epub`/`atoms.json`, maps the
book-scoped catalog rows and memory tables into tenant-scoped storage, then runs the same identity,
frontier, receipt, and hash checks before publication. Raw-text upload remains a separate explicit
privacy/product decision; this ADR authorizes local archives, not cloud synchronization.

## Consequences and limits

- Backup works while WAL databases are open and is exact at per-database snapshot boundaries.
- Restore never partially publishes and always has an explicit collision/rollback policy.
- A per-book archive intentionally cannot merge into an existing multi-book library yet; restore to a
  fresh directory, or replace only the same one-book directory. A future merge importer must stage a
  full catalog transaction and preserve other books.
- Archives are not encrypted by this application. Store them only on owner-controlled encrypted
  storage, rotate/expire them deliberately, and remember retained `.rollback-*` directories contain
  the same private data.
- General cloud backup/sync, multi-tenant deletion/retention, and cryptographic signing are later
  productization work. They are not prerequisites for safe local recovery.

## Verification

The lifecycle suite proves online backup with live WAL connections, exact snapshot restore, portable
JSON reconstruction, checksums and SQLite corruption detection, archive privacy mode, active-directory
locking, collision refusal, retained rollback, interrupted backup cleanup, and interrupted restore
rollback. The accepted current-schema Karamazov store is also backed up and verified with the shipped
CLI before LIT-24 close-out.
