import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from ai_daily.collectors import github
from ai_daily.collectors.github import discover_candidates, fetch_repo_snapshots
from ai_daily.github_rank import (
    historical_candidate_names,
    load_recent_snapshots,
    rank_and_store_repositories,
    rank_repositories,
)
from ai_daily.models import RepoSnapshot
from ai_daily.state import StateStore

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 8, 24, 9, tzinfo=UTC)
TODAY = NOW.date()


def snap(name: str, stars: int, *, archived: bool = False, fork: bool = False) -> RepoSnapshot:
    return RepoSnapshot(
        repository=name,
        stars=stars,
        forks=3,
        updated_at=NOW,
        collected_at=NOW,
        archived=archived,
        is_fork=fork,
    )


class GitHubFixtureClient:
    """Records the collector boundary while returning complete fixed GitHub payloads."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.trending = (FIXTURES / "github_trending.html").read_bytes()
        self.search = (FIXTURES / "github_search.json").read_bytes()
        self.repos = json.loads((FIXTURES / "github_repos.json").read_text(encoding="utf-8"))

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        self.requests.append((url, kwargs))
        request = httpx.Request("GET", url)
        if url.startswith("https://github.com/trending"):
            return httpx.Response(200, content=self.trending, request=request)
        if url == "https://api.github.com/search/repositories":
            return httpx.Response(200, content=self.search, request=request)
        if url.startswith("https://api.github.com/repos/"):
            name = url.rsplit("/repos/", 1)[1]
            row = next((item for item in self.repos if item["full_name"].casefold() == name.casefold()), None)
            if row is None:
                row = {
                    "full_name": name,
                    "description": None,
                    "language": None,
                    "stargazers_count": 1,
                    "forks_count": 0,
                    "updated_at": "2026-08-24T08:00:00Z",
                    "archived": False,
                    "fork": False,
                }
            return httpx.Response(200, json=row, request=request)
        raise AssertionError(f"unexpected URL: {url}")


def test_discovery_unions_trending_search_and_history_case_insensitively() -> None:
    """Would catch a discovery change that drops one source or duplicates repository casing."""
    client = GitHubFixtureClient()

    names = discover_candidates(
        client,
        "token",
        TODAY,
        historical_names=["OTHER/TOOL", "saved/repo"],
    )

    assert names == ["Acme/Widget", "other/tool", "saved/repo", "new/project"]
    assert [url for url, _ in client.requests].count("https://github.com/trending?since=daily") == 1
    assert [url for url, _ in client.requests].count("https://github.com/trending?since=weekly") == 1
    search_calls = [request for request in client.requests if "search/repositories" in request[0]]
    assert len(search_calls) == 2
    assert search_calls[0][1]["params"] == {
        "q": "created:>=2026-08-10",
        "sort": "stars",
        "order": "desc",
        "per_page": 100,
    }
    assert search_calls[1][1]["params"] == {
        "q": "pushed:>=2026-08-17 stars:>=100",
        "sort": "stars",
        "order": "desc",
        "per_page": 100,
    }
    assert search_calls[0][1]["headers"] == {
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer token",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def test_github_api_headers_omit_empty_authorization_but_keep_nonempty_tokens() -> None:
    """Would catch unauthenticated API calls emitting an invalid empty Bearer credential."""
    assert github.github_headers("") == {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    assert github.github_headers("token")["Authorization"] == "Bearer token"


def test_discovery_rejects_malformed_search_json_with_sanitized_typed_error() -> None:
    """Would catch malformed Search JSON bypassing cached fallback or leaking its response body."""
    client = GitHubFixtureClient()
    client.search = b'{"token":"ghp-secret"'

    with pytest.raises(github.GitHubResponseError, match="GitHub search response is invalid") as error:
        discover_candidates(client, "token", TODAY)

    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert "ghp-secret" not in str(error.value)


def test_discovery_rejects_missing_search_fields_with_typed_data_error() -> None:
    """Would catch a structurally unusable Search response being treated as an empty success."""
    client = GitHubFixtureClient()
    client.search = b'{"items":[{"name":"missing full name"}]}'

    with pytest.raises(github.GitHubDataError, match="GitHub search data is invalid"):
        discover_candidates(client, "token", TODAY)


def test_discovery_rejects_empty_required_repository_name() -> None:
    """Would catch an empty required Search field being silently discarded as a usable response."""
    client = GitHubFixtureClient()
    client.search = b'{"items":[{"full_name":""}]}'

    with pytest.raises(github.GitHubDataError, match="GitHub search data is invalid"):
        discover_candidates(client, "token", TODAY)


def test_fetch_snapshots_maps_metadata_filters_ineligible_and_caps_requests() -> None:
    """Would catch invalid GitHub metadata mapping or an unbounded repository API fan-out."""
    client = GitHubFixtureClient()
    client.repos.extend(
        [
            {**client.repos[0], "full_name": "archived/repo", "archived": True},
            {**client.repos[0], "full_name": "forked/repo", "fork": True},
        ]
    )

    snapshots = fetch_repo_snapshots(
        client,
        "token",
        ["acme/widget", "archived/repo", "forked/repo", *(f"o/{i}" for i in range(101))],
        NOW,
        max_repositories=4,
    )

    assert snapshots == [
        snap("acme/widget", 125).model_copy(
            update={"description": "A useful widget", "primary_language": "Python", "forks": 7,
                     "updated_at": datetime(2026, 8, 24, 8, tzinfo=UTC)}
        ),
        snap("o/0", 1).model_copy(update={"forks": 0, "updated_at": datetime(2026, 8, 24, 8, tzinfo=UTC)}),
    ]
    metadata_calls = [request for request in client.requests if "/repos/" in request[0]]
    assert len(metadata_calls) == 4
    assert all(call[1]["headers"]["Authorization"] == "Bearer token" for call in metadata_calls)


def test_fetch_snapshots_isolates_malformed_repository_records_and_warns() -> None:
    """Would catch one malformed repository aborting usable metadata or disappearing silently."""
    client = GitHubFixtureClient()
    malformed = {**client.repos[0], "full_name": "bad/repo"}
    malformed.pop("stargazers_count")
    client.repos.append(malformed)

    batch = fetch_repo_snapshots(client, "token", ["bad/repo", "acme/widget"], NOW)

    assert isinstance(batch, github.GitHubSnapshotBatch)
    assert [row.repository for row in batch] == ["acme/widget"]
    assert batch.warnings == (
        "GitHub metadata partial: 1/2 repositories unavailable (data=1)",
    )


def test_fetch_snapshots_preserves_githubs_explicit_mirror_marker_for_ranking() -> None:
    """Would catch GitHub mirror metadata being discarded before eligibility filtering."""
    client = GitHubFixtureClient()
    client.repos.append(
        {
            **client.repos[0],
            "full_name": "mirror/repo",
            "mirror_url": "https://gitlab.com/source/project",
        }
    )

    batch = fetch_repo_snapshots(client, "token", ["mirror/repo"], NOW)

    assert len(batch) == 1
    assert batch[0].repository == "mirror/repo"
    assert batch[0].is_mirror is True


@pytest.mark.parametrize("failure_kind", ["http_404", "transport", "response"])
def test_fetch_snapshots_isolates_http_transport_and_json_failures(failure_kind: str) -> None:
    """Would catch one repository-level failure source breaking the rest of the bounded batch."""
    base = GitHubFixtureClient()

    class PartialClient:
        def get(self, url: str, **kwargs: object) -> httpx.Response:
            if url.endswith("/bad/repo"):
                request = httpx.Request("GET", url)
                if failure_kind == "http_404":
                    raise httpx.HTTPStatusError(
                        "body contains ghp-secret",
                        request=request,
                        response=httpx.Response(404, request=request),
                    )
                if failure_kind == "transport":
                    raise httpx.ConnectError("token ghp-secret", request=request)
                return httpx.Response(200, content=b'{"token":"ghp-secret"', request=request)
            return base.get(url, **kwargs)

    batch = fetch_repo_snapshots(
        PartialClient(),  # type: ignore[arg-type]
        "token",
        ["bad/repo", "acme/widget"],
        NOW,
    )

    assert [row.repository for row in batch] == ["acme/widget"]
    assert batch.warnings == (
        f"GitHub metadata partial: 1/2 repositories unavailable ({failure_kind}=1)",
    )
    assert "secret" not in batch.warnings[0]


def test_ranking_uses_full_baseline_clamps_loss_and_sorts_ties() -> None:
    """Would catch wrong gain ordering, negative deltas, or nondeterministic ties."""
    current = [
        snap("z/tie", 300),
        snap("a/tie", 300),
        snap("lost/repo", 5),
        snap("archived/repo", 999, archived=True),
        snap("forked/repo", 999, fork=True),
    ]
    baseline = [snap("z/tie", 290), snap("a/tie", 290), snap("lost/repo", 10)]

    ranked = rank_repositories(current, baseline)

    assert [(item.snapshot.repository, item.stars_gained, item.is_trial) for item in ranked] == [
        ("a/tie", 10, False),
        ("z/tie", 10, False),
        ("lost/repo", 0, False),
    ]


def test_ranking_marks_missing_baseline_as_trial_and_limits_to_ten() -> None:
    """Would catch first-week candidates being presented as a full seven-day ranking."""
    current = [snap(f"owner/{number:02}", 100 + number) for number in range(15)]

    ranked = rank_repositories(current, [], top_n=10)

    assert [item.snapshot.repository for item in ranked] == [f"owner/{number:02}" for number in range(14, 4, -1)]
    assert all(item.is_trial for item in ranked)
    assert all(item.stars_gained == 0 for item in ranked)


def test_ranking_excludes_only_explicit_mirrors_and_extreme_star_anomalies() -> None:
    """Would catch obvious ranking manipulation or an over-broad viral-repository filter."""
    normal = snap("owner/normal", 1_100_000)
    near_limit = snap("owner/viral-but-plausible", 1_000_100)
    explicit_mirror = snap("owner/mirror", 500)
    explicit_mirror.is_mirror = True
    described_mirror = snap("owner/described-mirror", 400)
    described_mirror.description = "Read-only mirror of https://gitlab.com/source/project"
    anomaly = snap("owner/anomaly", 1_000_101)
    current = [normal, near_limit, explicit_mirror, described_mirror, anomaly]
    baseline = [
        snap("owner/normal", 1_000_000),
        snap("owner/viral-but-plausible", 100),
        snap("owner/mirror", 0),
        snap("owner/described-mirror", 0),
        snap("owner/anomaly", 0),
    ]

    ranked = rank_repositories(current, baseline)

    assert [item.snapshot.repository for item in ranked] == [
        "owner/viral-but-plausible",
        "owner/normal",
    ]
    assert ranked.exclusion_counts == {"mirror": 2, "star_anomaly": 1}
    assert ranked.warnings == (
        "GitHub ranking excluded 2 obvious mirrors",
        "GitHub ranking excluded 1 implausible seven-day star anomaly",
    )


def test_recent_snapshots_loads_exact_baseline_or_oldest_first_week_snapshot(tmp_path: Path) -> None:
    """Would catch a startup rank that ignores a saved baseline or chooses a newer fallback."""
    store = StateStore(tmp_path)
    store.save_snapshot(TODAY - timedelta(days=6), [snap("owner/repo", 50)])
    store.save_snapshot(TODAY - timedelta(days=2), [snap("owner/repo", 90)])

    baseline, has_full_baseline, history = load_recent_snapshots(store, TODAY)

    assert baseline == [snap("owner/repo", 50)]
    assert has_full_baseline is False
    assert history == [snap("owner/repo", 50), snap("owner/repo", 90)]

    store.save_snapshot(TODAY - timedelta(days=7), [snap("owner/repo", 10)])
    baseline, has_full_baseline, _ = load_recent_snapshots(store, TODAY)
    assert baseline == [snap("owner/repo", 10)]
    assert has_full_baseline is True


def test_historical_candidates_keep_seven_most_recent_saved_snapshot_dates(tmp_path: Path) -> None:
    """Would catch missed runs dropping snapshots merely because their dates are older than a week."""
    store = StateStore(tmp_path)
    for offset in range(8, 15):
        store.save_snapshot(TODAY - timedelta(days=offset), [snap(f"retained/{offset}", offset)])

    assert historical_candidate_names(store, TODAY) == [
        f"retained/{offset}" for offset in range(14, 7, -1)
    ]


def test_ranking_uses_each_repositories_oldest_fallback_when_exact_file_lacks_it(
    tmp_path: Path,
) -> None:
    """Would catch a global exact-baseline file masking a repository's own first-week record."""
    store = StateStore(tmp_path)
    store.save_snapshot(TODAY - timedelta(days=7), [snap("other/repo", 10)])
    store.save_snapshot(TODAY - timedelta(days=6), [snap("target/repo", 60)])

    ranked = rank_and_store_repositories(store, TODAY, [snap("target/repo", 100)])

    assert [(item.snapshot.repository, item.stars_gained, item.is_trial) for item in ranked] == [
        ("target/repo", 40, True)
    ]


