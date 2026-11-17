import copy
from datetime import UTC, datetime

import pytest

from ai_daily.state import StateStore

NOW = datetime(2026, 11, 17, 4, tzinfo=UTC)


def ledger(month="2026-11", baseline=0):
    return {"version": 1, "months": {month: {
        "month": month, "limit": 300000, "baseline": baseline,
        "reserved": baseline, "reservations": {}}}}


def install(store, value):
    state = store.load_run_state()
    state.translation_budget = value
    store.save_run_state(state)


def test_manifest_counts_punctuation_spaces_and_non_bmp():
    from ai_daily.world_translation_budget import make_manifest, units
    assert units("A 😀!") == 5
    manifest = make_manifest(["A 😀!", "A 😀!", "中文", "https://a.com", "Next"])
    assert manifest.texts == ("A 😀!", "Next")
    assert manifest.characters == 9
    assert len(manifest.digest) == 64


def test_reservation_persists_without_source_and_cannot_be_reused(tmp_path):
    from ai_daily.world_translation_budget import BudgetUnavailable, make_manifest, reserve
    store = StateStore(tmp_path)
    install(store, ledger())
    grant = reserve(store, make_manifest(["Trade 5%"]), NOW, "123", "1")
    saved = store.load_run_state().translation_budget
    assert saved["months"]["2026-11"]["reserved"] == 8
    record = saved["months"]["2026-11"]["reservations"]["123"]
    assert record["characters"] == 8
    assert record["run_attempt"] == "1"
    assert "Trade" not in (tmp_path / "state.json").read_text()
    assert not grant.consume("Trade 5%", NOW)
    grant.confirm("a" * 40)
    assert grant.consume("Trade 5%", NOW)
    assert not grant.consume("Trade 5%", NOW)
    with pytest.raises(BudgetUnavailable):
        reserve(store, make_manifest(["Other"]), NOW, "123", "2")


@pytest.mark.parametrize("value", [None, {}, {"version": 1, "months": {}},
                                     ledger(baseline=300000)])
def test_missing_unknown_or_exhausted_budget_blocks_reservation(tmp_path, value):
    from ai_daily.world_translation_budget import BudgetUnavailable, make_manifest, reserve
    store = StateStore(tmp_path)
    install(store, value)
    with pytest.raises(BudgetUnavailable):
        reserve(store, make_manifest(["Trade"]), NOW, "123", "1")


def test_corrupt_totals_and_rollback_rejected(tmp_path):
    from ai_daily.world_translation_budget import (
        BudgetUnavailable,
        make_manifest,
        reserve,
        validate_history,
    )
    store = StateStore(tmp_path)
    before = ledger()
    install(store, before)
    reserve(store, make_manifest(["Trade"]), NOW, "123", "1")
    after = store.load_run_state().translation_budget
    validate_history([before, after])
    with pytest.raises(BudgetUnavailable):
        validate_history([before, after, before])
    damaged = copy.deepcopy(after)
    damaged["months"]["2026-11"]["reserved"] = 0
    with pytest.raises(BudgetUnavailable):
        validate_history([damaged])


def test_month_boundary_blocks_old_permit_and_does_not_initialize_month(tmp_path):
    from ai_daily.world_translation_budget import BudgetUnavailable, make_manifest, reserve
    store = StateStore(tmp_path)
    install(store, ledger())
    grant = reserve(store, make_manifest(["Trade"]), NOW, "123", "1")
    grant.confirm("a" * 40)
    assert not grant.consume("Trade", datetime(2026, 11, 30, 15, 59, 30, tzinfo=UTC))
    assert not grant.consume("Trade", datetime(2026, 11, 30, 16, tzinfo=UTC))
    with pytest.raises(BudgetUnavailable):
        reserve(store, make_manifest(["Trade"]), datetime(2026, 12, 1, tzinfo=UTC), "124", "1")


def test_manifest_run_limits_and_mutated_manifest(tmp_path):
    from ai_daily.world_translation_budget import BudgetUnavailable, make_manifest, reserve
    store = StateStore(tmp_path)
    install(store, ledger())
    manifest = make_manifest([f"English {i} " + "x" * 900 for i in range(100)])
    assert manifest.characters <= 12000
    assert len(manifest.texts) <= 40
    grant = reserve(store, manifest, NOW, "123", "1")
    assert not grant.consume("not in manifest", NOW)
    with pytest.raises(BudgetUnavailable):
        reserve(store, make_manifest(["Trade"]), NOW.replace(tzinfo=None), "124", "1")
