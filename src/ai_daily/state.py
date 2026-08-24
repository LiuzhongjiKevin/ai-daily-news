from datetime import date, timedelta
from pathlib import Path

from pydantic import TypeAdapter

from ai_daily.models import RepoSnapshot, RunState


class StateStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.snapshot_dir = data_dir / "github"

    def load_run_state(self) -> RunState:
        path = self.data_dir / "state.json"
        return RunState.model_validate_json(path.read_text("utf-8")) if path.exists() else RunState()

    def save_run_state(self, state: RunState) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        target = self.data_dir / "state.json"
        temporary = self.data_dir / "state.json.tmp"
        temporary.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(target)

    def save_snapshot(self, day: date, snapshots: list[RepoSnapshot]) -> Path:
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        path = self.snapshot_dir / f"{day.isoformat()}.json"
        path.write_text(
            TypeAdapter(list[RepoSnapshot]).dump_json(snapshots, indent=2).decode(),
            "utf-8",
        )
        return path

    def load_snapshot(self, day: date) -> list[RepoSnapshot]:
        path = self.snapshot_dir / f"{day.isoformat()}.json"
        if not path.exists():
            return []
        return TypeAdapter(list[RepoSnapshot]).validate_json(path.read_text("utf-8"))

    def prune_snapshots(self, today: date, retention_days: int) -> list[Path]:
        cutoff = today - timedelta(days=retention_days)
        removed = []
        for path in self.snapshot_dir.glob("????-??-??.json"):
            if date.fromisoformat(path.stem) < cutoff:
                path.unlink()
                removed.append(path)
        return removed