def test_ranking_finds_a_repositories_oldest_retained_fallback_beyond_candidate_history(
    tmp_path: Path,
) -> None:
    """Would catch a per-repository baseline lookup limited to the seven candidate snapshot dates."""
    store = StateStore(tmp_path)
    store.save_snapshot(TODAY - timedelta(days=14), [snap("target/repo", 40)])
    for offset in range(1, 8):
        store.save_snapshot(TODAY - timedelta(days=offset), [snap(f"other/{offset}", offset)])

    ranked = rank_and_store_repositories(store, TODAY, [snap("target/repo", 100)])

    assert [(item.snapshot.repository, item.stars_gained, item.is_trial) for item in ranked] == [
        ("target/repo", 60, True)
    ]


def test_priority_order_preserves_trending_search_and_history_before_metadata_cap() -> None:
    """Would catch alphabetic truncation that discards high-priority discovery signals after 100 names."""
    client = GitHubFixtureClient()
    client.trending = (
        b'<article class="Box-row"><h2><a href="/zz/trending">trending</a></h2></article>'
    )
    client.search = json.dumps(
        {"items": [{"full_name": "zz/search"}, *({"full_name": f"aa/{number:03}"} for number in range(101))]}
    ).encode()

    names = discover_candidates(client, "token", TODAY, historical_names=["zz/history"])
    fetch_repo_snapshots(client, "token", names, NOW, max_repositories=3)

    metadata_names = [request[0].rsplit("/repos/", 1)[1] for request in client.requests if "/repos/" in request[0]]
    assert names[:3] == ["zz/trending", "zz/history", "zz/search"]
    assert "zz/history" in names
    assert metadata_names == ["zz/trending", "zz/history", "zz/search"]


def test_rank_and_store_saves_then_prunes_only_after_successful_ranking(tmp_path: Path, monkeypatch) -> None:
    """Would catch retention deleting historical baselines before a ranking can be produced."""
    store = StateStore(tmp_path)
    events: list[str] = []
    original_save = store.save_snapshot
    original_prune = store.prune_snapshots

    def record_save(day: date, rows: list[RepoSnapshot]) -> Path:
        events.append("save")
        return original_save(day, rows)

    def record_prune(day: date, retention_days: int) -> list[Path]:
        events.append("prune")
        return original_prune(day, retention_days)

    monkeypatch.setattr(store, "save_snapshot", record_save)
    monkeypatch.setattr(store, "prune_snapshots", record_prune)

    ranked = rank_and_store_repositories(store, TODAY, [snap("owner/repo", 100)])

    assert ranked[0].is_trial is True
    assert events == ["save", "prune"]
    assert store.load_snapshot(TODAY) == [snap("owner/repo", 100)]
