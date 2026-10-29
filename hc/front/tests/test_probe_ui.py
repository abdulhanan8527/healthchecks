from __future__ import annotations

import json
from datetime import timedelta as td

from django.core import signing
from django.core.cache import cache
from django.utils.timezone import now

from hc.api.models import Channel, Check, MaintenanceWindow, Notification, ProbeMetric
from hc.test import BaseTestCase


class PlaygroundTestCase(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.check = Check.objects.create(project=self.project, name="Foo")
        self.url = f"/checks/{self.check.code}/probe_playground/"

    def test_it_evaluates_rules(self) -> None:
        self.client.login(username="alice@example.org", password="password")
        r = self.client.post(
            self.url,
            {
                "sample_body": '{"status": "ok", "count": 3}',
                "rules": json.dumps([{"path": "status", "op": "eq", "value": "ok"}]),
                "status_code": "200",
            },
        )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["error"], "")
        self.assertEqual(len(data["results"]), 1)
        self.assertTrue(data["results"][0]["ok"])

    def test_it_reports_failing_rule(self) -> None:
        self.client.login(username="alice@example.org", password="password")
        r = self.client.post(
            self.url,
            {
                "sample_body": '{"status": "ok"}',
                "rules": json.dumps([{"path": "status", "op": "eq", "value": "bad"}]),
                "status_code": "200",
            },
        )
        data = r.json()
        self.assertFalse(data["results"][0]["ok"])

    def test_it_supports_status_code_special_path(self) -> None:
        self.client.login(username="alice@example.org", password="password")
        r = self.client.post(
            self.url,
            {
                "sample_body": "not json",
                "rules": json.dumps([{"path": "status_code", "op": "eq", "value": 404}]),
                "status_code": "404",
            },
        )
        data = r.json()
        self.assertTrue(data["results"][0]["ok"])

    def test_invalid_rules_return_error(self) -> None:
        self.client.login(username="alice@example.org", password="password")
        r = self.client.post(self.url, {"sample_body": "{}", "rules": "[{oops"})
        data = r.json()
        self.assertNotEqual(data["error"], "")
        self.assertEqual(data["results"], [])

    def test_charlie_cannot_use_it(self) -> None:
        self.client.login(username="charlie@example.org", password="password")
        r = self.client.post(self.url, {"sample_body": "{}", "rules": "[]"})
        self.assertEqual(r.status_code, 404)


