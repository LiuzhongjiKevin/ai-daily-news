from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ai_daily.models import RepoSnapshot, RunState
from ai_daily.state import StateStore


def test_state_round_trip_is_atomic(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.save_run_state(RunState(sent_dates={"2026-08-24": "message-id"}))
    assert store.load_run_state().sent_dates["2026-08-24"] == "message-id"
    assert not (tmp_path / "state.json.tmp").exists()


def test_missing_state_returns_default(tmp_path: Path) -> None:
    assert StateStore(tmp_path).load_run_state() == RunState()


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
