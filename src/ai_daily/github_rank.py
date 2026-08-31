import re
from collections import Counter
from datetime import date, timedelta

from ai_daily.models import RankedRepo, RepoSnapshot
from ai_daily.state import StateStore

MAX_PLAUSIBLE_SEVEN_DAY_STAR_GAIN = 1_000_000
_EXPLICIT_MIRROR = re.compile(
    r"(?:^|\b)(?:read[ -]only\s+)?mirror(?:ed)?(?:\s+repository)?\s+"
    r"(?:of|from)\s+https?://(?:www\.)?(?:github\.com|gitlab\.com|bitbucket\.org)/",
    re.IGNORECASE,
)


class RepositoryRankingBatch(list[RankedRepo]):
    """Rankings plus aggregate, reader-safe reasons for conservative exclusions."""

    def __init__(
        self,
        rankings: list[RankedRepo],
        *,
        exclusion_counts: Counter[str] | None = None,
    ) -> None:
        super().__init__(rankings)
        counts = exclusion_counts or Counter()
        self.exclusion_counts = {
            reason: counts[reason]
            for reason in ("mirror", "star_anomaly")
            if counts[reason]
        }
        warnings: list[str] = []
        if counts["mirror"]:
            noun = "mirror" if counts["mirror"] == 1 else "mirrors"
            warnings.append(
                f"GitHub ranking excluded {counts['mirror']} obvious {noun}"
            )
        if counts["star_anomaly"]:
            noun = "anomaly" if counts["star_anomaly"] == 1 else "anomalies"
            warnings.append(
                "GitHub ranking excluded "
                f"{counts['star_anomaly']} implausible seven-day star {noun}"
            )
        self.warnings = tuple(warnings)


def _is_obvious_mirror(row: RepoSnapshot) -> bool:
    return row.is_mirror or bool(_EXPLICIT_MIRROR.search(row.description))


def rank_repositories(
    current: list[RepoSnapshot],
    baseline: list[RepoSnapshot],
    top_n: int = 10,
    *,
    has_full_baseline: bool = True,
    fallback: list[RepoSnapshot] | None = None,
) -> RepositoryRankingBatch:
    """Rank eligible repositories after narrow mirror and impossible-growth exclusions."""
    if top_n < 1:
        raise ValueError("top_n must be positive")
    before = {row.repository.casefold(): row for row in baseline}
    fallback_before: dict[str, RepoSnapshot] = {}
    for row in fallback or []:
        fallback_before.setdefault(row.repository.casefold(), row)
    ranked: list[RankedRepo] = []
    exclusion_counts: Counter[str] = Counter()
    for row in current:
        if row.archived or row.is_fork:
            continue
        if _is_obvious_mirror(row):
            exclusion_counts["mirror"] += 1
            continue
        exact_old = before.get(row.repository.casefold())
        old = exact_old or fallback_before.get(row.repository.casefold())
        gain = max(0, row.stars - old.stars) if old else 0
        if (
            has_full_baseline
            and exact_old is not None
            and row.stars - exact_old.stars > MAX_PLAUSIBLE_SEVEN_DAY_STAR_GAIN
        ):
            exclusion_counts["star_anomaly"] += 1
            continue
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
    return RepositoryRankingBatch(ranked[:top_n], exclusion_counts=exclusion_counts)


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
