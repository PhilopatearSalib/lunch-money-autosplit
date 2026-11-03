"""Tests for Lunch Money Auto-Split.

Run them with:  python -m unittest discover -s tests -v
No network access or Lunch Money account is needed: the API is replaced by a fake.
"""
import datetime as dt
import io
import logging
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import autosplit as a  # noqa: E402


def rule(split_by="percent", parts=None, **extra):
    raw = {"name": "Test", "payee_contains": ["ROGERS"], "split_by": split_by,
           "part": parts or [{"category": "Phone", "value": 60},
                             {"category": "Internet", "value": "rest"}]}
    raw.update(extra)
    return a.normalize_rule(raw, 1)


# ---------------------------------------------------------------------------
class TestSplitMath(unittest.TestCase):
    def test_percent_with_rest(self):
        r = rule(parts=[{"category": "Phone", "value": 58.3333},
                        {"category": "Internet", "value": "rest"}])
        self.assertEqual(a.compute_split(r, 135.60), [79.10, 56.50])

    def test_percent_without_rest_last_part_absorbs_rounding(self):
        r = rule(parts=[{"category": "A", "value": 33.33}, {"category": "B", "value": 33.33},
                        {"category": "C", "value": 33.34}])
        amounts = a.compute_split(r, 100.00)
        self.assertEqual(amounts[:2], [33.33, 33.33])
        self.assertAlmostEqual(sum(amounts), 100.00, places=2)

    def test_parts_always_add_up_to_the_charge(self):
        r = rule(parts=[{"category": "A", "value": 33.3333}, {"category": "B", "value": 33.3333},
                        {"category": "C", "value": "rest"}])
        for cents in range(3, 50_000, 37):
            total = cents / 100
            amounts = a.compute_split(r, total)
            self.assertEqual(round(sum(amounts), 2), total, f"total {total}")
            self.assertTrue(all(x > 0 for x in amounts), f"total {total}: {amounts}")

    def test_rest_part_can_be_first(self):
        r = rule(parts=[{"category": "Phone", "value": "rest"},
                        {"category": "Internet", "value": 40}])
        self.assertEqual(a.compute_split(r, 100.00), [60.00, 40.00])

    def test_fixed_amount_with_rest(self):
        r = rule("amount", [{"category": "Household", "value": 40},
                            {"category": "Groceries", "value": "rest"}])
        self.assertEqual(a.compute_split(r, 135.60), [40.00, 95.60])

    def test_fixed_amounts_must_match_total(self):
        r = rule("amount", [{"category": "A", "value": 50}, {"category": "B", "value": 50}])
        self.assertEqual(a.compute_split(r, 100.00), [50.00, 50.00])
        with self.assertRaisesRegex(ValueError, "add up to 100.00, not 120.00"):
            a.compute_split(r, 120.00)

    def test_charge_too_small_for_fixed_amount(self):
        r = rule("amount", [{"category": "A", "value": 50}, {"category": "B", "value": "rest"}])
        with self.assertRaisesRegex(ValueError, "too small"):
            a.compute_split(r, 50.00)
        with self.assertRaisesRegex(ValueError, "too small"):
            a.compute_split(r, 20.00)


