from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_daily.ai import AIEnricher, AIEnrichmentError, OffEnricher
from ai_daily.config import AppSettings
from ai_daily.models import NewsCluster, RankedRepo, RawItem, RepoSnapshot

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 8, 24, 9, tzinfo=UTC)


def news_cluster(cluster_id: str = "cluster-one", *, excerpt: str = "release details") -> NewsCluster:
    return NewsCluster(
        cluster_id=cluster_id,
        title="Acme releases a model",
        trust_grade="A",
        items=[
            RawItem(
                source_id="acme",
                source_name="Acme",
                source_type="official",
                title="Acme releases a model",
                published_at=NOW,
                canonical_url="https://private.example.test/full-article",
                excerpt=excerpt,
            )
        ],
    )


def ranked_repo(name: str = "acme/widget", *, description: str = "A useful widget") -> RankedRepo:
    return RankedRepo(
        snapshot=RepoSnapshot(
            repository=name,
            description=description,
            primary_language="Python",
            stars=100,
            forks=10,
            updated_at=NOW,
            collected_at=NOW,
        ),
        stars_gained=20,
        is_trial=False,
    )


def fixture_response(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FixtureClient:
    """Chat Completions-shaped client with recorded offline responses."""

    def __init__(self, responses: list[object]) -> None:
        self.responses = iter(responses)
        self.calls: list[dict[str, object]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class ExplodingClient:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **_: object) -> object:
        raise AssertionError("the client must not be called in off mode")


def chat_response(content: str, *, usage: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": usage or {},
    }


def settings() -> AppSettings:
    return AppSettings(ai_model="deepseek-v4-flash", ai_max_output_tokens=256)


def test_off_mode_never_calls_client_and_leaves_inputs_unchanged() -> None:
    """Would catch the off switch still constructing an AI request or mutating digest data."""
    original_news = [news_cluster()]
    original_repos = [ranked_repo()]

    news, repos, usage = OffEnricher(ExplodingClient()).enrich(original_news, original_repos, mode="off")

    assert news == original_news
    assert repos == original_repos
    assert usage == []


@pytest.mark.parametrize("mode", ["full", "economy"])
def test_non_empty_modes_make_one_batched_call_per_section(mode: str) -> None:
    """Would catch per-item model requests that make trial costs unbounded."""
    client = FixtureClient(
        [
            chat_response(
                fixture_response("ai_news_response.json"),
                usage={
                    "prompt_cache_hit_tokens": 12,
                    "prompt_cache_miss_tokens": 34,
                    "completion_tokens": 56,
                },
            ),
            chat_response(fixture_response("ai_repos_response.json")),
        ]
    )

    news, repos, usage = AIEnricher(client, settings()).enrich([news_cluster()], [ranked_repo()], mode=mode)

    assert len(client.calls) == 2
    assert news[0].summary == "A concise model-launch summary."
    assert news[0].why_it_matters == "It changes the available capabilities."
    assert repos[0].explanation == "Fast growth reflects practical developer demand."
    assert [record.model_dump() for record in usage] == [
        {
            "stage": "news",
            "model": "deepseek-v4-flash",
            "input_cache_hit_tokens": 12,
            "input_cache_miss_tokens": 34,
            "output_tokens": 56,
        },
        {
            "stage": "github",
            "model": "deepseek-v4-flash",
            "input_cache_hit_tokens": 0,
            "input_cache_miss_tokens": 0,
            "output_tokens": 0,
        },
    ]


def test_empty_sections_are_not_sent_to_the_model() -> None:
    """Would catch empty daily digests consuming a model request."""
    client = FixtureClient([chat_response(fixture_response("ai_repos_response.json"))])

    news, repos, usage = AIEnricher(client, settings()).enrich([], [ranked_repo()], mode="full")

    assert news == []
    assert repos[0].explanation
    assert len(client.calls) == 1
    assert [record.stage for record in usage] == ["github"]


def test_prompts_bound_excerpts_descriptions_output_and_exclude_urls() -> None:
    """Would catch source URLs or unbounded article text reaching the external model."""
    long_excerpt = "n" * 801
    long_description = "r" * 301
    client = FixtureClient(
        [
            chat_response(fixture_response("ai_news_response.json")),
            chat_response(fixture_response("ai_repos_response.json")),
        ]
    )

    AIEnricher(client, settings()).enrich(
        [news_cluster(excerpt=long_excerpt)],
        [ranked_repo(description=long_description)],
        mode="full",
    )

    request_text = "\n".join(
        str(message["content"])
        for call in client.calls
        for message in call["messages"]  # type: ignore[index]
    )
    assert "https://private.example.test/full-article" not in request_text
    assert long_excerpt not in request_text
    assert long_description not in request_text
    assert "n" * 800 in request_text
    assert "r" * 300 in request_text
    assert all(call["max_tokens"] == 256 for call in client.calls)


def test_retries_once_after_malformed_json_with_compact_validation_error() -> None:
    """Would catch malformed output being accepted or retried indefinitely."""
    client = FixtureClient(
        [
            chat_response("not-json"),
            chat_response(fixture_response("ai_news_response.json")),
        ]
    )

    news, _, usage = AIEnricher(client, settings()).enrich([news_cluster()], [], mode="full")

    assert news[0].summary
    assert len(client.calls) == 2
    retry_messages = client.calls[1]["messages"]  # type: ignore[index]
    assert "validation" in str(retry_messages[-1]["content"]).lower()
    assert len(str(retry_messages[-1]["content"])) <= 300
    assert [record.stage for record in usage] == ["news"]


@pytest.mark.parametrize(
    ("invalid_payload", "match"),
    [
        ('{"selected_cluster_ids":["unknown"],"items":[]}', "unknown cluster"),
        (
            (
                '{"items":[{"repository":"acme/widget","explanation":"first"},'
                '{"repository":"acme/widget","explanation":"second"}]}'
            ),
            "duplicate repository",
        ),
        ('{"items":[{"repository":"acme/widget","explanation":""}]}', "non-empty"),
    ],
)
def test_rejects_semantically_invalid_output_after_one_retry(invalid_payload: str, match: str) -> None:
    """Would catch model output that references unknown or ambiguous digest entries."""
    is_news_payload = "selected_cluster_ids" in invalid_payload
    client = FixtureClient([chat_response(invalid_payload), chat_response(invalid_payload)])

    with pytest.raises(AIEnrichmentError, match=match):
        AIEnricher(client, settings()).enrich(
            [news_cluster()] if is_news_payload else [],
            [] if is_news_payload else [ranked_repo()],
            mode="full",
        )

    assert len(client.calls) == 2


def test_wraps_api_failures_without_exposing_provider_message() -> None:
    """Would catch provider exceptions escaping with credentials or implementation details."""
    client = FixtureClient([RuntimeError("api key sk-secret must stay private")])

    with pytest.raises(AIEnrichmentError, match="request failed") as error:
        AIEnricher(client, settings()).enrich([news_cluster()], [], mode="full")

    assert "sk-secret" not in str(error.value)
    assert len(client.calls) == 1


def test_maps_responses_style_usage_fields() -> None:
    """Would catch Responses API cached tokens being omitted from cost accounting."""
    client = FixtureClient(
        [
            {
                "output_text": fixture_response("ai_news_response.json"),
                "usage": {
                    "input_tokens": 9,
                    "input_tokens_details": {"cached_tokens": 5},
                    "output_tokens": 3,
                },
            }
        ]
    )

    _, _, usage = AIEnricher(client, settings()).enrich([news_cluster()], [], mode="economy")

    assert usage[0].model == "deepseek-v4-flash"
    assert usage[0].input_cache_hit_tokens == 5
    assert usage[0].input_cache_miss_tokens == 4
    assert usage[0].output_tokens == 3
