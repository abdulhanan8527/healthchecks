"""Active API probing: request the check's target API and evaluate the response.

A probe check is a Check with a non-empty "api_url" field. Instead of waiting
for the monitored job to ping Healthchecks, Healthchecks itself requests the
target API on schedule (see the "sendalerts" management command), treats any
HTTP 2xx status code as success and anything else as failure, records the
event in the check's ping log, and delivers the response body and verdict to
every notification destination assigned to the check: the check's notify
email plus all integrations (email channels, Mattermost, Slack, webhooks, ...).

On top of the basic probe, this module implements the alert policy layer:

* request options    - extra headers (Check.api_headers) and request body
                       (Check.api_body) for POST probes
* latency tracking   - every probe measures response time; rules can target
                       the special "status_code" and "response_time_ms" paths
* retry & threshold  - Check.probe_min_failures consecutive failures are
                       required before notifying; intermediate failures are
                       automatically retried PROBE_RETRY_DELAY seconds later
* escalation         - if a failure persists for Check.probe_escalate_after
                       minutes, a second, escalated notification is sent
* schema drift       - the JSON leaf paths of the response are compared
                       against the last successful probe and reported
* suppression        - notifications are skipped while the check is snoozed
                       (Mattermost buttons / UI) or during a project
                       maintenance window
* digest mode        - with Project.probe_digest enabled, results are queued
                       as DigestEntry rows and flushed hourly/daily
* history            - every probe run is recorded as a ProbeMetric row,
                       powering sparklines, the status page and escalation
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta as td, timezone
from zoneinfo import ZoneInfo

from cronsim import CronSim
from django.db import close_old_connections, connection
from django.utils.dateparse import parse_datetime
from django.utils.timezone import now
from oncalendar import OnCalendar

from hc.accounts.models import Project
from hc.api import rules as rules_lib
from hc.api.models import (
    NOTIFICATION_RETRY_DELAYS,
    Channel,
    Check,
    DigestEntry,
    Flip,
    MaintenanceWindow,
    Notification,
    ProbeMetric,
)
from hc.api.transports import TransportError, probe_report_text
from hc.lib import emails
from hc.lib.curl import CurlError, request
from hc.lib.string import is_valid_uuid_string

logger = logging.getLogger("hc")

PROBE_UA = "healthchecks-probe"
# Time limit in seconds for completing a single probe request:
PROBE_TIMEOUT = 30
# Delay in seconds before an automatic probe retry (failure threshold):
PROBE_RETRY_DELAY = 60
# Maximum number of response body characters included in the result email:
MAX_BODY_IN_EMAIL = 10 * 1024
# Probe history retention:
METRIC_RETENTION_DAYS = 31


def email_body(body: bytes, skip_paths: list[str]) -> tuple[str, list[str]]:
    """Return (response body for email display, list of skipped paths).

    The body is pretty-printed if it is JSON, and the subtrees at the given
    skip paths (for example large lists like "data.route_wise_trips") are
    removed first, so result emails stay small and focused on the stats.
    """

    text = body.decode(errors="replace")
    try:
        parsed = json.loads(text)
    except ValueError:
        return text[:MAX_BODY_IN_EMAIL], []

    skipped = [p for p in skip_paths if rules_lib.delete_path(parsed, p)]
    pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
    return pretty[:MAX_BODY_IN_EMAIL], skipped


def parse_api_headers(text: str) -> tuple[dict[str, str], str]:
    """Parse Check.api_headers (a JSON object). Returns (headers, error)."""

    if not text or not text.strip():
        return {}, ""

    try:
        data = json.loads(text)
    except ValueError as e:
        return {}, f"Request headers are not valid JSON: {e}"

    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in data.items()
    ):
        return {}, "Request headers must be a JSON object with string keys and values."

    return data, ""


def probe_email_recipients(check: Check, success: bool) -> list[str]:
    """Return the addresses that should receive the probe report email.

    That is every verified, enabled email integration assigned to the check
    that is subscribed to this verdict (up/down), plus the check's legacy
    notify email if set. Falls back to the project owner's email when the
    project has no email destinations at all.
    """

    recipients = []
    if check.notify_email:
        recipients.append(check.notify_email)

    q = check.channel_set.filter(kind="email", disabled=False)
    for ch in q:
        conf = ch.email
        if not ch.email_verified:
            continue
        if success and not conf.notify_up:
            continue
        if not success and not conf.notify_down:
            continue
        if conf.value not in recipients:
            recipients.append(conf.value)

    if not recipients:
        recipients.append(check.project.owner.email)

    return recipients


def notify_integrations(
    check: Check, success: bool, frozen_now: datetime, probe: dict
) -> list[str]:
    """Deliver the probe report through every assigned non-email integration.

    Uses the standard channel/transport machinery (the same one flip alerts
    use), so Mattermost, Slack, webhooks, SMS, ... each receive a
    notification for every probe result. The probe report is attached to
    the flip, so chat integrations render the same content as the email.
    Returns one log entry per notified channel.
    """

    flip = Flip(owner=check)
    flip.created = frozen_now
    # Present each probe as a transition from "down", so transports that
    # calculate downtime for "up" alerts never trip over missing history:
    flip.old_status = "down"
    flip.new_status = "up" if success else "down"
    flip.reason = ""
    flip.probe = probe

    logs = []
    q = check.channel_set.exclude(kind="email").exclude(disabled=True)
    for ch in q:
        if ch.transport.is_noop(flip.new_status):
            continue

        error = ch.notify(flip)
        logs.append(f"{ch.kind} error: {error}" if error else ch.kind)

    return logs


def compute_next_probe(check: Check, frozen_now: datetime) -> datetime | None:
    """Return the datetime of the next probe for the given check.

    For "simple" checks, probe every period (the timeout field).
    For "cron" and "oncalendar" checks, probe on the next schedule tick.
    Returns None if the schedule will never elapse again.
    """

    if check.kind == "simple":
        return frozen_now + check.timeout

    local_now = frozen_now.astimezone(ZoneInfo(check.tz))
    if check.kind == "cron":
        return next(CronSim(check.schedule, local_now)).astimezone(timezone.utc)

    try:
        return next(OnCalendar(check.schedule, local_now)).astimezone(timezone.utc)
    except StopIteration:
        return None


def suppression_reason(check: Check, at: datetime) -> str:
    """Return why notifications are currently suppressed for this check (or "").

    Three suppression sources: a per-check snooze (set from the UI or from
    Mattermost message buttons), project-wide maintenance windows, and
    dependency suppression (a check this one depends on is currently down).
    """

    if check.snoozed_until and check.snoozed_until > at:
        return f"snoozed until {check.snoozed_until:%Y-%m-%d %H:%M} UTC"

    q = MaintenanceWindow.objects.filter(project=check.project)
    window = q.filter(starts__lte=at, ends__gte=at).first()
    if window:
        note = f" ({window.note})" if window.note else ""
        return f"maintenance window{note}"

    # Dependency suppression: if a check this one depends on is currently
    # down, the root cause is already alerting - stay quiet about the
    # downstream symptom:
    for dep in check.depends_on.all():
        if dep.get_status() == "down":
            return f"dependency '{dep.name_then_code()}' is down"

    return ""


def compute_drift(check: Check, body: bytes, success: bool) -> dict | None:
    """Detect schema drift between this and the last successful JSON response.

    On successful probes, stores the response's leaf paths on the check.
    Returns {"added": [...], "removed": [...]} when they differ from the
    previously stored schema, None otherwise (including non-JSON bodies).
    """

    if not success:
        return None

    try:
        parsed = json.loads(body.decode(errors="replace"))
    except ValueError:
        return None

    keys = rules_lib.leaf_paths(parsed)
    prev = set(check.api_schema_keys or [])
    drift = None
    if prev:
        added = [k for k in keys if k not in prev]
        removed = sorted(k for k in prev if k not in set(keys))
        if added or removed:
            drift = {"added": added[:20], "removed": removed[:20]}

    Check.objects.filter(id=check.id).update(api_schema_keys=keys)
    check.api_schema_keys = keys
    return drift


def digest_next_flush(mode: str, ref: datetime) -> datetime:
    """Return the next digest flush time for "hourly"/"daily" mode."""

    if mode == "hourly":
        return (ref + td(hours=1)).replace(minute=0, second=0, microsecond=0)
    return (ref + td(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def queue_digest(check: Check, success: bool, report: str, frozen_now: datetime) -> int:
    """Queue one DigestEntry per destination instead of sending immediately."""

    count = 0
    for addr in probe_email_recipients(check, success):
        DigestEntry.objects.create(
            project=check.project,
            owner=check,
            channel=None,
            recipient=addr,
            verdict=success,
            report=report,
            created=frozen_now,
        )
        count += 1

    new_status = "up" if success else "down"
    q = check.channel_set.exclude(kind="email").exclude(disabled=True)
    for ch in q:
        if ch.transport.is_noop(new_status):
            continue
        DigestEntry.objects.create(
            project=check.project,
            owner=check,
            channel=ch,
            verdict=success,
            report=report,
            created=frozen_now,
        )
        count += 1

    project = check.project
    if not project.digest_next_flush:
        project.digest_next_flush = digest_next_flush(project.probe_digest, frozen_now)
        project.save(update_fields=["digest_next_flush"])

    return count


def digest_entries_payload(project: Project, group: list[DigestEntry]) -> dict:
    """Build the flip.probe payload for a batched digest message."""

    return {
        "digest": [
            {
                "name": e.owner.name_then_code(),
                "verdict": e.verdict,
                "report": e.report,
                "created": e.created,
            }
            for e in group
        ],
        "period": project.probe_digest,
        "project": project.name or str(project.code),
        "details_url": group[0].owner.details_url(),
    }


def flush_project_digest(project: Project) -> int:
    """Send all queued DigestEntry rows for one project as combined messages."""

    entries = list(
        DigestEntry.objects.filter(project=project)
        .select_related("owner", "channel")
        .order_by("created", "id")
    )
    if not entries:
        project.digest_next_flush = digest_next_flush(project.probe_digest, now())
        project.save(update_fields=["digest_next_flush"])
        return 0

    by_channel: dict[int, list[DigestEntry]] = defaultdict(list)
    by_recipient: dict[str, list[DigestEntry]] = defaultdict(list)
    for e in entries:
        if e.channel_id:
            by_channel[e.channel_id].append(e)
        elif e.recipient:
            by_recipient[e.recipient].append(e)

    sent = 0
    for group in by_channel.values():
        ch = group[0].channel
        if ch is None or ch.disabled:
            continue

        flip = Flip(owner=group[0].owner)
        flip.created = now()
        flip.old_status = "down"
        flip.new_status = "up" if all(e.verdict for e in group) else "down"
        flip.reason = ""
        flip.probe = digest_entries_payload(project, group)

        error = ch.notify(flip)
        sent += 1
        if error:
            logger.error("Digest notify failed for %s: %s", ch.kind, error)

    for addr, group in by_recipient.items():
        try:
            emails.probe_digest(
                addr,
                {
                    "project": project,
                    "period": project.probe_digest,
                    "entries": digest_entries_payload(project, group)["digest"],
                },
            )
            sent += 1
        except Exception:
            logger.exception("Failed to send probe digest to %s", addr)

    DigestEntry.objects.filter(id__in=[e.id for e in entries]).delete()
    project.digest_next_flush = digest_next_flush(project.probe_digest, now())
    project.save(update_fields=["digest_next_flush"])
    return sent


def flush_due_digests() -> int:
    """Flush digests for all projects whose digest_next_flush has passed.

    Called from the sendalerts worker loop. Returns the number of messages.
    """

    if not connection.in_atomic_block:
        close_old_connections()

    q = Project.objects.exclude(probe_digest="off")
    q = q.filter(digest_next_flush__isnull=False, digest_next_flush__lte=now())

    total = 0
    for project in q:
        try:
            total += flush_project_digest(project)
        except Exception:
            logger.exception("Failed to flush digest for project %s", project.code)
    return total


def escalate(check: Check) -> str:
    """Send an escalation notification for a long-running probe failure."""

    frozen_now = now()
    metric = ProbeMetric.objects.filter(owner=check).order_by("-created").first()
    since = check.probe_fail_since or frozen_now
    minutes = max(1, int((frozen_now - since).total_seconds() // 60))

    ctx = {
        "check": check,
        "success": False,
        "escalation": True,
        "status_code": metric.status_code if metric else 0,
        "error": (
            f"ESCALATION: failing continuously for {minutes} min "
            f"(since {since:%Y-%m-%d %H:%M} UTC)"
        ),
        "body": (metric.report[:MAX_BODY_IN_EMAIL] if metric else ""),
        "skipped": [],
        "rules": (metric.rules if metric else None) or [],
        "rules_error": "",
        "response_ms": metric.response_ms if metric else 0,
        "drift": None,
        "probe_time": frozen_now,
        "details_url": check.details_url(),
        "api_method": check.api_method,
        "api_url": check.api_url,
    }

    suppressed = suppression_reason(check, frozen_now)
    if suppressed:
        return f"{check.name_then_code()}: escalation suppressed ({suppressed})"

    for addr in probe_email_recipients(check, False):
        try:
            emails.probe_result(addr, ctx)
        except Exception:
            logger.exception("Failed to send escalation email for %s", check.code)

    integration_logs = notify_integrations(check, False, frozen_now, ctx)
    summary = f"{check.name_then_code()}: ESCALATION (failing for {minutes} min)"
    if integration_logs:
        summary += "; integrations: " + ", ".join(integration_logs)
    return summary


def handle_escalations() -> int:
    """Escalate checks failing longer than their probe_escalate_after setting.

    Called from the sendalerts worker loop. Returns the number of checks
    that were escalated in this pass.
    """

    if not connection.in_atomic_block:
        close_old_connections()

    q = Check.objects.exclude(api_url="").exclude(probe_escalate_after=0)
    q = q.filter(probe_escalated=False, probe_fail_since__isnull=False)
    q = q.select_related("project")

    count = 0
    for check in q:
        due = check.probe_fail_since + td(minutes=check.probe_escalate_after)
        if now() < due:
            continue

        # Claim the escalation atomically, so concurrent workers escalate once:
        updated = Check.objects.filter(id=check.id, probe_escalated=False)
        if updated.update(probe_escalated=True) != 1:
            continue

        try:
            logger.info(escalate(check))
        except Exception:
            logger.exception("Escalation failed for %s", check.code)
        count += 1

    return count


def ping_watchdogs() -> int:
    """Ping every passive check tagged "watchdog" (monitor the monitor).

    The sendalerts worker calls this once a minute. If the worker dies, the
    watchdog checks time out and the normal alerting path fires - so the
    deployment gets alerted about a dead worker through its own channels.
    Returns the number of pings recorded.
    """

    if not connection.in_atomic_block:
        close_old_connections()

    q = Check.objects.filter(api_url="", tags__contains=" watchdog ")
    q = q.exclude(status="paused")

    count = 0
    for check in q:
        try:
            check.ping(
                remote_addr="",
                scheme="http",
                method="POST",
                ua="hc-watchdog",
                body=b"",
                action="success",
                rid=None,
            )
            count += 1
        except Exception:
            logger.exception("Watchdog ping failed for %s", check.code)
    return count


def prune_probe_metrics(days: int = METRIC_RETENTION_DAYS) -> int:
    """Delete probe history older than the retention period."""

    cutoff = now() - td(days=days)
    deleted, _ = ProbeMetric.objects.filter(created__lt=cutoff).delete()
    # Also drop orphaned digest entries that were never flushed:
    DigestEntry.objects.filter(created__lt=cutoff).delete()
    return deleted


def probe_check(check: Check) -> str:
    """Request the check's target API locally, then evaluate and notify.

    Safe to run in a worker thread: refreshes the DB connection if needed.
    Returns a one-line human-readable summary of the probe.
    """

    if not connection.in_atomic_block:
        close_old_connections()

    frozen_now = now()

    headers = {"User-Agent": PROBE_UA}
    if check.api_token:
        headers["Authorization"] = "Bearer " + check.api_token

    extra_headers, headers_error = parse_api_headers(check.api_headers)
    headers.update(extra_headers)

    status_code = 0
    error = headers_error
    body = b""
    response_ms = 0
    if not headers_error:
        data = check.api_body.encode() if check.api_body.strip() else None
        # Per-probe timeout override (0 = the global PROBE_TIMEOUT default):
        timeout = check.api_timeout or PROBE_TIMEOUT
        started = time.time()
        try:
            response = request(
                check.api_method.lower(),
                check.api_url,
                headers=headers,
                data=data,
                timeout=timeout,
            )
            status_code = response.status_code
            body = response.content
        except CurlError as e:
            error = e.message
            body = e.message.encode()
        response_ms = int((time.time() - started) * 1000)

    return _evaluate_and_notify(
        check,
        status_code=status_code,
        body=body,
        response_ms=response_ms,
        error=error,
        location="",
        frozen_now=frozen_now,
    )


def ingest_remote_probe(
    check: Check,
    *,
    status_code: int,
    body: bytes | str,
    response_ms: int,
    location: str,
    error: str = "",
) -> str:
    """Process a probe result submitted by a remote multi-location agent.

    The agent performs the HTTP request from its own location and posts the
    raw outcome here; the server runs the exact same evaluation/notification
    pipeline as a local probe (see _evaluate_and_notify), tagging the recorded
    ProbeMetric and report with the agent's location label.
    """

    if not connection.in_atomic_block:
        close_old_connections()

    if isinstance(body, str):
        body = body.encode()

    return _evaluate_and_notify(
        check,
        status_code=status_code,
        body=body,
        response_ms=response_ms,
        error=error,
        location=location[:100],
        frozen_now=now(),
    )


def _evaluate_and_notify(
    check: Check,
    *,
    status_code: int,
    body: bytes,
    response_ms: int,
    error: str,
    location: str,
    frozen_now: datetime,
) -> str:
    """Shared pipeline: evaluate one probe outcome and deliver notifications.

    Used by both the local worker (probe_check) and the remote agent ingest
    path (ingest_remote_probe), so a probe behaves identically no matter where
    the HTTP request was made. "location" labels the probing origin ("" for
    the local worker). Returns a one-line human-readable summary.
    """

    # Reschedule *before* the heavy work below, so that an unexpected
    # exception cannot leave next_probe in the past and cause a tight loop:
    next_probe = compute_next_probe(check, frozen_now)
    success = 200 <= status_code < 300

    # Optional success rules: inspect the response body fields and only
    # count the probe as success if every rule passes. Rules can also
    # target the special "status_code" and "response_time_ms" paths:
    rule_results: list[dict] = []
    rules_error = ""
    if success and check.api_rules.strip():
        try:
            parsed_rules = rules_lib.parse_rules(check.api_rules)
        except rules_lib.RulesError as e:
            parsed_rules, rules_error = [], f"Invalid rule configuration: {e}"

        if rules_error:
            success = False
        else:
            rule_results, rules_error = rules_lib.evaluate_rules(
                parsed_rules,
                body,
                check.tz,
                extra={
                    "status_code": status_code,
                    "response_time_ms": response_ms,
                },
            )
            if rules_error:
                success = False
            elif not all(r["ok"] for r in rule_results):
                success = False

    # Schema drift detection against the last successful JSON response:
    drift = compute_drift(check, body, success)

    # Alert policy: failure threshold with automatic retries. Failures
    # below the threshold are not notified; instead, a quick retry probe
    # is scheduled PROBE_RETRY_DELAY seconds from now:
    retry_note = ""
    should_notify = True
    if success:
        Check.objects.filter(id=check.id).update(
            next_probe=next_probe,
            probe_fail_count=0,
            probe_notified_failure=False,
            probe_fail_since=None,
            probe_escalated=False,
        )
    else:
        fail_count = (check.probe_fail_count or 0) + 1
        min_failures = max(1, check.probe_min_failures or 1)
        fail_since = check.probe_fail_since or frozen_now
        if fail_count < min_failures:
            should_notify = False
            next_probe = frozen_now + td(seconds=PROBE_RETRY_DELAY)
            retry_note = (
                f", failure {fail_count}/{min_failures},"
                f" retry at {next_probe:%H:%M:%S} UTC"
            )
        Check.objects.filter(id=check.id).update(
            next_probe=next_probe,
            probe_fail_count=fail_count,
            probe_notified_failure=should_notify,
            probe_fail_since=fail_since,
        )
        check.probe_fail_count = fail_count
        check.probe_fail_since = fail_since

    check.next_probe = next_probe

    # Record the probe in the check's ping log and update its status.
    # Reuses the standard ping machinery, so status flips still notify
    # all integrations (email channels, Slack, webhooks, ...) as usual:
    check.ping(
        remote_addr="",
        scheme="http",
        method=check.api_method,
        ua=PROBE_UA,
        body=body,
        action="success" if success else "fail",
        rid=None,
    )

    body_text, skipped = email_body(
        body, rules_lib.parse_skip_paths(check.api_skip_paths)
    )
    ctx = {
        "check": check,
        "success": success,
        "status_code": status_code,
        "error": error,
        "body": body_text,
        "skipped": skipped,
        "rules": rule_results,
        "rules_error": rules_error,
        "response_ms": response_ms,
        "drift": drift,
        "escalation": False,
        "probe_time": frozen_now,
        "location": location,
        "details_url": check.details_url(),
        "api_method": check.api_method,
        "api_url": check.api_url,
    }
    report = probe_report_text(check.name_then_code(), ctx)

    # Always record history (used by sparklines, status page, escalation):
    ProbeMetric.objects.create(
        owner=check,
        created=frozen_now,
        success=success,
        status_code=status_code,
        response_ms=response_ms,
        rules=rule_results,
        report=report,
        location=location,
    )

    suppressed = ""
    if should_notify:
        suppressed = suppression_reason(check, frozen_now)
        if suppressed:
            should_notify = False

    emailed = []
    queued = 0
    integration_logs = []
    routed_logs = []
    if should_notify:
        digest_mode = check.project.probe_digest
        if digest_mode in ("hourly", "daily"):
            queued = queue_digest(check, success, report, frozen_now)
        else:
            for addr in probe_email_recipients(check, success):
                try:
                    emails.probe_result(addr, ctx)
                    emailed.append(addr)
                except Exception:
                    # A broken mail setup must not stop the probe worker:
                    logger.exception(
                        "Failed to send probe result email for %s", check.code
                    )
            integration_logs = notify_integrations(check, success, frozen_now, ctx)
            # Per-rule routing: notify extra channels named by failed rules:
            routed_logs = notify_routed_channels(check, rule_results, frozen_now, ctx)

    verdict = "SUCCESS" if success else "FAILURE"
    summary = f"{check.name_then_code()}: {verdict} (HTTP {status_code or 'n/a'}"
    summary += f", {response_ms} ms"
    if location:
        summary += f", from {location}"
    if rule_results:
        passed = sum(1 for r in rule_results if r["ok"])
        summary += f", {passed}/{len(rule_results)} rules passed"
    if rules_error:
        summary += f", {rules_error}"
    if error and not success:
        summary += f", {error}"
    if drift:
        summary += f", schema drift +{len(drift['added'])}/-{len(drift['removed'])}"
    summary += retry_note
    if suppressed:
        summary += f") -> suppressed ({suppressed})"
    elif queued:
        summary += f") -> queued for {check.project.probe_digest} digest ({queued})"
    elif should_notify:
        if emailed:
            summary += ") -> emailed " + ", ".join(emailed)
        else:
            summary += ") -> EMAIL FAILED (see worker log)"
        if integration_logs:
            summary += "; integrations: " + ", ".join(integration_logs)
        if routed_logs:
            summary += "; routed: " + ", ".join(routed_logs)
    else:
        summary += ")"
    return summary


def notify_routed_channels(
    check: Check, rule_results: list[dict], frozen_now: datetime, probe: dict
) -> list[str]:
    """Per-rule routing: notify extra channels named by *failed* rules.

    A rule may carry an optional "route" key - a list of project channel codes
    (UUID strings). When that rule fails, the named channels are notified in
    addition to the check's normal destinations, so a specific team is paged
    only when "their" rule breaks. Channels already assigned to the check are
    skipped to avoid duplicate messages. Returns one log entry per channel.
    """

    codes: list[str] = []
    for r in rule_results:
        if r.get("ok"):
            continue
        for code in r.get("route") or []:
            if code not in codes:
                codes.append(code)

    valid = [c for c in codes if is_valid_uuid_string(c)]
    if not valid:
        return []

    assigned = set(check.channel_set.values_list("id", flat=True))
    flip = Flip(owner=check)
    flip.created = frozen_now
    flip.old_status = "down"
    flip.new_status = "down"
    flip.reason = ""
    flip.probe = probe

    logs = []
    q = check.project.channel_set.filter(code__in=valid).exclude(disabled=True)
    for ch in q:
        if ch.id in assigned or ch.transport.is_noop("down"):
            continue
        error = ch.notify(flip)
        logs.append(f"{ch.kind} error: {error}" if error else ch.kind)
    return logs


def retry_notification(n: Notification) -> bool:
    """Re-attempt delivery of one failed notification. Returns True on success.

    Rebuilds the flip from the serialized retry_payload (re-fetching the owning
    check) and replays it through the channel's transport, using the backoff
    schedule in NOTIFICATION_RETRY_DELAYS. Gives up after the last delay or on
    a permanent transport error.
    """

    payload = n.retry_payload
    if not payload:
        return False

    try:
        check = Check.objects.get(id=payload.get("owner"))
    except (Check.DoesNotExist, ValueError, TypeError):
        Notification.objects.filter(id=n.id).update(
            error="Check no longer exists", next_retry=None, retry_payload=None
        )
        return False

    flip = Flip(owner=check)
    flip.created = parse_datetime(payload.get("created", "")) or n.created
    flip.old_status = payload.get("old_status", "down")
    flip.new_status = payload.get("new_status", "down")
    flip.reason = payload.get("reason", "")
    probe = payload.get("probe")
    if isinstance(probe, dict):
        if probe.get("probe_time"):
            probe["probe_time"] = parse_datetime(probe["probe_time"])
        # Transports expect the live check on the probe context:
        probe["check"] = check
        flip.probe = probe

    error = ""
    permanent = False
    try:
        n.channel.transport.notify(flip, notification=n)
    except TransportError as e:
        error, permanent = e.message, e.permanent
    except Exception as e:  # a transport bug must not kill the worker
        error = str(e)

    retry_count = n.retry_count + 1
    if not error:
        Notification.objects.filter(id=n.id).update(
            error="", retry_count=retry_count, next_retry=None, retry_payload=None
        )
        return True

    if permanent or retry_count >= len(NOTIFICATION_RETRY_DELAYS):
        # Exhausted (or permanently broken): stop retrying, keep the error:
        Notification.objects.filter(id=n.id).update(
            error=error, retry_count=retry_count, next_retry=None, retry_payload=None
        )
        return False

    delay = NOTIFICATION_RETRY_DELAYS[retry_count]
    Notification.objects.filter(id=n.id).update(
        error=error, retry_count=retry_count, next_retry=now() + td(seconds=delay)
    )
    return False


def retry_failed_notifications() -> tuple[int, int]:
    """Retry notifications whose delivery failed transiently.

    Called from the sendalerts worker loop. Returns (attempted, succeeded).
    """

    if not connection.in_atomic_block:
        close_old_connections()

    q = Notification.objects.filter(
        next_retry__isnull=False,
        next_retry__lte=now(),
        retry_payload__isnull=False,
    )
    q = q.select_related("channel", "owner")

    attempted = succeeded = 0
    for n in q:
        if n.channel.disabled:
            # The channel was disabled since: drop the queued retry:
            Notification.objects.filter(id=n.id).update(
                next_retry=None, retry_payload=None
            )
            continue
        attempted += 1
        try:
            if retry_notification(n):
                succeeded += 1
        except Exception:
            logger.exception("Notification retry failed for %s", n.id)
    return attempted, succeeded
