from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_daily.models import RepoSnapshot, RunState
from ai_daily.state import StateStore

INTENT_TIME = datetime(2026, 8, 24, 6, 0, tzinfo=timezone(timedelta(hours=8)))


def test_state_round_trip_is_atomic(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.save_run_state(RunState(sent_dates={"2026-08-24": "message-id"}))
    assert store.load_run_state().sent_dates["2026-08-24"] == "message-id"
    assert not (tmp_path / "state.json.tmp").exists()


def test_missing_state_returns_default(tmp_path: Path) -> None:
    assert StateStore(tmp_path).load_run_state() == RunState()


def test_legacy_state_without_delivery_intents_remains_loadable(tmp_path: Path) -> None:
    """Would catch the delivery schema rejecting state written by the previous release."""
    (tmp_path / "state.json").write_text(
        '{"last_success_at":null,"sent_dates":{"2026-08-23":"safe-id"}}',
        encoding="utf-8",
    )

    state = StateStore(tmp_path).load_run_state()

    assert state.sent_dates == {"2026-08-23": "safe-id"}
    assert state.delivery_intents == {}


@pytest.mark.parametrize(
    ("attempt_id", "created_at"),
    [
        ("reader@example.test", INTENT_TIME),
        ("contains whitespace", INTENT_TIME),
        ("gh-123", datetime(2026, 8, 24, 6, 0)),  # noqa: DTZ001 - invalid by design
    ],
)
def test_delivery_intent_rejects_unsafe_attempts_and_naive_timestamps(
    attempt_id: str, created_at: datetime
) -> None:
    """Would catch secret-like ownership values or timezone ambiguity entering durable state."""
    from ai_daily.models import DeliveryIntent

    with pytest.raises(ValidationError):
        DeliveryIntent(attempt_id=attempt_id, created_at=created_at, status="reserved")


def test_run_state_rejects_non_iso_delivery_intent_date_key() -> None:
    """Would catch malformed durable keys bypassing the Beijing-date ownership contract."""
    from ai_daily.models import DeliveryIntent

    intent = DeliveryIntent(
        attempt_id="gh-100-1", created_at=INTENT_TIME, status="reserved"
    )
    with pytest.raises(ValidationError):
        RunState(delivery_intents={"2026-8-24": intent})


def test_reservation_is_atomic_idempotent_and_owned(tmp_path: Path) -> None:
    """Would catch a second workflow silently replacing a committed send reservation."""
    from ai_daily.state import DeliveryIntentConflictError

    store = StateStore(tmp_path)
    assert store.reserve_delivery("2026-08-24", "gh-100-1", INTENT_TIME) == "reserved"
    assert store.reserve_delivery("2026-08-24", "gh-100-1", INTENT_TIME) == "reserved"
    intent = store.load_run_state().delivery_intents["2026-08-24"]
    assert intent.attempt_id == "gh-100-1"
    assert intent.status == "reserved"
    assert intent.created_at == INTENT_TIME
    assert not (tmp_path / "state.json.tmp").exists()

    with pytest.raises(DeliveryIntentConflictError):
        store.reserve_delivery("2026-08-24", "gh-200-1", INTENT_TIME)

    assert store.load_run_state().delivery_intents["2026-08-24"] == intent


def test_known_sent_date_is_not_reserved_without_intentional_force(tmp_path: Path) -> None:
    """Would catch the scheduled compensation path reserving a known completed send."""
    store = StateStore(tmp_path)
    store.save_run_state(RunState(sent_dates={"2026-08-24": "accepted-safe-id"}))

    assert store.reserve_delivery("2026-08-24", "gh-100-1", INTENT_TIME) == "already_sent"
    assert store.load_run_state().delivery_intents == {}

    assert (
        store.reserve_delivery("2026-08-24", "gh-100-1", INTENT_TIME, force=True)
        == "reserved"
    )
    assert store.load_run_state().delivery_intents["2026-08-24"].attempt_id == "gh-100-1"


def test_force_never_overrides_an_ambiguous_intent(tmp_path: Path) -> None:
    """Would catch force converting unknown remote acceptance into an automatic duplicate."""
    from ai_daily.models import DeliveryIntent
    from ai_daily.state import DeliveryIntentConflictError

    store = StateStore(tmp_path)
    store.save_run_state(
        RunState(
            delivery_intents={
                "2026-08-24": DeliveryIntent(
                    attempt_id="gh-100-1", created_at=INTENT_TIME, status="ambiguous"
                )
            }
        )
    )

    with pytest.raises(DeliveryIntentConflictError):
        store.reserve_delivery("2026-08-24", "gh-200-1", INTENT_TIME, force=True)


def test_operator_resolution_requires_mailbox_decision_and_safe_fingerprint(tmp_path: Path) -> None:
    """Would catch ambiguity being cleared without an explicit retry/sent operator decision."""
    from ai_daily.state import DeliveryResolutionError

    store = StateStore(tmp_path)
    store.reserve_delivery("2026-08-24", "gh-100-1", INTENT_TIME)
    state = store.load_run_state()
    state.delivery_intents["2026-08-24"].status = "ambiguous"
    store.save_run_state(state)

    with pytest.raises(DeliveryResolutionError):
        store.resolve_delivery("2026-08-24", "sent", "reader@example.test")
    assert "2026-08-24" in store.load_run_state().delivery_intents

    assert store.resolve_delivery("2026-08-24", "retry") == "retry"
    assert store.load_run_state().delivery_intents == {}

    store.reserve_delivery("2026-08-24", "gh-200-1", INTENT_TIME)
    assert store.resolve_delivery("2026-08-24", "sent", "sha256-abcdef123456") == "sent"
    resolved = store.load_run_state()
    assert resolved.sent_dates == {"2026-08-24": "sha256-abcdef123456"}
    assert resolved.delivery_intents == {}


def test_release_only_clears_matching_reserved_work(tmp_path: Path) -> None:
    """Would catch automated failure cleanup erasing ambiguous or foreign ownership."""
    from ai_daily.models import DeliveryIntent
    from ai_daily.state import DeliveryIntentConflictError

    store = StateStore(tmp_path)
    store.reserve_delivery("2026-08-24", "gh-100-1", INTENT_TIME)
    with pytest.raises(DeliveryIntentConflictError):
        store.release_delivery("2026-08-24", "gh-200-1")
    assert store.release_delivery("2026-08-24", "gh-100-1") == "released"
    assert store.release_delivery("2026-08-24", "gh-100-1") == "absent"

    store.save_run_state(
        RunState(
            delivery_intents={
                "2026-08-24": DeliveryIntent(
                    attempt_id="gh-100-1", created_at=INTENT_TIME, status="ambiguous"
                )
            }
        )
    )
    with pytest.raises(DeliveryIntentConflictError):
        store.release_delivery("2026-08-24", "gh-100-1")


def test_snapshot_round_trip_preserves_timezone_aware_datetimes(tmp_path: Path) -> None:
    collected_at = datetime(2026, 8, 24, 9, 30, tzinfo=timezone(timedelta(hours=8)))
    snapshot = RepoSnapshot(
        repository="owner/repo",
        stars=10,
        forks=2,
        updated_at=collected_at,
        collected_at=collected_at,
    )
    store = StateStore(tmp_path)
    path = store.save_snapshot(date(2026, 8, 24), [snapshot])
    assert path == tmp_path / "github" / "2026-08-24.json"
    loaded = store.load_snapshot(date(2026, 8, 24))
    assert loaded == [snapshot]
    assert loaded[0].collected_at.tzinfo is not None


def test_snapshot_save_atomically_replaces_temporary_file(
    tmp_path: Path, monkeypatch
) -> None:
    replacements: list[tuple[Path, Path]] = []
    original_replace = Path.replace

    def record_replace(self: Path, target: Path) -> Path:
        replacements.append((self, target))
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", record_replace)
    store = StateStore(tmp_path)
    store.save_snapshot(date(2026, 8, 24), [])

    assert replacements == [
        (
            tmp_path / "github" / "2026-08-24.json.tmp",
            tmp_path / "github" / "2026-08-24.json",
        )
    ]
    assert not (tmp_path / "github" / "2026-08-24.json.tmp").exists()


def test_missing_snapshot_returns_empty_list(tmp_path: Path) -> None:
    assert StateStore(tmp_path).load_snapshot(date(2026, 8, 24)) == []


def test_prune_snapshots_keeps_35_days_and_deletes_36_days(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.snapshot_dir.mkdir(parents=True)
    old = date(2026, 7, 19)
    recent = date(2026, 7, 20)
    store.save_snapshot(old, [])
    store.save_snapshot(recent, [])

    removed = store.prune_snapshots(date(2026, 8, 24), retention_days=35)

    assert removed == [tmp_path / "github" / "2026-07-19.json"]
    assert not (tmp_path / "github" / "2026-07-19.json").exists()
    assert (tmp_path / "github" / "2026-07-20.json").exists()
