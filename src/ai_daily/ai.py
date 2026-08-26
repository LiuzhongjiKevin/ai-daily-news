import json
from collections.abc import Callable
from typing import Any, Literal

from ai_daily.config import AppSettings
from ai_daily.models import NewsCluster, RankedRepo, UsageRecord

EnrichmentMode = Literal["full", "economy", "off"]


class AIEnrichmentError(RuntimeError):
    """Raised when AI output cannot safely be incorporated into a digest."""


def _value(source: object, name: str, default: object = None) -> object:
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def _non_empty_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not (text := value.strip()):
        raise AIEnrichmentError(f"{field} must be non-empty")
    return text


def _response_content(response: object) -> str:
    output_text = _value(response, "output_text")
    if isinstance(output_text, str):
        return output_text

    choices = _value(response, "choices")
    if not isinstance(choices, list) or not choices:
        raise AIEnrichmentError("response contains no JSON content")
    message = _value(choices[0], "message")
    content = _value(message, "content")
    if not isinstance(content, str):
        raise AIEnrichmentError("response contains no JSON content")
    return content


def _usage_record(stage: str, model: str, response: object) -> UsageRecord:
    usage = _value(response, "usage", {})
    if usage is None:
        usage = {}
    hit = _value(usage, "prompt_cache_hit_tokens")
    miss = _value(usage, "prompt_cache_miss_tokens")
    output = _value(usage, "completion_tokens")
    if hit is None:
        details = _value(usage, "input_tokens_details", {})
        hit = _value(details, "cached_tokens", 0)
    if miss is None:
        input_tokens = _value(usage, "input_tokens")
        miss = max(int(input_tokens or 0) - int(hit or 0), 0)
    if output is None:
        output = _value(usage, "output_tokens", 0)
    return UsageRecord(
        stage=stage,
        model=model,
        input_cache_hit_tokens=int(hit or 0),
        input_cache_miss_tokens=int(miss or 0),
        output_tokens=int(output or 0),
    )


class OffEnricher:
    """Preserves a digest without using a configured AI client."""

    def __init__(self, client: object | None = None) -> None:
        self.client = client

    def enrich(
        self,
        news: list[NewsCluster],
        repos: list[RankedRepo],
        mode: EnrichmentMode = "off",
    ) -> tuple[list[NewsCluster], list[RankedRepo], list[UsageRecord]]:
        if mode != "off":
            raise ValueError("OffEnricher only supports mode='off'")
        return news, repos, []


