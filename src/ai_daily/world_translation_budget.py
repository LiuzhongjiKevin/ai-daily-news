"""Fail-closed, append-only reservations for the world edition's translation quota."""
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import uuid4

from ai_daily.pipeline import _timezone


class BudgetUnavailable(RuntimeError):
    """Safe diagnostic: no supplied values in the exception."""


def units(text):
    # UTF-16 counts non-BMP as two; includes whitespace and punctuation.
    return len(text.encode("utf-16-le")) // 2


def month_at(now):
    if now.tzinfo is None or now.utcoffset() is None:
        raise BudgetUnavailable("budget_clock")
    return now.astimezone(_timezone("Asia/Shanghai")).strftime("%Y-%m")


def safe_month(now, month):
    return month_at(now) == month and month_at(now + timedelta(seconds=65)) == month


@dataclass(frozen=True)
class Manifest:
    texts: tuple[str, ...]
    characters: int
    digest: str


def make_manifest(texts):
    chosen, count = [], 0
    for text in texts:
        if (not text or len(text) > 1000 or text in chosen or "://" in text
                or re.search(r"[\u3400-\u9fff]", text) or not re.search(r"[A-Za-z]", text)):
            continue
        size = units(text)
        if len(chosen) >= 40 or count + size > 12000:
            continue
        chosen.append(text)
        count += size
    digest = hashlib.sha256(json.dumps(chosen, ensure_ascii=True,
                                      separators=(",", ":")).encode()).hexdigest()
    return Manifest(tuple(chosen), count, digest)


def validate(value):
    try:
        if (not isinstance(value, dict) or set(value) != {"version", "months"}
                or type(value["version"]) is not int or value["version"] != 1
                or not isinstance(value["months"], dict) or not value["months"]):
            raise ValueError
        run_ids = set()
        for month, row in value["months"].items():
            if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
                raise ValueError
            if set(row) != {"month", "limit", "baseline", "reserved", "reservations"}:
                raise ValueError
            if row["month"] != month or any(
                    type(row[k]) is not int for k in ("limit", "baseline", "reserved")):
                raise ValueError
            if not 0 <= row["baseline"] <= row["reserved"] <= row["limit"] <= 300000:
                raise ValueError
            if not isinstance(row["reservations"], dict):
                raise TypeError
            total = row["baseline"]
            for run_id, record in row["reservations"].items():
                if run_id in run_ids or not re.fullmatch(r"[1-9][0-9]{0,19}", run_id):
                    raise ValueError
                run_ids.add(run_id)
                if set(record) != {"id", "run_id", "run_attempt", "manifest_hash",
                                   "characters", "created_at"}:
                    raise ValueError
                if (record["run_id"] != run_id or
                        not re.fullmatch(r"[1-9][0-9]{0,9}", record["run_attempt"]) or
                        not re.fullmatch(r"[0-9a-f]{32}", record["id"]) or
                        not re.fullmatch(r"[0-9a-f]{64}", record["manifest_hash"]) or
                        type(record["characters"]) is not int or
                        not 0 < record["characters"] <= 12000 or
                        month_at(datetime.fromisoformat(record["created_at"])) != month):
                    raise ValueError
                total += record["characters"]
            if total != row["reserved"]:
                raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        raise BudgetUnavailable("budget_invalid") from None
    return value


def validate_history(snapshots):
    if not snapshots:
        raise BudgetUnavailable("budget_history_missing")
    previous = None
    for raw in snapshots:
        current = validate(raw)
        if previous is not None:
            for month, old in previous["months"].items():
                new = current["months"].get(month)
                if (new is None or new["baseline"] != old["baseline"]
                        or new["limit"] != old["limit"] or new["reserved"] < old["reserved"]
                        or any(new["reservations"].get(k) != v
                               for k, v in old["reservations"].items())):
                    raise BudgetUnavailable("budget_rollback")
        previous = current


@dataclass
class Grant:
    manifest: Manifest
    month: str
    _confirmed: bool = False
    _used: set[str] = field(default_factory=set)

    def confirm(self, commit):
        if self._confirmed or not isinstance(commit, str) or not re.fullmatch(
                r"[0-9a-f]{40}", commit):
            raise BudgetUnavailable("budget_checkpoint_unconfirmed")
        self._confirmed = True

    def consume(self, text, now):
        if (not self._confirmed or text not in self.manifest.texts or text in self._used
                or not safe_month(now, self.month)):
            return False
        self._used.add(text)  # no refund, including timeout/HTTP/business error
        return True


def reserve(store, manifest, now, run_id, run_attempt):
    month = month_at(now)
    if (not safe_month(now, month) or not re.fullmatch(r"[1-9][0-9]{0,19}", run_id)
            or not re.fullmatch(r"[1-9][0-9]{0,9}", run_attempt)
            or not manifest.characters or make_manifest(manifest.texts) != manifest):
        raise BudgetUnavailable("budget_request_invalid")
    with store._delivery_transaction():
        state = store.load_run_state()
        budget = validate(state.translation_budget)
        row = budget["months"].get(month)
        if row is None:
            raise BudgetUnavailable("budget_month_unapproved")
        if any(run_id in entry["reservations"] for entry in budget["months"].values()):
            raise BudgetUnavailable("budget_run_already_reserved")
        if row["reserved"] + manifest.characters > row["limit"]:
            raise BudgetUnavailable("budget_exhausted")
        row["reservations"][run_id] = {
            "id": uuid4().hex, "run_id": run_id, "run_attempt": run_attempt,
            "manifest_hash": manifest.digest, "characters": manifest.characters,
            "created_at": now.isoformat(),
        }
        row["reserved"] += manifest.characters
        state.translation_budget = budget
        store._save_run_state_unlocked(state)
    return Grant(manifest, month)
