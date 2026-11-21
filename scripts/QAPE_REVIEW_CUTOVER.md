# QAPE review cycle cutover (NOT EXECUTED)

1. Restore a representative PostgreSQL 15 copy outside production. With read-only credentials, verify the physical schema and run `scripts/qape_review_dry_run.sql`; preserve the output and reconcile all anomalies manually. `AbuseEvent.details` maps to SQL `abuse_events.metadata`.
2. Stop/drain ALL old API and recovery writers, including review decisions and ticket creation. Do not allow old binaries to write REVIEW_REQUIRED while schema or code changes. Hold traffic until the new application has been deployed everywhere.
3. Never run revision 018 on an existing database before reviewing the real dry-run report and obtaining explicit authorization. The migration fails closed for databases with existing QAPE operations unless `QAPE_REVIEW_BACKFILL_AUTHORIZED=explicitly-approved-after-dry-run` is set. This flag is NOT a substitute for approval. Any ambiguity/collision fails the migration transaction without changing history.
4. Run migration and tests on the approved copy first. After approval for a separate production change window, repeat the read-only report against production, freeze writes, check that results agree, and apply under transaction. Do not silently downgrade to 017 after creating review cycles; roll forward or keep review writers stopped.
5. Before resuming traffic, verify generation counters, human audit unique index and route compatibility (decision requests now require review_generation). Verify Redis reservations, quotas and recovery separately.

No deployment or production backfill is performed by preparing these files.