# ---------------------------------------------------------------------------
class TestMatching(unittest.TestCase):
    def tx(self, payee="ROGERS ******9211", account="Rogers"):
        return {"payee": payee, "_account": account}

    def test_all_words_must_appear_case_insensitive(self):
        r = rule(payee_contains=["rogers", "9211"])
        self.assertTrue(a.rule_matches(r, self.tx(), 135.60))
        self.assertFalse(a.rule_matches(r, self.tx("Rogers Centre"), 60.00))

    def test_account_filter(self):
        r = rule(account_contains="rogers")
        self.assertTrue(a.rule_matches(r, self.tx(), 135.60))
        self.assertFalse(a.rule_matches(r, self.tx(account="RBC Visa"), 135.60))

    def test_missing_account_name(self):
        t = {"payee": "ROGERS"}
        self.assertEqual(a.account_name(t), "")
        self.assertFalse(a.rule_matches(rule(account_contains="Rogers"), t, 10))
        self.assertTrue(a.rule_matches(rule(), t, 10))

    def test_amount_range(self):
        r = rule(min_amount=100, max_amount=200)
        self.assertTrue(a.rule_matches(r, self.tx(), 100))
        self.assertTrue(a.rule_matches(r, self.tx(), 200))
        self.assertFalse(a.rule_matches(r, self.tx(), 99.99))
        self.assertFalse(a.rule_matches(r, self.tx(), 200.01))

    def test_already_split_detection(self):
        self.assertTrue(a.already_split({"is_split_parent": True}))
        self.assertTrue(a.already_split({"split_parent_id": 5}))       # a part of a split
        self.assertTrue(a.already_split({"is_group_parent": True}))
        self.assertTrue(a.already_split({"group_parent_id": 7}))
        self.assertFalse(a.already_split({"split_parent_id": None, "is_split_parent": False,
                                          "is_group_parent": False, "group_parent_id": None}))
        self.assertTrue(a.is_split_part({"split_parent_id": 5}))
        self.assertFalse(a.is_split_part({"is_split_parent": True}))

    def test_clean_ignores_emoji_and_case(self):
        self.assertEqual(a.clean("📞 Phone"), "phone")
        self.assertEqual(a.clean("  Internet  and   Cable "), "internet and cable")


# ---------------------------------------------------------------------------
class TestConfig(unittest.TestCase):
    def bad(self, **raw):
        base = {"payee_contains": "X", "part": [{"category": "A", "value": 50},
                                                {"category": "B", "value": 50}]}
        base.update(raw)
        with self.assertRaises(a.ConfigError) as ctx:
            a.normalize_rule(base, 1)
        return str(ctx.exception)

    def test_validation_errors(self):
        self.assertIn("payee_contains", self.bad(payee_contains=""))
        self.assertIn("split_by", self.bad(split_by="ratio"))
        self.assertIn("at least two", self.bad(part=[{"category": "A", "value": "rest"}]))
        self.assertIn("only one part", self.bad(part=[{"category": "A", "value": "rest"},
                                                      {"category": "B", "value": "rest"}]))
        self.assertIn("add up to 90%", self.bad(part=[{"category": "A", "value": 60},
                                                      {"category": "B", "value": 30}]))
        self.assertIn("leaving nothing", self.bad(part=[{"category": "A", "value": 100},
                                                        {"category": "B", "value": "rest"}]))
        self.assertIn("positive number", self.bad(part=[{"category": "A", "value": -5},
                                                        {"category": "B", "value": "rest"}]))
        self.assertIn("min_amount", self.bad(min_amount="ten"))

    def test_write_then_load_round_trip(self):
        rules = [{"name": 'Rogers "bill"', "payee_contains": ["ROGERS", "9211"],
                  "account_contains": "Rogers", "min_amount": 100, "split_by": "percent",
                  "part": [{"category": "📞 Phone", "category_id": 11, "value": 58.3333},
                           {"category": "🌐 Internet", "category_id": 12, "value": "rest"}]}]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            a.write_config(path, 45, rules)
            lookback, loaded = a.load_config(path)
        self.assertEqual(lookback, 45)
        self.assertEqual(loaded[0]["name"], 'Rogers "bill"')
        self.assertEqual(loaded[0]["payee_contains"], ["ROGERS", "9211"])
        self.assertEqual(loaded[0]["part"][0]["category"], "📞 Phone")
        self.assertEqual(loaded[0]["part"][0]["value"], 58.3333)

    def test_example_config_is_valid(self):
        example = Path(__file__).resolve().parent.parent / "config.example.toml"
        lookback, rules = a.load_config(example)
        self.assertGreaterEqual(len(rules), 1)

    def test_resolve_categories(self):
        cats = [{"id": 1, "name": "Bills", "is_group": True},
                {"id": 10, "name": "Phone", "archived": True},             # archived: ignored
                {"id": 11, "name": "📞 Phone", "group_id": 1},
                {"id": 12, "name": "Telephone"},
                {"id": 13, "name": "Food"}, {"id": 14, "name": "food"}]
        r = rule(parts=[{"category": "Phone", "value": 50},
                        {"category": "Telephone", "value": "rest"}])
        a.resolve_categories([r], cats)
        self.assertEqual([p["_id"] for p in r["part"]], [11, 12])

        by_id = rule(parts=[{"category": "Renamed", "category_id": 13, "value": 50},
                            {"category": "Phone", "value": "rest"}])
        a.resolve_categories([by_id], cats)
        self.assertEqual(by_id["part"][0]["_id"], 13)

        with self.assertRaisesRegex(a.ConfigError, "not found"):
            a.resolve_categories([rule(parts=[{"category": "Nope", "value": 50},
                                              {"category": "Phone", "value": "rest"}])], cats)
        with self.assertRaisesRegex(a.ConfigError, "more than one"):
            a.resolve_categories([rule(parts=[{"category": "FOOD", "value": 50},
                                              {"category": "Phone", "value": "rest"}])], cats)
        with self.assertRaisesRegex(a.ConfigError, "not found"):      # groups can't be split into
            a.resolve_categories([rule(parts=[{"category": "Bills", "value": 50},
                                              {"category": "Phone", "value": "rest"}])], cats)


