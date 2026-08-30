import json
from collections.abc import Callable
from typing import Any, Literal

from ai_daily.config import AppSettings
from ai_daily.models import NewsCluster, RankedRepo, UsageRecord

EnrichmentMode = Literal["full", "economy", "off"]
_CATEGORY_LABELS = {
    "model": "模型发布",
    "product": "产品更新",
    "api": "API 变更",
    "funding": "融资",
    "ipo": "上市",
    "acquisition": "并购",
    "regulation": "监管政策",
    "security": "安全事件",
    "research": "研究进展",
    "chips": "芯片与算力",
    "open_source": "开源动态",
    "other": "行业动态",
}


class AIEnrichmentError(RuntimeError):
    """Raised when AI output cannot safely be incorporated into a digest."""

    def __init__(self, message: str, *, usage: list[UsageRecord] | None = None) -> None:
        super().__init__(message)
        self.usage = list(usage or [])


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
        details = _value(usage, "prompt_tokens_details", {})
        hit = _value(details, "cached_tokens")
    if hit is None:
        details = _value(usage, "input_tokens_details", {})
        hit = _value(details, "cached_tokens", 0)
    if miss is None:
        input_tokens = _value(usage, "input_tokens")
        if input_tokens is None:
            input_tokens = _value(usage, "prompt_tokens", 0)
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


