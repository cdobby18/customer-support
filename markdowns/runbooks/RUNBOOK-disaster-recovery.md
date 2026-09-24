# Runbook: Disaster Recovery

## Scope

The system state lives in PostgreSQL (dev: SQLite `support.db`), Redis (queue
broker + rate-limit cache), and Celery workers. Tickets, comments, users, and
audit logs are source-of-truth data; knowledge/embeddings and cache are
rebuildable.

## Data persistence map

| Store | Rebuildable? | Best-case RPO | Best-case RTO |
|-------|--------------|---------------|---------------|
| PostgreSQL (tickets/comments/users/audit) | No | PITR (continuous) | <15 min with warm standby |
| Redis | Yes (worker restart/drain) | n/a | minutes |
| Embedding index (FAISS) | Yes — rebuild from knowledge corpus | n/a | minutes-hours |
| Celery queues | Yes — lost tasks are retryable/auditable | n/a | n/a |

## Backup strategy (PostgreSQL)

```bash
# Point-in-time logical backup
pg_dump -Fc "$DATABASE_URL" -f backups/support-$(date +%F).dump
# Restore
createdb support_restored
pg_restore -d support_restored backups/support-*.dump
```

- Schedule nightly full dump + continuous WAL archiving (PITR) for production.
- Validate restore in a scratch database after every backup run (backups that
  are never restored are not backups).
- SQLite (dev): copy `support.db` offline or use `sqlite3 support.db ".backup
  'backups/support-YYYYMMDD.db'"` — do not copy a hot file.

## Recovery procedure (worst case, DB loss)

1. Stand up a healthy PostgreSQL instance; disable app traffic (stop workers,
   pause uvicorn).
2. Restore the latest dump (or PITR target near the incident).
3. Run `alembic upgrade head` to confirm schema matches the restored data.
4. Start app; run `GET /health`.
5. Drain stale Celery tasks (`celery purge -A app.workers` only if the queue
   holds poison messages; otherwise let them finish).
6. Rebuild the FAISS index from the knowledge corpus if the index volume was
   lost (`python -m app.knowledge` rebuild path / documented task).
7. Announce RTO/RPO actually achieved (target < 15 min / < 5 min).

## Chaos considerations (see chaos runbook)

Recovery procedures should be drilled quarterly: restore on a scratch DB,
purge Redis, kill a worker mid-job, and verify `/admin/audit-logs` continuity.