# ---------------------------------------------------------------------------
class FakeLunchMoney:
    def __init__(self, transactions):
        self.txs = transactions
        self.splits = []

    def categories(self):
        return [{"id": 11, "name": "📞 Phone"}, {"id": 12, "name": "🌐 Internet"},
                {"id": 13, "name": "Groceries"}, {"id": 14, "name": "Household"}]

    def transactions(self, start, end, include_split_parents=False):
        return [t for t in self.txs if start.isoformat() <= t["date"] <= end.isoformat()
                and (include_split_parents or not t.get("is_split_parent"))]

    def split(self, tx_id, parts):
        self.splits.append((tx_id, parts))
        return {"updated": True}


class TestRun(unittest.TestCase):
    CONFIG = '''
lookback_days = 30

[[rule]]
name = "Rogers"
payee_contains = ["ROGERS", "9211"]
split_by = "percent"
[[rule.part]]
category = "Phone"
value = 58.3333
[[rule.part]]
category = "Internet"
value = "rest"

[[rule]]
name = "Catch-all Rogers"
payee_contains = ["ROGERS"]
split_by = "amount"
[[rule.part]]
category = "Household"
value = 10
[[rule.part]]
category = "Groceries"
value = "rest"
'''

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.config = self.dir / "config.toml"
        self.config.write_text(self.CONFIG, encoding="utf-8")
        self.log_patch = mock.patch.object(a, "LOG_FILE", self.dir / "autosplit.log")
        self.log_patch.start()
        a.log.handlers.clear()

    def tearDown(self):
        for h in list(a.log.handlers):
            h.close()
        a.log.handlers.clear()
        self.log_patch.stop()
        self.tmp.cleanup()

    def day(self, ago):
        return (dt.date.today() - dt.timedelta(days=ago)).isoformat()

    def run_with(self, txs, apply, write_log=True):
        fake = FakeLunchMoney(txs)
        with mock.patch.object(a, "get_client", return_value=fake), redirect_stdout(io.StringIO()):
            code, matched = a.run_rules(self.config, apply=apply, write_log=write_log)
        return fake, code, matched

    def txs(self):
        return [
            {"id": 1, "date": self.day(11), "payee": "ROGERS ******9211", "amount": "135.6000"},
            {"id": 2, "date": self.day(10), "payee": "ROGERS ******9211", "amount": "79.1000",
             "split_parent_id": 20},                                   # part of an earlier split
            {"id": 7, "date": self.day(10), "payee": "ROGERS ******9211", "amount": "56.5000",
             "split_parent_id": 20},                                   # the other part
            {"id": 3, "date": self.day(9), "payee": "ROGERS ******9211", "amount": "-20.00"},  # refund
            {"id": 4, "date": self.day(8), "payee": "Rogers Centre", "amount": "60.00"},       # 2nd rule
            {"id": 5, "date": self.day(7), "payee": "Shell", "amount": "73.00"},               # no rule
            {"id": 6, "date": self.day(60), "payee": "ROGERS ******9211", "amount": "135.60"},  # too old
        ]

    def test_dry_run_changes_nothing(self):
        fake, code, matched = self.run_with(self.txs(), apply=False)
        self.assertEqual((code, matched), (0, 2))
        self.assertEqual(fake.splits, [])

    def test_apply_splits_matches_with_first_matching_rule(self):
        fake, code, matched = self.run_with(self.txs(), apply=True)
        self.assertEqual(code, 0)
        self.assertEqual(fake.splits, [
            (1, [{"amount": 79.10, "category_id": 11}, {"amount": 56.50, "category_id": 12}]),
            (4, [{"amount": 10.00, "category_id": 14}, {"amount": 50.00, "category_id": 13}]),
        ])

    def test_log_file_is_optional(self):
        self.run_with(self.txs(), apply=False, write_log=False)
        self.assertFalse((self.dir / "autosplit.log").exists())
        a.log.handlers.clear()
        self.run_with(self.txs(), apply=False, write_log=True)
        for h in a.log.handlers:
            h.flush()
        self.assertIn("ROGERS", (self.dir / "autosplit.log").read_text(encoding="utf-8"))

    def test_skips_amounts_with_more_than_two_decimals(self):
        txs = [{"id": 8, "date": self.day(3), "payee": "ROGERS ******9211", "amount": "135.6050"}]
        fake, code, matched = self.run_with(txs, apply=True)
        self.assertEqual((code, matched, fake.splits), (0, 0, []))

    def test_skipped_when_fixed_amount_too_large(self):
        txs = [{"id": 9, "date": self.day(3), "payee": "Rogers Centre", "amount": "5.00"}]
        fake, code, matched = self.run_with(txs, apply=True)
        self.assertEqual((code, matched, fake.splits), (0, 0, []))

    def test_missing_config_returns_error(self):
        self.config.unlink()
        fake, code, matched = self.run_with([], apply=False)
        self.assertEqual(code, 1)


# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status=200, body=None):
        import json
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body
        self.content = b"" if body is None else json.dumps(body).encode()
        self.text = self.content.decode()
        self.reason = "reason"

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class TestV2Client(unittest.TestCase):
    def client(self, handler):
        lm = a.LunchMoney("token")
        calls = []

        def request(method, url, timeout=None, **kwargs):
            calls.append((method, url, kwargs))
            return handler(method, url, kwargs)

        lm.session.request = request
        return lm, calls

    def test_uses_v2_base_and_bearer_token(self):
        lm, calls = self.client(lambda m, u, k: FakeResponse(200, {"name": "Philo"}))
        self.assertEqual(lm.me()["name"], "Philo")
        self.assertEqual(calls[0][1], "https://api.lunchmoney.dev/v2/me")
        self.assertEqual(lm.session.headers["Authorization"], "Bearer token")

    def test_transactions_follow_pagination_and_get_account_names(self):
        pages = {0: {"transactions": [{"id": 1, "plaid_account_id": 5},
                                      {"id": 2, "manual_account_id": 9}], "has_more": True},
                 2: {"transactions": [{"id": 3}], "has_more": False}}

        def handler(method, url, kwargs):
            if url.endswith("/transactions"):
                return FakeResponse(200, pages[kwargs["params"]["offset"]])
            if url.endswith("/plaid_accounts"):
                return FakeResponse(200, {"plaid_accounts": [
                    {"id": 5, "display_name": "", "institution_name": "Rogers", "name": "Mastercard"}]})
            if url.endswith("/manual_accounts"):
                return FakeResponse(200, {"manual_accounts": [
                    {"id": 9, "display_name": "Cash jar", "name": "Cash"}]})
            raise AssertionError(url)

        lm, calls = self.client(handler)
        txs = lm.transactions(dt.date(2026, 10, 1), dt.date(2026, 10, 31))
        self.assertEqual([t["id"] for t in txs], [1, 2, 3])
        self.assertEqual([t["_account"] for t in txs], ["Rogers Mastercard", "Cash jar", ""])
        tx_calls = [c for c in calls if c[1].endswith("/transactions")]
        self.assertEqual([c[2]["params"]["offset"] for c in tx_calls], [0, 2])
        self.assertEqual(tx_calls[0][2]["params"]["start_date"], "2026-10-01")
        self.assertNotIn("include_split_parents", tx_calls[0][2]["params"])

    def test_split_uses_the_split_endpoint(self):
        lm, calls = self.client(lambda m, u, k: FakeResponse(201, {"id": 42, "children": []}))
        lm.split(42, [{"amount": 79.10, "category_id": 11}, {"amount": 56.50, "category_id": 12}])
        method, url, kwargs = calls[0]
        self.assertEqual((method, url), ("POST", "https://api.lunchmoney.dev/v2/transactions/split/42"))
        self.assertEqual(kwargs["json"], {"child_transactions": [
            {"amount": 79.10, "category_id": 11}, {"amount": 56.50, "category_id": 12}]})

    def test_error_messages_are_readable(self):
        body = {"message": "Request Validation Failure",
                "errors": [{"errMsg": "amounts must sum to parent amount"}]}
        lm, _ = self.client(lambda m, u, k: FakeResponse(400, body))
        with self.assertRaisesRegex(a.ApiError, "Request Validation Failure: amounts must sum"):
            lm.split(1, [])

    def test_unauthorized_token(self):
        body = {"message": "Unauthorized", "errors": [{"errMsg": "Access token does not exist."}]}
        lm, _ = self.client(lambda m, u, k: FakeResponse(401, body))
        with self.assertRaises(a.ApiError) as ctx:
            lm.me()
        self.assertTrue(ctx.exception.auth)
        self.assertIn("Access token does not exist", str(ctx.exception))

    def test_empty_success_body(self):
        lm, _ = self.client(lambda m, u, k: FakeResponse(204, None))
        self.assertEqual(lm._call("DELETE", "/transactions/split/1"), {})


# ---------------------------------------------------------------------------
class TestToken(unittest.TestCase):
    def test_environment_variable_wins_over_file(self):
        with tempfile.TemporaryDirectory() as d:
            token_file = Path(d) / ".lunchmoney-token"
            token_file.write_text("from-file", encoding="utf-8")
            with mock.patch.object(a, "TOKEN_FILE", token_file):
                with mock.patch.dict(os.environ, {a.TOKEN_ENV: "from-env"}):
                    self.assertEqual(a.token_source()[0], "from-env")
                env = {k: v for k, v in os.environ.items() if k != a.TOKEN_ENV}
                with mock.patch.dict(os.environ, env, clear=True):
                    token, source = a.token_source()
                    self.assertEqual(token, "from-file")
                    self.assertIn("plain text", source)

    def test_no_token(self):
        env = {k: v for k, v in os.environ.items() if k != a.TOKEN_ENV}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(a, "TOKEN_FILE", Path(tempfile.gettempdir()) / "nope-token"):
            self.assertEqual(a.token_source(), (None, ""))
            with self.assertRaises(a.ConfigError):
                a.get_client()


if __name__ == "__main__":
    unittest.main()