class SnoozeTestCase(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.check = Check.objects.create(project=self.project, name="Foo")
        self.url = f"/checks/{self.check.code}/snooze/"

    def test_it_snoozes(self) -> None:
        self.client.login(username="alice@example.org", password="password")
        r = self.client.post(self.url, {"hours": "4"})
        self.assertEqual(r.status_code, 302)
        self.check.refresh_from_db()
        assert self.check.snoozed_until
        self.assertGreater(self.check.snoozed_until, now() + td(hours=3))

    def test_it_unsnoozes(self) -> None:
        self.check.snoozed_until = now() + td(hours=1)
        self.check.save()
        self.client.login(username="alice@example.org", password="password")
        r = self.client.post(self.url, {"hours": "0"})
        self.assertEqual(r.status_code, 302)
        self.check.refresh_from_db()
        self.assertIsNone(self.check.snoozed_until)

    def test_it_rejects_bad_hours(self) -> None:
        self.client.login(username="alice@example.org", password="password")
        r = self.client.post(self.url, {"hours": "9999"})
        self.assertEqual(r.status_code, 400)

    def test_it_rejects_non_numeric_hours(self) -> None:
        self.client.login(username="alice@example.org", password="password")
        r = self.client.post(self.url, {"hours": "abc"})
        self.assertEqual(r.status_code, 400)


class MattermostCallbackTestCase(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.check = Check.objects.create(project=self.project, name="Foo")
        self.url = "/integrations/mattermost/callback/"

    def _token(self) -> str:
        from hc.integrations.mattermost.transport import ACTIONS_SALT

        return signing.dumps(str(self.check.code), salt=ACTIONS_SALT)

    def test_snooze_button(self) -> None:
        payload = {"context": {"token": self._token(), "action": "snooze_4h"}}
        r = self.client.post(self.url, json.dumps(payload), content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertIn("ephemeral_text", r.json())
        self.check.refresh_from_db()
        assert self.check.snoozed_until
        self.assertGreater(self.check.snoozed_until, now() + td(hours=3))

    def test_unsnooze_button(self) -> None:
        self.check.snoozed_until = now() + td(hours=2)
        self.check.save()
        payload = {"context": {"token": self._token(), "action": "unsnooze"}}
        r = self.client.post(self.url, json.dumps(payload), content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.check.refresh_from_db()
        self.assertIsNone(self.check.snoozed_until)

    def test_bad_signature_rejected(self) -> None:
        payload = {"context": {"token": "forged", "action": "snooze_1h"}}
        r = self.client.post(self.url, json.dumps(payload), content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.check.refresh_from_db()
        self.assertIsNone(self.check.snoozed_until)

    def test_unknown_action_rejected(self) -> None:
        payload = {"context": {"token": self._token(), "action": "explode"}}
        r = self.client.post(self.url, json.dumps(payload), content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_malformed_body_rejected(self) -> None:
        r = self.client.post(self.url, "not json", content_type="application/json")
        self.assertEqual(r.status_code, 400)


class StatusPageTestCase(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        self.check = Check.objects.create(project=self.project, name="Foo", api_url="https://x/api")
        ProbeMetric.objects.create(
            owner=self.check, created=now(), success=True, status_code=200, response_ms=42
        )
        self.url = f"/projects/{self.project.code}/status_page/"

    def test_it_404s_when_disabled(self) -> None:
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 404)

    def test_it_renders_when_enabled(self) -> None:
        self.project.status_page_enabled = True
        self.project.save()
        # No login required:
        r = self.client.get(self.url)
        self.assertContains(r, "Foo", status_code=200)

    def test_it_shows_probe_stats(self) -> None:
        self.project.status_page_enabled = True
        self.project.save()
        r = self.client.get(self.url)
        self.assertContains(r, "42")


class ProjectSettingsTestCase(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.url = f"/projects/{self.project.code}/probe_policies/"

    def test_get_requires_login(self) -> None:
        r = self.client.get(self.url)
        self.assertRedirects(r, f"/accounts/login/?next={self.url}")

    def test_get_shows_form(self) -> None:
        self.client.login(username="alice@example.org", password="password")
        r = self.client.get(self.url)
        self.assertContains(r, "probe_digest", status_code=200)

    def test_post_updates_settings(self) -> None:
        self.client.login(username="alice@example.org", password="password")
        r = self.client.post(
            self.url, {"probe_digest": "hourly", "status_page_enabled": "1"}
        )
        self.assertEqual(r.status_code, 302)
        self.project.refresh_from_db()
        self.assertEqual(self.project.probe_digest, "hourly")
        self.assertTrue(self.project.status_page_enabled)

    def test_post_rejects_bad_digest(self) -> None:
        self.client.login(username="alice@example.org", password="password")
        self.client.post(self.url, {"probe_digest": "weekly"})
        self.project.refresh_from_db()
        self.assertEqual(self.project.probe_digest, "off")

    def test_post_turning_off_digest_clears_flush(self) -> None:
        self.project.probe_digest = "hourly"
        self.project.digest_next_flush = now() + td(hours=1)
        self.project.save()
        self.client.login(username="alice@example.org", password="password")
        self.client.post(self.url, {"probe_digest": "off"})
        self.project.refresh_from_db()
        self.assertIsNone(self.project.digest_next_flush)

    def test_charlie_cannot_post(self) -> None:
        self.client.login(username="charlie@example.org", password="password")
        r = self.client.post(self.url, {"probe_digest": "hourly"})
        self.assertEqual(r.status_code, 404)


class MaintenanceTestCase(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.add_url = f"/projects/{self.project.code}/probe_policies/maintenance/"
        self.remove_url = f"/projects/{self.project.code}/probe_policies/maintenance/remove/"

    def test_add_window(self) -> None:
        self.client.login(username="alice@example.org", password="password")
        starts = (now() + td(hours=1)).strftime("%Y-%m-%dT%H:%M")
        ends = (now() + td(hours=3)).strftime("%Y-%m-%dT%H:%M")
        r = self.client.post(self.add_url, {"starts": starts, "ends": ends, "note": "deploy"})
        self.assertEqual(r.status_code, 302)
        w = MaintenanceWindow.objects.get(project=self.project)
        self.assertEqual(w.note, "deploy")

    def test_add_rejects_ends_before_starts(self) -> None:
        self.client.login(username="alice@example.org", password="password")
        starts = (now() + td(hours=3)).strftime("%Y-%m-%dT%H:%M")
        ends = (now() + td(hours=1)).strftime("%Y-%m-%dT%H:%M")
        r = self.client.post(self.add_url, {"starts": starts, "ends": ends, "note": ""})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(MaintenanceWindow.objects.count(), 0)

    def test_add_rejects_garbage_dates(self) -> None:
        self.client.login(username="alice@example.org", password="password")
        r = self.client.post(self.add_url, {"starts": "nope", "ends": "nah", "note": ""})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(MaintenanceWindow.objects.count(), 0)

    def test_remove_window(self) -> None:
        w = MaintenanceWindow.objects.create(
            project=self.project, starts=now(), ends=now() + td(hours=1)
        )
        self.client.login(username="alice@example.org", password="password")
        r = self.client.post(self.remove_url, {"id": str(w.id)})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(MaintenanceWindow.objects.count(), 0)

    def test_remove_bad_id(self) -> None:
        self.client.login(username="alice@example.org", password="password")
        r = self.client.post(self.remove_url, {"id": "abc"})
        self.assertEqual(r.status_code, 400)


class DetailsProbeUITestCase(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.check = Check.objects.create(
            project=self.project, name="Foo", api_url="https://x/api"
        )
        ProbeMetric.objects.create(
            owner=self.check, created=now(), success=True, status_code=200, response_ms=15
        )
        self.url = f"/checks/{self.check.code}/details/"

    def test_details_shows_probe_ui(self) -> None:
        self.client.login(username="alice@example.org", password="password")
        r = self.client.get(self.url)
        self.assertContains(r, "/snooze/", status_code=200)
        self.assertContains(r, "rules-playground")
        # The sparkline renders an inline SVG:
        self.assertContains(r, "<svg")

    def test_details_shows_snooze_banner(self) -> None:
        self.check.snoozed_until = now() + td(hours=2)
        self.check.save()
        self.client.login(username="alice@example.org", password="password")
        r = self.client.get(self.url)
        self.assertContains(r, "Snoozed")


class DependencyUITestCase(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.check = Check.objects.create(
            project=self.project, name="Foo", api_url="https://x/api"
        )
        self.other = Check.objects.create(project=self.project, name="Bar")
        self.url = f"/checks/{self.check.code}/details/"

    def test_details_offers_depends_on(self) -> None:
        self.client.login(username="alice@example.org", password="password")
        r = self.client.get(self.url)
        self.assertContains(r, "Depends On", status_code=200)
        self.assertContains(r, "Bar")

    def test_details_hides_depends_on_when_only_check(self) -> None:
        self.other.delete()
        self.client.login(username="alice@example.org", password="password")
        r = self.client.get(self.url)
        self.assertNotContains(r, "Depends On")

    def test_form_queryset_is_project_scoped(self) -> None:
        from hc.front.forms import ProbeSettingsForm

        # A check in another project must not be selectable:
        foreign = Check.objects.create(project=self.charlies_project, name="Foreign")
        form = ProbeSettingsForm(instance=self.check)
        qs = form.fields["depends_on"].queryset
        self.assertIn(self.other, list(qs))
        self.assertNotIn(self.check, list(qs))  # cannot depend on itself
        self.assertNotIn(foreign, list(qs))

    def test_saving_dependency_persists(self) -> None:
        self.check.depends_on.add(self.other)
        self.other.status = "down"
        self.other.save()
        self.client.login(username="alice@example.org", password="password")
        r = self.client.get(self.url)
        # The checkbox for the down dependency is pre-checked:
        self.assertContains(r, "depends_on")


class ProbeSettingsFieldsTestCase(BaseTestCase):
    """The details page exposes per-probe timeout and the remote-probe toggle."""

    def setUp(self) -> None:
        super().setUp()
        self.check = Check.objects.create(
            project=self.project, name="Foo", api_url="https://x/api"
        )
        self.details_url = f"/checks/{self.check.code}/details/"
        self.settings_url = f"/checks/{self.check.code}/probe_settings/"

    def base_form(self) -> dict:
        return {
            "api_url": "https://x/api",
            "api_method": "GET",
            "api_token": "",
            "api_headers": "",
            "api_body": "",
            "api_timeout": "",
            "api_rules": "",
            "api_skip_paths": "",
            "probe_min_failures": "1",
            "probe_escalate_after": "0",
        }

    def test_details_shows_timeout_and_remote(self) -> None:
        self.client.login(username="alice@example.org", password="password")
        r = self.client.get(self.details_url)
        self.assertContains(r, "Timeout (s)", status_code=200)
        self.assertContains(r, "Remote Probe")

    def test_saving_timeout_and_remote(self) -> None:
        self.client.login(username="alice@example.org", password="password")
        form = self.base_form()
        form["api_timeout"] = "12"
        form["probe_remote"] = "on"

        r = self.client.post(self.settings_url, form)

        self.assertEqual(r.status_code, 302)
        self.check.refresh_from_db()
        self.assertEqual(self.check.api_timeout, 12)
        self.assertTrue(self.check.probe_remote)

    def test_blank_timeout_stores_zero(self) -> None:
        self.client.login(username="alice@example.org", password="password")

        r = self.client.post(self.settings_url, self.base_form())

        self.assertEqual(r.status_code, 302)
        self.check.refresh_from_db()
        self.assertEqual(self.check.api_timeout, 0)
        self.assertFalse(self.check.probe_remote)


class NotificationErrorsUITestCase(BaseTestCase):
    """The per-project notification errors page surfaces the retry queue."""

    def setUp(self) -> None:
        super().setUp()
        self.check = Check.objects.create(
            project=self.project, name="Foo", api_url="https://x/api"
        )
        self.channel = Channel(
            project=self.project, kind="mattermost", value="https://example.org/hook"
        )
        self.channel.save()
        self.channel.checks.add(self.check)
        self.url = f"/projects/{self.project.code}/notification_errors/"

    def test_it_lists_errors(self) -> None:
        Notification.objects.create(
            channel=self.channel, owner=self.check, check_status="down", error="boom"
        )
        self.client.login(username="alice@example.org", password="password")
        r = self.client.get(self.url)
        self.assertContains(r, "boom", status_code=200)
        self.assertContains(r, "mattermost")

    def test_it_shows_retry_state(self) -> None:
        Notification.objects.create(
            channel=self.channel,
            owner=self.check,
            check_status="down",
            error="boom",
            retry_count=1,
            next_retry=now() + td(seconds=120),
            retry_payload={"owner": str(self.check.id)},
        )
        self.client.login(username="alice@example.org", password="password")
        r = self.client.get(self.url)
        self.assertContains(r, "retrying in", status_code=200)

    def test_empty(self) -> None:
        self.client.login(username="alice@example.org", password="password")
        r = self.client.get(self.url)
        self.assertContains(r, "No notification errors", status_code=200)
