# Catalog migrations and integrity

The catalog records immutable original-file references and saved evidence documents. Opening a supported version 1 catalog upgrades it to version 2 only after creating and verifying a private SQLite snapshot under `catalog-migrations/`. Migration runs in a transaction; failure preserves the previous schema and documents. New catalogs start at version 2. Unknown future versions are rejected before schema writes, including collection and orchestration tables.

The migration snapshot includes SQLite catalog content only. It excludes raw originals, normalized Parquet data, live recorder files and the separate job journal. It is not a complete flight-data backup. Keep the entire application data directory in your normal backup process, with recording stopped. The app does not silently restore an old snapshot or run SQL supplied by a user or report.

Open **Flights → Advanced tools → Import & manage sources → Catalog version & original integrity** to inspect the schema version and recorded migration snapshots. **Verify archived originals** is an explicit background job. It checks a consistent temporary catalog snapshot, immutable-table guards, document shape, and the existence, regular-file identity, length and SHA-256 of archived originals. It releases the live catalog before hashing files so recording can continue. Cancellation stops verification without modifying evidence.

A passing result applies to that snapshot and its referenced originals. New records added afterward are outside the result. The check does not validate normalized analysis artifacts, parameter applicability, aircraft identity, stream completeness, or hardware health. Failures list bounded issue details and the total count; they do not repair, remove or replace evidence. Preserve the reported files and consult the original backup before any manual recovery.

RPCs are `catalog.status {}` and the job-backed `catalog.verify {}`. Neither accepts a path, SQL command, restore request, or repair flag.

Synthetic tests exercise a populated version 1 upgrade, verified backup content, injected post-DDL failure and rollback, low-space rejection, future-version rejection, raw-byte corruption, cancellation, and a concurrent catalog append while a snapshot is being verified. Windows/Linux installed-app upgrades and power-loss behavior still need native qualification.
