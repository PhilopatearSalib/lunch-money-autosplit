#!/usr/bin/env python3
"""
Lunch Money Auto-Split
======================
Automatically split recurring transactions (for example a phone + internet
bill) into several categories in Lunch Money.

Quick start:
    python autosplit.py setup         guided setup (connect, create rules, schedule)
    python autosplit.py run           dry run: shows what would be split
    python autosplit.py run --apply   splits the matching transactions
    python autosplit.py categories    lists your category names
    python autosplit.py schedule      runs it automatically every day
    python autosplit.py unschedule    removes the daily run

https://github.com/PhilopatearSalib/lunch-money-autosplit
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import logging
import os
import platform
import re
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit("Lunch Money Auto-Split needs Python 3.11 or newer. "
             "Download it from https://www.python.org/downloads/")

import tomllib

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    sys.exit("The 'requests' package is missing. Install it with:\n"
             "    python -m pip install requests")

__version__ = "2.0.0"

API_BASE = "https://api.lunchmoney.dev/v2"
MAX_PAGES = 50                                   # safety stop when paging through transactions
HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.toml"
TOKEN_FILE = HERE / ".lunchmoney-token"
LOG_FILE = HERE / "autosplit.log"
TOKEN_ENV = "LUNCHMONEY_TOKEN"
TASK_NAME = "Lunch Money Auto-Split"
DEFAULT_LOOKBACK_DAYS = 30
PREVIEW_DAYS = 120

log = logging.getLogger("autosplit")


class ApiError(Exception):
    def __init__(self, message: str, auth: bool = False):
        super().__init__(message)
        self.auth = auth


class ConfigError(Exception):
    pass


# --------------------------------------------------------------------------
# Lunch Money API
# --------------------------------------------------------------------------
class LunchMoney:
    def __init__(self, token: str):
        retry = Retry(
            total=4,
            backoff_factor=5,                      # waits 5s, 10s, 20s, 40s
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods={"GET"},               # never auto-repeat a split
        )
        self.session = requests.Session()
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.headers["Authorization"] = f"Bearer {token}"
        self._account_names: dict | None = None

    def _call(self, method: str, path: str, **kwargs):
        try:
            r = self.session.request(method, API_BASE + path, timeout=(10, 60), **kwargs)
        except requests.exceptions.RequestException as e:
            raise ApiError(f"Could not reach Lunch Money ({method} {path}). "
                           f"Check your internet connection or try again later.\n"
                           f"  Details: {e}") from e
        if r.status_code == 401:
            raise ApiError(f"Lunch Money rejected the access token ({error_text(r)}). "
                           "Create a new token and run:  python autosplit.py setup", auth=True)
        if not r.ok:
            raise ApiError(f"Lunch Money returned HTTP {r.status_code} for "
                           f"{method} {path}: {error_text(r)}")
        if r.status_code == 204 or not r.content:
            return {}
        try:
            data = r.json()
        except ValueError as e:
            raise ApiError(f"Unexpected reply from Lunch Money for {method} {path}.") from e
        if isinstance(data, dict) and data.get("error"):
            raise ApiError(f"Lunch Money error for {method} {path}: {data['error']}")
        return data

    def me(self) -> dict:
        return self._call("GET", "/me")

    def categories(self) -> list[dict]:
        """All categories and category groups in one flat list."""
        return self._call("GET", "/categories", params={"format": "flattened"})["categories"]

    def account_names(self) -> dict:
        """Maps ("plaid" | "manual", account id) to a readable account name.

        v2 transactions carry only account IDs, so the names are looked up once.
        """
        if self._account_names is None:
            names = {}
            for kind, path, key in (("plaid", "/plaid_accounts", "plaid_accounts"),
                                    ("manual", "/manual_accounts", "manual_accounts")):
                for acc in self._call("GET", path).get(key, []):
                    label = acc.get("display_name") or " ".join(
                        x for x in (acc.get("institution_name"), acc.get("name")) if x)
                    names[(kind, acc["id"])] = label
            self._account_names = names
        return self._account_names

    def transactions(self, start: dt.date, end: dt.date,
                     include_split_parents: bool = False) -> list[dict]:
        """Every non-pending transaction in the date range, following pagination.

        Each transaction gets an extra "_account" key with its account name.
        """
        params = {"start_date": start.isoformat(), "end_date": end.isoformat()}
        if include_split_parents:
            params["include_split_parents"] = "true"
        out: list[dict] = []
        for _ in range(MAX_PAGES):
            data = self._call("GET", "/transactions", params={**params, "offset": len(out)})
            batch = data.get("transactions", [])
            out.extend(batch)
            if not data.get("has_more") or not batch:
                break
        else:
            raise ApiError(f"Too many transactions to page through ({len(out)}+). "
                           "Lower lookback_days in config.toml.")
        names = self.account_names()
        for t in out:
            if t.get("plaid_account_id"):
                t["_account"] = names.get(("plaid", t["plaid_account_id"]), "")
            elif t.get("manual_account_id"):
                t["_account"] = names.get(("manual", t["manual_account_id"]), "")
            else:
                t["_account"] = ""
        return out

    def split(self, transaction_id: int, parts: list[dict]) -> dict:
        return self._call("POST", f"/transactions/split/{transaction_id}",
                          json={"child_transactions": parts})


def error_text(r) -> str:
    """Readable message from a v2 error body: {"message": ..., "errors": [{"errMsg": ...}]}."""
    try:
        data = r.json()
    except ValueError:
        return r.text[:300] or r.reason or "no details"
    if isinstance(data, dict):
        details = [e.get("errMsg") for e in data.get("errors") or []
                   if isinstance(e, dict) and e.get("errMsg")]
        text = data.get("message") or data.get("error") or ""
        if details:
            text = f"{text}: {'; '.join(details)}" if text else "; ".join(details)
        if text:
            return str(text)
    return r.text[:300]


def token_source() -> tuple[str | None, str]:
    """Returns (token, where it came from). The environment variable wins."""
    token = os.environ.get(TOKEN_ENV, "").strip()
    if token:
        return token, f"the {TOKEN_ENV} environment variable"
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            return token, f"the token file {TOKEN_FILE.name} (plain text)"
    return None, ""


def get_token() -> str | None:
    return token_source()[0]


def save_token(token: str) -> None:
    """Optional convenience: store the token in a plain-text file next to the script."""
    TOKEN_FILE.write_text(token, encoding="utf-8")
    try:
        os.chmod(TOKEN_FILE, 0o600)                 # owner-only on Mac/Linux
    except OSError:
        pass


def set_windows_env(token: str) -> bool:
    """Store the token as a user environment variable on Windows (used by scheduled runs)."""
    r = subprocess.run(["setx", TOKEN_ENV, token], capture_output=True, text=True)
    return r.returncode == 0


def env_instructions(system: str | None = None) -> str:
    system = system or platform.system()
    if system == "Windows":
        return (f"In PowerShell, run (with your token in the quotes):\n"
                f"    setx {TOKEN_ENV} \"your-token\"\n"
                "Then open a new terminal window.")
    profile = "~/.zshrc" if system == "Darwin" else "~/.bashrc"
    return (f"Add this line to {profile} (with your token in the quotes), then open a new terminal:\n"
            f"    export {TOKEN_ENV}=\"your-token\"\n"
            "Scheduled runs with cron don't read that file; 'python autosplit.py schedule'\n"
            "shows how to give cron the token.")


def get_client() -> LunchMoney:
    token = get_token()
    if not token:
        raise ConfigError("No Lunch Money access token found. Run:  python autosplit.py setup")
    return LunchMoney(token)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def clean(text: str) -> str:
    """Lowercase and drop emoji/symbols so '📞 Phone' matches 'Phone'."""
    kept = "".join(ch for ch in str(text) if ch.isalnum() or ch.isspace())
    return " ".join(kept.lower().split())


def account_name(t: dict) -> str:
    """Account name added by LunchMoney.transactions()."""
    return str(t.get("_account") or "")


def is_split_part(t: dict) -> bool:
    """One of the pieces a transaction was split into."""
    return bool(t.get("split_parent_id"))


def already_split(t: dict) -> bool:
    """True for split parents, split parts, transaction groups and grouped transactions.

    In v2, GET /transactions returns the parts of a split instead of the original,
    so the parts (split_parent_id set) are what usually needs skipping.
    """
    return bool(t.get("is_split_parent") or is_split_part(t)
                or t.get("is_group_parent") or t.get("group_parent_id"))


def usable_categories(categories: list[dict]) -> list[dict]:
    """Categories a charge can be split into: no groups, nothing archived."""
    return [c for c in categories if not c.get("is_group") and not c.get("archived")]


def is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def normalize_rule(raw: dict, n: int) -> dict:
    where = f"Rule #{n}" + (f" ({raw['name']})" if raw.get("name") else "")

    payee = raw.get("payee_contains")
    if isinstance(payee, str):
        payee = [payee]
    if not payee or not all(isinstance(w, str) and w.strip() for w in payee):
        raise ConfigError(f"{where}: 'payee_contains' must be a word or a list of words.")

    split_by = raw.get("split_by", "percent")
    if split_by not in ("percent", "amount"):
        raise ConfigError(f"{where}: 'split_by' must be \"percent\" or \"amount\".")

    for key in ("min_amount", "max_amount"):
        if key in raw and not is_number(raw[key]):
            raise ConfigError(f"{where}: '{key}' must be a number.")

    parts = raw.get("part", [])
    if len(parts) < 2:
        raise ConfigError(f"{where}: needs at least two [[rule.part]] entries.")

    rest_count, total = 0, 0.0
    norm_parts = []
    for i, p in enumerate(parts, 1):
        if not p.get("category") and p.get("category_id") is None:
            raise ConfigError(f"{where}, part {i}: needs a 'category'.")
        v = p.get("value")
        if v == "rest":
            rest_count += 1
        elif is_number(v) and v > 0:
            total += v
        else:
            raise ConfigError(f"{where}, part {i}: 'value' must be a positive number or \"rest\".")
        norm_parts.append({"category": p.get("category", ""),
                           "category_id": p.get("category_id"), "value": v})

    if rest_count > 1:
        raise ConfigError(f"{where}: only one part can be \"rest\".")
    if split_by == "percent":
        if rest_count and total >= 100:
            raise ConfigError(f"{where}: percentages add up to {total:g}%, "
                              "leaving nothing for the \"rest\" part.")
        if not rest_count and abs(total - 100) > 0.01:
            raise ConfigError(f"{where}: percentages add up to {total:g}% "
                              "(they must total 100, or make one part \"rest\").")

    return {
        "name": raw.get("name") or ", ".join(payee),
        "payee_contains": [w.strip() for w in payee],
        "account_contains": (raw.get("account_contains") or "").strip(),
        "min_amount": raw.get("min_amount"),
        "max_amount": raw.get("max_amount"),
        "split_by": split_by,
        "part": norm_parts,
    }


def read_config_file(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"No config found at {path}.\nRun:  python autosplit.py setup")
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path.name} has a formatting mistake: {e}") from e


def load_config(path: Path) -> tuple[int, list[dict]]:
    cfg = read_config_file(path)
    lookback = cfg.get("lookback_days", DEFAULT_LOOKBACK_DAYS)
    if not isinstance(lookback, int) or not 1 <= lookback <= 365:
        raise ConfigError("'lookback_days' must be a whole number from 1 to 365.")
    raw_rules = cfg.get("rule", [])
    if not raw_rules:
        raise ConfigError(f"{path.name} has no [[rule]] entries. Run:  python autosplit.py setup")
    return lookback, [normalize_rule(r, i) for i, r in enumerate(raw_rules, 1)]


def toml_value(v) -> str:
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(round(v, 4))
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    raise TypeError(f"Cannot write {v!r} to TOML")


CONFIG_HEADER = """\
# Lunch Money Auto-Split configuration
# Re-run  python autosplit.py setup  to add rules, or edit this file by hand.
# See config.example.toml for every option.
"""


def write_config(path: Path, lookback: int, rules: list[dict]) -> None:
    lines = [CONFIG_HEADER, f"lookback_days = {lookback}", ""]
    for r in rules:
        lines.append("[[rule]]")
        for key in ("name", "payee_contains", "account_contains",
                    "min_amount", "max_amount", "split_by"):
            if r.get(key) not in (None, "", []):
                lines.append(f"{key} = {toml_value(r[key])}")
        for p in r["part"]:
            lines += ["", "[[rule.part]]", f"category = {toml_value(p['category'])}"]
            if p.get("category_id") is not None:
                lines.append(f"category_id = {toml_value(p['category_id'])}")
            lines.append(f"value = {toml_value(p['value'])}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def resolve_categories(rules: list[dict], categories: list[dict]) -> None:
    usable = usable_categories(categories)
    by_id = {c["id"]: c for c in usable}
    for r in rules:
        for p in r["part"]:
            if p.get("category_id") in by_id:
                c = by_id[p["category_id"]]
            else:
                hits = [c for c in usable if clean(c["name"]) == clean(p["category"])]
                if not hits:
                    label = p["category"] or f"id {p['category_id']}"
                    raise ConfigError(f"Rule '{r['name']}': category \"{label}\" was not found.\n"
                                      "Run  python autosplit.py categories  to see the exact names.")
                if len(hits) > 1:
                    raise ConfigError(f"Rule '{r['name']}': \"{p['category']}\" matches more than "
                                      "one category. Add the right category_id from "
                                      "python autosplit.py categories")
                c = hits[0]
            p["_id"], p["_label"] = c["id"], c["name"]


# --------------------------------------------------------------------------
# Matching and splitting
# --------------------------------------------------------------------------
def rule_matches(rule: dict, t: dict, amount: float) -> bool:
    payee = (t.get("payee") or "").lower()
    if not all(w.lower() in payee for w in rule["payee_contains"]):
        return False
    if rule["account_contains"] and rule["account_contains"].lower() not in account_name(t).lower():
        return False
    if rule["min_amount"] is not None and amount < rule["min_amount"]:
        return False
    if rule["max_amount"] is not None and amount > rule["max_amount"]:
        return False
    return True


def compute_split(rule: dict, total: float) -> list[float]:
    amounts: list[float | None] = []
    for p in rule["part"]:
        v = p["value"]
        if v == "rest":
            amounts.append(None)
        elif rule["split_by"] == "percent":
            amounts.append(round(total * v / 100, 2))
        else:
            amounts.append(round(float(v), 2))

    if None in amounts:
        absorb = amounts.index(None)
    else:
        if rule["split_by"] == "amount" and abs(sum(amounts) - total) > 0.005:
            raise ValueError(f"the fixed amounts add up to {sum(amounts):.2f}, not {total:.2f}")
        absorb = len(amounts) - 1          # last part absorbs rounding cents
        amounts[absorb] = None

    remainder = round(total - sum(a for a in amounts if a is not None), 2)
    if remainder <= 0:
        raise ValueError(f"the charge ({total:.2f}) is too small to cover the fixed amounts")
    amounts[absorb] = remainder
    return [float(a) for a in amounts]


def setup_logging(write_file: bool = True) -> None:
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    if sys.stdout is not None:                      # None under pythonw.exe
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(console)
    if not write_file:
        return
    try:
        fh = RotatingFileHandler(LOG_FILE, maxBytes=200_000, backupCount=1, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s"))
        log.addHandler(fh)
    except OSError:
        pass


def run_rules(config: Path, apply: bool, write_log: bool = True) -> tuple[int, int]:
    """Returns (exit_code, number_of_transactions_that_would_be/were_split).

    The log file records payees, amounts and categories; write_log=False turns it off.
    """
    setup_logging(write_log)
    log.info(f"Lunch Money Auto-Split {__version__} - {'APPLY' if apply else 'dry run'}")
    try:
        lookback, rules = load_config(config)
        lm = get_client()
        resolve_categories(rules, lm.categories())
        end = dt.date.today()
        txs = lm.transactions(end - dt.timedelta(days=lookback), end)
    except (ConfigError, ApiError) as e:
        log.error(str(e))
        return 1, 0

    matched, failed = 0, 0
    for t in sorted(txs, key=lambda t: t.get("date", "")):
        if already_split(t):
            continue
        try:
            raw_amount = float(t.get("amount", 0))
        except (TypeError, ValueError):
            continue
        amount = round(raw_amount, 2)
        if amount <= 0:                             # refunds and income
            continue
        rule = next((r for r in rules if rule_matches(r, t, amount)), None)
        if rule is None:
            continue

        head = f"{t.get('date')}  {t.get('payee')}  {amount:.2f}  [{rule['name']}]"
        if abs(raw_amount - amount) > 1e-6:
            log.warning(f"{head} skipped: amount {t.get('amount')} has more than 2 decimal places")
            continue
        try:
            amounts = compute_split(rule, amount)
        except ValueError as e:
            log.warning(f"{head} skipped: {e}")
            continue

        matched += 1
        log.info(f"{head} -> " + " + ".join(
            f"{p['_label']} {a:.2f}" for p, a in zip(rule["part"], amounts)))
        if apply:
            try:
                lm.split(t["id"], [{"amount": a, "category_id": p["_id"]}
                                   for p, a in zip(rule["part"], amounts)])
                log.info("    split done")
            except ApiError as e:
                failed += 1
                log.error(f"    split failed: {e}")

    if not matched:
        log.info(f"Nothing to split: no unsplit transactions matched your rules "
                 f"in the last {lookback} days.")
    elif not apply:
        log.info("Dry run only - nothing was changed. Run again with --apply to split them.")
    return (1 if failed else 0), matched


# --------------------------------------------------------------------------
# Interactive input
# --------------------------------------------------------------------------
def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    while True:
        s = input(f"{prompt}{suffix}: ").strip()
        if not s and default not in (None, ""):
            return str(default)
        if s:
            return s
        print("  Please type something.")


def yes(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        s = input(f"{prompt} [{hint}]: ").strip().lower()
        if not s:
            return default
        if s in ("y", "yes"):
            return True
        if s in ("n", "no"):
            return False
        print("  Please answer y or n.")


def ask_choice(prompt: str, options: list[str], default: str) -> str:
    while True:
        s = ask(prompt, default)
        if s in options:
            return s
        print(f"  Please type one of: {', '.join(options)}")


def ask_int(prompt: str, default: int, lo: int, hi: int) -> int:
    while True:
        s = ask(prompt, str(default))
        if s.isdigit() and lo <= int(s) <= hi:
            return int(s)
        print(f"  Please type a whole number from {lo} to {hi}.")


def ask_amount(prompt: str, below: float | None = None) -> float:
    while True:
        s = ask(prompt).replace("$", "").replace("%", "").replace(",", "").strip()
        try:
            v = float(s)
        except ValueError:
            print("  Please type a number, like 25 or 79.10")
            continue
        if v <= 0:
            print("  Please type a number above 0.")
        elif below is not None and v >= below:
            print(f"  Please type a number below {below:g}.")
        else:
            return v


def print_categories(cats: list[dict], groups: dict) -> None:
    for i, c in enumerate(cats, 1):
        g = groups.get(c.get("group_id"))
        print(f"  {i:>3}. {c['name']}" + (f"   (in {g})" if g else ""))


def pick_category(cats: list[dict], prompt: str, groups: dict) -> dict:
    while True:
        s = input(f"{prompt} - type part of its name (or ? for the list): ").strip()
        if not s:
            continue
        if s == "?":
            print_categories(cats, groups)
            continue
        if s.isdigit() and 1 <= int(s) <= len(cats):
            c = cats[int(s) - 1]
            print(f"     -> {c['name']}")
            return c
        hits = [c for c in cats if clean(s) and clean(s) in clean(c["name"])]
        if len(hits) == 1:
            print(f"     -> {hits[0]['name']}")
            return hits[0]
        if not hits:
            print("  No category matches that. Try again.")
            continue
        exact = [h for h in hits if clean(h["name"]) == clean(s)]
        if len(exact) == 1:
            print(f"     -> {exact[0]['name']}")
            return exact[0]
        print("  Several categories match:")
        for h in hits[:15]:
            g = groups.get(h.get("group_id"))
            print(f"  {cats.index(h) + 1:>3}. {h['name']}" + (f"   (in {g})" if g else ""))
        print("  Type the number of the one you want.")


def preview_matches(lm: LunchMoney, words: list[str]) -> list[dict]:
    end = dt.date.today()
    try:
        txs = lm.transactions(end - dt.timedelta(days=PREVIEW_DAYS), end,
                              include_split_parents=True)
    except ApiError as e:
        print(f"  (Couldn't load recent transactions to preview: {e})")
        return []
    # Show each original charge once: split originals are included, their parts are not.
    hits = [t for t in txs if not is_split_part(t)
            and all(w.lower() in (t.get("payee") or "").lower() for w in words)]
    return sorted(hits, key=lambda t: t.get("date", ""), reverse=True)


# --------------------------------------------------------------------------
# Guided setup
# --------------------------------------------------------------------------
TOKEN_HELP = """
To connect, this tool needs a Lunch Money access token:
  1. Open https://my.lunchmoney.app and go to Settings > Developers.
  2. Type a name such as "autosplit", pick "I'm developing a personal project",
     and request a new access token.
  3. Copy the token. Lunch Money shows it only once.
