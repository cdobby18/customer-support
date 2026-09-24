# Runbook: Audit Compliance

## Audit model

Every state-changing action is recorded in `audit_logs`
(`AuditLogRecord`): `action`, `entity_type`, `entity_id`, `actor_id`,
`details` (JSON), `created_at`. Consumers consult them via
`GET /admin/audit-logs` (admin only).

## Retention

- `AUDIT_LOG_RETENTION_DAYS` (default `365`) controls how long records are kept.
- Admin operators purge expired records with
  `POST /admin/audit-logs/purge`, which deletes everything strictly older than
  the retention window and returns `{deleted, retention_days}`.
- Schedule the purge as a cron job (`POST /admin/audit-logs/purge` daily) so
  the table does not grow unbounded.

## Privacy

- PII-bearing fields (`message`, `body`, `customer_id`, email) must NOT be
  copied into `audit_logs.details`; `details` stores structural facts (intents,
  status transitions, integration sync state). Verify new `add_audit_log` calls
  follow this rule.
- Exporting a single customer's footprint = query audit logs by `actor_id` /
  `entity_id` = customer id; itemize it against retention policy for GDPR
  erasure (delete rows by entity/actor id after `tickets`/`users` purge).

## Compliance expectations

- Erasable: `users`, `tickets`, `comments`, `audit_logs` (no cross-store
  hard dependencies beyond soft `customer_id` references).
- Immutable: audit log rows are only ever appended (purge deletes whole rows;
  no UPDATE paths exist). Do not add update endpoints for audit rows.
- Proof of review: keep a quarterly sign-off in repo/docs stating the retention
  window, purge schedule, and who performs the admin purge.