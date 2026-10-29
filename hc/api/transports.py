from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, NoReturn

from django.template.loader import render_to_string

from hc.front.templatetags.hc_extras import sortchecks
from hc.lib import curl

if TYPE_CHECKING:
    from hc.api.models import Channel, Check, Flip, Notification, Ping


logger = logging.getLogger(__name__)


def get_ping_body_bytes(ping: Ping | None) -> bytes | None:
    """Return ping body as bytes for a given Ping object.

    If body has not been uploaded to object storage yet, wait 5 seconds
    and try to fetch it again.
    """
    from hc.api.models import Ping

    if ping and ping.has_body():
        try:
            if result := ping.get_body_bytes():
                return result

            if ping.object_size:
                # If the ping object has an object size but get_body_bytes returns
                # None then body is not uploaded to the object storage yet.
                # When sending notifications we can afford to wait a little
                # bit and retry.
                time.sleep(5)
                return ping.get_body_bytes()
        except Ping.GetBodyError:
            pass

    return None


def get_ping_body(ping: Ping | None, maxlen: int | None = None) -> str | None:
    """Return ping body for a given Ping object."""
    body = None
    if body_bytes := get_ping_body_bytes(ping):
        body = body_bytes.decode(errors="replace")
        if maxlen and len(body) > maxlen:
            body = body[:maxlen] + "\n[truncated]"

    return body


def probe_report_text(
    name: str, probe: dict, maxlen: int = 4000, include_body: bool = True
) -> str:
    """Render the unified probe report as plain text.

    This is the same report the probe result email and the Slack-compatible
    integrations show. All fields come from the probe context, so the report
    is dynamic and works for any API shape. Digest payloads (a "digest" key
    with a list of queued probe reports) render as one combined message.

    """

    if "digest" in probe:
        entries = probe["digest"]
        fails = sum(1 for e in entries if not e["verdict"])
        header = f"Probe digest ({probe.get('period', '')}): {len(entries)} result(s)"
        if fails:
            header += f", {fails} FAILURE"
        lines = [header, f"Project: {probe.get('project', '')}"]
        for e in entries:
            lines.append("")
            lines.append("------------------------------------------------")
            lines.append(e["report"])
        if probe.get("details_url"):
            lines.append("")
            lines.append(f"View check details: {probe['details_url']}")
        text = "\n".join(lines)
        if len(text) > maxlen:
            text = text[:maxlen] + "\n[truncated]"
        return text

    verdict = "SUCCESS" if probe["success"] else "FAILURE"
    title = f"Probe result: {verdict} | {name}"
    if probe.get("escalation"):
        title = f"ESCALATION - {title}"
    lines = [
        title,
        "",
        f"Check: {name}",
        f"API: {probe['api_method']} {probe['api_url']}",
        f"HTTP status: {probe['status_code'] or 'n/a'}",
    ]
    if probe.get("response_ms") is not None:
        lines.append(f"Response time: {probe['response_ms']} ms")
    if probe.get("location"):
        lines.append(f"Probed from: {probe['location']}")
    if probe["error"]:
        lines.append(f"Error: {probe['error']}")

    if probe["rules"]:
        lines.append("")
        lines.append("Response checks:")
        for r in probe["rules"]:
            mark = "PASS" if r["ok"] else "FAIL"
            line = f"  [{mark}] {r['path']} {r['op']}"
            if r["expected"] not in (None, ""):
                line += f" expected: {r['expected']} |"
            line += f" actual: {r['actual']}"
            if not r["ok"] and r.get("error"):
                line += f" ({r['error']})"
            lines.append(line)

    if probe["rules_error"]:
        lines.append(f"Rules error: {probe['rules_error']}")

    if probe.get("drift"):
        lines.append("")
        lines.append("Schema drift detected (vs the last successful response):")
        if probe["drift"].get("added"):
            lines.append(f"  New fields: {', '.join(probe['drift']['added'])}")
        if probe["drift"].get("removed"):
            lines.append(f"  Missing fields: {', '.join(probe['drift']['removed'])}")

    if probe["skipped"]:
        lines.append(f"Omitted from response body: {', '.join(probe['skipped'])}")

    lines.append(f"Time (UTC): {probe['probe_time']:%Y-%m-%d %H:%M}")
    lines.append("")
    lines.append(f"View check details: {probe['details_url']}")

    if include_body and probe["body"]:
        lines.append("")
        lines.append("Response body:")
        lines.append("--------------")
        lines.append(probe["body"])

    text = "\n".join(lines)
    if len(text) > maxlen:
        text = text[:maxlen] + "\n[truncated]"
    return text


class TransportError(Exception):
    def __init__(self, message: str, permanent: bool = False) -> None:
        self.message = message
        self.permanent = permanent


