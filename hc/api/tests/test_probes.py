from __future__ import annotations

import json
from datetime import timedelta as td
from unittest.mock import Mock, patch

from django.core import mail
from django.utils.timezone import now

from hc.api.models import Channel, Check, Flip, Notification, Ping
from hc.api.probes import probe_check
from hc.lib.curl import CurlError
from hc.test import BaseTestCase


class ProbesTestCase(BaseTestCase):
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

    @patch("hc.api.probes.request")
    def test_it_records_success(self, mock_request: Mock) -> None:
        mock_request.return_value = self.make_response(200)

        probe_check(self.check)

        self.check.refresh_from_db()
        self.assertEqual(self.check.status, "up")

        ping = Ping.objects.get(owner=self.check)
        self.assertIsNone(ping.kind)
        self.assertEqual(ping.method, "GET")
        self.assertEqual(ping.ua, "healthchecks-probe")

        # The check must be rescheduled into the future:
        assert self.check.next_probe
        self.assertGreater(self.check.next_probe, now())

    @patch("hc.api.probes.request")
    def test_it_records_failure_on_non_2xx(self, mock_request: Mock) -> None:
        mock_request.return_value = self.make_response(500, b"boom")

        probe_check(self.check)

        self.check.refresh_from_db()
        self.assertEqual(self.check.status, "down")

        ping = Ping.objects.get(owner=self.check)
        self.assertEqual(ping.kind, "fail")
        self.assertEqual(bytes(ping.body_raw), b"boom")

    @patch("hc.api.probes.request")
    def test_it_handles_connection_error(self, mock_request: Mock) -> None:
        mock_request.side_effect = CurlError("Connection failed")

        probe_check(self.check)

        self.check.refresh_from_db()
        self.assertEqual(self.check.status, "down")

        ping = Ping.objects.get(owner=self.check)
        self.assertEqual(ping.kind, "fail")
        self.assertIn(b"Connection failed", bytes(ping.body_raw))

        # Even a failed probe must reschedule the check:
        assert self.check.next_probe
        self.assertGreater(self.check.next_probe, now())

    @patch("hc.api.probes.request")
    def test_it_sends_result_email_to_project_owner(self, mock_request: Mock) -> None:
        mock_request.return_value = self.make_response(200)

        probe_check(self.check)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["alice@example.org"])
        self.assertEmailContains("https://example.org/api")
        self.assertEmailContains("SUCCESS")

    @patch("hc.api.probes.request")
    def test_it_sends_result_email_to_notify_email(self, mock_request: Mock) -> None:
        self.check.notify_email = "ops@example.net"
        self.check.save()
        mock_request.return_value = self.make_response(200)

        probe_check(self.check)

        self.assertEqual(mail.outbox[0].to, ["ops@example.net"])

    @patch("hc.api.probes.request")
    def test_it_sends_bearer_token(self, mock_request: Mock) -> None:
        self.check.api_token = "secret-token"
        self.check.save()
        mock_request.return_value = self.make_response(200)

        probe_check(self.check)

        headers = mock_request.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer secret-token")

    @patch("hc.api.probes.request")
    def test_passing_rules_mean_success(self, mock_request: Mock) -> None:
        self.check.api_rules = '[{"path": "status", "op": "eq", "value": "ok"}]'
        self.check.save()
        mock_request.return_value = self.make_response(200)

        summary = probe_check(self.check)

        self.check.refresh_from_db()
        self.assertEqual(self.check.status, "up")
        self.assertIn("SUCCESS", summary)
        self.assertIn("1/1 rules passed", summary)
        self.assertIn("emailed alice@example.org", summary)

    @patch("hc.api.probes.request")
    def test_failing_rules_mean_failure(self, mock_request: Mock) -> None:
        self.check.api_rules = '[{"path": "status", "op": "eq", "value": "perfect"}]'
        self.check.save()
        mock_request.return_value = self.make_response(200)

        probe_check(self.check)

        self.check.refresh_from_db()
        self.assertEqual(self.check.status, "down")

        ping = Ping.objects.get(owner=self.check)
        self.assertEqual(ping.kind, "fail")

    @patch("hc.api.probes.request")
    def test_email_reports_rule_results(self, mock_request: Mock) -> None:
        self.check.api_rules = '[{"path": "status", "op": "eq", "value": "perfect"}]'
        self.check.save()
        mock_request.return_value = self.make_response(200)

        probe_check(self.check)

        # The rule report appears in both text and HTML parts:
        self.assertEmailContains("FAIL")
        self.assertEmailContainsText("[FAIL] status eq")
        # The response body is pretty-printed (indented) in the text part:
        self.assertEmailContainsText('  "status": "ok"')

    @patch("hc.api.probes.request")
    def test_invalid_rules_configuration_fails(self, mock_request: Mock) -> None:
        self.check.api_rules = '[{"op": "bogus"}]'
        self.check.save()
        mock_request.return_value = self.make_response(200)

        probe_check(self.check)

        self.check.refresh_from_db()
        self.assertEqual(self.check.status, "down")
        self.assertEmailContains("Invalid rule configuration")

    @patch("hc.api.probes.request")
    def test_it_skips_paths_in_email_body(self, mock_request: Mock) -> None:
        self.check.api_skip_paths = "data.items"
        self.check.save()
        mock_request.return_value = self.make_response(
            200, b'{"data": {"items": [1, 2], "count": 2}}'
        )

        probe_check(self.check)

        # The skipped subtree is gone, the rest of the body is still there:
        self.assertEmailNotContains('"items": [')
        self.assertEmailContainsText('"count": 2')
        # The email says what was omitted:
        self.assertEmailContains("Omitted from response body: data.items")

    def test_ping_triggers_immediate_probe(self) -> None:
        self.check.next_probe = now() + td(days=1)
        self.check.save()

        r = self.client.get(f"/ping/{self.check.code}")
        self.assertEqual(r.status_code, 200)

        self.check.refresh_from_db()
        assert self.check.next_probe
        self.assertLess(self.check.next_probe, now())
        # The completion signal is log-only: no status change, no flip:
        self.assertEqual(self.check.status, "new")
        self.assertEqual(Flip.objects.count(), 0)
        self.assertEqual(self.check.ping_set.latest("n").kind, "log")

    @patch("hc.api.probes.request")
    def test_probe_verdict_does_not_create_flip(self, mock_request: Mock) -> None:
        self.check.status = "up"
        self.check.save()
        mock_request.return_value = self.make_response(500)

        probe_check(self.check)

        self.check.refresh_from_db()
        self.assertEqual(self.check.status, "down")
        # Probe checks notify via probe result emails, not up/down flips:
        self.assertEqual(Flip.objects.count(), 0)

    def test_ping_keeps_schedule_for_passive_checks(self) -> None:
        passive = Check(project=self.project, name="passive")
        passive.save()

        r = self.client.get(f"/ping/{passive.code}")
        self.assertEqual(r.status_code, 200)

        passive.refresh_from_db()
        self.assertIsNone(passive.next_probe)

    @patch("hc.api.probes.request")
    def test_it_emails_assigned_email_channels(self, mock_request: Mock) -> None:
        channel = Channel(
            project=self.project,
            kind="email",
            value="alerts@example.org",
            email_verified=True,
        )
        channel.save()
        channel.checks.add(self.check)
        mock_request.return_value = self.make_response(200)

        probe_check(self.check)

        recipients = [addr for m in mail.outbox for addr in m.to]
        self.assertEqual(recipients, ["alerts@example.org"])

    @patch("hc.api.probes.request")
    def test_email_channel_can_unsubscribe_from_verdicts(
        self, mock_request: Mock
    ) -> None:
        channel = Channel(
            project=self.project,
            kind="email",
            value='{"value": "alerts@example.org", "up": true, "down": false}',
            email_verified=True,
        )
        channel.save()
        channel.checks.add(self.check)
        mock_request.return_value = self.make_response(500)

        probe_check(self.check)

        recipients = [addr for m in mail.outbox for addr in m.to]
        self.assertNotIn("alerts@example.org", recipients)

    @patch("hc.api.transports.curl.request", autospec=True)
    @patch("hc.api.probes.request")
    def test_it_notifies_assigned_integrations(
        self, mock_request: Mock, mock_curl: Mock
    ) -> None:
        mock_curl.return_value.status_code = 200
        channel = Channel(
            project=self.project, kind="mattermost", value="https://example.org/hook"
        )
        channel.save()
        channel.checks.add(self.check)
        mock_request.return_value = self.make_response(500)

        summary = probe_check(self.check)

        # The Mattermost webhook got the probe verdict:
        self.assertTrue(mock_curl.called)
        self.assertEqual(mock_curl.call_args.args[1], "https://example.org/hook")
        self.assertIn("mattermost", summary)
        # ...in the same report format as the probe result email:
        attachment = mock_curl.call_args.kwargs["json"]["attachments"][0]
        self.assertTrue(attachment["title"].startswith("Probe result: FAILURE"))
        self.assertIn("View check details", attachment["text"])
        # And a notification row was recorded for the channel:
        self.assertEqual(Notification.objects.filter(channel=channel).count(), 1)

    @patch("hc.api.transports.curl.request", autospec=True)
    @patch("hc.api.probes.request")
    def test_telegram_gets_probe_report(
        self, mock_request: Mock, mock_curl: Mock
    ) -> None:
        self.check.api_rules = json.dumps(
            [{"path": "status_code", "op": "eq", "value": 500}]
        )
        self.check.save()
        mock_curl.return_value.status_code = 200
        channel = Channel(
            project=self.project,
            kind="telegram",
            value='{"id": 123456, "thread_id": null}',
        )
        channel.save()
        channel.checks.add(self.check)
        mock_request.return_value = self.make_response(200)

        summary = probe_check(self.check)

        self.assertIn("telegram", summary)
        payload = mock_curl.call_args.kwargs["json"]
        self.assertIn("Probe result: FAILURE", payload["text"])
        self.assertIn("Response checks:", payload["text"])
        self.assertIn("[FAIL] status_code eq", payload["text"])
        self.assertIn("https://example.org/api", payload["text"])

    @patch("hc.api.transports.curl.request", autospec=True)
    @patch("hc.api.probes.request")
    def test_webhook_gets_probe_report(
        self, mock_request: Mock, mock_curl: Mock
    ) -> None:
        self.check.api_rules = json.dumps(
            [{"path": "status_code", "op": "eq", "value": 500}]
        )
        self.check.save()
        mock_curl.return_value.status_code = 200
        channel = Channel(
            project=self.project,
            kind="webhook",
            value=json.dumps(
                {
                    "url_down": "https://example.org/hook",
                    "method_down": "POST",
                    "body_down": "$PROBE_STATUS $PROBE_REPORT",
                    "headers_down": {},
                }
            ),
        )
        channel.save()
        channel.checks.add(self.check)
        mock_request.return_value = self.make_response(200)

        summary = probe_check(self.check)

        self.assertIn("webhook", summary)
        data = mock_curl.call_args.kwargs["data"]
        assert isinstance(data, bytes)
        self.assertIn(b"failure", data)
        self.assertIn(b"Probe result: FAILURE", data)

    @patch("hc.integrations.shell.transport.call")
    @patch("hc.api.probes.request")
    def test_shell_gets_probe_report(
        self, mock_request: Mock, mock_call: Mock
    ) -> None:
        mock_call.return_value = 0
        self.check.api_rules = json.dumps(
            [{"path": "status_code", "op": "eq", "value": 500}]
        )
        self.check.save()
        channel = Channel(
            project=self.project,
            kind="shell",
            value=json.dumps({"cmd_down": "echo hi", "cmd_up": "echo hi"}),
        )
        channel.save()
        channel.checks.add(self.check)
        mock_request.return_value = self.make_response(200)

        with self.settings(SHELL_ENABLED=True):
            summary = probe_check(self.check)

        self.assertIn("shell", summary)
        env = mock_call.call_args.kwargs["env"]
        self.assertEqual(env["PROBE_STATUS"], "failure")
        self.assertIn("Probe result: FAILURE", env["PROBE_REPORT"])
        self.assertIn("https://example.org/api", env["PROBE_REPORT"])

    @patch("hc.api.transports.curl.request", autospec=True)
    @patch("hc.api.probes.request")
    def test_gotify_gets_probe_report(
        self, mock_request: Mock, mock_curl: Mock
    ) -> None:
        self.check.api_rules = json.dumps(
            [{"path": "status_code", "op": "eq", "value": 500}]
        )
        self.check.save()
        mock_curl.return_value.status_code = 200
        channel = Channel(
            project=self.project,
            kind="gotify",
            value=json.dumps(
                {
                    "url": "https://example.org",
                    "token": "tk",
                    "priority": 5,
                    "priority_up": 0,
                }
            ),
        )
        channel.save()
        channel.checks.add(self.check)
        mock_request.return_value = self.make_response(200)

        summary = probe_check(self.check)

        self.assertIn("gotify", summary)
        payload = mock_curl.call_args.kwargs["json"]
        self.assertTrue(payload["title"].startswith("Probe result: FAILURE"))
        self.assertIn("Response checks:", payload["message"])
        self.assertIn("[FAIL] status_code eq", payload["message"])
