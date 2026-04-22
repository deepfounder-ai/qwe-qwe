# SQLite Migrations

Versioned schema migrations for `qwe_qwe.db`. Runner lives in `db.py`
(`_apply_migrations`). No external tool (alembic, yoyo, ...) required.

## Convention

- Files live in this directory and are named `NNN_snake_case.sql`, where `NNN`
  is a zero-padded monotonically increasing integer (`001`, `002`, ... `099`,
  `100`, ...). Numbers are never reused or reordered.
- Each file contains plain SQL. The runner wraps it in a single transaction,
  so a file either applies completely or not at all.
- The applied version is stored as the `schema_version` key in the `kv` table
  (integer serialised as text).

## Writing a new migration

1. Pick the next number. `ls migrations/ | tail -n 5` shows the latest.
2. Create `migrations/NNN_what_this_does.sql`.
3. Keep statements idempotent-ish:
   - `CREATE TABLE IF NOT EXISTS ...`
   - `CREATE INDEX IF NOT EXISTS ...`
   - For `ALTER TABLE ... ADD COLUMN ...` there is no `IF NOT EXISTS`; the
     runner catches "duplicate column" errors and treats them as applied.
     That means you can safely re-run a migration against a partially-
     migrated DB.
4. Test on a fresh DB *and* on a copy of your real DB before shipping.

## Runner behaviour

- On every connection the runner reads `kv.schema_version` (default `0`).
- It lists `migrations/*.sql`, parses the leading `NNN_` prefix, and applies
  any file whose number is strictly greater than the current version, in
  ascending order.
- Each file runs in a single `BEGIN; ... COMMIT;` transaction. On error the
  transaction is rolled back and `schema_version` is **not** bumped.
- After each successful file the runner writes the new `schema_version`.
- Log line: `applied migration 002_message_thread_ts_index.sql`.

## Backward compatibility for existing installs

When the runner sees a DB that has the `messages` table but no
`schema_version` key, it assumes the pre-migration baseline is already in
place and stamps `schema_version = 1` without re-running `001_initial.sql`.
This means existing users upgrade in place without having their tables
recreated.

## Rolling back

There is no `.down.sql`. SQLite's `ALTER` is too limited for the general
case. If you need to undo a change, write a new forward migration that does
the reverse (e.g. `DROP INDEX`, or recreate a table without the offending
column). Preserving user data is always the priority.
