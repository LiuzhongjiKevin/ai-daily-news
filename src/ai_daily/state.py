import errno
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import BinaryIO, Literal

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


class DeliveryOperation:
    """State mutations permitted while one process owns the delivery-operation lock."""

    def __init__(self, store: "StateStore", local_date: str, attempt_id: str) -> None:
        self._store = store
        self._local_date = local_date
        self._attempt_id = attempt_id
        self._active = False

    def _set_active(self, active: bool) -> None:
        self._active = active

    def _require_active(self) -> None:
        if not self._active:
            raise DeliveryStateError("Delivery operation is inactive")

    def mark_ambiguous(self) -> RunState:
        self._require_active()
        return self._store._mark_delivery_ambiguous_locked(
            self._local_date, self._attempt_id
        )

    def complete(self, message_id: str, accepted_at: datetime) -> RunState:
        self._require_active()
        return self._store._complete_delivery_locked(
            self._local_date,
            self._attempt_id,
            _validated_message_id(message_id),
            accepted_at,
        )


class StateStore:
    def __init__(
        self,
        data_dir: Path,
        *,
        delivery_lock_timeout: float = 10.0,
        delivery_lock_poll_interval: float = 0.05,
    ) -> None:
        if delivery_lock_timeout <= 0 or delivery_lock_poll_interval <= 0:
            raise ValueError("delivery lock timings must be positive")
        self.data_dir = data_dir
        self.snapshot_dir = data_dir / "github"
        self.delivery_lock_timeout = delivery_lock_timeout
        self.delivery_lock_poll_interval = delivery_lock_poll_interval

    def load_run_state(self) -> RunState:
        path = self.data_dir / "state.json"
        return RunState.model_validate_json(path.read_text("utf-8")) if path.exists() else RunState()

    def save_run_state(self, state: RunState) -> None:
        with self._delivery_transaction():
            self._save_run_state_unlocked(state)

    def _save_run_state_unlocked(self, state: RunState) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        target = self.data_dir / "state.json"
        temporary = self.data_dir / "state.json.tmp"
        temporary.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(target)

    @contextmanager
    def _delivery_transaction(self) -> Iterator[None]:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.data_dir / "state.json.lock"
        try:
            handle = lock_path.open("a+b")
        except OSError as error:
            raise DeliveryStateError("Delivery state lock is unavailable") from error
        with handle:
            try:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
            except OSError as error:
                raise DeliveryStateError("Delivery state lock is unavailable") from error

            deadline = time.monotonic() + self.delivery_lock_timeout
            while True:
                try:
                    _lock_file(handle)
                    break
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise DeliveryStateError(
                            "Delivery state lock is unavailable"
                        ) from None
                    if time.monotonic() >= deadline:
                        raise DeliveryStateError("Delivery state is busy") from None
                    time.sleep(self.delivery_lock_poll_interval)
            try:
                yield
            finally:
                try:
                    _unlock_file(handle)
                except OSError:
                    # Closing the handle also releases the OS lock; never mask the body error.
                    pass

    @contextmanager
    def delivery_operation(
        self, local_date: str, attempt_id: str
    ) -> Iterator[DeliveryOperation]:
        """Hold the cross-process lock across ownership check, mail, and final state."""
        day = _validated_date(local_date)
        safe_attempt = _validated_intent(
            attempt_id, datetime.now(UTC)
        ).attempt_id
        operation = DeliveryOperation(self, day, safe_attempt)
        with self._delivery_transaction():
            operation._set_active(True)
            try:
                yield operation
            finally:
                operation._set_active(False)

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
        with self._delivery_transaction():
            state = self.load_run_state()
            existing = state.delivery_intents.get(day)
            if existing is not None:
                if existing.attempt_id == candidate.attempt_id and existing.status == "reserved":
                    return "reserved"
                raise DeliveryIntentConflictError("Delivery is blocked by unresolved intent")
            if day in state.sent_dates and not force:
                return "already_sent"
            state.delivery_intents[day] = candidate
            self._save_run_state_unlocked(state)
            return "reserved"

    def release_delivery(
        self, local_date: str, attempt_id: str
    ) -> Literal["released", "absent"]:
        day = _validated_date(local_date)
        safe_attempt = _validated_intent(attempt_id, datetime.now(UTC)).attempt_id
        with self._delivery_transaction():
            state = self.load_run_state()
            existing = state.delivery_intents.get(day)
            if existing is None:
                return "absent"
            if existing.attempt_id != safe_attempt or existing.status != "reserved":
                raise DeliveryIntentConflictError(
                    "Delivery intent cannot be released automatically"
                )
            del state.delivery_intents[day]
            self._save_run_state_unlocked(state)
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
        with self._delivery_transaction():
            state = self.load_run_state()
            if day not in state.delivery_intents:
                raise DeliveryResolutionError("No unresolved delivery intent exists")
            if resolution == "retry":
                if message_id is not None:
                    raise DeliveryResolutionError(
                        "Retry resolution does not accept a message ID"
                    )
                del state.delivery_intents[day]
                self._save_run_state_unlocked(state)
                return "retry"
            safe_message_id = _validated_message_id(message_id)
            state.sent_dates[day] = safe_message_id
            del state.delivery_intents[day]
            self._save_run_state_unlocked(state)
            return "sent"

    def mark_delivery_ambiguous(self, local_date: str, attempt_id: str) -> RunState:
        with self.delivery_operation(local_date, attempt_id) as operation:
            return operation.mark_ambiguous()

    def _mark_delivery_ambiguous_locked(
        self, local_date: str, attempt_id: str
    ) -> RunState:
        state = self.load_run_state()
        existing = state.delivery_intents.get(local_date)
        if (
            existing is None
            or existing.attempt_id != attempt_id
            or existing.status != "reserved"
        ):
            raise DeliveryIntentConflictError(
                "Delivery ownership changed before the mail boundary"
            )
        existing.status = "ambiguous"
        self._save_run_state_unlocked(state)
        return state

    def complete_delivery(
        self,
        local_date: str,
        attempt_id: str,
        message_id: str,
        accepted_at: datetime,
    ) -> RunState:
        with self.delivery_operation(local_date, attempt_id) as operation:
            return operation.complete(message_id, accepted_at)

    def _complete_delivery_locked(
        self,
        local_date: str,
        attempt_id: str,
        message_id: str,
        accepted_at: datetime,
    ) -> RunState:
        state = self.load_run_state()
        existing = state.delivery_intents.get(local_date)
        if (
            existing is None
            or existing.attempt_id != attempt_id
            or existing.status != "ambiguous"
        ):
            raise DeliveryIntentConflictError(
                "Delivery ownership changed before the accepted-state commit"
            )
        state.sent_dates[local_date] = message_id
        state.last_success_at = accepted_at
        del state.delivery_intents[local_date]
        self._save_run_state_unlocked(state)
        return state

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


def _validated_message_id(message_id: str | None) -> str:
    if message_id is None or _SAFE_MESSAGE_ID.fullmatch(message_id) is None:
        raise DeliveryResolutionError("Sent resolution requires a safe local message ID")
    return message_id


def _lock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
