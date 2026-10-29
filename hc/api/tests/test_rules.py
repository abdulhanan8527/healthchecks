from __future__ import annotations

import json
from datetime import date

from hc.api import rules
from hc.test import BaseTestCase

BODY = json.dumps(
    {
        "code": 200,
        "success": True,
        "data": {
            "date": date.today().isoformat(),
            "discrepancy_in_frequency_metadata_count": 29,
            "discrepancy_in_frequencies_count": 0,
            "items": [{"name": "first"}, {"name": "second"}],
        },
    }
).encode()


def evaluate(rule: dict) -> tuple[list[dict], str]:
    return rules.evaluate_rules([rule], BODY, "UTC")


class RulesParseTestCase(BaseTestCase):
    def test_it_parses_list(self) -> None:
        parsed = rules.parse_rules('[{"path": "a", "op": "eq", "value": 1}]')
        self.assertEqual(parsed, [{"path": "a", "op": "eq", "value": 1}])

    def test_it_accepts_empty(self) -> None:
        self.assertEqual(rules.parse_rules("  "), [])

    def test_it_rejects_invalid_json(self) -> None:
        with self.assertRaises(rules.RulesError):
            rules.parse_rules("[not json")

    def test_it_rejects_unknown_op(self) -> None:
        with self.assertRaises(rules.RulesError):
            rules.parse_rules('[{"path": "a", "op": "regex"}]')

    def test_it_rejects_missing_path(self) -> None:
        with self.assertRaises(rules.RulesError):
            rules.parse_rules('[{"op": "eq"}]')


class RulesEvaluateTestCase(BaseTestCase):
    def test_eq(self) -> None:
        result, err = evaluate({"path": "code", "op": "eq", "value": 200})
        self.assertEqual((result[0]["ok"], err), (True, ""))

    def test_ne_and_compare(self) -> None:
        self.assertTrue(evaluate({"path": "code", "op": "gte", "value": 200})[0][0]["ok"])
        self.assertTrue(evaluate({"path": "code", "op": "lt", "value": 300})[0][0]["ok"])
        self.assertFalse(evaluate({"path": "code", "op": "ne", "value": 200})[0][0]["ok"])

    def test_eq_today(self) -> None:
        result, _ = evaluate({"path": "data.date", "op": "eq_today"})
        self.assertTrue(result[0]["ok"])

    def test_eq_today_fails_for_old_date(self) -> None:
        body = json.dumps({"data": {"date": "2020-01-01"}}).encode()
        result, _ = rules.evaluate_rules(
            [{"path": "data.date", "op": "eq_today"}], body, "UTC"
        )
        self.assertFalse(result[0]["ok"])

    def test_array_index_path(self) -> None:
        result, _ = evaluate({"path": "data.items[1].name", "op": "eq", "value": "second"})
        self.assertTrue(result[0]["ok"])

    def test_missing_path_fails(self) -> None:
        result, _ = evaluate({"path": "data.nope", "op": "eq", "value": 1})
        self.assertFalse(result[0]["ok"])
        self.assertEqual(result[0]["error"], "path not found in response")

    def test_exists_and_missing_ops(self) -> None:
        self.assertTrue(evaluate({"path": "data.items", "op": "exists"})[0][0]["ok"])
        self.assertTrue(evaluate({"path": "data.nope", "op": "missing"})[0][0]["ok"])

    def test_true_false_ops(self) -> None:
        self.assertTrue(evaluate({"path": "success", "op": "true"})[0][0]["ok"])
        self.assertFalse(evaluate({"path": "success", "op": "false"})[0][0]["ok"])

    def test_contains(self) -> None:
        result, _ = evaluate({"path": "data.items[0].name", "op": "contains", "value": "irs"})
        self.assertTrue(result[0]["ok"])

    def test_invalid_json_body(self) -> None:
        result, err = rules.evaluate_rules([{"path": "a", "op": "exists"}], b"hello", "UTC")
        self.assertEqual(result, [])
        self.assertIn("not valid JSON", err)


class SkipPathsTestCase(BaseTestCase):
    def test_it_parses_skip_paths(self) -> None:
        self.assertEqual(rules.parse_skip_paths(" a.b \n\nc\n"), ["a.b", "c"])
        self.assertEqual(rules.parse_skip_paths(""), [])

    def test_it_deletes_path(self) -> None:
        data = {"data": {"items": [1, 2, 3], "keep": 1}}
        self.assertTrue(rules.delete_path(data, "data.items"))
        self.assertEqual(data, {"data": {"keep": 1}})

    def test_delete_returns_false_for_missing_path(self) -> None:
        data = {"a": {"b": 1}}
        self.assertFalse(rules.delete_path(data, "a.c"))
        self.assertFalse(rules.delete_path(data, "x.y"))
        self.assertEqual(data, {"a": {"b": 1}})

    def test_it_deletes_list_element(self) -> None:
        data = {"a": [1, 2, 3]}
        self.assertTrue(rules.delete_path(data, "a[1]"))
        self.assertEqual(data, {"a": [1, 3]})
