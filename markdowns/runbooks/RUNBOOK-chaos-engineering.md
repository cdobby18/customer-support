# Runbook: Chaos Engineering

Purpose: prove the system fails safe and recoverable when infra collapses.
Drill in a non-production environment; automate with pytest where the failure
can be injected at the app boundary.

## Failure injection catalogue

| Failure | How to inject | Expected behavior | Verify |
|---------|---------------|-------------------|--------|
| PostgreSQL down | stop DB service / revoke network | `/health` flaps, API returns 503/500 cleanly, no crash loop, `get_db` closes sessions | API recovers once DB returns |
| Redis down | `redis-cli shutdown` | rate limiter falls back to in-process memory (`_get_redis` returns `None`), webhooks still rate-limited per worker | manual: spam webhook still limited |
| CRM provider down | set bogus `ZENDESK_*` creds + `SUPPORT_TOOL_PROVIDER=zendesk` | `sync_ticket_outbound` catches `IntegrationError`, logs `integration.synced` with `status=failed`, ticket is NOT lost | audit log shows failed sync; ticket intact |
| Celery broker unreachable | stop Redis, call `enqueue_notification` | task dispatch failure is contained; the HTTP response does not hang | API latency unaffected |
| Worker killed mid-job | `kill -9` a worker during queue drain | unacked tasks redeliver; no silent loss | queue drains fully after restart |
| Disk full | fill data volume to 100% | DB write failures are loud and quick; monitoring alert fires | no unlogged corruption |

## Recurring drill (quarterly)

1. Pick 2 failures from the table.
2. Inject in staging while replaying a Locust load test
   (`load_tests/README.md`).
3. Record: error-rate impact, recovery time, and the steps taken.
4. File any gap as a follow-up task (e.g., "webhook failing open is slow").

## Principles

- Never chaos-test production data without a validated restore path (see
  disaster-recovery runbook) and a full staffed on-call window.
- Prefer *component* failure injection (DB, broker, provider) over
  network-level sabotage for this codebase — the failure modes are at service
  boundaries, not inside the app.
- Every drill ends with a restore + verification step (`GET /health`, audit log
  continuity check).