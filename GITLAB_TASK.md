# GitLab Task (copy into your issue / work item)

**Title:** Build active API monitoring platform on Healthchecks (probes, alert policies, multi-location agents, production deploy)

**Type:** Feature / Epic
**Status:** Done
**Labels:** `backend`, `django`, `monitoring`, `devops`, `testing`

---

## Summary
Extended the self-hosted Healthchecks app into a full active API monitoring
platform: the server probes target APIs on a schedule, validates responses
against configurable rules, applies alert policies, tracks history, and delivers
unified notifications through 29 integrations. Also delivered production
deployment artifacts and full test coverage.

## Deliverables
- [x] Active API probing (GET/POST, custom headers, body, bearer token, per-probe timeout)
- [x] Dynamic success-rules engine (13 operators, nested JSON paths, `status_code` / `response_time_ms`, rules playground)
- [x] Alert policies: failure threshold + auto-retry, maintenance windows, snooze, escalation, hourly/daily digests
- [x] Schema-drift detection and dependency suppression
- [x] Per-rule routing (a failing rule pages a specific channel/team)
- [x] Notification retry queue with backoff (1m/5m/30m/2h) + errors UI
- [x] Multi-location probe agents (`probe_agent` command + `/api/v3/agent/` endpoints)
- [x] Probe metric history, sparklines, public status page, unified report across all integrations + interactive Mattermost buttons
- [x] Worker watchdog ("monitor the monitor") and housekeeping (reports + pruning) loop
- [x] Production deploy: systemd units, nginx, gunicorn, backups, SQLite→Postgres migration (`deploy/`)
- [x] Full test suite green (1915 tests) + deployment & project documentation

## Tech
Python 3.12 · Django 6.1 · PostgreSQL/SQLite · Gunicorn · Nginx · systemd

## Acceptance criteria
- Probes run only in the `sendalerts` worker and reschedule safely.
- Rules/threshold/suppression behave identically for local and remote (agent) probes.
- Failed notifications retry with backoff and are visible in the UI.
- `python manage.py test` passes; `deploy/DEPLOYMENT.md` reproduces a live server.

## Notes / references
- Full details: `PROJECT_DOCUMENTATION.md`
- Go-live steps: `deploy/DEPLOYMENT.md`
- Branch/commit: `master` — "Add active API probing, alert policies, and production deployment"
