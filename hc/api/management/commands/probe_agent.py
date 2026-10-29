"""Remote multi-location probe agent.

Run this command on a host in each location you want to probe from. It polls
the Healthchecks API for probe checks marked "remote" (Check.probe_remote),
performs each HTTP request *from this location*, and posts the raw result back
so the server can evaluate rules and send notifications. The recorded history
(ProbeMetric) and the report are tagged with the --location label, so you can
tell eu-west from us-east.

Multiple agents (one per location) can run against the same project. Each uses
the project's read-write API key.

Example:

    python manage.py probe_agent \
        --server https://hc.example.com \
        --api-key <rw-api-key> \
        --location eu-west

Use --once to run a single poll cycle (handy under an external scheduler such
as cron or systemd timers, where you do not want a long-lived process).
"""

from __future__ import annotations

import time
from argparse import ArgumentParser
from typing import Any

from django.core.management.base import BaseCommand

from hc.lib.curl import CurlError, request

# Cap on how much of the response body we ship back to the server:
MAX_BODY = 100 * 1024


class Command(BaseCommand):
    help = "Poll for remote probe checks and report results from this location"

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--server",
            required=True,
            help="Base URL of the Healthchecks server, e.g. https://hc.example.com",
        )
        parser.add_argument(
            "--api-key",
            required=True,
            help="The project's read-write API key",
        )
        parser.add_argument(
            "--location",
            default="",
            help="Label for this agent's location, e.g. eu-west (default: empty)",
        )
        parser.add_argument(
            "--interval",
            type=int,
            default=30,
            help="Poll interval in seconds when running as a loop (default: 30)",
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run a single poll cycle and exit (for cron/systemd timers)",
        )

    def handle(
        self,
        server: str,
        api_key: str,
        location: str,
        interval: int,
        once: bool,
        **options: Any,
    ) -> str:
        base = server.rstrip("/")
        headers = {"X-Api-Key": api_key}
        label = location or "default"
        self.stdout.write(f"probe_agent starting (location={label}, server={base})")

        while True:
            try:
                self.poll_cycle(base, headers, label)
            except Exception as exc:
                self.stderr.write(f"poll cycle failed: {exc}")

            if once:
                break
            time.sleep(max(5, interval))

        return "probe_agent stopped"

    def poll_cycle(self, base: str, headers: dict[str, str], label: str) -> None:
        response = request(
            "get", f"{base}/api/v3/agent/checks/", headers=headers, timeout=30
        )
        if response.status_code != 200:
            self.stderr.write(
                f"agent/checks returned {response.status_code}: "
                f"{response.text[:200]}"
            )
            return

        data = response.json()
        checks = data.get("checks", []) if isinstance(data, dict) else []
        for spec in checks:
            self.run_one(base, headers, label, spec)

    def run_one(
        self, base: str, headers: dict[str, str], label: str, spec: dict[str, Any]
    ) -> None:
        code = spec.get("code")
        method = (spec.get("method") or "GET").lower()
        url = spec.get("url") or ""
        req_headers = dict(spec.get("headers") or {})
        body = spec.get("body") or ""
        timeout = spec.get("timeout") or 30

        status_code, response_ms, resp_body, err = 0, 0, "", ""
        started = time.time()
        try:
            resp = request(
                method,
                url,
                headers=req_headers,
                data=body.encode() if body else None,
                timeout=timeout,
            )
            status_code = resp.status_code
            resp_body = resp.content.decode(errors="replace")[:MAX_BODY]
        except CurlError as exc:
            err = exc.message[:200]
        except Exception as exc:
            err = str(exc)[:200]
        response_ms = int((time.time() - started) * 1000)

        payload = {
            "status_code": status_code,
            "response_ms": response_ms,
            "body": resp_body,
            "location": label if label != "default" else "",
            "error": err,
        }
        try:
            r = request(
                "post",
                f"{base}/api/v3/agent/checks/{code}/result/",
                headers=headers,
                json=payload,
                timeout=30,
            )
            if r.status_code == 200:
                result = r.json()
                summary = result.get("summary", "") if isinstance(result, dict) else ""
                self.stdout.write(f"[{label}] {summary}")
            else:
                self.stderr.write(
                    f"result post failed {r.status_code}: {r.text[:200]}"
                )
        except Exception as exc:
            self.stderr.write(f"result post error: {exc}")
