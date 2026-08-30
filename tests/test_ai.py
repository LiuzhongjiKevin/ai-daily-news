import json
import traceback
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
        "usage": usage if usage is not None else {"prompt_tokens": 0, "completion_tokens": 0},
    }


def settings() -> AppSettings:
    return AppSettings(ai_model="deepseek-v4-flash", ai_max_output_tokens=256)


def test_off_mode_never_calls_client_and_populates_useful_deterministic_chinese() -> None:
    """Would catch off/fallback output being empty, non-Chinese scaffolding, or mutating inputs."""
    original_news = [news_cluster()]
    original_repos = [ranked_repo()]

    news, repos, usage = OffEnricher(ExplodingClient()).enrich(original_news, original_repos, mode="off")

    assert original_news[0].summary == ""
    assert original_news[0].why_it_matters == ""
    assert original_repos[0].explanation == ""
    assert news[0].summary.startswith("事件概述：")
    assert "可信度为 A 级" in news[0].why_it_matters
    assert repos[0].explanation.startswith("项目概览：")
    assert "近七日新增 20 Star" in repos[0].explanation
    assert len(news[0].summary) <= 500
    assert len(repos[0].explanation) <= 500
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
            "call_count": 1,
            "input_cache_hit_tokens": 12,
            "input_cache_miss_tokens": 34,
            "output_tokens": 56,
            "is_complete": True,
        },
        {
            "stage": "github",
            "model": "deepseek-v4-flash",
            "call_count": 1,
            "input_cache_hit_tokens": 0,
            "input_cache_miss_tokens": 0,
            "output_tokens": 0,
            "is_complete": True,
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


def test_economy_mode_repairs_partial_news_and_summarizes_every_rule_selected_item() -> None:
    """Would catch economy mode accepting a partial summary set for rule-selected news."""
    first = news_cluster("cluster-one")
    second = news_cluster("cluster-two")
    complete = json.dumps(
        {
            "selected_cluster_ids": ["cluster-one", "cluster-two"],
            "items": [
                {
                    "cluster_id": "cluster-one",
                    "summary": "第一条摘要",
                    "why_it_matters": "第一条价值",
                },
                {
                    "cluster_id": "cluster-two",
                    "summary": "第二条摘要",
                    "why_it_matters": "第二条价值",
                },
            ],
        },
        ensure_ascii=False,
    )
    client = FixtureClient(
        [chat_response(fixture_response("ai_news_response.json")), chat_response(complete)]
    )

    news, _, _ = AIEnricher(client, settings()).enrich([first, second], [], mode="economy")

    assert [cluster.cluster_id for cluster in news] == ["cluster-one", "cluster-two"]
    assert [cluster.summary for cluster in news] == ["第一条摘要", "第二条摘要"]
    assert len(client.calls) == 2


def test_full_mode_explicitly_uses_ai_news_selection() -> None:
    """Would catch full mode preserving all candidates after AI selects the final subset."""
    client = FixtureClient([chat_response(fixture_response("ai_news_response.json"))])

    news, _, _ = AIEnricher(client, settings()).enrich(
        [news_cluster("cluster-one"), news_cluster("cluster-two")], [], mode="full"
    )

    assert [cluster.cluster_id for cluster in news] == ["cluster-one"]


def test_prompts_bound_excerpts_descriptions_output_and_exclude_urls() -> None:
    """Would catch omitted source evidence, URLs, or per-cluster excerpt overflow in prompts."""
    long_excerpt = "n" * 600
    conflicting_excerpt = "c" * 600
    long_description = "r" * 301
    cluster = news_cluster(excerpt=long_excerpt)
    cluster.items.append(
        RawItem(
            source_id="coverage",
            source_name="Coverage Desk",
            source_type="media",
            title="Coverage disputes one launch detail",
            published_at=datetime(2026, 8, 24, 8, tzinfo=UTC),
            canonical_url="https://other.private.test/full-body",
            excerpt=conflicting_excerpt,
            category="security",
        )
    )
    client = FixtureClient(
        [
            chat_response(fixture_response("ai_news_response.json")),
            chat_response(fixture_response("ai_repos_response.json")),
        ]
    )

    AIEnricher(client, settings()).enrich(
        [cluster],
        [ranked_repo(description=long_description)],
        mode="full",
    )

    request_text = "\n".join(
        str(message["content"])
        for call in client.calls
        for message in call["messages"]  # type: ignore[index]
    )
    assert "https://private.example.test/full-article" not in request_text
    assert "https://other.private.test/full-body" not in request_text
    assert conflicting_excerpt not in request_text
    assert long_description not in request_text
    assert "r" * 300 in request_text
    assert "Simplified Chinese" in request_text
    assert "简体中文" in request_text
    assert "conflict" in request_text.casefold()
    assert all(call["max_tokens"] == 256 for call in client.calls)

    news_prompt = client.calls[0]["messages"][1]["content"]  # type: ignore[index]
    payload = json.loads(str(news_prompt).splitlines()[-1])
    evidence = payload[0]["evidence"]
    assert [(row["source_name"], row["published_at"], row["source_type"], row["category"]) for row in evidence] == [
        ("Acme", "2026-08-24", "official", "other"),
        ("Coverage Desk", "2026-08-24", "media", "security"),
    ]
    assert payload[0]["trust_grade"] == "A"
    assert sum(len(row["excerpt"]) for row in evidence) == 800


def test_news_prompt_fairly_preserves_titles_and_conflicting_later_evidence() -> None:
    """Would catch an early long excerpt starving later contradictory source evidence."""
    cluster = news_cluster(excerpt="first-account " * 100)
    cluster.items.append(
        RawItem(
            source_id="later-conflict",
            source_name="Later Evidence Desk",
            source_type="media",
            title="Later source disputes the release date",
            published_at=datetime(2026, 8, 24, 8, tzinfo=UTC),
            canonical_url="https://later.invalid/report",
            excerpt="later-conflict " * 30,
            category="model",
        )
    )
    client = FixtureClient(
        [chat_response(fixture_response("ai_news_response.json"))]
    )

    AIEnricher(client, settings()).enrich([cluster], [], mode="full")

    news_prompt = client.calls[0]["messages"][1]["content"]  # type: ignore[index]
    evidence = json.loads(str(news_prompt).splitlines()[-1])[0]["evidence"]
    assert [row.get("title") for row in evidence] == [
        "Acme releases a model",
        "Later source disputes the release date",
    ]
    assert evidence[1]["excerpt"].startswith("later-conflict")
    assert sum(len(row["excerpt"]) for row in evidence) <= 800


def test_prompts_strip_urls_and_controls_from_every_untrusted_field() -> None:
    """Would catch embedded links or controls leaking through non-canonical prompt fields."""
    cluster = news_cluster()
    cluster.title = "Useful cluster https://cluster-secret.invalid/path\x00"
    cluster.items[0].title = "[Useful headline](https://title-secret.invalid/article)\x01"
    cluster.items[0].source_name = "Useful Desk www.source-secret.invalid/about\r\n"
    cluster.items[0].excerpt = "Useful evidence <https://excerpt-secret.invalid/detail>\x07"
    repository = "useful/repo https://repo-secret.invalid/private"
    repo = ranked_repo(
        repository,
        description="Useful project [documentation](www.description-secret.invalid/docs)\x02",
    )
    repo_response = json.dumps(
        {"items": [{"repository": "useful/repo", "explanation": "项目说明"}]},
        ensure_ascii=False,
    )
    client = FixtureClient(
        [
            chat_response(fixture_response("ai_news_response.json")),
            chat_response(repo_response),
        ]
    )

    AIEnricher(client, settings()).enrich([cluster], [repo], mode="full")

    news_prompt = client.calls[0]["messages"][1]["content"]  # type: ignore[index]
    news_payload = json.loads(str(news_prompt).splitlines()[-1])[0]
    repo_prompt = client.calls[1]["messages"][1]["content"]  # type: ignore[index]
    repo_payload = json.loads(str(repo_prompt).splitlines()[-1])[0]
    untrusted_prompt_data = json.dumps(
        [news_payload, repo_payload], ensure_ascii=False
    )
    for host in (
        "cluster-secret.invalid",
        "title-secret.invalid",
        "source-secret.invalid",
        "excerpt-secret.invalid",
        "repo-secret.invalid",
        "description-secret.invalid",
    ):
        assert host not in untrusted_prompt_data
    assert news_payload["title"] == "Useful cluster"
    assert news_payload["evidence"][0]["title"] == "Useful headline"
    assert news_payload["evidence"][0]["source_name"] == "Useful Desk"
    assert news_payload["evidence"][0]["excerpt"] == "Useful evidence"
    assert repo_payload["repository"] == "useful/repo"
    assert repo_payload["description"] == "Useful project documentation"
    assert not any(ord(character) < 32 for character in untrusted_prompt_data)


@pytest.mark.parametrize("mode", ["full", "economy"])
def test_repository_enrichment_repairs_partial_output_and_explains_every_input(mode: str) -> None:
    """Would catch either AI mode accepting a partial GitHub Top-10 explanation list."""
    second = ranked_repo("other/tool")
    partial = fixture_response("ai_repos_response.json")
    complete = json.dumps(
        {
            "items": [
                {"repository": "acme/widget", "explanation": "第一项说明"},
                {"repository": "other/tool", "explanation": "第二项说明"},
            ]
        },
        ensure_ascii=False,
    )
    client = FixtureClient([chat_response(partial), chat_response(complete)])

    _, repos, _ = AIEnricher(client, settings()).enrich(
        [], [ranked_repo(), second], mode=mode
    )

    assert [repo.explanation for repo in repos] == ["第一项说明", "第二项说明"]
    assert len(client.calls) == 2


def test_empty_repository_output_is_invalid_after_single_repair() -> None:
    """Would catch an empty model response silently erasing every requested repository explanation."""
    empty = chat_response('{"items":[]}')
    client = FixtureClient([empty, empty])

    with pytest.raises(AIEnrichmentError, match="every input repository"):
        AIEnricher(client, settings()).enrich([], [ranked_repo()], mode="full")

    assert len(client.calls) == 2


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


def test_successful_repair_retry_aggregates_usage_from_both_provider_schemas() -> None:
    """Would catch the paid invalid attempt being omitted from the final stage usage record."""
    client = FixtureClient(
        [
            chat_response(
                "not-json",
                usage={
                    "prompt_cache_hit_tokens": 2,
                    "prompt_cache_miss_tokens": 3,
                    "completion_tokens": 5,
                },
            ),
            {
                "output_text": fixture_response("ai_news_response.json"),
                "usage": {
                    "input_tokens": 13,
                    "input_tokens_details": {"cached_tokens": 7},
                    "output_tokens": 11,
                },
            },
        ]
    )

    _, _, usage = AIEnricher(client, settings()).enrich([news_cluster()], [], mode="full")

    assert [record.model_dump() for record in usage] == [
        {
            "stage": "news",
            "model": "deepseek-v4-flash",
            "call_count": 2,
            "input_cache_hit_tokens": 9,
            "input_cache_miss_tokens": 9,
            "output_tokens": 16,
            "is_complete": True,
        }
    ]


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


def test_initial_transport_failure_counts_the_paid_attempt_as_incomplete() -> None:
    """Would catch a provider create attempt disappearing when no response usage is returned."""
    client = FixtureClient([RuntimeError("provider lost response with sk-secret")])

    with pytest.raises(AIEnrichmentError, match="request failed") as error:
        AIEnricher(client, settings()).enrich([news_cluster()], [], mode="full")

    assert [record.model_dump() for record in error.value.usage] == [
        {
            "stage": "news",
            "model": "deepseek-v4-flash",
            "call_count": 1,
            "input_cache_hit_tokens": 0,
            "input_cache_miss_tokens": 0,
            "output_tokens": 0,
            "is_complete": False,
        }
    ]
    assert "sk-secret" not in "".join(traceback.format_exception(error.value))


def test_api_failure_does_not_retain_secret_in_exception_chain_or_traceback() -> None:
    """Would catch provider secrets surviving in the wrapped exception context or traceback."""
    client = FixtureClient([RuntimeError("provider rejected sk-secret")])

    with pytest.raises(AIEnrichmentError) as error:
        AIEnricher(client, settings()).enrich([news_cluster()], [], mode="full")

    rendered = "".join(traceback.format_exception(error.value))
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert "sk-secret" not in rendered


def test_failed_repair_preserves_both_paid_attempts_without_provider_content() -> None:
    """Would catch invalid paid responses disappearing from fallback cost or leaking their text."""
    secret_payload = '{"selected_cluster_ids":["sk-secret"],"items":[]}'
    client = FixtureClient(
        [
            chat_response(
                secret_payload,
                usage={"prompt_tokens": 10, "completion_tokens": 2},
            ),
            chat_response(
                secret_payload,
                usage={"prompt_tokens": 20, "completion_tokens": 3},
            ),
        ]
    )

    with pytest.raises(AIEnrichmentError, match="unknown cluster") as error:
        AIEnricher(client, settings()).enrich([news_cluster()], [], mode="full")

    assert [record.model_dump() for record in error.value.usage] == [
        {
            "stage": "news",
            "model": "deepseek-v4-flash",
            "call_count": 2,
            "input_cache_hit_tokens": 0,
            "input_cache_miss_tokens": 30,
            "output_tokens": 5,
            "is_complete": True,
        }
    ]
    rendered = "".join(traceback.format_exception(error.value))
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert "sk-secret" not in rendered


def test_transport_failure_after_paid_invalid_response_preserves_usage() -> None:
    """Would catch a repair transport error erasing usage from the first paid response."""
    client = FixtureClient(
        [
            chat_response(
                "not-json",
                usage={"prompt_tokens": 13, "completion_tokens": 4},
            ),
            RuntimeError("provider failed with sk-secret"),
        ]
    )

    with pytest.raises(AIEnrichmentError, match="request failed") as error:
        AIEnricher(client, settings()).enrich([news_cluster()], [], mode="full")

    assert error.value.usage[0].call_count == 2
    assert error.value.usage[0].input_cache_miss_tokens == 13
    assert error.value.usage[0].output_tokens == 4
    assert error.value.usage[0].is_complete is False
    assert "sk-secret" not in "".join(traceback.format_exception(error.value))


def test_later_section_failure_carries_usage_from_the_successful_news_section() -> None:
    """Would catch a GitHub failure erasing the already-paid successful news call."""
    client = FixtureClient(
        [
            chat_response(
                fixture_response("ai_news_response.json"),
                usage={"prompt_tokens": 17, "completion_tokens": 5},
            ),
            RuntimeError("repository request failed"),
        ]
    )

    with pytest.raises(AIEnrichmentError) as error:
        AIEnricher(client, settings()).enrich([news_cluster()], [ranked_repo()], mode="full")

    assert [
        (record.stage, record.call_count, record.is_complete)
        for record in error.value.usage
    ] == [("news", 1, True), ("github", 1, False)]
    assert error.value.usage[0].input_cache_miss_tokens == 17


def test_malformed_later_usage_is_sanitized_and_preserves_prior_stage_and_retry_usage() -> None:
    """Would catch provider usage conversion leaking a value or erasing paid calls."""
    secret_usage = "usage-sk-secret-must-not-survive"
    client = FixtureClient(
        [
            chat_response(
                fixture_response("ai_news_response.json"),
                usage={"prompt_tokens": 17, "completion_tokens": 5},
            ),
            chat_response(
                "not-json",
                usage={"prompt_tokens": 3, "completion_tokens": 2},
            ),
            chat_response(
                fixture_response("ai_repos_response.json"),
                usage={"prompt_tokens": secret_usage, "completion_tokens": 7},
            ),
        ]
    )

    with pytest.raises(AIEnrichmentError, match="usage data is invalid") as error:
        AIEnricher(client, settings()).enrich(
            [news_cluster()], [ranked_repo()], mode="full"
        )

    assert [
        (
            record.stage,
            record.call_count,
            record.input_cache_miss_tokens,
            record.output_tokens,
            record.is_complete,
        )
        for record in error.value.usage
    ] == [
        ("news", 1, 17, 5, True),
        ("github", 2, 3, 9, False),
    ]
    rendered = "".join(traceback.format_exception(error.value))
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert secret_usage not in str(error.value)
    assert secret_usage not in rendered


def test_missing_usage_records_the_paid_call_as_incomplete() -> None:
    """Would catch a paid response with no usage being mislabeled as complete zero cost."""
    response_without_usage = {
        "choices": [
            {"message": {"content": fixture_response("ai_news_response.json")}}
        ]
    }
    client = FixtureClient([response_without_usage])

    with pytest.raises(AIEnrichmentError, match="usage data is invalid") as error:
        AIEnricher(client, settings()).enrich([news_cluster()], [], mode="full")

    assert len(error.value.usage) == 1
    assert error.value.usage[0].call_count == 1
    assert error.value.usage[0].is_complete is False


def test_hostile_usage_scalar_cannot_raise_or_leak_during_conversion() -> None:
    """Would catch provider string subclasses executing or leaking through token coercion."""
    secret_usage = "usage-sk-secret-from-strip"

    class HostileToken(str):
        def strip(self, *_: object, **__: object) -> str:
            raise ValueError(secret_usage)

    client = FixtureClient(
        [
            chat_response(
                fixture_response("ai_news_response.json"),
                usage={
                    "prompt_tokens": HostileToken("17"),
                    "completion_tokens": 5,
                },
            )
        ]
    )

    with pytest.raises(AIEnrichmentError, match="usage data is invalid") as error:
        AIEnricher(client, settings()).enrich([news_cluster()], [], mode="full")

    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert secret_usage not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize(
    "malformed_details",
    [
        "detail-sk-secret-string",
        ["detail-sk-secret-list"],
        True,
        17,
    ],
    ids=["string", "list", "bool", "number"],
)
def test_chat_usage_rejects_malformed_prompt_detail_containers(
    malformed_details: object,
) -> None:
    """Would catch a present non-object Chat usage detail being treated as absent."""
    client = FixtureClient(
        [
            chat_response(
                fixture_response("ai_news_response.json"),
                usage={
                    "prompt_tokens": 19,
                    "prompt_tokens_details": malformed_details,
                    "completion_tokens": 7,
                },
            )
        ]
    )

    with pytest.raises(AIEnrichmentError, match="usage data is invalid") as error:
        AIEnricher(client, settings()).enrich([news_cluster()], [], mode="full")

    assert error.value.usage[0].call_count == 1
    assert error.value.usage[0].output_tokens == 7
    assert error.value.usage[0].is_complete is False
    rendered = "".join(traceback.format_exception(error.value))
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert "detail-sk-secret" not in str(error.value)
    assert "detail-sk-secret" not in rendered


def test_responses_usage_rejects_malformed_input_detail_container() -> None:
    """Would catch a present non-object Responses usage detail being treated as absent."""
    secret_details = ["responses-detail-sk-secret"]
    client = FixtureClient(
        [
            {
                "output_text": fixture_response("ai_news_response.json"),
                "usage": {
                    "input_tokens": 9,
                    "input_tokens_details": secret_details,
                    "output_tokens": 3,
                },
            }
        ]
    )

    with pytest.raises(AIEnrichmentError, match="usage data is invalid") as error:
        AIEnricher(client, settings()).enrich([news_cluster()], [], mode="economy")

    assert error.value.usage[0].call_count == 1
    assert error.value.usage[0].output_tokens == 3
    assert error.value.usage[0].is_complete is False
    rendered = "".join(traceback.format_exception(error.value))
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert secret_details[0] not in rendered


@pytest.mark.parametrize(
    ("response_style", "detail_field", "malformed_details"),
    [
        ("chat", "prompt_tokens_details", "detail-sk-secret-chat-string"),
        ("chat", "prompt_tokens_details", ["detail-sk-secret-chat-list"]),
        ("chat", "prompt_tokens_details", True),
        ("chat", "prompt_tokens_details", 17),
        ("responses", "input_tokens_details", "detail-sk-secret-responses-string"),
        ("responses", "input_tokens_details", ["detail-sk-secret-responses-list"]),
        ("responses", "input_tokens_details", True),
        ("responses", "input_tokens_details", 17),
    ],
)
@pytest.mark.parametrize("top_level_hit", [0, 4])
def test_top_level_cache_hit_does_not_bypass_malformed_detail_validation(
    response_style: str,
    detail_field: str,
    malformed_details: object,
    top_level_hit: int,
) -> None:
    """Would catch cache-hit precedence skipping validation of a present detail container."""
    usage: dict[str, object] = {
        "prompt_cache_hit_tokens": top_level_hit,
        detail_field: malformed_details,
    }
    if response_style == "chat":
        usage.update({"prompt_tokens": 19, "completion_tokens": 7})
        response = chat_response(fixture_response("ai_news_response.json"), usage=usage)
    else:
        usage.update({"input_tokens": 19, "output_tokens": 7})
        response = {
            "output_text": fixture_response("ai_news_response.json"),
            "usage": usage,
        }
    client = FixtureClient([response])

    with pytest.raises(AIEnrichmentError, match="usage data is invalid") as error:
        AIEnricher(client, settings()).enrich([news_cluster()], [], mode="full")

    record = error.value.usage[0]
    assert record.call_count == 1
    assert record.input_cache_hit_tokens == top_level_hit
    assert record.input_cache_miss_tokens == 19 - top_level_hit
    assert record.output_tokens == 7
    assert record.is_complete is False
    rendered = "".join(traceback.format_exception(error.value))
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert "detail-sk-secret" not in str(error.value)
    assert "detail-sk-secret" not in rendered


@pytest.mark.parametrize(
    "details",
    [
        {"cached_tokens": 4},
        SimpleNamespace(cached_tokens=4),
    ],
    ids=["mapping", "sdk-object"],
)
@pytest.mark.parametrize(
    ("response_style", "detail_field"),
    [
        ("chat", "prompt_tokens_details"),
        ("responses", "input_tokens_details"),
    ],
)
def test_top_level_cache_hit_preserves_valid_present_detail_objects(
    details: object, response_style: str, detail_field: str
) -> None:
    """Would catch independent detail validation rejecting supported object shapes."""
    usage_payload: dict[str, object] = {
        "prompt_cache_hit_tokens": 4,
        detail_field: details,
    }
    if response_style == "chat":
        usage_payload.update({"prompt_tokens": 19, "completion_tokens": 7})
        response = chat_response(
            fixture_response("ai_news_response.json"), usage=usage_payload
        )
    else:
        usage_payload.update({"input_tokens": 19, "output_tokens": 7})
        response = {
            "output_text": fixture_response("ai_news_response.json"),
            "usage": usage_payload,
        }
    client = FixtureClient([response])

    _, _, usage = AIEnricher(client, settings()).enrich(
        [news_cluster()], [], mode="full"
    )

    assert usage[0].input_cache_hit_tokens == 4
    assert usage[0].input_cache_miss_tokens == 15
    assert usage[0].output_tokens == 7
    assert usage[0].is_complete is True


def test_successful_news_usage_survives_malformed_github_detail_container() -> None:
    """Would catch a later detail-shape failure erasing known news usage or GitHub call count."""
    secret_details = ["github-detail-sk-secret"]
    client = FixtureClient(
        [
            chat_response(
                fixture_response("ai_news_response.json"),
                usage={"prompt_tokens": 17, "completion_tokens": 5},
            ),
            chat_response(
                fixture_response("ai_repos_response.json"),
                usage={
                    "prompt_cache_hit_tokens": 0,
                    "prompt_tokens": 23,
                    "prompt_tokens_details": secret_details,
                    "completion_tokens": 7,
                },
            ),
        ]
    )

    with pytest.raises(AIEnrichmentError, match="usage data is invalid") as error:
        AIEnricher(client, settings()).enrich(
            [news_cluster()], [ranked_repo()], mode="full"
        )

    assert [
        (record.stage, record.call_count, record.output_tokens, record.is_complete)
        for record in error.value.usage
    ] == [
        ("news", 1, 5, True),
        ("github", 1, 7, False),
    ]
    rendered = "".join(traceback.format_exception(error.value))
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert secret_details[0] not in rendered


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


def test_responses_usage_without_optional_details_remains_complete() -> None:
    """Would catch a truly absent optional Responses detail being marked malformed."""
    client = FixtureClient(
        [
            {
                "output_text": fixture_response("ai_news_response.json"),
                "usage": {"input_tokens": 9, "output_tokens": 3},
            }
        ]
    )

    _, _, usage = AIEnricher(client, settings()).enrich(
        [news_cluster()], [], mode="economy"
    )

    assert usage[0].input_cache_hit_tokens == 0
    assert usage[0].input_cache_miss_tokens == 9
    assert usage[0].output_tokens == 3
    assert usage[0].is_complete is True


def test_maps_conventional_chat_completion_prompt_tokens_as_cache_miss() -> None:
    """Would catch billable prompt tokens being reported as free when cache detail is absent."""
    client = FixtureClient(
        [
            chat_response(
                fixture_response("ai_news_response.json"),
                usage={"prompt_tokens": 19, "completion_tokens": 7},
            )
        ]
    )

    _, _, usage = AIEnricher(client, settings()).enrich([news_cluster()], [], mode="full")

    assert usage[0].input_cache_hit_tokens == 0
    assert usage[0].input_cache_miss_tokens == 19
    assert usage[0].output_tokens == 7
    assert usage[0].call_count == 1
    assert usage[0].is_complete is True


def test_maps_chat_completion_cached_prompt_detail() -> None:
    """Would catch conventional cached prompt tokens being charged entirely at cache-miss rates."""
    client = FixtureClient(
        [
            chat_response(
                fixture_response("ai_news_response.json"),
                usage={
                    "prompt_tokens": 23,
                    "prompt_tokens_details": {"cached_tokens": 11},
                    "completion_tokens": 3,
                },
            )
        ]
    )

    _, _, usage = AIEnricher(client, settings()).enrich([news_cluster()], [], mode="full")

    assert usage[0].input_cache_hit_tokens == 11
    assert usage[0].input_cache_miss_tokens == 12
