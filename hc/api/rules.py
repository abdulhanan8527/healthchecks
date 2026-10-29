"""Per-check success rules for active API probes.

A probe check can store a JSON list of "rules" in Check.api_rules. After a
probe receives an HTTP 2xx response, the response body is parsed as JSON and
every rule is evaluated against it. The probe counts as success only if the
HTTP status is 2xx *and* all rules pass.

Rules are generic (path + operator + optional expected value), so each check
can describe the success criteria of its own API format:

    [
        {"path": "data.date", "op": "eq_today"},
        {"path": "data.discrepancy_in_frequencies_count", "op": "eq", "value": 0},
        {"path": "data.items", "op": "exists"}
    ]

Supported operators:
    eq, ne, gt, gte, lt, lte   compare against "value"
    contains, not_contains     substring / element membership test
    exists, missing            path present / absent in the response
    true, false                truthiness of the resolved value
    eq_today                   value is today's date (YYYY-MM-DD) in the
                               check's timezone

Paths use dot notation with optional array indexes, for example
"data.route_wise_trips[0].discrepancy" or "data.items.0.name".

Two special paths are always available and refer to the probe itself
rather than the response body:

    status_code        the HTTP status code of the probe response
    response_time_ms   how long the probe request took, in milliseconds
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

# A path segment: a key, optionally followed by an [index]:
_INDEX_RE = re.compile(r"^([^\[\]]*)((\[\d+\])*)$")

OPS = (
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "contains",
    "not_contains",
    "exists",
    "missing",
    "true",
    "false",
    "eq_today",
)

_MISSING = object()

# Special rule paths resolved from the probe context, not the response body:
SPECIAL_PATHS = ("status_code", "response_time_ms")


def leaf_paths(data: Any, prefix: str = "", depth: int = 6, limit: int = 300) -> list[str]:
    """Return a sorted list of leaf paths in parsed JSON (for drift detection).

    Arrays are collapsed into "path[]" segments so that list length changes
    do not count as schema drift. Depth and count are capped to keep the
    stored key list small.
    """

    paths: list[str] = []

    def walk(node: Any, path: str, level: int) -> None:
        if len(paths) >= limit:
            return
        if level > depth:
            paths.append(path)
            return
        if isinstance(node, dict):
            if not node:
                paths.append(path)
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else key, level + 1)
        elif isinstance(node, list):
            if not node:
                paths.append(path + "[]")
            else:
                # One representative element is enough for schema purposes:
                walk(node[0], path + "[]", level + 1)
        else:
            paths.append(path)

    walk(data, prefix, 0)
    return sorted(set(paths))[:limit]


class RulesError(ValueError):
    """Raised when the stored rule configuration is malformed."""


def parse_rules(text: str) -> list[dict[str, Any]]:
    """Parse and validate a rule configuration. Raises RulesError."""

    if not text or not text.strip():
        return []

    try:
        data = json.loads(text)
    except ValueError as e:
        raise RulesError(f"Invalid JSON: {e}")

    if isinstance(data, dict):
        data = data.get("rules", [])

    if not isinstance(data, list):
        raise RulesError("Rules must be a JSON list of objects.")

    result = []
    for i, rule in enumerate(data):
        if not isinstance(rule, dict):
            raise RulesError(f"Rule #{i + 1} must be a JSON object.")

        path = rule.get("path")
        if not isinstance(path, str) or not path.strip():
            raise RulesError(f"Rule #{i + 1} must have a string 'path'.")

        op = rule.get("op", "eq")
        if op not in OPS:
            raise RulesError(
                f"Rule #{i + 1} has unknown op '{op}'. Supported: {', '.join(OPS)}."
            )

        result.append({"path": path.strip(), "op": op, "value": rule.get("value")})

        # Optional per-rule routing: a list of project channel codes (UUID
        # strings) notified *in addition* to the check's normal destinations
        # when this specific rule fails ("rule X fails -> team Y"):
        route = rule.get("route")
        if route:
            if not isinstance(route, list) or not all(
                isinstance(c, str) for c in route
            ):
                raise RulesError(
                    f"Rule #{i + 1} 'route' must be a list of channel code strings."
                )
            cleaned = [c.strip() for c in route if c.strip()]
            if cleaned:
                result[-1]["route"] = cleaned

    if not result:
        raise RulesError("Rule list is empty.")

    return result


def resolve_path(data: Any, path: str) -> Any:
    """Walk a dotted/bracketed path into parsed JSON. _MISSING if absent."""

    current = data
    for part in path.split("."):
        if not part:
            continue

        current = _step(current, part)
        if current is _MISSING:
            return _MISSING

    return current


def _step(current: Any, part: str) -> Any:
    """Descend one path segment (a key optionally followed by [indexes])."""

    m = _INDEX_RE.match(part)
    if not m:
        return _MISSING

    key, indexes = m.group(1), m.group(2)
    if key:
        if not isinstance(current, dict) or key not in current:
            return _MISSING
        current = current[key]

    for index in re.findall(r"\[(\d+)\]", indexes):
        if not isinstance(current, list) or int(index) >= len(current):
            return _MISSING
        current = current[int(index)]

    return current


def parse_skip_paths(text: str) -> list[str]:
    """Parse a newline-separated list of paths to omit from email bodies."""

    return [line.strip() for line in text.splitlines() if line.strip()]


def delete_path(data: Any, path: str) -> bool:
    """Remove the value at the given path from parsed JSON in place.

    Returns True if something was removed.
    """

    parts = [p for p in path.split(".") if p]
    if not parts:
        return False

    parent = data
    for part in parts[:-1]:
        parent = _step(parent, part)
        if parent is _MISSING:
            return False

    m = _INDEX_RE.match(parts[-1])
    if not m:
        return False

    key, indexes = m.group(1), m.group(2)
    idx = [int(i) for i in re.findall(r"\[(\d+)\]", indexes)]

    if key:
        if not isinstance(parent, dict) or key not in parent:
            return False
        if not idx:
            del parent[key]
            return True
        parent = parent[key]

    # A trailing [index]: descend all but the last index, then delete:
    for i in idx[:-1]:
        if not isinstance(parent, list) or i >= len(parent):
            return False
        parent = parent[i]

    if idx and isinstance(parent, list) and idx[-1] < len(parent):
        del parent[idx[-1]]
        return True

    return False


def _display(value: Any) -> str:
    if value is _MISSING:
        return "(path not found)"
    if isinstance(value, str):
        return value if len(value) <= 100 else value[:100] + "..."
    return json.dumps(value, ensure_ascii=False)


def _evaluate(
    rule: dict[str, Any], payload: Any, today: str, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    path, op, expected = rule["path"], rule["op"], rule.get("value")
    if extra is not None and path in extra:
        actual = extra[path]
    else:
        actual = resolve_path(payload, path)

    result = {
        "path": path,
        "op": op,
        "expected": "today's date"
        if op == "eq_today"
        else ""
        if op in ("exists", "missing", "true", "false")
        else _display(expected),
        "actual": _display(actual),
        "ok": False,
        "error": "",
    }
    # Carry the optional routing targets through to the evaluation result so
    # the probe engine can notify them when this rule fails:
    if rule.get("route"):
        result["route"] = rule["route"]

    if op == "missing":
        result["ok"] = actual is _MISSING
        if not result["ok"]:
            result["error"] = "path exists but should be missing"
        return result

    if actual is _MISSING:
        result["error"] = "path not found in response"
        return result

    try:
        if op == "eq":
            result["ok"] = actual == expected
        elif op == "ne":
            result["ok"] = actual != expected
        elif op in ("gt", "gte", "lt", "lte"):
            if not isinstance(actual, (int, float)) or isinstance(actual, bool):
                result["error"] = f"value is {type(actual).__name__}, not a number"
            elif op == "gt":
                result["ok"] = actual > expected
            elif op == "gte":
                result["ok"] = actual >= expected
            elif op == "lt":
                result["ok"] = actual < expected
            else:
                result["ok"] = actual <= expected
        elif op == "contains":
            result["ok"] = expected in actual
        elif op == "not_contains":
            result["ok"] = expected not in actual
        elif op == "exists":
            result["ok"] = True
        elif op == "true":
            result["ok"] = actual is True
        elif op == "false":
            result["ok"] = actual is False
        elif op == "eq_today":
            result["ok"] = str(actual)[:10] == today
    except TypeError:
        result["ok"] = False
        result["error"] = f"cannot compare {type(actual).__name__} with {type(expected).__name__}"

    if not result["ok"] and not result["error"]:
        result["error"] = "condition not met"

    return result


def evaluate_rules(
    rules: list[dict[str, Any]],
    body: bytes,
    tz: str,
    extra: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Evaluate rules against a response body.

    "extra" provides values for the special probe-context paths
    (status_code, response_time_ms). If every rule targets a special path,
    a non-JSON body is tolerated.

    Returns (per-rule results, parse error). If the body is not valid JSON,
    the parse error string is set and the results list is empty.
    """

    text = body.decode(errors="replace")
    try:
        payload = json.loads(text)
    except ValueError as e:
        if extra is not None and rules and all(
            r["path"] in SPECIAL_PATHS for r in rules
        ):
            payload = None
        else:
            return [], f"Response body is not valid JSON: {e}"

    try:
        today = datetime.now(ZoneInfo(tz)).date().isoformat()
    except Exception:
        today = datetime.now().date().isoformat()

    return [_evaluate(rule, payload, today, extra) for rule in rules], ""