class Transport:
    def __init__(self, channel: Channel):
        self.channel = channel

    def notify(self, flip: Flip, notification: Notification) -> None:
        """Send notification about current status of the check.

        This method raises TransportError on error, and returns None
        on success.

        """

        raise NotImplementedError()

    def is_noop(self, status: str) -> bool:
        """Return True if transport will ignore check's current status.

        This method is overridden in Webhook subclass where the user can
        configure webhook urls for "up" and "down" events, and both are
        optional.

        """

        return False

    def down_checks(self, check: Check) -> list[Check] | None:
        """Return a sorted list of other checks in the same project that are down.

        If there are no other hecks in the project, return None instead of empty list.
        Templates can check for None to decide whether to show or not show the
        "All other checks are up" note.

        """

        siblings = self.channel.project.check_set.exclude(id=check.id)
        if not siblings.exists():
            return None

        down_siblings = list(siblings.filter(status="down"))
        sortchecks(down_siblings, "name")

        return down_siblings

    def last_ping(self, flip: Flip) -> Ping | None:
        """Return the last Ping object received before this flip."""

        if not flip.owner.pk:
            return None

        # Sort by "created". Sorting by "id" can cause postgres to pick api_ping.id
        # index (slow if the api_ping table is big)
        q = flip.owner.ping_set.order_by("created")
        # Make sure we're not selecting pings that occurred after the flip
        q = q.filter(created__lte=flip.created)

        return q.last()

    def probe_report(
        self, flip: Flip, maxlen: int = 4000, include_body: bool = True
    ) -> str | None:
        """Return the unified probe report text, if flip carries a probe result.

        Active probe checks attach their probe report to the flip object, so
        that every integration can deliver the exact same content as the
        probe result email.

        """

        probe = getattr(flip, "probe", None)
        if not probe:
            return None

        name = flip.owner.name_then_code()
        return probe_report_text(name, probe, maxlen, include_body)

    def tmpl(self, template_name: str, **ctx: Any) -> str:
        # \xa0 is non-breaking space. It causes SMS messages to use UCS2 encoding
        # and cost twice the money.
        return render_to_string(template_name, ctx).strip().replace("\xa0", " ")


class RemovedTransport(Transport):
    """Dummy transport class for obsolete integrations."""

    def is_noop(self, status: str) -> bool:
        return True


class HttpTransport(Transport):
    @classmethod
    def raise_for_response(cls, response: curl.Response) -> NoReturn:
        # Subclasses can override this method to produce a more specific message.
        raise TransportError(f"Received status code {response.status_code}")

    @classmethod
    def _request(
        cls,
        method: str,
        url: str,
        *,
        params: curl.Params,
        data: curl.Data,
        json: Any,
        headers: curl.Headers,
        auth: curl.Auth,
    ) -> None:
        try:
            r = curl.request(
                method,
                url,
                params=params,
                data=data,
                json=json,
                headers=headers,
                auth=auth,
                timeout=30,
            )
            if r.status_code not in (200, 201, 202, 204, 205):
                cls.raise_for_response(r)
        except curl.CurlError as e:
            raise TransportError(e.message)

    @classmethod
    def request(
        cls,
        method: str,
        url: str,
        *,
        retry: bool,
        params: curl.Params = None,
        data: curl.Data = None,
        json: Any = None,
        headers: curl.Headers = None,
        auth: curl.Auth = None,
    ) -> None:
        tries_left = 3 if retry else 1
        while True:
            try:
                return cls._request(
                    method,
                    url,
                    params=params,
                    data=data,
                    json=json,
                    headers=headers,
                    auth=auth,
                )
            except TransportError as e:
                tries_left = 0 if e.permanent else tries_left - 1
                # If we have no tries left then abort the retry loop by re-raising
                # the exception:
                if tries_left == 0:
                    raise

    # Convenience wrapper around self.request for making "POST" requests
    @classmethod
    def post(
        cls,
        url: str,
        retry: bool = True,
        *,
        params: curl.Params = None,
        data: curl.Data = None,
        json: Any = None,
        headers: curl.Headers = None,
        auth: curl.Auth = None,
    ) -> None:
        cls.request(
            "post",
            url,
            retry=retry,
            params=params,
            data=data,
            json=json,
            headers=headers,
            auth=auth,
        )

    # Convenience wrapper around self.request for making "PUT" requests
    @classmethod
    def put(
        cls,
        url: str,
        retry: bool = True,
        *,
        params: curl.Params = None,
        data: curl.Data = None,
        json: Any = None,
        headers: curl.Headers = None,
        auth: curl.Auth = None,
    ) -> None:
        cls.request(
            "put",
            url,
            retry=retry,
            params=params,
            data=data,
            json=json,
            headers=headers,
            auth=auth,
        )
