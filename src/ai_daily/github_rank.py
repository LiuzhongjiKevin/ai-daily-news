from datetime import date, timedelta

from ai_daily.models import RankedRepo, RepoSnapshot
from ai_daily.state import StateStore


def rank_repositories(
    current: list[RepoSnapshot],
    baseline: list[RepoSnapshot],
    top_n: int = 10,
    *,
    has_full_baseline: bool = True,
    fallback: list[RepoSnapshot] | None = None,
) -> list[RankedRepo]:
    """Rank eligible repositories by rolling gain, then total stars and their name."""
    if top_n < 1:
        raise ValueError("top_n must be positive")
    before = {row.repository.casefold(): row for row in baseline}
    fallback_before: dict[str, RepoSnapshot] = {}
    for row in fallback or []:
        fallback_before.setdefault(row.repository.casefold(), row)
    ranked: list[RankedRepo] = []
    for row in current:
        if row.archived or row.is_fork:
            continue
        exact_old = before.get(row.repository.casefold())
        old = exact_old or fallback_before.get(row.repository.casefold())
        gain = max(0, row.stars - old.stars) if old else 0
        ranked.append(
            RankedRepo(
                snapshot=row,
                stars_gained=gain,
                is_trial=not has_full_baseline or exact_old is None,
            )
        )
    ranked.sort(
        key=lambda value: (
            -value.stars_gained,
            -value.snapshot.stars,
            value.snapshot.repository.casefold(),
            value.snapshot.repository,
        )
    )
    return ranked[:top_n]


def load_recent_snapshots(
    store: StateStore, today: date, window_days: int = 7
) -> tuple[list[RepoSnapshot], bool, list[RepoSnapshot]]:
    """Load the exact baseline, or the oldest saved snapshot available during the first week."""
    if window_days < 1:
        raise ValueError("window_days must be positive")
    history: list[RepoSnapshot] = []
    recent_days = sorted(store.recent_snapshot_days(today, limit=window_days))
    for day in recent_days:
        history.extend(store.load_snapshot(day))
    exact_day = today - timedelta(days=window_days)
    exact = store.load_snapshot(exact_day)
    if exact:
        return exact, True, history
    if recent_days:
        return store.load_snapshot(recent_days[0]), False, history
    return [], False, history


def historical_candidate_names(store: StateStore, today: date, window_days: int = 7) -> list[str]:
    """Return names from the most recent saved snapshots for candidate retention."""
    _, _, history = load_recent_snapshots(store, today, window_days)
    return [snapshot.repository for snapshot in history]


def _retained_snapshots(store: StateStore, today: date, retention_days: int) -> list[RepoSnapshot]:
    """Load the bounded retained history oldest first for repository-specific trial baselines."""
    snapshots: list[RepoSnapshot] = []
    for day in sorted(
        store.recent_snapshot_days(
            today,
            limit=retention_days,
            retention_days=retention_days,
        )
    ):
        snapshots.extend(store.load_snapshot(day))
    return snapshots


def rank_and_store_repositories(
    store: StateStore,
    today: date,
    current: list[RepoSnapshot],
    *,
    top_n: int = 10,
    window_days: int = 7,
    retention_days: int = 35,
) -> list[RankedRepo]:
    """Rank before atomically saving the current snapshot, then prune retained history."""
    exact_baseline = store.load_snapshot(today - timedelta(days=window_days))
    ranked = rank_repositories(
        current,
        exact_baseline,
        top_n,
        has_full_baseline=bool(exact_baseline),
        fallback=_retained_snapshots(store, today, retention_days),
    )
    store.save_snapshot(today, current)
    store.prune_snapshots(today, retention_days)
    return ranked
