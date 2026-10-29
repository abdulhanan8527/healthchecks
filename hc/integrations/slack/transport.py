from __future__ import annotations

import logging
from typing import NoReturn

from django.conf import settings

from hc.api.models import Flip, Notification
from hc.api.transports import HttpTransport, TransportError, get_ping_body
from hc.front.templatetags.hc_extras import absolute_site_logo_url
from hc.lib import curl
from hc.lib.date import format_duration, format_duration_for_sentence
from hc.lib.typealias import JSONDict, JSONValue

logger = logging.getLogger(__name__)


class SlackFields(list[JSONValue]):
    """Helper class for preparing [{"title": ..., "value": ... }, ...] structures."""

    def add(self, title: str, value: str, short: bool = True) -> None:
        field: JSONDict = {"title": title, "value": value}
        if short:
            field["short"] = True
        self.append(field)


class Slackalike(HttpTransport):
    """Base class for transports that use Slack-compatible incoming webhooks."""

    def payload(self, flip: Flip) -> JSONDict:
        """Prepare JSON-serializable payload for Slack-compatible incoming webhook."""
        check = flip.owner
        name = check.name_then_code()

        # Active probe checks attach their probe report to the flip, so that
        # chat integrations show the exact same content as the result email:
        probe = getattr(flip, "probe", None)
        if probe:
            return self.probe_payload(name, probe)

        fields = SlackFields()
        text = None
        if flip.reason:
            text = f"Reason: {flip.reason_long()}." if flip.reason else None
        elif flip.new_status == "up" and flip.down_duration:
            formatted_duration = format_duration_for_sentence(flip.down_duration)
            text = f"The downtime lasted {formatted_duration}."

        result: JSONDict = {
            "username": settings.SITE_NAME,
            "icon_url": absolute_site_logo_url(),
            "attachments": [
                {
                    "color": "good" if flip.new_status == "up" else "danger",
                    "fallback": f'The check "{name}" is {flip.new_status.upper()}.',
                    "mrkdwn_in": ["fields"],
                    "title": f"“{name}” is {flip.new_status.upper()}.",
                    "title_link": check.cloaked_url(),
                    "text": text,
                    "fields": fields,
                }
            ],
        }

        if check.desc:
            fields.add("Description", check.desc, short=False)

        if check.project.name:
            fields.add("Project", check.project.name)

        if tags := check.tags_list():
            fields.add("Tags", " ".join(f"`{tag}`" for tag in tags))

        if check.kind == "simple":
            fields.add("Period", format_duration(check.timeout))

        if check.kind in ("cron", "oncalendar"):
            fields.add("Schedule", self.fix_asterisks(check.schedule))
            fields.add("Time Zone", check.tz)

        if ping := self.last_ping(flip):
            fields.add("Total Pings", str(ping.n))
            fields.add("Last Ping", ping.formatted_kind_created())
        else:
            fields.add("Total Pings", "0")
            fields.add("Last Ping", "Never")

        body = get_ping_body(ping, maxlen=1000)
        if body and "```" not in body:
            fields.add("Last Ping Body", f"```\n{body}\n```", short=False)

        return result

    def probe_payload(self, name: str, probe: dict) -> JSONDict:
        """Render the probe report in the same layout as the result email."""

        # Digest payloads combine several queued probe reports into one card:
        if "digest" in probe:
            from hc.api.transports import probe_report_text

            text = probe_report_text(name, probe, maxlen=6000)
            fails = sum(1 for e in probe["digest"] if not e["verdict"])
            return {
                "username": settings.SITE_NAME,
                "icon_url": absolute_site_logo_url(),
                "attachments": [
                    {
                        "color": "good" if not fails else "danger",
                        "fallback": text[:500],
                        "mrkdwn_in": ["text"],
                        "title": f"Probe digest ({probe.get('period', '')})"
                        f" | {len(probe['digest'])} result(s)",
                        "title_link": probe.get("details_url", ""),
                        "text": f"```\n{text}\n```",
                    }
                ],
            }

        def cell(value: object) -> str:
            # Pipes would break the markdown table structure:
            return str(value).replace("|", "\\|")

        verdict = "SUCCESS" if probe["success"] else "FAILURE"
        title = f"Probe result: {verdict} | {name}"
        if probe.get("escalation"):
            title = f"ESCALATION - {title}"
        lines = [
            f"*Check:* {name}",
            f"*API:* `{probe['api_method']} {probe['api_url']}`",
            f"*HTTP status:* {probe['status_code'] or 'n/a'}",
        ]
        if probe.get("response_ms") is not None:
            lines.append(f"*Response time:* {probe['response_ms']} ms")
        lines.append(f"*Time (UTC):* {probe['probe_time']:%b %d, %Y, %H:%M}")
        if probe["error"]:
            lines.append(f"*Error:* {probe['error']}")

        if probe["rules"]:
            lines.append("")
            lines.append("*Response checks*")
            lines.append("| Result | Field (path) | Condition | Expected | Actual |")
            lines.append("| --- | --- | --- | --- | --- |")
            for r in probe["rules"]:
                mark = "PASS" if r["ok"] else "FAIL"
                expected = r["expected"] if r["expected"] not in (None, "") else "-"
                actual = cell(r["actual"])
                if not r["ok"] and r.get("error"):
                    actual += f" ({cell(r['error'])})"
                lines.append(
                    f"| {mark} | `{cell(r['path'])}` | {cell(r['op'])} "
                    f"| {cell(expected)} | {actual} |"
                )

        if probe["rules_error"]:
            lines.append(f"*Rules error:* {probe['rules_error']}")

        if probe.get("drift"):
            lines.append("")
            lines.append("*Schema drift* (vs the last successful response)")
            if probe["drift"].get("added"):
                lines.append(f"New fields: `{'`, `'.join(probe['drift']['added'])}`")
            if probe["drift"].get("removed"):
                lines.append(
                    f"Missing fields: `{'`, `'.join(probe['drift']['removed'])}`"
                )

        lines.append("")
        lines.append(f"<{probe['details_url']}|View check details>")

        body = probe["body"][:6000]
        if body:
            lines.append("")
            lines.append("*Response body*")
            if probe["skipped"]:
                lines.append(
                    f"_Omitted from response body: {', '.join(probe['skipped'])}_"
                )
            if "```" not in body:
                lines.append(f"```\n{body}\n```")
            else:
                lines.append(body)

        attachment: JSONDict = {
            "color": "good" if probe["success"] else "danger",
            "fallback": f'Probe result for "{name}": {verdict}.',
            "mrkdwn_in": ["text"],
            "title": title,
            "title_link": probe["details_url"],
            "text": "\n".join(lines),
        }
        if actions := self.probe_actions(probe):
            attachment["actions"] = actions

        return {
            "username": settings.SITE_NAME,
            "icon_url": absolute_site_logo_url(),
            "attachments": [attachment],
        }

    def probe_actions(self, probe: dict) -> list[JSONDict]:
        """Interactive message buttons for probe reports.

        The base implementation returns none; Mattermost overrides this to
        add Snooze/Un-snooze buttons that call back into Healthchecks.
        """

        return []

    def fix_asterisks(self, s: str) -> str:
        """Escape asterisks so that they are not recognized as Markdown syntax."""

        # The base implementation prepends asterisks with "Combining Grapheme Joiner"
        # characters but subclasses can override this function and escape
        # asterisks differently
        return s.replace("*", "\u034f*")

    def notify(self, flip: Flip, notification: Notification) -> None:
        self.post(self.channel.slack_webhook_url, json=self.payload(flip))


class Slack(Slackalike):
    @classmethod
    def raise_for_response(cls, response: curl.Response) -> NoReturn:
        message = f"Received status code {response.status_code}"
        permanent = False
        if response.status_code == 404:
            # If Slack returns 404, this endpoint is unlikely to ever work again
            # https://api.slack.com/messaging/webhooks#handling_errors
            permanent = True
        elif response.status_code == 400:
            if response.content == b"invalid_token":
                # If Slack returns 400 with "invalid_token" in response body,
                # we're using a deactivated user's token to post to a private channel.
                # In theory this condition can recover (a deactivated user can be
                # activated), but in practice it is unlikely to happen.
                permanent = True
            else:
                # Log it for later inspection
                logger.debug("Slack returned HTTP 400 with body: %s", response.content)

        raise TransportError(message, permanent=permanent)

    def notify(self, flip: Flip, notification: Notification) -> None:
        if not settings.SLACK_ENABLED:
            raise TransportError("Slack notifications are not enabled.")

        self.post(self.channel.slack_webhook_url, json=self.payload(flip))
