from __future__ import annotations

import json
from datetime import timedelta as td
from unittest.mock import Mock, patch

from django.core import mail
from django.core.management import call_command
from django.utils.timezone import now

from hc.accounts.models import Project
from hc.api.models import (
    NOTIFICATION_RETRY_DELAYS,
    Channel,
    Check,
    DigestEntry,
    Flip,
    MaintenanceWindow,
    Notification,
    Ping,
    ProbeMetric,
)
from hc.api.probes import (
    PROBE_TIMEOUT,
    compute_drift,
    digest_next_flush,
    flush_due_digests,
    flush_project_digest,
    handle_escalations,
    ingest_remote_probe,
    notify_routed_channels,
    parse_api_headers,
    ping_watchdogs,
    probe_check,
    prune_probe_metrics,
    queue_digest,
    retry_failed_notifications,
    retry_notification,
    suppression_reason,
)
from hc.api.rules import RulesError, leaf_paths, parse_rules
from hc.lib.curl import CurlError
from hc.test import BaseTestCase


class ProbePolicyTestCase(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.check = Check(project=self.project, api_url="https://example.org/api")
        self.check.next_probe = now() - td(minutes=1)
        self.check.save()

    def make_response(self, status: int, body: bytes = b'{"status": "ok"}') -> Mock:
        response = Mock()
        response.status_code = status
        response.content = body
        return response

    # -- request options ----------------------------------------------------

    @patch("hc.api.probes.request")
    def test_it_sends_custom_headers(self, mock_request: Mock) -> None:
        self.check.api_headers = json.dumps({"X-Api-Key": "abc", "Accept": "application/json"})
        self.check.save()
        mock_request.return_value = self.make_response(200)

        probe_check(self.check)

        headers = mock_request.call_args.kwargs["headers"]
        self.assertEqual(headers["X-Api-Key"], "abc")
        self.assertEqual(headers["Accept"], "application/json")
        # The probe UA is still present:
        self.assertEqual(headers["User-Agent"], "healthchecks-probe")

    @patch("hc.api.probes.request")
    def test_custom_headers_merge_with_bearer(self, mock_request: Mock) -> None:
        self.check.api_token = "tok"
        self.check.api_headers = json.dumps({"X-Trace": "1"})
        self.check.save()
        mock_request.return_value = self.make_response(200)

        probe_check(self.check)

        headers = mock_request.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer tok")
        self.assertEqual(headers["X-Trace"], "1")

    @patch("hc.api.probes.request")
    def test_invalid_headers_fail_the_probe(self, mock_request: Mock) -> None:
        self.check.api_headers = "not json"
        self.check.save()

        summary = probe_check(self.check)

        # The request is never issued:
        self.assertFalse(mock_request.called)
        self.check.refresh_from_db()
        self.assertEqual(self.check.status, "down")
        self.assertIn("FAILURE", summary)
        self.assertEmailContains("not valid JSON")

    @patch("hc.api.probes.request")
    def test_non_string_header_values_fail(self, mock_request: Mock) -> None:
        self.check.api_headers = json.dumps({"X-Count": 5})
        self.check.save()

        probe_check(self.check)

        self.assertFalse(mock_request.called)
        self.check.refresh_from_db()
        self.assertEqual(self.check.status, "down")

    @patch("hc.api.probes.request")
    def test_it_sends_request_body(self, mock_request: Mock) -> None:
        self.check.api_method = "POST"
        self.check.api_body = '{"query": "hello"}'
        self.check.save()
        mock_request.return_value = self.make_response(200)

        probe_check(self.check)

        data = mock_request.call_args.kwargs["data"]
        self.assertEqual(data, b'{"query": "hello"}')
        self.assertEqual(mock_request.call_args.args[0], "post")

    @patch("hc.api.probes.request")
    def test_empty_body_sends_none(self, mock_request: Mock) -> None:
        self.check.api_body = "   "
        self.check.save()
        mock_request.return_value = self.make_response(200)

        probe_check(self.check)

        self.assertIsNone(mock_request.call_args.kwargs["data"])

    def test_parse_api_headers_variants(self) -> None:
        self.assertEqual(parse_api_headers(""), ({}, ""))
        self.assertEqual(parse_api_headers("   "), ({}, ""))
        headers, err = parse_api_headers('{"A": "b"}')
        self.assertEqual(headers, {"A": "b"})
        self.assertEqual(err, "")
        _, err = parse_api_headers("[1, 2]")
        self.assertIn("JSON object", err)
        _, err = parse_api_headers("{oops")
        self.assertIn("not valid JSON", err)

    # -- latency & special rule paths --------------------------------------

    @patch("hc.api.probes.time")
    @patch("hc.api.probes.request")
    def test_response_time_rule_can_fail(self, mock_request: Mock, mock_time: Mock) -> None:
        mock_time.time.side_effect = [1000.0, 1000.6]  # 600 ms
        self.check.api_rules = json.dumps(
            [{"path": "response_time_ms", "op": "lt", "value": 200}]
        )
        self.check.save()
        mock_request.return_value = self.make_response(200)

        summary = probe_check(self.check)

        self.check.refresh_from_db()
        self.assertEqual(self.check.status, "down")
        self.assertIn("600 ms", summary)
        self.assertIn("0/1 rules passed", summary)

    @patch("hc.api.probes.time")
    @patch("hc.api.probes.request")
    def test_response_time_rule_can_pass(self, mock_request: Mock, mock_time: Mock) -> None:
        mock_time.time.side_effect = [1000.0, 1000.05]  # 50 ms
        self.check.api_rules = json.dumps(
            [{"path": "response_time_ms", "op": "lt", "value": 200}]
        )
        self.check.save()
        mock_request.return_value = self.make_response(200)

        summary = probe_check(self.check)

        self.check.refresh_from_db()
        self.assertEqual(self.check.status, "up")
        self.assertIn("1/1 rules passed", summary)

    @patch("hc.api.probes.request")
    def test_status_code_rule_on_non_json_body(self, mock_request: Mock) -> None:
        self.check.api_rules = json.dumps(
            [{"path": "status_code", "op": "eq", "value": 201}]
        )
        self.check.save()
        mock_request.return_value = self.make_response(201, b"not json at all")

        summary = probe_check(self.check)

        self.check.refresh_from_db()
        self.assertEqual(self.check.status, "up")
        self.assertIn("1/1 rules passed", summary)

    @patch("hc.api.probes.request")
    def test_latency_is_recorded_in_metric(self, mock_request: Mock) -> None:
        mock_request.return_value = self.make_response(200)

        probe_check(self.check)

        metric = ProbeMetric.objects.get(owner=self.check)
        self.assertTrue(metric.success)
        self.assertEqual(metric.status_code, 200)
        self.assertGreaterEqual(metric.response_ms, 0)

    # -- failure threshold & retry -----------------------------------------

    @patch("hc.api.probes.request")
    def test_first_failure_below_threshold_is_silent(self, mock_request: Mock) -> None:
        self.check.probe_min_failures = 2
        self.check.save()
        mock_request.return_value = self.make_response(500)

        before = now()
        summary = probe_check(self.check)

        # No notification email on the first failure:
        self.assertEqual(len(mail.outbox), 0)
        self.check.refresh_from_db()
        self.assertEqual(self.check.probe_fail_count, 1)
        self.assertFalse(self.check.probe_notified_failure)
        # A quick retry is scheduled ~60s out:
        assert self.check.next_probe
        self.assertLess(self.check.next_probe, before + td(seconds=90))
        self.assertGreater(self.check.next_probe, before)
        self.assertIn("failure 1/2", summary)
        # History is still recorded:
        self.assertEqual(ProbeMetric.objects.filter(owner=self.check).count(), 1)

    @patch("hc.api.probes.request")
    def test_second_failure_notifies(self, mock_request: Mock) -> None:
        self.check.probe_min_failures = 2
        self.check.save()
        mock_request.return_value = self.make_response(500)

        probe_check(self.check)  # 1st: silent
        self.assertEqual(len(mail.outbox), 0)

        summary = probe_check(self.check)  # 2nd: notify
        self.assertEqual(len(mail.outbox), 1)
        self.check.refresh_from_db()
        self.assertEqual(self.check.probe_fail_count, 2)
        self.assertTrue(self.check.probe_notified_failure)
        self.assertIn("emailed alice@example.org", summary)

    @patch("hc.api.probes.request")
    def test_success_resets_failure_counters(self, mock_request: Mock) -> None:
        self.check.probe_min_failures = 3
        self.check.probe_fail_count = 2
        self.check.probe_fail_since = now() - td(minutes=5)
        self.check.probe_escalated = True
        self.check.save()
        mock_request.return_value = self.make_response(200)

        probe_check(self.check)

        self.check.refresh_from_db()
        self.assertEqual(self.check.probe_fail_count, 0)
        self.assertFalse(self.check.probe_notified_failure)
        self.assertIsNone(self.check.probe_fail_since)
        self.assertFalse(self.check.probe_escalated)

    # -- schema drift -------------------------------------------------------

    def test_leaf_paths(self) -> None:
        paths = leaf_paths({"a": 1, "b": {"c": 2}, "d": [{"e": 3}]})
        self.assertIn("a", paths)
        self.assertIn("b.c", paths)
        self.assertIn("d[].e", paths)

    @patch("hc.api.probes.request")
    def test_drift_detected_on_added_field(self, mock_request: Mock) -> None:
        # First success stores the baseline schema:
        mock_request.return_value = self.make_response(200, b'{"a": 1}')
        probe_check(self.check)
        mail.outbox.clear()

        # Second success has a new field:
        mock_request.return_value = self.make_response(200, b'{"a": 1, "b": 2}')
        summary = probe_check(self.check)

        self.assertIn("schema drift +1/-0", summary)
        self.assertEmailContains("Schema drift detected")
        self.assertEmailContains("New fields:")

    @patch("hc.api.probes.request")
    def test_drift_detected_on_removed_field(self, mock_request: Mock) -> None:
        mock_request.return_value = self.make_response(200, b'{"a": 1, "b": 2}')
        probe_check(self.check)
        mail.outbox.clear()

        mock_request.return_value = self.make_response(200, b'{"a": 1}')
        summary = probe_check(self.check)

        self.assertIn("schema drift +0/-1", summary)

    def test_compute_drift_ignores_failures(self) -> None:
        self.assertIsNone(compute_drift(self.check, b'{"a": 1}', False))

    def test_compute_drift_ignores_non_json(self) -> None:
        self.assertIsNone(compute_drift(self.check, b"plain text", True))

    # -- suppression --------------------------------------------------------

    @patch("hc.api.probes.request")
    def test_snooze_suppresses_notification(self, mock_request: Mock) -> None:
        self.check.snoozed_until = now() + td(hours=1)
        self.check.save()
        mock_request.return_value = self.make_response(500)

        summary = probe_check(self.check)

        self.assertEqual(len(mail.outbox), 0)
        self.assertIn("suppressed", summary)
        self.assertIn("snoozed", summary)
        # The failure is still recorded in history:
        self.assertEqual(ProbeMetric.objects.filter(owner=self.check).count(), 1)

    @patch("hc.api.probes.request")
    def test_maintenance_window_suppresses(self, mock_request: Mock) -> None:
        MaintenanceWindow.objects.create(
            project=self.project,
            starts=now() - td(hours=1),
            ends=now() + td(hours=1),
            note="deploy",
        )
        mock_request.return_value = self.make_response(500)

        summary = probe_check(self.check)

        self.assertEqual(len(mail.outbox), 0)
        self.assertIn("maintenance window", summary)

    def test_suppression_reason_none_when_clear(self) -> None:
        self.assertEqual(suppression_reason(self.check, now()), "")

    def test_suppression_reason_snooze(self) -> None:
        self.check.snoozed_until = now() + td(hours=1)
        self.assertIn("snoozed until", suppression_reason(self.check, now()))

    @patch("hc.api.probes.request")
    def test_down_dependency_suppresses_notification(self, mock_request: Mock) -> None:
        upstream = Check.objects.create(project=self.project, name="upstream")
        upstream.status = "down"
        upstream.save()
        self.check.depends_on.add(upstream)
        mock_request.return_value = self.make_response(500)

        summary = probe_check(self.check)

        self.assertEqual(len(mail.outbox), 0)
        self.assertIn("suppressed", summary)
        self.assertIn("dependency", summary)
        self.assertIn("upstream", summary)

    @patch("hc.api.probes.request")
    def test_healthy_dependency_does_not_suppress(self, mock_request: Mock) -> None:
        upstream = Check.objects.create(project=self.project, name="upstream")
        upstream.status = "up"
        upstream.last_ping = now()
        upstream.save()
        self.check.depends_on.add(upstream)
        mock_request.return_value = self.make_response(500)

        summary = probe_check(self.check)

        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("suppressed", summary)

    def test_suppression_reason_dependency(self) -> None:
        upstream = Check.objects.create(project=self.project, name="db")
        upstream.status = "down"
        upstream.save()
        self.check.depends_on.add(upstream)
        reason = suppression_reason(self.check, now())
        self.assertIn("dependency", reason)
        self.assertIn("db", reason)

    # -- digest -------------------------------------------------------------

    def test_digest_next_flush_hourly(self) -> None:
        ref = now().replace(minute=15, second=30, microsecond=0)
        nxt = digest_next_flush("hourly", ref)
        self.assertEqual(nxt.minute, 0)
        self.assertEqual(nxt.second, 0)
        self.assertGreater(nxt, ref)

    def test_digest_next_flush_daily(self) -> None:
        ref = now().replace(hour=13, minute=15, second=0, microsecond=0)
        nxt = digest_next_flush("daily", ref)
        self.assertEqual(nxt.hour, 0)
        self.assertEqual(nxt.minute, 0)
        self.assertGreater(nxt, ref)

    @patch("hc.api.probes.request")
    def test_digest_queues_instead_of_sending(self, mock_request: Mock) -> None:
        self.project.probe_digest = "hourly"
        self.project.save()
        mock_request.return_value = self.make_response(500)

        summary = probe_check(self.check)

        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(DigestEntry.objects.filter(project=self.project).count(), 1)
        self.assertIn("queued for hourly digest", summary)
        self.project.refresh_from_db()
        self.assertIsNotNone(self.project.digest_next_flush)

    def test_flush_project_digest_sends_email(self) -> None:
        self.project.probe_digest = "daily"
        self.project.save()
        queue_digest(self.check, False, "report text", now())
        self.assertEqual(DigestEntry.objects.count(), 1)

        sent = flush_project_digest(self.project)

        self.assertEqual(sent, 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["alice@example.org"])
        # Entries are consumed:
        self.assertEqual(DigestEntry.objects.count(), 0)

    @patch("hc.api.transports.curl.request", autospec=True)
    def test_flush_project_digest_notifies_channel(self, mock_curl: Mock) -> None:
        mock_curl.return_value.status_code = 200
        self.project.probe_digest = "hourly"
        self.project.save()
        channel = Channel(
            project=self.project, kind="mattermost", value="https://example.org/hook"
        )
        channel.save()
        channel.checks.add(self.check)

        DigestEntry.objects.create(
            project=self.project,
            owner=self.check,
            channel=channel,
            verdict=False,
            report="boom",
            created=now(),
        )

        sent = flush_project_digest(self.project)

        self.assertEqual(sent, 1)
        self.assertTrue(mock_curl.called)
        payload = mock_curl.call_args.kwargs["json"]
        self.assertIn("Probe digest", payload["attachments"][0]["title"])
        self.assertEqual(DigestEntry.objects.count(), 0)

    def test_flush_due_digests_only_flushes_due(self) -> None:
        self.project.probe_digest = "hourly"
        self.project.digest_next_flush = now() + td(hours=1)  # not due yet
        self.project.save()
        queue_digest(self.check, True, "ok report", now())
        # queue_digest does not overwrite an existing next_flush:
        self.project.refresh_from_db()
        self.project.digest_next_flush = now() + td(hours=1)
        self.project.save(update_fields=["digest_next_flush"])

        self.assertEqual(flush_due_digests(), 0)
        self.assertEqual(len(mail.outbox), 0)

        # Now make it due:
        self.project.digest_next_flush = now() - td(minutes=1)
        self.project.save(update_fields=["digest_next_flush"])
        self.assertEqual(flush_due_digests(), 1)
        self.assertEqual(len(mail.outbox), 1)

    # -- escalation ---------------------------------------------------------

    @patch("hc.api.probes.request")
    def test_handle_escalations_sends_second_notice(self, mock_request: Mock) -> None:
        self.check.probe_escalate_after = 10
        self.check.probe_fail_since = now() - td(minutes=30)
        self.check.probe_fail_count = 5
        self.check.probe_notified_failure = True
        self.check.save()
        # A stored metric provides the escalation context:
        ProbeMetric.objects.create(
            owner=self.check,
            created=now(),
            success=False,
            status_code=500,
            response_ms=120,
            report="last failure report",
        )

        count = handle_escalations()

        self.assertEqual(count, 1)
        self.check.refresh_from_db()
        self.assertTrue(self.check.probe_escalated)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("ESCALATION", mail.outbox[0].subject)

    def test_handle_escalations_skips_not_due(self) -> None:
        self.check.probe_escalate_after = 60
        self.check.probe_fail_since = now() - td(minutes=5)
        self.check.save()

        self.assertEqual(handle_escalations(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_handle_escalations_runs_once(self) -> None:
        self.check.probe_escalate_after = 1
        self.check.probe_fail_since = now() - td(minutes=30)
        self.check.probe_escalated = True  # already escalated
        self.check.save()

        self.assertEqual(handle_escalations(), 0)

    # -- watchdog -----------------------------------------------------------

    def test_ping_watchdogs(self) -> None:
        watchdog = Check(project=self.project, name="wd", tags=" watchdog ")
        watchdog.save()
        passive = Check(project=self.project, name="passive")
        passive.save()

        count = ping_watchdogs()

        self.assertEqual(count, 1)
        self.assertEqual(Ping.objects.filter(owner=watchdog).count(), 1)
        self.assertEqual(Ping.objects.filter(owner=passive).count(), 0)

    def test_ping_watchdogs_skips_paused(self) -> None:
        watchdog = Check(project=self.project, name="wd", tags=" watchdog ", status="paused")
        watchdog.save()

        self.assertEqual(ping_watchdogs(), 0)

    # -- pruning ------------------------------------------------------------

    def test_prune_probe_metrics(self) -> None:
        ProbeMetric.objects.create(owner=self.check, created=now() - td(days=40))
        ProbeMetric.objects.create(owner=self.check, created=now())
        DigestEntry.objects.create(
            project=self.project, owner=self.check, report="x", created=now() - td(days=40)
        )

        deleted = prune_probe_metrics()

        self.assertEqual(deleted, 1)
        self.assertEqual(ProbeMetric.objects.filter(owner=self.check).count(), 1)
        self.assertEqual(DigestEntry.objects.count(), 0)

    # -- housekeeping command ----------------------------------------------

    def test_housekeeping_command_runs_once(self) -> None:
        ProbeMetric.objects.create(owner=self.check, created=now() - td(days=60))

        # Should not raise, and should prune old history:
        call_command("housekeeping")

        self.assertEqual(ProbeMetric.objects.filter(owner=self.check).count(), 0)


class ProbeProjectFieldsTestCase(BaseTestCase):
    def test_project_defaults(self) -> None:
        p = Project.objects.get(code=self.project.code)
        self.assertEqual(p.probe_digest, "off")
        self.assertFalse(p.status_page_enabled)
        self.assertIsNone(p.digest_next_flush)

    def test_check_defaults(self) -> None:
        check = Check.objects.create(project=self.project, api_url="https://x/api")
        self.assertEqual(check.probe_min_failures, 1)
        self.assertEqual(check.probe_escalate_after, 0)
        self.assertEqual(check.probe_fail_count, 0)
        self.assertIsNone(check.snoozed_until)
        self.assertEqual(check.api_schema_keys, [])

    def test_maintenance_window_is_active(self) -> None:
        w = MaintenanceWindow.objects.create(
            project=self.project, starts=now() - td(hours=1), ends=now() + td(hours=1)
        )
        self.assertTrue(w.is_active())
        w2 = MaintenanceWindow.objects.create(
            project=self.project, starts=now() + td(hours=1), ends=now() + td(hours=2)
        )
        self.assertFalse(w2.is_active())


class ProbeTimeoutTestCase(BaseTestCase):
    """Per-probe timeout override (feature: api_timeout)."""

    def setUp(self) -> None:
        super().setUp()
        self.check = Check(project=self.project, api_url="https://example.org/api")
        self.check.save()

    def make_response(self, status: int, body: bytes = b'{"status": "ok"}') -> Mock:
        response = Mock()
        response.status_code = status
        response.content = body
        return response

    @patch("hc.api.probes.request")
    def test_default_timeout_used_when_zero(self, mock_request: Mock) -> None:
        self.check.api_timeout = 0
        self.check.save()
        mock_request.return_value = self.make_response(200)

        probe_check(self.check)

        self.assertEqual(mock_request.call_args.kwargs["timeout"], PROBE_TIMEOUT)

    @patch("hc.api.probes.request")
    def test_custom_timeout_passed_to_request(self, mock_request: Mock) -> None:
        self.check.api_timeout = 7
        self.check.save()
        mock_request.return_value = self.make_response(200)

        probe_check(self.check)

        self.assertEqual(mock_request.call_args.kwargs["timeout"], 7)


class PerRuleRoutingTestCase(BaseTestCase):
    """Per-rule routing: a failed rule can page an extra channel (feature: route)."""

    def setUp(self) -> None:
        super().setUp()
        self.check = Check(project=self.project, api_url="https://example.org/api")
        self.check.save()
        self.channel = Channel(
            project=self.project, kind="mattermost", value="https://example.org/hook"
        )
        self.channel.save()

    def make_response(self, status: int, body: bytes = b'{"status": "ok"}') -> Mock:
        response = Mock()
        response.status_code = status
        response.content = body
        return response

    def test_parse_rules_preserves_route(self) -> None:
        code = str(self.channel.code)
        parsed = parse_rules(
            json.dumps([{"path": "status", "op": "eq", "value": "ok", "route": [code]}])
        )
        self.assertEqual(parsed[0]["route"], [code])

    def test_parse_rules_rejects_bad_route(self) -> None:
        with self.assertRaises(RulesError):
            parse_rules(
                json.dumps(
                    [{"path": "status", "op": "eq", "value": "ok", "route": "nope"}]
                )
            )

    @patch("hc.api.transports.curl.request")
    @patch("hc.api.probes.request")
    def test_failed_rule_notifies_routed_channel(
        self, mock_probe: Mock, mock_curl: Mock
    ) -> None:
        # The routed channel is NOT assigned to the check:
        self.check.api_rules = json.dumps(
            [
                {
                    "path": "status",
                    "op": "eq",
                    "value": "bad",
                    "route": [str(self.channel.code)],
                }
            ]
        )
        self.check.save()
        mock_probe.return_value = self.make_response(200)
        mock_curl.return_value = self.make_response(200)

        summary = probe_check(self.check)

        self.assertIn("routed:", summary)
        self.assertIn("mattermost", summary)
        self.assertTrue(mock_curl.called)
        self.assertTrue(
            Notification.objects.filter(owner=self.check, channel=self.channel).exists()
        )

    @patch("hc.api.transports.curl.request")
    @patch("hc.api.probes.request")
    def test_passing_rule_does_not_route(
        self, mock_probe: Mock, mock_curl: Mock
    ) -> None:
        self.check.api_rules = json.dumps(
            [
                {
                    "path": "status",
                    "op": "eq",
                    "value": "ok",
                    "route": [str(self.channel.code)],
                }
            ]
        )
        self.check.save()
        mock_probe.return_value = self.make_response(200)
        mock_curl.return_value = self.make_response(200)

        summary = probe_check(self.check)

        self.assertNotIn("routed:", summary)
        # No channel is assigned and no rule failed, so nothing was posted:
        self.assertFalse(mock_curl.called)

    def test_assigned_channel_not_double_notified(self) -> None:
        self.channel.checks.add(self.check)
        rule_results = [
            {
                "ok": False,
                "path": "status",
                "op": "eq",
                "expected": "ok",
                "actual": "bad",
                "route": [str(self.channel.code)],
            }
        ]

        logs = notify_routed_channels(self.check, rule_results, now(), {})

        self.assertEqual(logs, [])


class NotificationRetryTestCase(BaseTestCase):
    """Notification retry queue with backoff (feature: retry_failed_notifications)."""

    def setUp(self) -> None:
        super().setUp()
        self.check = Check(project=self.project, api_url="https://example.org/api")
        self.check.save()
        self.channel = Channel(
            project=self.project, kind="mattermost", value="https://example.org/hook"
        )
        self.channel.save()
        self.channel.checks.add(self.check)

    def make_response(self, status: int, body: bytes = b'{"status": "ok"}') -> Mock:
        response = Mock()
        response.status_code = status
        response.content = body
        return response

    def test_retry_payload_serializes_flip(self) -> None:
        flip = Flip(owner=self.check)
        flip.created = now()
        flip.old_status = "up"
        flip.new_status = "down"
        flip.reason = ""
        flip.probe = {
            "check": self.check,
            "success": False,
            "status_code": 500,
            "probe_time": now(),
        }

        payload = flip.retry_payload()

        self.assertEqual(payload["owner"], str(self.check.id))
        self.assertEqual(payload["new_status"], "down")
        self.assertIn("probe", payload)
        self.assertNotIn("check", payload["probe"])
        # Datetimes were converted, so the payload is plain JSON:
        json.dumps(payload)

    @patch("hc.api.transports.curl.request")
    @patch("hc.api.probes.request")
    def test_transient_failure_queues_retry(
        self, mock_probe: Mock, mock_curl: Mock
    ) -> None:
        mock_probe.return_value = self.make_response(500)
        mock_curl.return_value = self.make_response(500)

        probe_check(self.check)

        n = Notification.objects.get(owner=self.check, channel=self.channel)
        self.assertNotEqual(n.error, "")
        self.assertIsNotNone(n.next_retry)
        self.assertIsNotNone(n.retry_payload)
        self.assertEqual(n.retry_count, 0)

    @patch("hc.api.transports.curl.request")
    @patch("hc.api.probes.request")
    def test_retry_succeeds_and_clears(self, mock_probe: Mock, mock_curl: Mock) -> None:
        mock_probe.return_value = self.make_response(500)
        mock_curl.return_value = self.make_response(500)
        probe_check(self.check)

        n = Notification.objects.get(owner=self.check, channel=self.channel)
        self.assertIsNotNone(n.retry_payload)
        # Make the retry due now, and let the next delivery succeed:
        Notification.objects.filter(id=n.id).update(next_retry=now() - td(seconds=1))
        mock_curl.return_value = self.make_response(200)

        attempted, succeeded = retry_failed_notifications()

        self.assertEqual((attempted, succeeded), (1, 1))
        n.refresh_from_db()
        self.assertEqual(n.error, "")
        self.assertEqual(n.retry_count, 1)
        self.assertIsNone(n.next_retry)
        self.assertIsNone(n.retry_payload)

    @patch("hc.api.transports.curl.request")
    @patch("hc.api.probes.request")
    def test_retry_backoff_reschedules(self, mock_probe: Mock, mock_curl: Mock) -> None:
        mock_probe.return_value = self.make_response(500)
        mock_curl.return_value = self.make_response(500)
        probe_check(self.check)

        n = Notification.objects.get(owner=self.check, channel=self.channel)
        Notification.objects.filter(id=n.id).update(next_retry=now() - td(seconds=1))

        attempted, succeeded = retry_failed_notifications()

        self.assertEqual((attempted, succeeded), (1, 0))
        n.refresh_from_db()
        self.assertEqual(n.retry_count, 1)
        self.assertIsNotNone(n.next_retry)
        self.assertIsNotNone(n.retry_payload)

    @patch("hc.api.transports.curl.request")
    @patch("hc.api.probes.request")
    def test_retry_gives_up_after_max(self, mock_probe: Mock, mock_curl: Mock) -> None:
        mock_probe.return_value = self.make_response(500)
        mock_curl.return_value = self.make_response(500)
        probe_check(self.check)

        n = Notification.objects.get(owner=self.check, channel=self.channel)
        Notification.objects.filter(id=n.id).update(
            retry_count=len(NOTIFICATION_RETRY_DELAYS) - 1,
            next_retry=now() - td(seconds=1),
        )

        attempted, succeeded = retry_failed_notifications()

        self.assertEqual((attempted, succeeded), (1, 0))
        n.refresh_from_db()
        self.assertEqual(n.retry_count, len(NOTIFICATION_RETRY_DELAYS))
        self.assertIsNone(n.next_retry)
        self.assertIsNone(n.retry_payload)
        self.assertNotEqual(n.error, "")

    def test_retry_noop_without_payload(self) -> None:
        n = Notification.objects.create(
            channel=self.channel, owner=self.check, check_status="down", error="boom"
        )
        self.assertFalse(retry_notification(n))


class RemoteProbeTestCase(BaseTestCase):
    """Multi-location agents: ingest path + worker exclusion."""

    def setUp(self) -> None:
        super().setUp()
        self.check = Check(
            project=self.project, api_url="https://example.org/api", probe_remote=True
        )
        self.check.save()

    def test_ingest_records_location(self) -> None:
        summary = ingest_remote_probe(
            self.check,
            status_code=200,
            body=b'{"status": "ok"}',
            response_ms=42,
            location="eu-west",
        )

        self.assertIn("from eu-west", summary)
        metric = ProbeMetric.objects.get(owner=self.check)
        self.assertEqual(metric.location, "eu-west")
        self.assertTrue(metric.success)

    def test_ingest_failure_marks_down(self) -> None:
        summary = ingest_remote_probe(
            self.check,
            status_code=500,
            body="oops",
            response_ms=10,
            location="us-east",
        )

        self.assertIn("FAILURE", summary)
        self.check.refresh_from_db()
        self.assertEqual(self.check.status, "down")
        metric = ProbeMetric.objects.get(owner=self.check)
        self.assertEqual(metric.location, "us-east")

    @patch("hc.api.probes.probe_check")
    def test_handle_probes_skips_remote(self, mock_probe_check: Mock) -> None:
        from hc.api.management.commands.sendalerts import Command

        cmd = Command()
        cmd.executor = Mock()
        self.check.next_probe = now() - td(minutes=1)
        self.check.save()

        # The remote check is due but must be left to the agents:
        self.assertEqual(cmd.handle_probes(), 0)

        local = Check(project=self.project, api_url="https://example.org/other")
        local.next_probe = now() - td(minutes=1)
        local.save()
        self.assertEqual(cmd.handle_probes(), 1)


class AgentApiTestCase(BaseTestCase):
    """The /api/v3/agent/ endpoints used by remote multi-location agents."""

    def setUp(self) -> None:
        super().setUp()
        self.check = Check(
            project=self.project, api_url="https://example.org/api", probe_remote=True
        )
        self.check.next_probe = now() - td(minutes=1)
        self.check.save()
        self.h = {"HTTP_X_API_KEY": "X" * 32}

    def test_agent_checks_lists_due_remote(self) -> None:
        r = self.client.get("/api/v3/agent/checks/", **self.h)

        self.assertEqual(r.status_code, 200)
        checks = r.json()["checks"]
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["code"], str(self.check.code))
        self.assertEqual(checks[0]["timeout"], PROBE_TIMEOUT)

    def test_agent_checks_excludes_local(self) -> None:
        self.check.probe_remote = False
        self.check.save()

        r = self.client.get("/api/v3/agent/checks/", **self.h)

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["checks"], [])

    def test_agent_result_ingests(self) -> None:
        url = f"/api/v3/agent/checks/{self.check.code}/result/"
        r = self.client.post(
            url,
            {
                "status_code": 200,
                "response_ms": 30,
                "body": '{"ok": true}',
                "location": "eu-west",
            },
            content_type="application/json",
            **self.h,
        )

        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        metric = ProbeMetric.objects.get(owner=self.check)
        self.assertEqual(metric.location, "eu-west")

    def test_agent_result_rejects_non_remote(self) -> None:
        self.check.probe_remote = False
        self.check.save()
        url = f"/api/v3/agent/checks/{self.check.code}/result/"

        r = self.client.post(
            url,
            {"status_code": 200, "response_ms": 1},
            content_type="application/json",
            **self.h,
        )

        self.assertEqual(r.status_code, 400)

    def test_agent_requires_key(self) -> None:
        r = self.client.get("/api/v3/agent/checks/")
        self.assertEqual(r.status_code, 401)
