# AI Daily News GitHub Actions Implementation Plan

> [!IMPORTANT]
> **The workflow authority steps in this dated plan are superseded and remain only as implementation history.** Do not execute passages that prescribe ref-selectable `workflow_dispatch`, persisted checkout write credentials, pull/rebase state writes, or inline built-in `GITHUB_TOKEN` pushes. The current sources of truth are `README.md` and `.github/workflows/`: default-branch `repository_dispatch` handles manual controls, `workflow_run` starts trusted production, every checkout uses the built-in read token with `persist-credentials: false`, and only individual push steps receive the `ai-daily-production` Environment's step-scoped `AI_DAILY_STATE_TOKEN`. The personal-account trust boundary treats same-repository writers as trusted with respect to the built-in token; untrusted contributions must use forks.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a private-repository Python application that gathers broad AI news and GitHub all-domain trending data, optionally summarizes it with DeepSeek, and sends one deduplicated Outlook daily email before 08:00 Asia/Shanghai.

**Architecture:** A configuration-driven Python package separates collection, deterministic processing, AI enrichment, rendering, state persistence, and Microsoft Graph delivery. GitHub Actions runs the package twice each morning with a date-based sent marker; repository snapshots provide seven-day Star deltas, while AI failures degrade to deterministic templates.

**Tech Stack:** Python 3.12, Pydantic 2, HTTPX, feedparser, Beautiful Soup 4, RapidFuzz, PyYAML, Jinja2, OpenAI Python SDK, MSAL, cryptography, pytest, respx, Ruff, GitHub Actions, Microsoft Graph.

**Spec:** `docs/superpowers/specs/2026-08-24-ai-daily-github-actions-design.md`

## Global Constraints

- Run on GitHub Actions; do not require a NAS, database, browser session, or always-on server.
- Use timezone `Asia/Shanghai`, primary schedule `06:47`, and compensation schedule `07:22`.
- Produce at most 12 news candidates; in `full` mode AI chooses 0-12 final items.
- Rank GitHub projects across all domains by rolling seven-day Star increase; label days 1-7 as trial rankings.
- Support `full`, `economy`, and `off` AI modes through configuration only.
- Default to a DeepSeek OpenAI-compatible endpoint and calculate cost from API-returned usage, not local character estimates.
- Send mail through Microsoft Graph delegated `Mail.Send`; never store an Outlook password.
- Store Microsoft token state only as an encrypted MSAL cache; store its encryption key only in GitHub Secrets.
- Keep GitHub snapshots for 35 days and Markdown digests indefinitely.
- A single source, AI, or GitHub API failure must not prevent a degraded digest where usable data exists.
- Never log API keys, OAuth tokens, encrypted-cache keys, or full recipient addresses.
- Use test-driven development, fixture-based parser tests, and one focused commit per task.

---

## File Map

```text
pyproject.toml                         Package metadata, dependencies, pytest and Ruff settings
.gitignore                             Local caches, decrypted tokens, previews and logs
.env.example                           Secret names without values
config/settings.yaml                  Schedule-independent runtime behavior and AI mode
config/sources.yaml                   Official, media, release and discovery sources
config/pricing.yaml                   Versioned token prices per one million tokens
src/ai_daily/__init__.py               Package version
src/ai_daily/models.py                 Shared validated domain models
src/ai_daily/config.py                 YAML and environment configuration loader
src/ai_daily/http.py                   Bounded retrying HTTP client
src/ai_daily/state.py                  Atomic JSON state and snapshot retention
src/ai_daily/collectors/base.py        Collector protocol and registry
src/ai_daily/collectors/feed.py        RSS/Atom collector
src/ai_daily/collectors/page.py        Configurable first-party/media page collector
src/ai_daily/collectors/github.py      Release, Trending and repository API collectors
src/ai_daily/collectors/discovery.py   Allowlisted media discovery collector
src/ai_daily/news.py                   Normalization, deduplication, clustering and trust grading
src/ai_daily/github_rank.py            Candidate snapshots and rolling seven-day ranking
src/ai_daily/ai.py                     Full/economy/off enrichment and JSON validation
src/ai_daily/cost.py                   Usage aggregation and configured cost calculation
src/ai_daily/render.py                 HTML, text and Markdown rendering
src/ai_daily/mail.py                   Encrypted MSAL cache and Microsoft Graph sendMail
src/ai_daily/pipeline.py               End-to-end orchestration and degradation policy
src/ai_daily/cli.py                    collect, preview, send and validate-sources commands
templates/daily.html.j2                Outlook-safe inline-style HTML email
templates/daily.txt.j2                 Plain-text alternative
templates/daily.md.j2                  Long-term Markdown archive
scripts/setup_outlook.py               One-time interactive delegated authorization
scripts/check_secrets.py                Scan tracked files without printing secret values
.github/workflows/ci.yml               Offline tests and lint
.github/workflows/daily.yml            Manual, primary and compensation runs
tests/fixtures/                         Fixed RSS, HTML, GitHub, AI and Graph responses
tests/test_*.py                         Focused unit and integration tests
```

---

### Task 1: Package Skeleton, Models, and Configuration

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `config/settings.yaml`
- Create: `config/pricing.yaml`
- Create: `src/ai_daily/__init__.py`
- Create: `src/ai_daily/models.py`
- Create: `src/ai_daily/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: none.
- Produces: `load_settings(root: Path) -> AppSettings`, `load_prices(root: Path) -> PricingTable`, and shared Pydantic models `RawItem`, `NewsCluster`, `RepoSnapshot`, `RankedRepo`, `UsageRecord`, `Digest`, and `RunState`.

- [ ] **Step 1: Write the failing configuration test**

```python
# tests/test_config.py
from pathlib import Path

from ai_daily.config import load_prices, load_settings


def test_loads_required_defaults(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config/settings.yaml").write_text(
        "timezone: Asia/Shanghai\n"
        "ai_mode: full\n"
        "ai_base_url: https://api.deepseek.com\n"
        "ai_model: deepseek-chat\n"
        "ai_max_output_tokens: 2500\n"
        "max_news_candidates: 12\n"
        "github_top_n: 10\n"
        "github_window_days: 7\n"
        "snapshot_retention_days: 35\n",
        encoding="utf-8",
    )
    (tmp_path / "config/pricing.yaml").write_text(
        "effective_date: 2026-08-24\ncurrency: USD\nmodels:\n  deepseek-chat:\n"
        "    input_cache_hit: 0.0\n    input_cache_miss: 0.0\n    output: 0.0\n",
        encoding="utf-8",
    )

    settings = load_settings(tmp_path)
    prices = load_prices(tmp_path)

    assert settings.timezone == "Asia/Shanghai"
    assert settings.max_news_candidates == 12
    assert settings.github_window_days == 7
    assert prices.models["deepseek-chat"].output == 0.0
```

- [ ] **Step 2: Run the test and verify the package is absent**

Run: `python -m pytest tests/test_config.py -v`  
Expected: FAIL with `ModuleNotFoundError: No module named 'ai_daily'`.

- [ ] **Step 3: Create package metadata and exact dependencies**

```toml
# pyproject.toml
[build-system]
requires = ["hatchling>=1.27,<2"]
build-backend = "hatchling.build"