class AIEnricher:
    """Adds bounded, structured AI summaries using at most one call per digest section."""

    def __init__(self, client: object, settings: AppSettings) -> None:
        self.client = client
        self.settings = settings

    def enrich(
        self,
        news: list[NewsCluster],
        repos: list[RankedRepo],
        mode: EnrichmentMode,
    ) -> tuple[list[NewsCluster], list[RankedRepo], list[UsageRecord]]:
        if mode == "off":
            return news, repos, []
        if mode not in {"full", "economy"}:
            raise ValueError(f"Unsupported AI mode: {mode!r}")

        enriched_news = news
        enriched_repos = repos
        usage: list[UsageRecord] = []
        if news:
            enriched_news, news_usage = self._enrich_news(news, mode)
            usage.append(news_usage)
        if repos:
            enriched_repos, repo_usage = self._enrich_repositories(repos, mode)
            usage.append(repo_usage)
        return enriched_news, enriched_repos, usage

    def _enrich_news(
        self, news: list[NewsCluster], mode: EnrichmentMode
    ) -> tuple[list[NewsCluster], UsageRecord]:
        payload = [
            {
                "cluster_id": cluster.cluster_id,
                "title": cluster.title,
                "trust_grade": cluster.trust_grade,
                "excerpt": (cluster.items[0].excerpt if cluster.items else "")[:800],
            }
            for cluster in news
        ]
        prompt = (
            f"Mode: {mode}. Return JSON only with selected_cluster_ids (0-12 ids) and items. "
            "Each item must contain cluster_id, a non-empty summary, and a non-empty why_it_matters. "
            "Use only the supplied cluster ids. Do not invent source links.\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        response, usage = self._request_with_retry(
            "news", prompt, lambda parsed: self._validate_news(parsed, news)
        )
        selected_ids, items = self._validate_news_response(response)
        by_id = {cluster.cluster_id: cluster for cluster in news}
        return (
            [
                by_id[item["cluster_id"]].model_copy(
                    update={"summary": item["summary"], "why_it_matters": item["why_it_matters"]}
                )
                for item in items
                if item["cluster_id"] in selected_ids
            ],
            usage,
        )

    def _enrich_repositories(
        self, repos: list[RankedRepo], mode: EnrichmentMode
    ) -> tuple[list[RankedRepo], UsageRecord]:
        payload = [
            {
                "repository": repo.snapshot.repository,
                "description": repo.snapshot.description[:300],
                "stars_gained": repo.stars_gained,
                "is_trial": repo.is_trial,
            }
            for repo in repos
        ]
        prompt = (
            f"Mode: {mode}. Return JSON only with items. Each item must contain a supplied repository "
            "name and a non-empty explanation. Include each repository at most once.\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        response, usage = self._request_with_retry(
            "github", prompt, lambda parsed: self._validate_repositories(parsed, repos)
        )
        items = self._validate_repository_response(response)
        explanations = {item["repository"]: item["explanation"] for item in items}
        return (
            [repo.model_copy(update={"explanation": explanations.get(repo.snapshot.repository, repo.explanation)}) for repo in repos],
            usage,
        )

    def _request_with_retry(
        self,
        stage: str,
        prompt: str,
        validate: Callable[[dict[str, Any]], None],
    ) -> tuple[dict[str, Any], UsageRecord]:
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": "Return only valid JSON matching the requested schema.",
            },
            {"role": "user", "content": prompt},
        ]
        last_error = "invalid response"
        for attempt in range(2):
            try:
                response = self.client.chat.completions.create(
                    model=self.settings.ai_model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    max_tokens=self.settings.ai_max_output_tokens,
                )
            except Exception as error:
                raise AIEnrichmentError("AI enrichment request failed") from error
            try:
                parsed = json.loads(_response_content(response))
                if not isinstance(parsed, dict):
                    raise AIEnrichmentError("response JSON must be an object")
                validate(parsed)
            except (json.JSONDecodeError, AIEnrichmentError) as error:
                last_error = str(error)[:200]
                if attempt == 0:
                    messages.append(
                        {
                            "role": "user",
                            "content": f"Validation error: {last_error}. Return corrected JSON only.",
                        }
                    )
                    continue
                raise AIEnrichmentError(last_error) from error
            return parsed, _usage_record(stage, self.settings.ai_model, response)
        raise AIEnrichmentError(last_error)

    @staticmethod
    def _validate_news(payload: dict[str, Any], news: list[NewsCluster]) -> None:
        selected = payload.get("selected_cluster_ids")
        items = payload.get("items")
        if not isinstance(selected, list) or not all(isinstance(item, str) for item in selected):
            raise AIEnrichmentError("selected_cluster_ids must be a list of strings")
        if not 0 <= len(selected) <= 12 or len(selected) != len(set(selected)):
            raise AIEnrichmentError("selected_cluster_ids must contain 0-12 unique ids")
        available = {cluster.cluster_id for cluster in news}
        if unknown := set(selected) - available:
            raise AIEnrichmentError(f"unknown cluster id: {min(unknown)}")
        if not isinstance(items, list):
            raise AIEnrichmentError("items must be a list")
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                raise AIEnrichmentError("news item must be an object")
            cluster_id = item.get("cluster_id")
            if not isinstance(cluster_id, str) or cluster_id not in available:
                raise AIEnrichmentError("unknown cluster id")
            if cluster_id not in selected:
                raise AIEnrichmentError("news item is not selected")
            if cluster_id in seen:
                raise AIEnrichmentError("duplicate cluster id")
            _non_empty_text(item.get("summary"), "summary")
            _non_empty_text(item.get("why_it_matters"), "why_it_matters")
            seen.add(cluster_id)
        if seen != set(selected):
            raise AIEnrichmentError("each selected cluster requires one summary")

    @staticmethod
    def _validate_repositories(payload: dict[str, Any], repos: list[RankedRepo]) -> None:
        items = payload.get("items")
        if not isinstance(items, list):
            raise AIEnrichmentError("items must be a list")
        available = {repo.snapshot.repository for repo in repos}
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                raise AIEnrichmentError("repository item must be an object")
            repository = item.get("repository")
            if not isinstance(repository, str) or repository not in available:
                raise AIEnrichmentError("unknown repository")
            if repository in seen:
                raise AIEnrichmentError("duplicate repository")
            _non_empty_text(item.get("explanation"), "explanation")
            seen.add(repository)

    @staticmethod
    def _validate_news_response(payload: dict[str, Any]) -> tuple[set[str], list[dict[str, str]]]:
        selected = set(payload["selected_cluster_ids"])
        return selected, [
            {
                "cluster_id": item["cluster_id"],
                "summary": item["summary"].strip(),
                "why_it_matters": item["why_it_matters"].strip(),
            }
            for item in payload["items"]
        ]

    @staticmethod
    def _validate_repository_response(payload: dict[str, Any]) -> list[dict[str, str]]:
        return [
            {"repository": item["repository"], "explanation": item["explanation"].strip()}
            for item in payload["items"]
        ]
