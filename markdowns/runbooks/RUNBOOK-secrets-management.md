# Runbook: Secrets Management

## Current posture

Secrets are read from environment variables (`os.getenv`) — no secrets live in
the repository. `.env.example` documents placeholders only. Defaults exist for
local development only and are rejected at startup when `APP_ENV=production`
(see `validate_security_configuration` in `app/main.py`).

## Secrets inventory

| Variable | Purpose | Prod source |
|----------|---------|-------------|
| `JWT_SECRET` | signs access tokens | secret store / k8s Secret |
| `CHANNEL_WEBHOOK_SECRET` | HMAC-verifies inbound webhooks | secret store |
| `ZENDESK_API_TOKEN` | outbound CRM sync | secret store |
| `HUBSPOT_ACCESS_TOKEN` | outbound CRM sync | secret store |
| `DATABASE_URL` | DB credentials | injected (DB-side) |
| `REDIS_URL` | broker/cache | injected |

## Rules

1. **Never commit real secrets.** Sanity check with:
   `git diff --cached | Select-String -Pattern 'token|secret|password'`.
2. Never log secrets — the structured logger records request metadata and
   stack traces only, never env values or header contents.
3. In production, inject secrets at deployment time (platform secret store,
   encrypted env, or k8s Secrets mounted as env) rather than `.env`.
4. Rotate `JWT_SECRET` and `CHANNEL_WEBHOOK_SECRET` at least quarterly and after
   any suspected exposure; rotation invalidates all existing sessions.

## Minimal steps to move off .env

1. Keep `.env` for local dev only (gitignored).
2. For any platform with a secret manager (Azure Key Vault, AWS Secrets
   Manager, HashiCorp Vault), load values into environment variables at
   container start.
3. Add CI guard that fails when a committed file contains the dev-default
   secrets (e.g., `local-development-secret-change-me`).

## Incident: suspected leak

1. Rotate the affected secret immediately (revoke + issue new).
2. For `JWT_SECRET`, rotation invalidates all tokens — plan for a brief login
   spike.
3. Audit `/admin/audit-logs` around the exposure window; check for anomalies
   with webhook HMAC failures or 401 spikes.