[project]
name = "ai-daily-news"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "beautifulsoup4>=4.13,<5",
  "cryptography>=45,<46",
  "feedparser>=6.0,<7",
  "httpx>=0.28,<1",
  "jinja2>=3.1,<4",
  "msal>=1.32,<2",
  "openai>=1.100,<2",
  "pydantic>=2.11,<3",
  "python-dateutil>=2.9,<3",
  "pyyaml>=6.0,<7",
  "rapidfuzz>=3.13,<4",
]

[project.optional-dependencies]
dev = ["pytest>=8.4,<9", "pytest-cov>=6.2,<7", "respx>=0.22,<1", "ruff>=0.12,<1"]

[project.scripts]
ai-daily = "ai_daily.cli:main"

[tool.pytest.ini_options]
addopts = "-q"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"
```

- [ ] **Step 4: Define validated shared models and loaders**

```python
# src/ai_daily/models.py
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl


class RawItem(BaseModel):
    source_id: str
    source_name: str
    source_type: Literal["official", "media", "release", "discovery"]
    title: str
    published_at: datetime
    canonical_url: HttpUrl
    excerpt: str = ""
    language: Literal["zh", "en", "other"] = "other"
    category: str = "other"
    content_fingerprint: str = ""


class NewsCluster(BaseModel):
    cluster_id: str
    title: str
    items: list[RawItem]
    trust_grade: Literal["A", "B", "C"]
    score: float = 0.0
    summary: str = ""
    why_it_matters: str = ""


class RepoSnapshot(BaseModel):
    repository: str
    description: str = ""
    primary_language: str | None = None
    stars: int = Field(ge=0)
    forks: int = Field(ge=0)
    updated_at: datetime
    archived: bool = False
    is_fork: bool = False
    collected_at: datetime


class RankedRepo(BaseModel):
    snapshot: RepoSnapshot
    stars_gained: int
    is_trial: bool
    explanation: str = ""


class UsageRecord(BaseModel):
    stage: str
    model: str
    input_cache_hit_tokens: int = 0
    input_cache_miss_tokens: int = 0
    output_tokens: int = 0


class Digest(BaseModel):
    local_date: str
    news: list[NewsCluster]
    repositories: list[RankedRepo]
    warnings: list[str] = Field(default_factory=list)
    usage: list[UsageRecord] = Field(default_factory=list)
    estimated_cost: float = 0.0


class RunState(BaseModel):
    last_success_at: datetime | None = None
    sent_dates: dict[str, str] = Field(default_factory=dict)
```

```python
# src/ai_daily/config.py
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, HttpUrl


class AppSettings(BaseModel):
    timezone: str = "Asia/Shanghai"
    ai_mode: Literal["full", "economy", "off"] = "full"
    ai_base_url: HttpUrl = "https://api.deepseek.com"
    ai_model: str = "deepseek-chat"
    ai_max_output_tokens: int = Field(default=2500, ge=256, le=8192)
    max_news_candidates: int = Field(default=12, ge=1, le=12)
    github_top_n: int = Field(default=10, ge=1, le=10)
    github_window_days: int = 7
    snapshot_retention_days: int = 35


class ModelPrice(BaseModel):
    input_cache_hit: float = Field(ge=0)
    input_cache_miss: float = Field(ge=0)
    output: float = Field(ge=0)


class PricingTable(BaseModel):
    effective_date: date
    currency: str = "USD"
    models: dict[str, ModelPrice]


def _read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_settings(root: Path) -> AppSettings:
    return AppSettings.model_validate(_read_yaml(root / "config/settings.yaml"))


def load_prices(root: Path) -> PricingTable:
    return PricingTable.model_validate(_read_yaml(root / "config/pricing.yaml"))
```

- [ ] **Step 5: Add production configuration and secret-name documentation**

```yaml
# config/settings.yaml
timezone: Asia/Shanghai
ai_mode: full
ai_base_url: https://api.deepseek.com
ai_model: deepseek-chat
ai_max_output_tokens: 2500
max_news_candidates: 12
github_top_n: 10
github_window_days: 7
snapshot_retention_days: 35
```

Create `config/pricing.yaml` with the current official DeepSeek prices recorded on the implementation date and an `effective_date` field. Create `.env.example` containing only `DEEPSEEK_API_KEY=`, `MS_CLIENT_ID=`, `MS_TOKEN_KEY=`, `OUTLOOK_SENDER=`, and `MAIL_TO=`. Add `.env`, `.venv/`, `.pytest_cache/`, `__pycache__/`, `*.log`, `preview/`, and `data/microsoft-token.json` to `.gitignore`.

- [ ] **Step 6: Install and run tests**

Run: `python -m pip install -e ".[dev]"`  
Run: `python -m pytest tests/test_config.py -v`  
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml .gitignore .env.example config src/ai_daily tests/test_config.py
git commit -m "chore: establish typed project configuration"
```

---

### Task 2: Retrying HTTP Client and Atomic State Store

**Files:**
- Create: `src/ai_daily/http.py`
- Create: `src/ai_daily/state.py`
- Test: `tests/test_http.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Consumes: `RunState` and `RepoSnapshot` from Task 1.
- Produces: `RetryingClient.get(url, **kwargs) -> httpx.Response`, `StateStore.load_run_state() -> RunState`, `StateStore.save_run_state(state) -> None`, `StateStore.save_snapshot(date, snapshots) -> Path`, and `StateStore.load_snapshot(date) -> list[RepoSnapshot]`.

- [ ] **Step 1: Write failing retry and state tests**

```python
# tests/test_http.py
import httpx
import respx

from ai_daily.http import RetryingClient


@respx.mock
def test_retries_transient_status_then_returns_response() -> None:
    route = respx.get("https://example.test/feed").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, text="ok")]
    )
    client = RetryingClient(max_attempts=2, backoff_seconds=0)
    assert client.get("https://example.test/feed").text == "ok"
    assert route.call_count == 2
```

```python
# tests/test_state.py
from pathlib import Path

from ai_daily.models import RunState
from ai_daily.state import StateStore


