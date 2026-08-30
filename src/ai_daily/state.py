import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter, ValidationError

from ai_daily.models import DeliveryIntent, RepoSnapshot, RunState

_SAFE_MESSAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,255}$")


class DeliveryStateError(RuntimeError):
    """Base class for safe delivery-state failures that never include supplied values."""


class DeliveryInputError(DeliveryStateError):
    """A date, attempt ID, resolution, or message fingerprint is unsafe or malformed."""


class DeliveryIntentConflictError(DeliveryStateError):
    """Existing unresolved work prevents changing delivery ownership automatically."""


class DeliveryResolutionError(DeliveryStateError):
    """An operator resolution is incomplete or cannot apply to current state."""


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

    def reserve_delivery(
        self,
        local_date: str,
        attempt_id: str,
        created_at: datetime,
        *,
        force: bool = False,
    ) -> Literal["reserved", "already_sent"]:
        day = _validated_date(local_date)
        candidate = _validated_intent(attempt_id, created_at)
        state = self.load_run_state()
        existing = state.delivery_intents.get(day)
        if existing is not None:
            if existing.attempt_id == candidate.attempt_id and existing.status == "reserved":
                return "reserved"
            raise DeliveryIntentConflictError("Delivery is blocked by unresolved intent")
        if day in state.sent_dates and not force:
            return "already_sent"
        state.delivery_intents[day] = candidate
        self.save_run_state(state)
        return "reserved"

    def release_delivery(
        self, local_date: str, attempt_id: str
    ) -> Literal["released", "absent"]:
        day = _validated_date(local_date)
        safe_attempt = _validated_intent(attempt_id, datetime.now(UTC)).attempt_id
        state = self.load_run_state()
        existing = state.delivery_intents.get(day)
        if existing is None:
            return "absent"
        if existing.attempt_id != safe_attempt or existing.status != "reserved":
            raise DeliveryIntentConflictError("Delivery intent cannot be released automatically")
        del state.delivery_intents[day]
        self.save_run_state(state)
        return "released"

    def resolve_delivery(
        self,
        local_date: str,
        resolution: Literal["retry", "sent"] | str,
        message_id: str | None = None,
    ) -> Literal["retry", "sent"]:
        day = _validated_date(local_date)
        if resolution not in {"retry", "sent"}:
            raise DeliveryInputError("Delivery resolution is invalid")
        state = self.load_run_state()
        if day not in state.delivery_intents:
            raise DeliveryResolutionError("No unresolved delivery intent exists")
        if resolution == "retry":
            if message_id is not None:
                raise DeliveryResolutionError("Retry resolution does not accept a message ID")
            del state.delivery_intents[day]
            self.save_run_state(state)
            return "retry"
        if message_id is None or _SAFE_MESSAGE_ID.fullmatch(message_id) is None:
            raise DeliveryResolutionError("Sent resolution requires a safe local message ID")
        state.sent_dates[day] = message_id
        del state.delivery_intents[day]
        self.save_run_state(state)
        return "sent"

    def save_snapshot(self, day: date, snapshots: list[RepoSnapshot]) -> Path:
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        path = self.snapshot_dir / f"{day.isoformat()}.json"
        temporary = self.snapshot_dir / f"{day.isoformat()}.json.tmp"
        temporary.write_text(
            TypeAdapter(list[RepoSnapshot]).dump_json(snapshots, indent=2).decode(),
            "utf-8",
        )
        temporary.replace(path)
        return path

    def load_snapshot(self, day: date) -> list[RepoSnapshot]:
        path = self.snapshot_dir / f"{day.isoformat()}.json"
        if not path.exists():
            return []
        return TypeAdapter(list[RepoSnapshot]).validate_json(path.read_text("utf-8"))

    def recent_snapshot_days(
        self,
        today: date,
        *,
        limit: int = 7,
        retention_days: int = 35,
    ) -> list[date]:
        """Return up to ``limit`` prior snapshot dates, newest first, within retention."""
        if limit < 1 or retention_days < 1:
            raise ValueError("snapshot limits must be positive")
        if not self.snapshot_dir.exists():
            return []
        cutoff = today - timedelta(days=retention_days)
        dates: list[date] = []
        for path in self.snapshot_dir.glob("????-??-??.json"):
            try:
                day = date.fromisoformat(path.stem)
            except ValueError:
                continue
            if cutoff <= day < today:
                dates.append(day)
        return sorted(dates, reverse=True)[:limit]

    def prune_snapshots(self, today: date, retention_days: int) -> list[Path]:
        cutoff = today - timedelta(days=retention_days)
        removed = []
        for path in self.snapshot_dir.glob("????-??-??.json"):
            if date.fromisoformat(path.stem) < cutoff:
                path.unlink()
                removed.append(path)
        return removed


def _validated_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise DeliveryInputError("Delivery date is invalid") from error
    if parsed.isoformat() != value:
        raise DeliveryInputError("Delivery date is invalid")
    return value


def _validated_intent(attempt_id: str, created_at: datetime) -> DeliveryIntent:
    try:
        return DeliveryIntent(attempt_id=attempt_id, created_at=created_at, status="reserved")
    except ValidationError as error:
        raise DeliveryInputError("Delivery intent input is invalid") from error
