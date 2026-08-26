from datetime import date, timedelta

from ai_daily.models import RankedRepo, RepoSnapshot
from ai_daily.state import StateStore


def rank_repositories(
    current: list[RepoSnapshot],
    baseline: list[RepoSnapshot],
    top_n: int = 10,
    *,
    has_full_baseline: bool = True,
) -> list[RankedRepo]:
    """Rank eligible repositories by rolling gain, then total stars and their name."""
    if top_n < 1:
        raise ValueError("top_n must be positive")
    before = {row.repository.casefold(): row for row in baseline}
    ranked: list[RankedRepo] = []
    for row in current:
        if row.archived or row.is_fork:
            continue
        old = before.get(row.repository.casefold())
        gain = max(0, row.stars - old.stars) if old else 0
        ranked.append(
            RankedRepo(
                snapshot=row,
                stars_gained=gain,
                is_trial=not has_full_baseline or old is None,
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
    by_day: dict[date, list[RepoSnapshot]] = {}
    for offset in range(window_days, 0, -1):
        day = today - timedelta(days=offset)
        snapshots = store.load_snapshot(day)
        if snapshots:
            by_day[day] = snapshots
            history.extend(snapshots)
    exact_day = today - timedelta(days=window_days)
    if exact_day in by_day:
        return by_day[exact_day], True, history
    if by_day:
        oldest_day = min(by_day)
        return by_day[oldest_day], False, history
    return [], False, history


def historical_candidate_names(store: StateStore, today: date, window_days: int = 7) -> list[str]:
    """Return names from the most recent saved snapshots for candidate retention."""
    _, _, history = load_recent_snapshots(store, today, window_days)
    return [snapshot.repository for snapshot in history]


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
    baseline, has_full_baseline, _ = load_recent_snapshots(store, today, window_days)
    ranked = rank_repositories(
        current,
        baseline,
        top_n,
        has_full_baseline=has_full_baseline,
    )
    store.save_snapshot(today, current)
    store.prune_snapshots(today, retention_days)
    return ranked