def test_state_round_trip_is_atomic(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.save_run_state(RunState(sent_dates={"2026-08-24": "message-id"}))
    assert store.load_run_state().sent_dates["2026-08-24"] == "message-id"
    assert not (tmp_path / "state.json.tmp").exists()
```

- [ ] **Step 2: Run tests and verify missing modules**

Run: `python -m pytest tests/test_http.py tests/test_state.py -v`  
Expected: FAIL during import of `ai_daily.http` and `ai_daily.state`.

- [ ] **Step 3: Implement bounded retries**

```python
# src/ai_daily/http.py
import time

import httpx


class RetryingClient:
    def __init__(self, max_attempts: int = 3, backoff_seconds: float = 1.0) -> None:
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self._client = httpx.Client(timeout=httpx.Timeout(20.0), follow_redirects=True)

    def get(self, url: str, **kwargs) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                response = self._client.get(url, **kwargs)
                if response.status_code not in {429, 500, 502, 503, 504}:
                    response.raise_for_status()
                    return response
                last_error = httpx.HTTPStatusError(
                    f"transient status {response.status_code}",
                    request=response.request,
                    response=response,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
            if attempt + 1 < self.max_attempts:
                time.sleep(self.backoff_seconds * (2**attempt))
        assert last_error is not None
        raise last_error
```

- [ ] **Step 4: Implement atomic JSON state and snapshot retention**

```python
# src/ai_daily/state.py
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
        path.write_text(TypeAdapter(list[RepoSnapshot]).dump_json(snapshots, indent=2).decode(), "utf-8")
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
```

- [ ] **Step 5: Add tests for permanent errors, missing state, snapshots, and 35-day pruning**

Add explicit tests asserting: a 404 is attempted once; missing state returns `RunState()`; snapshot serialization round-trips timezone-aware datetimes; a file dated 36 days ago is deleted while a file dated 35 days ago remains.

- [ ] **Step 6: Run tests and lint**

Run: `python -m pytest tests/test_http.py tests/test_state.py -v`  
Run: `python -m ruff check src/ai_daily/http.py src/ai_daily/state.py tests/test_http.py tests/test_state.py`  
Expected: all checks PASS.

- [ ] **Step 7: Commit**

```bash
git add src/ai_daily/http.py src/ai_daily/state.py tests/test_http.py tests/test_state.py
git commit -m "feat: add resilient HTTP and atomic state storage"
```

---

### Task 3: Source Registry and News Collectors

**Files:**
- Create: `config/sources.yaml`
- Create: `src/ai_daily/collectors/__init__.py`
- Create: `src/ai_daily/collectors/base.py`
- Create: `src/ai_daily/collectors/feed.py`
- Create: `src/ai_daily/collectors/page.py`
- Create: `src/ai_daily/collectors/github.py`
- Create: `src/ai_daily/collectors/discovery.py`
- Create: `tests/fixtures/feed.xml`
- Create: `tests/fixtures/news_page.html`
- Create: `tests/fixtures/github_releases.json`
- Create: `tests/fixtures/discovery.json`
- Test: `tests/test_collectors.py`

**Interfaces:**
- Consumes: `RetryingClient` and `RawItem`.
- Produces: `SourceConfig`, `load_sources(path) -> list[SourceConfig]`, collector protocol `collect(source, since) -> list[RawItem]`, and `CollectorRegistry.collect_all(sources, since) -> tuple[list[RawItem], list[str]]`.

- [ ] **Step 1: Write fixture-driven failing collector tests**

```python
# tests/test_collectors.py
from datetime import UTC, datetime

from ai_daily.collectors.base import SourceConfig
from ai_daily.collectors.feed import FeedCollector


def test_feed_collector_maps_entries(http_client) -> None:
    source = SourceConfig(
        id="deepmind",
        name="Google DeepMind",
        kind="feed",
        source_type="official",
        url="https://example.test/feed.xml",
        language="en",
        category="model",
    )
    items = FeedCollector(http_client).collect(source, datetime(2026, 8, 23, tzinfo=UTC))
    assert [item.title for item in items] == ["A new model release"]
    assert items[0].source_type == "official"
```

The `http_client` fixture must return `tests/fixtures/feed.xml` for the configured URL. Add parallel tests for page CSS selectors, GitHub Releases JSON, discovery allowlist filtering, and a failing source that returns a warning while another source succeeds.

- [ ] **Step 2: Run tests and verify imports fail**

Run: `python -m pytest tests/test_collectors.py -v`  
Expected: FAIL importing `ai_daily.collectors.base`.

- [ ] **Step 3: Define source schema and registry**

```python
# src/ai_daily/collectors/base.py
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

import yaml
from pydantic import BaseModel, Field, HttpUrl

from ai_daily.models import RawItem


class SourceConfig(BaseModel):
    id: str
    name: str
    kind: Literal["feed", "page", "github_releases", "discovery"]
    source_type: Literal["official", "media", "release", "discovery"]
    url: HttpUrl
    language: Literal["zh", "en", "other"]
    category: str
    enabled: bool = True
    item_selector: str | None = None
    title_selector: str | None = None
    link_selector: str | None = None
    date_selector: str | None = None
    excerpt_selector: str | None = None
    allowed_domains: list[str] = Field(default_factory=list)


class Collector(Protocol):
    def collect(self, source: SourceConfig, since: datetime) -> list[RawItem]: ...


def load_sources(path: Path) -> list[SourceConfig]:
    raw = yaml.safe_load(path.read_text("utf-8"))
    return [SourceConfig.model_validate(item) for item in raw["sources"] if item.get("enabled", True)]
```

- [ ] **Step 4: Implement collectors with strict boundaries**

Implement `FeedCollector` with feedparser, `PageCollector` with configured CSS selectors and canonical URL joining, `GitHubReleaseCollector` against `/repos/{owner}/{repo}/releases`, and `DiscoveryCollector` that rejects results whose hostname is not in `allowed_domains`. Each collector must discard items older than `since`, normalize missing excerpts to an empty string, and return timezone-aware datetimes.

Use GDELT DOC 2.0 as the discovery transport for allowlisted media that lack a stable native feed. Build a query from explicit bilingual AI keywords and `domain:` clauses, request JSON `ArtList` results for the 36-hour window, and still reject every returned hostname not present in `allowed_domains`. GDELT discovers links only; the source shown to the reader remains the original media domain.

The feed implementation follows this shape:

```python
class FeedCollector:
    def __init__(self, client: RetryingClient) -> None:
        self.client = client

    def collect(self, source: SourceConfig, since: datetime) -> list[RawItem]:
        response = self.client.get(str(source.url))
        parsed = feedparser.loads(response.content)
        rows = []
        for entry in parsed.entries:
            published = parse_entry_datetime(entry)
            if published < since:
                continue
            rows.append(
                RawItem(
                    source_id=source.id,
                    source_name=source.name,
                    source_type=source.source_type,
                    title=entry.title.strip(),
                    published_at=published,
                    canonical_url=urljoin(str(source.url), entry.link),
                    excerpt=BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ", strip=True),
                    language=source.language,
                    category=source.category,
                )
            )
        return rows
```

Use this registry interface:

```python
class CollectorRegistry:
    def __init__(self, collectors: dict[str, Collector]) -> None:
        self.collectors = collectors

    def collect_all(
        self, sources: list[SourceConfig], since: datetime
    ) -> tuple[list[RawItem], list[str]]:
        items: list[RawItem] = []
        warnings: list[str] = []
        for source in sources:
            try:
                items.extend(self.collectors[source.kind].collect(source, since))
            except Exception as exc:
                warnings.append(f"{source.id}: {type(exc).__name__}")
        return items, warnings
```

- [ ] **Step 5: Populate the source registry**

Create `config/sources.yaml` with entries for all of these official subjects: Kimi/月之暗面、GLM/智谱、DeepSeek、通义千问、豆包、文心、腾讯混元、MiniMax、阶跃星辰、百川、零一万物、讯飞星火、商汤、ModelScope、OpenAI、Anthropic、Google DeepMind、Google AI、Meta AI、Microsoft AI、xAI、Mistral、Cohere、Perplexity、Amazon、Apple、NVIDIA、AMD、Intel、Hugging Face、Databricks、Runway、Stability AI、ElevenLabs、GitHub Blog and GitHub Changelog. Add native feeds for TechCrunch AI, The Verge AI, Ars Technica, VentureBeat AI, MIT Technology Review, and Hacker News where a stable feed/API exists. Add GDELT discovery entries allowlisting Reuters, 机器之心、量子位、36氪、新智元、IT之家、财联社、虎嗅 and 钛媒体.

Every entry must have a unique `id`, an explicit `source_type`, and an explicit `category`. Do not silently substitute an unofficial domain for an official company source.

- [ ] **Step 6: Run offline parser tests**

Run: `python -m pytest tests/test_collectors.py -v`  
Expected: PASS without internet access.

- [ ] **Step 7: Add and run an opt-in live source validation test**

Create `tests/test_sources_live.py` marked `pytest.mark.live`. It loads every enabled source, fetches one response, asserts HTTP success and verifies that at least one item or a valid empty feed is parsed. Run it manually with `python -m pytest -m live tests/test_sources_live.py -v`; record each failing source ID in the Actions Summary and disable no source without documenting the reason in its YAML entry.

- [ ] **Step 8: Commit**

```bash
git add config/sources.yaml src/ai_daily/collectors tests/fixtures tests/test_collectors.py tests/test_sources_live.py
git commit -m "feat: collect configured official and trusted news sources"
```

---

### Task 4: Deterministic News Normalization, Clustering, and Trust Grades

**Files:**
- Create: `src/ai_daily/news.py`
- Test: `tests/test_news.py`

**Interfaces:**
- Consumes: `list[RawItem]`.
- Produces: `prepare_news(items: list[RawItem], limit: int = 12) -> list[NewsCluster]`, plus pure helpers `canonicalize_url`, `fingerprint`, `cluster_items`, `grade_cluster`, and `score_cluster`.

- [ ] **Step 1: Write failing news-processing tests**

```python
# tests/test_news.py
from datetime import UTC, datetime

from ai_daily.models import RawItem
from ai_daily.news import prepare_news


def item(source_id: str, source_type: str, title: str, url: str) -> RawItem:
    return RawItem(
        source_id=source_id,
        source_name=source_id,
        source_type=source_type,
        title=title,
        published_at=datetime(2026, 8, 24, 1, tzinfo=UTC),
        canonical_url=url,
        excerpt=title,
        language="zh",
        category="model",
    )


def test_official_and_media_versions_form_one_a_grade_cluster() -> None:
    clusters = prepare_news(
        [
            item("kimi", "official", "Kimi 发布新模型 K3", "https://kimi.ai/blog/k3"),
            item("media", "media", "月之暗面推出 Kimi K3", "https://media.test/kimi-k3"),
        ]
    )
    assert len(clusters) == 1
    assert clusters[0].trust_grade == "A"


def test_output_is_capped_at_twelve() -> None:
    rows = [item(str(n), "official", f"模型发布 {n}", f"https://example.test/{n}") for n in range(20)]
    assert len(prepare_news(rows, limit=12)) == 12
```

- [ ] **Step 2: Run tests and verify the module is absent**

Run: `python -m pytest tests/test_news.py -v`  
Expected: FAIL importing `ai_daily.news`.

- [ ] **Step 3: Implement canonicalization and fingerprints**

Canonicalization must lowercase hostnames, remove fragments and tracking parameters (`utm_*`, `spm`, `from`, `ref`), retain meaningful query parameters, collapse repeated slashes, and remove a trailing slash except for the origin. Fingerprints must use normalized Unicode NFKC title text plus the cleaned excerpt and return SHA-256 hex.

```python
def fingerprint(item: RawItem) -> str:
    material = normalize_text(f"{item.title}\n{item.excerpt[:500]}")
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Implement deterministic clustering and trust grades**

Use exact canonical URL/fingerprint matches first, then RapidFuzz token-set title similarity of at least 82 within a 72-hour window and the same category. Assign A when any member is official/release, B when at least two distinct media domains exist, and C otherwise.

```python
def grade_cluster(items: list[RawItem]) -> Literal["A", "B", "C"]:
    if any(item.source_type in {"official", "release"} for item in items):
        return "A"
    domains = {urlparse(str(item.canonical_url)).hostname for item in items}
    return "B" if len(domains) >= 2 else "C"
```

- [ ] **Step 5: Implement scoring and stable selection**

Score with explicit weights: A/B/C trust = 40/25/10, model/product/API releases = 25, funding/IPO/acquisition/regulation/security = 20, research/chips/open-source = 15, and age decay from 20 points at publication to 0 after 36 hours. Break ties by newest publication and then `cluster_id`. Return at most `limit` clusters.

```python
TRUST_POINTS = {"A": 40, "B": 25, "C": 10}
CATEGORY_POINTS = {
    "model": 25,
    "product": 25,
    "api": 25,
    "funding": 20,
    "ipo": 20,
    "acquisition": 20,
    "regulation": 20,
    "security": 20,
    "research": 15,
    "chips": 15,
    "open_source": 15,
}


def score_cluster(cluster: NewsCluster, now: datetime) -> float:
    newest = max(item.published_at for item in cluster.items)
    age_hours = max(0.0, (now - newest).total_seconds() / 3600)
    recency = max(0.0, 20.0 * (1.0 - age_hours / 36.0))
    category = max(CATEGORY_POINTS.get(item.category, 5) for item in cluster.items)
    return TRUST_POINTS[cluster.trust_grade] + category + recency
```

- [ ] **Step 6: Add edge-case tests**

Add tests for tracking-parameter removal, bilingual near-duplicate titles, two single-source articles remaining separate below similarity 82, B grade from two domains, C grade from one medium, stable tie ordering, and an empty input returning an empty list.

- [ ] **Step 7: Run tests and commit**

Run: `python -m pytest tests/test_news.py -v`  
Run: `python -m ruff check src/ai_daily/news.py tests/test_news.py`  
Expected: PASS.

```bash
git add src/ai_daily/news.py tests/test_news.py
git commit -m "feat: cluster and rank verified AI news events"
```

---

### Task 5: GitHub Candidate Discovery, Snapshots, and Seven-Day Top 10

**Files:**
- Modify: `src/ai_daily/collectors/github.py`
- Create: `src/ai_daily/github_rank.py`
- Create: `tests/fixtures/github_trending.html`
- Create: `tests/fixtures/github_search.json`
- Create: `tests/fixtures/github_repos.json`
- Test: `tests/test_github_rank.py`

**Interfaces:**
- Consumes: `RetryingClient`, `StateStore`, `RepoSnapshot`, and `AppSettings`.
- Produces: `discover_candidates(client, token) -> list[str]`, `fetch_repo_snapshots(client, token, names, collected_at) -> list[RepoSnapshot]`, and `rank_repositories(current, baseline, top_n=10) -> list[RankedRepo]`.

- [ ] **Step 1: Write failing ranking tests**

```python
# tests/test_github_rank.py
from datetime import UTC, datetime

from ai_daily.github_rank import rank_repositories
from ai_daily.models import RepoSnapshot


def snap(name: str, stars: int, *, archived: bool = False, fork: bool = False) -> RepoSnapshot:
    now = datetime(2026, 8, 24, tzinfo=UTC)
    return RepoSnapshot(
        repository=name,
        stars=stars,
        forks=0,
        updated_at=now,
        collected_at=now,
        archived=archived,
        is_fork=fork,
    )


def test_ranks_by_seven_day_gain_and_filters_ineligible_repos() -> None:
    current = [snap("a/one", 150), snap("b/two", 300), snap("c/old", 999, archived=True)]
    baseline = [snap("a/one", 100), snap("b/two", 290), snap("c/old", 1)]
    ranked = rank_repositories(current, baseline, top_n=10)
    assert [(r.snapshot.repository, r.stars_gained) for r in ranked] == [("a/one", 50), ("b/two", 10)]
```

- [ ] **Step 2: Run test and verify failure**

Run: `python -m pytest tests/test_github_rank.py -v`  
Expected: FAIL importing `ai_daily.github_rank`.

- [ ] **Step 3: Implement candidate discovery**

Parse `https://github.com/trending?since=daily` and `?since=weekly` for repository names. Query GitHub Search API for repositories created in the last 14 days sorted by stars and repositories pushed in the last 7 days with at least 100 stars. Union these with repository names found in the most recent seven saved snapshots; deduplicate case-insensitively.

Use the built-in `GITHUB_TOKEN` with `Accept: application/vnd.github+json` and the current stable GitHub API version header. Cap search pages so the workflow remains well below the `GITHUB_TOKEN` repository rate limit.

```python
def discover_candidates(client: RetryingClient, token: str, today: date) -> list[str]:
    names = set(parse_trending(client.get("https://github.com/trending?since=daily").text))
    names.update(parse_trending(client.get("https://github.com/trending?since=weekly").text))
    queries = [
        f"created:>={today - timedelta(days=14)}",
        f"pushed:>={today - timedelta(days=7)} stars:>=100",
    ]
    for query in queries:
        response = client.get(
            "https://api.github.com/search/repositories",
            params={"q": query, "sort": "stars", "order": "desc", "per_page": 100},
            headers=github_headers(token),
        )
        names.update(item["full_name"] for item in response.json()["items"])
    return sorted(names, key=str.casefold)
```

- [ ] **Step 4: Implement metadata fetch and ranking**

Fetch repositories in bounded batches, mapping API fields to `RepoSnapshot`. For a current repository absent from the baseline, use the oldest available snapshot and set `is_trial=True`. For a complete seven-day baseline, set `is_trial=False`. Clamp negative gains to zero, then sort by gain, total stars, and repository name.

```python
def rank_repositories(
    current: list[RepoSnapshot], baseline: list[RepoSnapshot], top_n: int = 10
) -> list[RankedRepo]:
    before = {row.repository.casefold(): row for row in baseline}
    ranked = []
    for row in current:
        if row.archived or row.is_fork:
            continue
        old = before.get(row.repository.casefold())
        gain = max(0, row.stars - old.stars) if old else 0
        ranked.append(RankedRepo(snapshot=row, stars_gained=gain, is_trial=old is None))
    ranked.sort(key=lambda value: (-value.stars_gained, -value.snapshot.stars, value.snapshot.repository))
    return ranked[:top_n]
```

- [ ] **Step 5: Add discovery and first-week tests**

Add fixture tests asserting Trending daily/weekly union, Search API union, historical-pool retention, case-insensitive deduplication, archived/fork filtering, missing baseline trial labels, negative-delta clamping, and exactly 10 outputs from 15 eligible repositories.

- [ ] **Step 6: Integrate snapshot retention**

Add a test that loads the snapshot at `today - 7 days`, falls back to the oldest available snapshot during days 1-7, saves the current snapshot, and calls `prune_snapshots(today, 35)` only after ranking succeeds.

- [ ] **Step 7: Run tests and commit**

Run: `python -m pytest tests/test_github_rank.py -v`  
Expected: PASS.

```bash
git add src/ai_daily/collectors/github.py src/ai_daily/github_rank.py tests/fixtures tests/test_github_rank.py
git commit -m "feat: rank GitHub projects by rolling star growth"
```

---

### Task 6: DeepSeek Enrichment Modes and Cost Accounting

**Files:**
- Create: `src/ai_daily/ai.py`
- Create: `src/ai_daily/cost.py`
- Create: `tests/fixtures/ai_news_response.json`
- Create: `tests/fixtures/ai_repos_response.json`
- Test: `tests/test_ai.py`
- Test: `tests/test_cost.py`

**Interfaces:**
- Consumes: `NewsCluster`, `RankedRepo`, `UsageRecord`, `AppSettings`, and `PricingTable`.
- Produces: `AIEnricher.enrich(news, repos, mode) -> tuple[list[NewsCluster], list[RankedRepo], list[UsageRecord]]`, `OffEnricher.enrich(...)`, and `calculate_cost(usage, prices) -> CostReport`.

- [ ] **Step 1: Write failing mode and cost tests**

```python
# tests/test_cost.py
from ai_daily.config import ModelPrice, PricingTable
from ai_daily.cost import calculate_cost
from ai_daily.models import UsageRecord


def test_calculates_cost_per_million_tokens() -> None:
    table = PricingTable(
        models={"deepseek-chat": ModelPrice(input_cache_hit=1, input_cache_miss=2, output=4)}
    )
    usage = [UsageRecord(stage="news", model="deepseek-chat", input_cache_hit_tokens=1_000_000,
                         input_cache_miss_tokens=500_000, output_tokens=250_000)]
    report = calculate_cost(usage, table)
    assert report.total == 3.0
    assert report.thirty_day_projection == 90.0
```

```python
# tests/test_ai.py
def test_off_mode_never_calls_client(sample_news, sample_repos, exploding_client) -> None:
    enricher = OffEnricher(exploding_client)
    news, repos, usage = enricher.enrich(sample_news, sample_repos, mode="off")
    assert news and repos
    assert usage == []
```

- [ ] **Step 2: Run tests and verify modules are absent**

Run: `python -m pytest tests/test_ai.py tests/test_cost.py -v`  
Expected: FAIL importing `ai_daily.ai` and `ai_daily.cost`.

- [ ] **Step 3: Implement cost models and exact arithmetic**

Use `Decimal` internally and expose a quantized six-decimal total. Reject usage for a model absent from `pricing.yaml` instead of silently returning zero.

```python
class CostReport(BaseModel):
    currency: str
    total: Decimal
    thirty_day_projection: Decimal
    by_stage: dict[str, Decimal]
```

- [ ] **Step 4: Implement structured batch prompts**

Use one news call and one GitHub call in `full`, one summary-only call per non-empty section in `economy`, and zero calls in `off`. Pass no more than 800 Unicode characters of excerpt per news cluster and no more than 300 characters of repository description. Set a bounded output-token limit and require JSON matching these shapes:

```json
{"selected_cluster_ids":["id"],"items":[{"cluster_id":"id","summary":"...","why_it_matters":"..."}]}
```

```json
{"items":[{"repository":"owner/name","explanation":"..."}]}
```

Validate that every returned ID exists in the input, selected news count is 0-12, summaries are non-empty and each repository appears at most once. Retry malformed JSON once with the validation error; then raise `AIEnrichmentError` so the pipeline can switch to `off`.

- [ ] **Step 5: Map DeepSeek usage fields**

Map `prompt_cache_hit_tokens`, `prompt_cache_miss_tokens`, and `completion_tokens` when Chat Completions fields are returned; also accept Responses-style `input_tokens_details.cached_tokens` and `output_tokens`. Write one `UsageRecord` per stage.

- [ ] **Step 6: Add tests for full, economy, malformed JSON, unknown IDs, and API failure**

Assert exact call counts: two for non-empty full mode, two for non-empty economy mode, zero for off mode, one retry for malformed JSON, and an `AIEnrichmentError` after the second invalid response. Assert that source URLs and full article bodies are absent from the model payload beyond configured excerpts.

- [ ] **Step 7: Run tests and commit**

Run: `python -m pytest tests/test_ai.py tests/test_cost.py -v`  
Expected: PASS.

```bash
git add src/ai_daily/ai.py src/ai_daily/cost.py tests/fixtures tests/test_ai.py tests/test_cost.py
git commit -m "feat: add switchable AI enrichment and cost reporting"
```

---

### Task 7: Outlook-Safe Digest Rendering

**Files:**
- Create: `src/ai_daily/render.py`
- Create: `templates/daily.html.j2`
- Create: `templates/daily.txt.j2`
- Create: `templates/daily.md.j2`
- Test: `tests/test_render.py`

**Interfaces:**
- Consumes: `Digest` and `CostReport`.
- Produces: `RenderedDigest(subject: str, html: str, text: str, markdown: str)` and `render_digest(digest, cost_report, templates_dir) -> RenderedDigest`.

- [ ] **Step 1: Write failing render tests**

```python
# tests/test_render.py
from ai_daily.render import render_digest


def test_renders_all_formats_and_escapes_untrusted_html(sample_digest, templates_dir) -> None:
    sample_digest.news[0].title = "<script>alert(1)</script>"
    rendered = render_digest(sample_digest, sample_cost_report, templates_dir)
    assert rendered.subject.startswith("[AI Daily] 2026-08-24")
    assert "<script>" not in rendered.html
    assert "GitHub" in rendered.html
    assert "Token" in rendered.text
    assert "## GitHub" in rendered.markdown
```

- [ ] **Step 2: Run test and verify failure**

Run: `python -m pytest tests/test_render.py -v`  
Expected: FAIL importing `ai_daily.render`.

- [ ] **Step 3: Implement the renderer**

Create a Jinja environment with autoescape for HTML and `StrictUndefined` for all templates. Subject format must be `[AI Daily] {local_date}｜{news_count} 条 AI 要闻 + GitHub 周榜 Top {repo_count}`. When `news_count == 0`, render “今日无重大 AI 官方动态”. Include warnings, data timestamps, trust badges, source links, trial ranking labels, Token counts, current-run cost and 30-day projection.

```python
def render_digest(digest: Digest, cost_report: CostReport, templates_dir: Path) -> RenderedDigest:
    html_env = Environment(loader=FileSystemLoader(templates_dir), autoescape=True, undefined=StrictUndefined)
    text_env = Environment(loader=FileSystemLoader(templates_dir), autoescape=False, undefined=StrictUndefined)
    context = {"digest": digest, "cost": cost_report}
    subject = f"[AI Daily] {digest.local_date}｜{len(digest.news)} 条 AI 要闻 + GitHub 周榜 Top {len(digest.repositories)}"
    return RenderedDigest(
        subject=subject,
        html=html_env.get_template("daily.html.j2").render(context),
        text=text_env.get_template("daily.txt.j2").render(context),
        markdown=text_env.get_template("daily.md.j2").render(context),
    )
```

- [ ] **Step 4: Create Outlook-safe templates**

Use a single-column layout no wider than 720px, inline CSS, semantic headings, visible absolute links in the text version, no JavaScript, no remote fonts, and no externally hosted stylesheet. Render each repository with rank, language, total Stars, seven-day gain and explanation. Render each news cluster with A/B/C badge and every supporting source link.

- [ ] **Step 5: Add render edge-case tests**

Test zero news, zero repositories, trial rankings, three trust grades, multiple source links, warnings, non-ASCII subjects, HTML escaping and an absent AI usage section in `off` mode.

- [ ] **Step 6: Run tests and commit**

Run: `python -m pytest tests/test_render.py -v`  
Expected: PASS.

```bash
git add src/ai_daily/render.py templates tests/test_render.py
git commit -m "feat: render Outlook-ready daily digests"
```

---

### Task 8: Encrypted Microsoft Token Cache and Graph Delivery

**Files:**
- Create: `src/ai_daily/mail.py`
- Create: `scripts/setup_outlook.py`
- Create: `tests/fixtures/graph_send_response.json`
- Test: `tests/test_mail.py`

**Interfaces:**
- Consumes: `RenderedDigest`, environment `MS_CLIENT_ID`, `MS_TOKEN_KEY`, `OUTLOOK_SENDER`, and `MAIL_TO`.
- Produces: `EncryptedTokenCache.load(path, key) -> SerializableTokenCache`, `EncryptedTokenCache.save(cache, path, key) -> None`, `GraphMailer.send(rendered) -> str`, and an interactive `scripts/setup_outlook.py` that creates `data/microsoft-token.enc`.

- [ ] **Step 1: Write failing encryption and send tests**

```python
# tests/test_mail.py
from pathlib import Path

from ai_daily.mail import EncryptedTokenCache


def test_token_cache_round_trip_is_encrypted(tmp_path: Path) -> None:
    key = EncryptedTokenCache.generate_key()
    path = tmp_path / "microsoft-token.enc"
    cache = EncryptedTokenCache.empty()
    cache.deserialize('{"AccessToken":{}}')
    EncryptedTokenCache.save(cache, path, key)
    assert b"AccessToken" not in path.read_bytes()
    assert EncryptedTokenCache.load(path, key).serialize() == cache.serialize()
```

Add a respx test asserting `POST https://graph.microsoft.com/v1.0/me/sendMail`, Bearer authorization, HTML and text MIME alternatives, recipient address, and success only on HTTP 202.

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/test_mail.py -v`  
Expected: FAIL importing `ai_daily.mail`.

- [ ] **Step 3: Implement AES-GCM token-cache encryption**

Use `cryptography.fernet.Fernet` with a URL-safe key from `MS_TOKEN_KEY`. Write encrypted bytes atomically through a sibling `.tmp` file. Reject missing, malformed or undecryptable caches with `MailAuthError` and never include ciphertext, plaintext or keys in exception messages.

- [ ] **Step 4: Implement delegated-token acquisition**

Create `msal.PublicClientApplication(client_id, authority="https://login.microsoftonline.com/consumers", token_cache=cache)`. Select the single cached account, call `acquire_token_silent(["Mail.Send"], account=account)`, and persist the encrypted cache whenever `cache.has_state_changed` is true. Raise `MailAuthError` when the result lacks `access_token`.

- [ ] **Step 5: Implement Microsoft Graph sendMail**

Build a MIME multipart/alternative message from `RenderedDigest`, encode it as base64, and POST it as `text/plain` to `/v1.0/me/sendMail`. Treat only HTTP 202 as accepted. Return a deterministic local message fingerprint derived from local date, recipient and subject; do not claim confirmed final delivery.

- [ ] **Step 6: Implement the one-time setup script**

The script must start an MSAL device flow for scope `Mail.Send`; MSAL adds the protocol-level `offline_access` request needed for a refresh token. Print only Microsoft's verification URL and user code, acquire the token after consent, encrypt the serialized cache, and write `data/microsoft-token.enc`. It must refuse to overwrite an existing encrypted cache unless invoked with `--replace`.

- [ ] **Step 7: Add failure and secrecy tests**

Test wrong encryption key, missing account, expired/unrefreshable cache, Graph 401, Graph 429, Graph 500, non-202 success codes, recipient header injection, and assertions that exception strings contain neither token nor full email address.

- [ ] **Step 8: Run tests and commit**

Run: `python -m pytest tests/test_mail.py -v`  
Expected: PASS.

```bash
git add src/ai_daily/mail.py scripts/setup_outlook.py tests/fixtures tests/test_mail.py
git commit -m "feat: send digests through encrypted Outlook Graph auth"
```

---

### Task 9: End-to-End Pipeline, Degradation, and Duplicate Protection

**Files:**
- Create: `src/ai_daily/pipeline.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: all services produced by Tasks 1-8.
- Produces: `DailyPipeline.run(options: RunOptions) -> RunResult`; `collect_news(since, now) -> tuple[list[RawItem], list[str]]`; `collect_and_rank_repositories(day) -> list[RankedRepo]`; and `write_outputs(local_date, rendered, cost) -> OutputPaths`.

Use these exact orchestration types:

```python
class RunOptions(BaseModel):
    send: bool = False
    force: bool = False
    ai_mode: Literal["full", "economy", "off"] | None = None


class OutputPaths(BaseModel):
    markdown: Path
    html: Path
    text: Path
    cost_report: Path


class RunResult(BaseModel):
    local_date: str
    sent: bool
    already_sent: bool = False
    message_id: str | None = None
    markdown_path: Path | None = None
    warnings: list[str] = Field(default_factory=list)
    estimated_cost: Decimal = Decimal("0")
```

- [ ] **Step 1: Write a failing happy-path integration test**

```python
# tests/test_pipeline.py
def test_pipeline_generates_archives_sends_and_marks_date(pipeline, fake_clock) -> None:
    result = pipeline.run(RunOptions(send=True, force=False))
    assert result.local_date == "2026-08-24"
    assert result.sent is True
    assert result.markdown_path.name == "2026-08-24.md"
    assert pipeline.state_store.load_run_state().sent_dates["2026-08-24"] == result.message_id
```

- [ ] **Step 2: Run test and verify failure**

Run: `python -m pytest tests/test_pipeline.py -v`  
Expected: FAIL importing `ai_daily.pipeline`.

- [ ] **Step 3: Implement the orchestration order**

Use this exact order: load state/config; determine Beijing date and `since`; exit if already sent unless `force`; collect sources; prepare at most 12 news clusters; discover/fetch/rank GitHub; save the current snapshot; attempt configured AI enrichment; on `AIEnrichmentError` use off-mode enrichment and append a warning; calculate cost; render; save Markdown and preview files; send only when requested; write sent marker only after Graph 202; update `last_success_at`; prune snapshots after successful state write.

```python
def run(self, options: RunOptions) -> RunResult:
    local_now = self.clock.now(ZoneInfo(self.settings.timezone))
    local_date = local_now.date().isoformat()
    state = self.state_store.load_run_state()
    if local_date in state.sent_dates and not options.force:
        return RunResult(local_date=local_date, sent=False, already_sent=True, message_id=state.sent_dates[local_date])
    raw_items, warnings = self.collect_news(state.last_success_at, local_now)
    candidates = prepare_news(raw_items, self.settings.max_news_candidates)
    ranked_repos = self.collect_and_rank_repositories(local_now.date())
    mode = options.ai_mode or self.settings.ai_mode
    try:
        news, repos, usage = self.enricher.enrich(candidates, ranked_repos, mode)
    except AIEnrichmentError:
        warnings.append("AI enrichment failed; deterministic fallback used")
        news, repos, usage = self.off_enricher.enrich(candidates, ranked_repos, "off")
    cost = calculate_cost(usage, self.prices)
    digest = Digest(local_date=local_date, news=news, repositories=repos, warnings=warnings, usage=usage, estimated_cost=float(cost.total))
    rendered = render_digest(digest, cost, self.templates_dir)
    paths = self.write_outputs(local_date, rendered, cost)
    message_id = self.mailer.send(rendered) if options.send else None
    if message_id:
        state.sent_dates[local_date] = message_id
        state.last_success_at = local_now
        self.state_store.save_run_state(state)
        self.state_store.prune_snapshots(local_now.date(), self.settings.snapshot_retention_days)
    return RunResult(
        local_date=local_date,
        sent=message_id is not None,
        message_id=message_id,
        markdown_path=paths.markdown,
        warnings=warnings,
        estimated_cost=cost.total,
    )
```

- [ ] **Step 4: Implement degradation boundaries**

Use typed exceptions and catch them only at component boundaries. News collector failures become warnings. If all news sources fail but GitHub succeeds, continue. If GitHub fails but news succeeds, use the newest saved ranked snapshot and show its date. If both news and GitHub have no usable current or saved data, fail without sending a misleading empty email. A mail failure must leave the sent marker absent.

- [ ] **Step 5: Implement preview and archive paths**

Write `digests/{date}.md` for the long-term archive. Write `preview/{date}.html`, `.txt`, and `cost-report.json` for Actions Artifacts. Use atomic writes. The cost report contains only model, stage, token counts, configured currency, current-run total and 30-day projection.

- [ ] **Step 6: Add integration tests for every degradation path**

Add tests for: already-sent exit; `force=True`; preview without send; one collector failure; all news failure; AI failure to off mode; GitHub failure with cached data; GitHub failure without cached data but valid news; both sections unavailable; mail failure followed by successful compensation; and state update only after accepted send.

- [ ] **Step 7: Run the pipeline test suite and commit**

Run: `python -m pytest tests/test_pipeline.py -v`  
Expected: PASS.

```bash
git add src/ai_daily/pipeline.py tests/test_pipeline.py
git commit -m "feat: orchestrate resilient daily digest runs"
```

---

### Task 10: CLI, GitHub Actions, CI, and Acceptance Verification

> [!WARNING]
> Task 10's original workflow YAML and manual/write instructions are superseded. Preserve them as history only; use the repository's current README and checked-in workflows instead.

**Files:**
- Create: `src/ai_daily/cli.py`
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/daily.yml`
- Create: `tests/test_cli.py`
- Create: `scripts/check_secrets.py`
- Create: `README.md`
- Modify: `config/pricing.yaml`

**Interfaces:**
- Consumes: `DailyPipeline`, `load_sources`, and all required environment variables.
- Produces: commands `ai-daily run`, `ai-daily preview`, and `ai-daily validate-sources`; scheduled and manual GitHub workflows; operator setup documentation.

- [ ] **Step 1: Write failing CLI tests**

```python
# tests/test_cli.py
from ai_daily.cli import main


def test_preview_never_sends(monkeypatch, fake_pipeline) -> None:
    monkeypatch.setattr("ai_daily.cli.build_pipeline", lambda: fake_pipeline)
    assert main(["preview", "--ai-mode", "off"]) == 0
    assert fake_pipeline.last_options.send is False
    assert fake_pipeline.last_options.ai_mode == "off"
```

Add tests for `run --send`, `run --force`, invalid AI mode, missing required send secrets, and `validate-sources` returning nonzero only when the configured minimum success percentage is missed.

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/test_cli.py -v`  
Expected: FAIL importing `ai_daily.cli`.

- [ ] **Step 3: Implement argparse commands**

`preview` defaults to no send and accepts `--ai-mode full|economy|off`. `run` accepts `--send`, `--force`, and the same mode override. `validate-sources` accepts `--minimum-success 80`. Print a concise redacted summary and write the full structured summary to the path in `GITHUB_STEP_SUMMARY` when present.

```python
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-daily")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "preview"):
        command = commands.add_parser(name)
        command.add_argument("--ai-mode", choices=("full", "economy", "off"))
        command.add_argument("--force", action="store_true")
        if name == "run":
            command.add_argument("--send", action="store_true")
    validate = commands.add_parser("validate-sources")
    validate.add_argument("--minimum-success", type=int, default=80)
    return parser
```

- [ ] **Step 4: Create offline CI workflow**

```yaml
# .github/workflows/ci.yml
name: CI
on:
  pull_request:
  push:
    branches: [master, main]
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
      - uses: actions/setup-python@42375524e23c412d93fb67b49958b491fce71c38 # v5.4.0
        with:
          python-version: "3.12"
          cache: pip
      - run: python -m pip install -e ".[dev]"
      - run: python -m ruff check .
      - run: python -m pytest -m "not live" --cov=ai_daily --cov-report=term-missing
```

Use these reviewed full commit SHAs in both workflows. Pin Artifact upload as `actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02` with comment `# v4.6.2`; do not use floating version tags.

- [ ] **Step 5: Create daily workflow with exact schedules and idempotent commit**

```yaml
# .github/workflows/daily.yml
name: AI Daily
on:
  workflow_dispatch:
    inputs:
      mode:
        type: choice
        options: [full, economy, off]
        default: full
      send:
        type: boolean
        default: false
      force:
        type: boolean
        default: false
  schedule:
    - cron: "47 6 * * *"
      timezone: "Asia/Shanghai"
    - cron: "22 7 * * *"
      timezone: "Asia/Shanghai"
permissions:
  contents: write
concurrency:
  group: ai-daily-${{ github.ref }}
  cancel-in-progress: false
```

Add checkout, Python 3.12 setup, locked dependency installation, `ai-daily run --send`, Artifact upload for `preview/`, and a commit step restricted to `data/`, `digests/`, and `data/microsoft-token.enc`. Pull with rebase before committing so a compensation run cannot overwrite primary-run state. Use the manual input values only for `workflow_dispatch`; scheduled runs use `config/settings.yaml` and always send.

- [ ] **Step 6: Write operator documentation**

Document: create a private repository; configure the five Secrets; register a personal-account Microsoft application; run `scripts/setup_outlook.py`; commit only the encrypted cache; run offline CI; manually run preview with `send=false`; manually run the first real email; read current and projected cost; switch `ai_mode`; enable schedules; diagnose failed sources; revoke Microsoft consent; rotate the encryption key; and rerun compensation safely.

- [ ] **Step 7: Run complete offline verification**

Run: `python -m ruff check .`  
Run: `python -m pytest -m "not live" --cov=ai_daily --cov-report=term-missing`  
Run: `ai-daily preview --ai-mode off`  
Expected: lint PASS, tests PASS, and preview files generated with no network secrets and no mail send.

- [ ] **Step 8: Run controlled live acceptance checks**

With repository Secrets configured, run these manual workflows in order:

1. `validate-sources --minimum-success 80`; expect at least 80% enabled sources successful and individual failures listed.
2. `preview --ai-mode full`; expect no email, two or fewer AI calls per non-empty section, and a cost report.
3. `run --send --ai-mode full`; expect Graph HTTP 202, one Outlook email, Markdown archive, snapshot, sent marker and encrypted cache update.
4. Repeat `run --send` without force; expect a clean already-sent exit and no second email.
5. Run `preview --ai-mode economy` and `preview --ai-mode off`; expect lower or zero Token usage and valid output.

- [ ] **Step 9: Confirm secrets and repository cleanliness**

Implement `scripts/check_secrets.py` to read the five configured secret values from the process environment, obtain tracked paths from `git ls-files -z`, compare raw file bytes against each non-empty secret value, and print only the secret variable name and matched path before returning nonzero. It must never print a secret value. Run `python scripts/check_secrets.py`. Verify `git status --short` contains only expected snapshot/digest changes, `data/microsoft-token.enc` is non-plaintext, and no workflow log contains full email addresses or OAuth fields.

- [ ] **Step 10: Commit**

```bash
git add src/ai_daily/cli.py .github/workflows tests/test_cli.py README.md config/pricing.yaml
git commit -m "feat: automate and document the AI daily workflow"
```

---

## Final Verification Checklist

- [ ] `python -m ruff check .` passes.
- [ ] `python -m pytest -m "not live" --cov=ai_daily --cov-report=term-missing` passes.
- [ ] Every enabled source has a fixture test; opt-in live validation reports at least 80% success.
- [ ] Manual full-mode preview reports actual input, cache-hit, cache-miss, and output Token counts.
- [ ] Economy mode costs less than or equal to full mode on the same fixture set.
- [ ] Off mode makes zero model calls and still renders all three formats.
- [ ] A real Graph smoke test returns HTTP 202 and produces exactly one Outlook email.
- [ ] Repeating a same-day scheduled send produces no duplicate email.
- [ ] A simulated DeepSeek failure still produces a template digest.
- [ ] A simulated individual source failure does not fail the run.
- [ ] GitHub days 1-7 are labeled trial; a fixture with a seven-day baseline is not.
- [ ] Snapshot pruning keeps 35 days and removes older files.
- [ ] No plaintext API key, OAuth token, encryption key, Outlook password, or full recipient address exists in committed files or logs.
- [ ] The first live run's cost report has been reviewed before enabling the schedule.

## Recommended Execution Order

Execute Tasks 1-10 sequentially. Each task has a contract consumed by later tasks, and each commit is a review checkpoint. Do not configure live Outlook authorization or enable scheduled sending until all offline tests through Task 10 Step 7 pass.