def _aggregate_usage(records: list[UsageRecord]) -> UsageRecord:
    first = records[0]
    return UsageRecord(
        stage=first.stage,
        model=first.model,
        call_count=sum(record.call_count for record in records),
        input_cache_hit_tokens=sum(record.input_cache_hit_tokens for record in records),
        input_cache_miss_tokens=sum(record.input_cache_miss_tokens for record in records),
        output_tokens=sum(record.output_tokens for record in records),
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
        enriched_news: list[NewsCluster] = []
        for cluster in news:
            title = cluster.title.strip()[:180]
            excerpt = next(
                (item.excerpt.strip()[:240] for item in cluster.items if item.excerpt.strip()),
                "",
            )
            category = cluster.items[0].category if cluster.items else "other"
            category_label = _CATEGORY_LABELS.get(category, _CATEGORY_LABELS["other"])
            summary = cluster.summary.strip() or f"事件概述：{title}。"
            if excerpt and excerpt.casefold() != title.casefold():
                summary = f"{summary} 来源摘要：{excerpt}"
            why_it_matters = cluster.why_it_matters.strip() or (
                f"关注理由：该事件属于{category_label}，由 {len(cluster.items)} 个来源支持，"
                f"可信度为 {cluster.trust_grade} 级。"
            )
            enriched_news.append(
                cluster.model_copy(
                    update={"summary": summary[:500], "why_it_matters": why_it_matters[:500]}
                )
            )

        enriched_repos: list[RankedRepo] = []
        for repo in repos:
            snapshot = repo.snapshot
            language = snapshot.primary_language or "未标注"
            description = snapshot.description.strip()[:220]
            explanation = repo.explanation.strip() or (
                f"项目概览：{snapshot.repository} 近七日新增 {repo.stars_gained} Star，"
                f"总 Star {snapshot.stars}，主要语言为 {language}。"
            )
            if description:
                explanation = f"{explanation} 项目简介：{description}"
            enriched_repos.append(
                repo.model_copy(update={"explanation": explanation[:500]})
            )
        return enriched_news, enriched_repos, []


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
            repository_failure: AIEnrichmentError | None = None
            try:
                enriched_repos, repo_usage = self._enrich_repositories(repos, mode)
            except AIEnrichmentError as error:
                repository_failure = AIEnrichmentError(
                    str(error), usage=[*usage, *error.usage]
                )
            if repository_failure is not None:
                raise repository_failure
            usage.append(repo_usage)
        return enriched_news, enriched_repos, usage

    def _enrich_news(
        self, news: list[NewsCluster], mode: EnrichmentMode
    ) -> tuple[list[NewsCluster], UsageRecord]:
        payload = []
        for cluster in news:
            remaining_excerpt = 800
            evidence = []
            for item in cluster.items:
                excerpt = item.excerpt.strip()[:remaining_excerpt]
                remaining_excerpt -= len(excerpt)
                evidence.append(
                    {
                        "source_name": item.source_name.strip()[:120],
                        "published_at": item.published_at.date().isoformat(),
                        "source_type": item.source_type,
                        "category": item.category,
                        "excerpt": excerpt,
                    }
                )
            payload.append(
                {
                    "cluster_id": cluster.cluster_id,
                    "title": cluster.title.strip()[:240],
                    "trust_grade": cluster.trust_grade,
                    "evidence": evidence,
                }
            )
        selection_contract = (
            "Select 0-12 supplied clusters and summarize every selected cluster."
            if mode == "full"
            else "Select and summarize every supplied cluster; omit none."
        )
        prompt = (
            f"Mode: {mode}. Write all summaries in Simplified Chinese (简体中文). "
            f"{selection_contract} Return JSON only with selected_cluster_ids and items. "
            "Each item must contain cluster_id, a non-empty summary, and a non-empty why_it_matters. "
            "Use only the supplied cluster ids. Surface conflicts or disagreements across the supplied "
            "source evidence instead of silently choosing one account. Do not invent source links.\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        response, usage = self._request_with_retry(
            "news", prompt, lambda parsed: self._validate_news(parsed, news, mode)
        )
        selected_ids, items = self._validate_news_response(response)
        summaries = {item["cluster_id"]: item for item in items}
        if mode == "economy":
            return (
                [
                    cluster.model_copy(
                        update={
                            "summary": summaries[cluster.cluster_id]["summary"],
                            "why_it_matters": summaries[cluster.cluster_id]["why_it_matters"],
                        }
                    )
                    if cluster.cluster_id in summaries
                    else cluster
                    for cluster in news
                ],
                usage,
            )
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
            f"Mode: {mode}. Write every explanation in Simplified Chinese (简体中文). Return JSON only "
            "with items. Each item must contain a supplied repository name and a non-empty explanation. "
            "Include every supplied repository exactly once.\n"
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
                "content": (
                    "Use concise Simplified Chinese (简体中文) and return only valid JSON matching "
                    "the requested schema."
                ),
            },
            {"role": "user", "content": prompt},
        ]
        last_error = "invalid response"
        usage_records: list[UsageRecord] = []
        for attempt in range(2):
            request_error: AIEnrichmentError | None = None
            try:
                response = self.client.chat.completions.create(
                    model=self.settings.ai_model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    max_tokens=self.settings.ai_max_output_tokens,
                )
            except Exception as error:  # noqa: BLE001 - client implementations expose no shared error base.
                accumulated = [_aggregate_usage(usage_records)] if usage_records else []
                request_error = AIEnrichmentError(
                    "AI enrichment request failed", usage=accumulated
                )
                del error
            if request_error is not None:
                raise request_error
            usage_records.append(_usage_record(stage, self.settings.ai_model, response))
            validation_failure: AIEnrichmentError | None = None
            try:
                parsed = json.loads(_response_content(response))
                if not isinstance(parsed, dict):
                    raise AIEnrichmentError("response JSON must be an object")
                validate(parsed)
            except (json.JSONDecodeError, AIEnrichmentError) as error:
                last_error = (
                    "response is not valid JSON"
                    if isinstance(error, json.JSONDecodeError)
                    else str(error)[:200]
                )
                if attempt == 0:
                    messages.append(
                        {
                            "role": "user",
                            "content": f"Validation error: {last_error}. Return corrected JSON only.",
                        }
                    )
                    continue
                validation_failure = AIEnrichmentError(
                    last_error, usage=[_aggregate_usage(usage_records)]
                )
            if validation_failure is not None:
                raise validation_failure
            return parsed, _aggregate_usage(usage_records)
        raise AIEnrichmentError(
            last_error,
            usage=[_aggregate_usage(usage_records)] if usage_records else [],
        )

    @staticmethod
    def _validate_news(
        payload: dict[str, Any], news: list[NewsCluster], mode: EnrichmentMode
    ) -> None:
        selected = payload.get("selected_cluster_ids")
        items = payload.get("items")
        if not isinstance(selected, list) or not all(isinstance(item, str) for item in selected):
            raise AIEnrichmentError("selected_cluster_ids must be a list of strings")
        if not 0 <= len(selected) <= 12 or len(selected) != len(set(selected)):
            raise AIEnrichmentError("selected_cluster_ids must contain 0-12 unique ids")
        available = {cluster.cluster_id for cluster in news}
        if unknown := set(selected) - available:
            del unknown
            raise AIEnrichmentError("unknown cluster id")
        if mode == "economy" and set(selected) != available:
            raise AIEnrichmentError("economy mode requires every input cluster")
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
        if seen != available:
            raise AIEnrichmentError("every input repository requires one explanation")

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