Keep the token private: anyone who has it can see and change your budget.
"""


def who(me: dict) -> str:
    return me.get("name") or me.get("email") or "you"


def store_token(token: str) -> None:
    """Ask where to keep the token. The environment variable is recommended."""
    os.environ[TOKEN_ENV] = token                   # use it for the rest of this setup
    windows = platform.system() == "Windows"
    print("\nWhere should the token be kept on this computer?")
    print(f"  1) Environment variable {TOKEN_ENV} (recommended)"
          + ("" if windows else " - I'll show you the line to add"))
    print(f"  2) Token file in this folder ({TOKEN_FILE.name}) - convenient, but plain text")
    print("  3) Don't keep it - I'll set it up myself")
    choice = ask_choice("Choose 1, 2 or 3", ["1", "2", "3"], "1")
    if choice == "1" and windows:
        if set_windows_env(token):
            print(f"  Saved as {TOKEN_ENV}. Scheduled runs and new terminal windows will use it.")
            return
        print("  Couldn't set it automatically.")
    if choice == "2":
        save_token(token)
        print(f"  Saved to {TOKEN_FILE}.")
        print("  This file is plain text: anyone who can read it can use your budget.")
        print("  Keep it private and never share or commit it.")
        return
    print("  " + env_instructions().replace("\n", "\n  "))


def ensure_token() -> str | None:
    token, source = token_source()
    if token:
        try:
            me = LunchMoney(token).me()
            print(f"Connected to Lunch Money as {who(me)} using {source}.")
            if (TOKEN_FILE.exists() and not os.environ.get(TOKEN_ENV)
                    and platform.system() == "Windows"
                    and yes(f"Move the token from the plain-text file into the {TOKEN_ENV} "
                            "environment variable (recommended)?", True)):
                if set_windows_env(token):
                    os.environ[TOKEN_ENV] = token
                    TOKEN_FILE.unlink()
                    print(f"  Done. Moved to {TOKEN_ENV} and deleted {TOKEN_FILE.name}.")
                else:
                    print("  Couldn't set the variable; keeping the token file.")
            return token
        except ApiError as e:
            print(f"The saved token didn't work: {e}")
            if not e.auth:
                return None

    print(TOKEN_HELP)
    while True:
        token = getpass.getpass("Paste your token and press Enter "
                                "(nothing appears while you paste - that's normal): ").strip()
        if not token:
            continue
        try:
            me = LunchMoney(token).me()
        except ApiError as e:
            print(f"  {e}")
            if not e.auth:
                return None
            continue
        print(f"Connected as {who(me)}.")
        break

    store_token(token)
    return token


def ask_rule(lm: LunchMoney, cats: list[dict], groups: dict) -> dict | None:
    print("\n=== New rule ===")
    print("Which charge should be split? Type text that appears in its payee name.")
    print("Use commas if several words must all appear (e.g.  ROGERS, 9211).")
    words = [w.strip() for w in ask("Payee text").split(",") if w.strip()]
    if not words:
        return None

    matches = preview_matches(lm, words)
    account = ""
    if matches:
        print(f"\nTransactions from the last {PREVIEW_DAYS} days that match:")
        for t in matches[:8]:
            note = "  (already split)" if already_split(t) else ""
            print(f"  {t.get('date')}  {(t.get('payee') or '')[:32]:<32}  "
                  f"{float(t.get('amount', 0)):>10.2f}  {account_name(t)}{note}")
        accounts = sorted({account_name(t) for t in matches if account_name(t)})
        if len(accounts) > 1:
            print(f"\nThese come from {len(accounts)} different accounts.")
            account = input("Limit the rule to one account? Type part of its name, "
                            "or press Enter for all: ").strip()
        if not yes("\nIs this the right charge?", True):
            return None
    else:
        print(f"  No transactions from the last {PREVIEW_DAYS} days match that text.")
        if not yes("  Create the rule anyway?", False):
            return None

    name = ask("Short name for this rule", f"{words[0].title()} split")
    n = ask_int("Into how many categories should each charge be split?", 2, 2, 10)

    print("\nHow should each charge be divided?")
    print("  1) Copy a past bill - enter how one bill splits, and the same")
    print("     proportions are used every month (best when the total varies)")
    print("  2) Percentages (e.g. 60% / 40%)")
    print("  3) Fixed amounts - the last category gets whatever is left")
    mode = ask_choice("Choose 1, 2 or 3", ["1", "2", "3"], "1")

    print("\nPick each category by typing part of its name (e.g. phone).")
    print("Type ? to see the full list of your categories.\n")

    parts, remaining = [], 100.0
    for i in range(n):
        last = i == n - 1
        c = pick_category(cats, f"Category {i + 1} of {n}", groups)
        if mode == "1":
            value = ask_amount(f"     Amount for {c['name']} on that bill")
        elif last:
            value = "rest"
            print("     -> gets " + (f"the remaining {remaining:g}%" if mode == "2"
                                     else "whatever is left of the charge"))
        elif mode == "2":
            value = ask_amount(f"     Percentage for {c['name']}", below=remaining)
            remaining -= value
        else:
            value = ask_amount(f"     Fixed amount for {c['name']}")
        parts.append({"category": c["name"], "category_id": c["id"], "value": value})

    if mode == "1":
        total = sum(p["value"] for p in parts)
        for p in parts[:-1]:
            p["value"] = round(p["value"] / total * 100, 4)
        parts[-1]["value"] = "rest"
        shown = [f"{p['category']} {p['value']:.2f}%" for p in parts[:-1]]
        print("  Proportions: " + ", ".join(shown) + f", {parts[-1]['category']} the rest")

    return {"name": name, "payee_contains": words, "account_contains": account,
            "split_by": "amount" if mode == "3" else "percent", "part": parts}


def cmd_setup(args) -> int:
    config = Path(args.config)
    print(f"\nLunch Money Auto-Split {__version__} - guided setup\n")
    token = ensure_token()
    if not token:
        return 1
    lm = LunchMoney(token)
    try:
        all_cats = lm.categories()
    except ApiError as e:
        print(e)
        return 1
    groups = {c["id"]: c["name"] for c in all_cats if c.get("is_group")}
    cats = sorted(usable_categories(all_cats), key=lambda c: clean(c["name"]))
    if len(cats) < 2:
        print("You need at least two categories in Lunch Money to split into.")
        return 1

    lookback, rules = DEFAULT_LOOKBACK_DAYS, []
    if config.exists():
        print(f"\nYou already have a config: {config}")
        if ask_choice("1) Add rules to it   2) Start over", ["1", "2"], "1") == "1":
            try:
                existing = read_config_file(config)
                lookback = existing.get("lookback_days", DEFAULT_LOOKBACK_DAYS)
                rules = list(existing.get("rule", []))
            except ConfigError as e:
                print(f"  {e}\n  Starting over instead.")

    added = 0
    while True:
        rule = ask_rule(lm, cats, groups)
        if rule:
            rules.append(rule)
            added += 1
        if not yes("\nAdd another rule?", False):
            break
    if not added:
        print("No rules added. Nothing was changed.")
        return 0

    write_config(config, lookback, rules)
    try:
        load_config(config)
    except ConfigError as e:
        print(f"The saved config has a problem: {e}")
        return 1
    print(f"\nSaved your rules to {config}")

    print("\n--- Dry run (nothing is changed) ---")
    code, matched = run_rules(config, apply=False)
    if code == 0 and matched and yes("\nSplit these transactions now?", True):
        run_rules(config, apply=True)

    if yes("\nRun automatically every day?", True):
        when = ask("What time each day? (24-hour HH:MM)", "10:00")
        cmd_schedule(argparse.Namespace(config=str(config), time=when, no_log=False))

    print("\nAll set. Useful commands:")
    print("  python autosplit.py run           see what would be split")
    print("  python autosplit.py run --apply   split now")
    print("  python autosplit.py setup         add more rules")
    print(f"Results of automatic runs are written to {LOG_FILE.name} "
          "(it records payees, amounts and categories).")
    return 0


# --------------------------------------------------------------------------
# Other commands
# --------------------------------------------------------------------------
def cmd_run(args) -> int:
    code, _ = run_rules(Path(args.config), apply=args.apply, write_log=not args.no_log)
    return code


def cmd_categories(args) -> int:
    try:
        cats = get_client().categories()
    except (ConfigError, ApiError) as e:
        print(e)
        return 1
    groups = {c["id"]: c["name"] for c in cats if c.get("is_group")}
    rows = sorted(usable_categories(cats),
                  key=lambda c: (clean(groups.get(c.get("group_id"), "")), clean(c["name"])))
    print(f"{'ID':>10}  CATEGORY")
    for c in rows:
        g = groups.get(c.get("group_id"))
        print(f"{c['id']:>10}  {c['name']}" + (f"   (in {g})" if g else ""))
    return 0


def _powershell(command: str) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                           "-Command", command], capture_output=True, text=True)


def cmd_schedule(args) -> int:
    config = Path(args.config).resolve()
    if not config.exists():
        print("Create your rules first:  python autosplit.py setup")
        return 1
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", args.time):
        print("Time must look like 10:00 or 18:30.")
        return 1
    if os.environ.get(TOKEN_ENV) is None and not TOKEN_FILE.exists():
        print(f"Warning: no saved token. Automatic runs need {TOKEN_FILE.name} "
              f"or the {TOKEN_ENV} variable.")

    script = Path(__file__).resolve()
    hour, minute = (int(x) for x in args.time.split(":"))

    if platform.system() == "Windows":
        exe = Path(sys.executable)
        if exe.with_name("pythonw.exe").exists():
            exe = exe.with_name("pythonw.exe")       # runs without a console window
        q = lambda s: str(s).replace("'", "''")
        extra = " --no-log" if getattr(args, "no_log", False) else ""
        argument = f'"{script}" run --apply --config "{config}"{extra}'
        r = _powershell(
            f"$a = New-ScheduledTaskAction -Execute '{q(exe)}' -Argument '{q(argument)}' "
            f"-WorkingDirectory '{q(HERE)}'; "
            f"$t = New-ScheduledTaskTrigger -Daily -At '{hour:02d}:{minute:02d}'; "
            "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable "
            "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries; "
            f"Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $a -Trigger $t "
            "-Settings $s -Force | Out-Null")
        if r.returncode != 0:
            print("Couldn't create the scheduled task:\n" + (r.stderr or r.stdout).strip())
            print("\nYou can create it by hand in Task Scheduler (see README).")
            return 1
        print(f"Scheduled \"{TASK_NAME}\" every day at {hour:02d}:{minute:02d}.")
        print("If the computer is off at that time, it runs as soon as it's back on.")
        if not getattr(args, "no_log", False):
            print(f"Results are written to {LOG_FILE} (contains transaction details).")
        return 0

    extra = " --no-log" if getattr(args, "no_log", False) else ""
    line = (f'{minute} {hour} * * * cd "{HERE}" && "{sys.executable}" "{script}" '
            f'run --apply --config "{config}"{extra} >/dev/null 2>&1')
    print("To run it every day, open your crontab with:  crontab -e")
    if os.environ.get(TOKEN_ENV) and not TOKEN_FILE.exists():
        print(f"cron doesn't read your shell profile, so first add this line at the top")
        print(f"(with your token in the quotes; the crontab is readable only by you):\n")
        print(f'{TOKEN_ENV}="your-token"\n')
        print("Then add this schedule line, save and close:\n")
    else:
        print("Then add this line, save and close:\n")
    print(line)
    if not getattr(args, "no_log", False):
        print(f"\nResults are written to {LOG_FILE} (contains transaction details).")
    return 0


def cmd_unschedule(args) -> int:
    if platform.system() == "Windows":
        r = _powershell(f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false")
        if r.returncode != 0:
            print(f"No scheduled task named \"{TASK_NAME}\" was found.")
            return 1
        print(f"Removed the scheduled task \"{TASK_NAME}\".")
        return 0
    print("Open  crontab -e  and delete the line that contains autosplit.py")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (OSError, ValueError):
                pass

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help=f"path to the config file (default: {DEFAULT_CONFIG.name})")

    parser = argparse.ArgumentParser(
        prog="autosplit.py",
        description="Automatically split recurring Lunch Money transactions into several categories.",
        epilog="New here? Start with:  python autosplit.py setup")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.add_parser("setup", parents=[common],
                   help="guided setup: connect, create rules, schedule")
    run_p = sub.add_parser("run", parents=[common],
                           help="check recent transactions (dry run unless --apply)")
    run_p.add_argument("--apply", action="store_true",
                       help="actually split the matching transactions")
    run_p.add_argument("--no-log", action="store_true",
                       help=f"don't write {LOG_FILE.name} (it records payees, amounts and categories)")
    sub.add_parser("categories", parents=[common], help="list your Lunch Money categories")
    sched = sub.add_parser("schedule", parents=[common], help="run automatically every day")
    sched.add_argument("--time", default="10:00", help="time of day, HH:MM (default 10:00)")
    sched.add_argument("--no-log", action="store_true",
                       help=f"scheduled runs don't write {LOG_FILE.name}")
    sub.add_parser("unschedule", parents=[common], help="stop the automatic daily run")

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    handlers = {"setup": cmd_setup, "run": cmd_run, "categories": cmd_categories,
                "schedule": cmd_schedule, "unschedule": cmd_unschedule}
    try:
        return handlers[args.command](args) or 0
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
