import json
import re
import unicodedata
from collections.abc import Callable, Mapping
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
_MARKDOWN_URL = re.compile(
    r"!?\[([^\]\r\n]*)\]\(\s*(?:https?://|www\.)[^)\r\n]*\)",
    re.IGNORECASE,
)
_AUTOLINK_URL = re.compile(r"<\s*(?:https?://|www\.)[^>\r\n]*>", re.IGNORECASE)
_BARE_URL = re.compile(r"(?<![\w@])(?:https?://|www\.)[^\s<>\[\]{}()]+", re.IGNORECASE)
_MISSING = object()
_INVALID = object()


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


def _safe_field(source: object, name: str) -> object:
    try:
        if isinstance(source, Mapping):
            return source.get(name, _MISSING)
        return getattr(source, name, _MISSING)
    except Exception:  # noqa: BLE001 - provider objects may raise from attribute access.
        return _INVALID


def _token_value(raw: object) -> object:
    if raw is _MISSING or raw is None:
        return _MISSING
    if raw is _INVALID or isinstance(raw, bool):
        return _INVALID
    if type(raw) is int:
        return raw if raw >= 0 else _INVALID
    if type(raw) is str:
        normalized = str.strip(raw)
        if len(normalized) <= 20 and re.fullmatch(r"[0-9]+", normalized):
            return int(normalized)
    return _INVALID


def _token_field(source: object, name: str) -> object:
    return _token_value(_safe_field(source, name))


def _nested_token_field(source: object, container_name: str, field_name: str) -> object:
    container = _safe_field(source, container_name)
    if container is _MISSING:
        return _MISSING
    if container is _INVALID:
        return _INVALID
    try:
        is_object = isinstance(container, Mapping) or hasattr(container, "__dict__")
    except Exception:  # noqa: BLE001 - provider objects may raise during shape inspection.
        return _INVALID
    if not is_object:
        return _INVALID
    return _token_field(container, field_name)


def _usage_record(stage: str, model: str, response: object) -> UsageRecord:
    usage = _safe_field(response, "usage")
    if usage is _MISSING or usage is None or usage is _INVALID:
        return UsageRecord(stage=stage, model=model, is_complete=False)

    hit_value = _token_field(usage, "prompt_cache_hit_tokens")
    if hit_value is _MISSING:
        hit_value = _nested_token_field(usage, "prompt_tokens_details", "cached_tokens")
    if hit_value is _MISSING:
        hit_value = _nested_token_field(usage, "input_tokens_details", "cached_tokens")
    hit_invalid = hit_value is _INVALID
    hit = int(hit_value) if isinstance(hit_value, int) else 0

    miss_value = _token_field(usage, "prompt_cache_miss_tokens")
    input_known = False
    miss = 0
    malformed = hit_invalid or miss_value is _INVALID
    if isinstance(miss_value, int):
        miss = miss_value
        input_known = True
    elif miss_value is _MISSING:
        total_value = _token_field(usage, "input_tokens")
        if total_value is _MISSING:
            total_value = _token_field(usage, "prompt_tokens")
        malformed = malformed or total_value is _INVALID
        if isinstance(total_value, int) and not hit_invalid and hit <= total_value:
            miss = total_value - hit
            input_known = True
        elif isinstance(total_value, int) and hit > total_value:
            malformed = True

    output_value = _token_field(usage, "completion_tokens")
    if output_value is _MISSING:
        output_value = _token_field(usage, "output_tokens")
    output_known = isinstance(output_value, int)
    malformed = malformed or output_value is _INVALID
    output = int(output_value) if output_known else 0

    return UsageRecord(
        stage=stage,
        model=model,
        input_cache_hit_tokens=hit,
        input_cache_miss_tokens=miss,
        output_tokens=output,
        is_complete=not malformed and input_known and output_known,
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
        is_complete=all(record.is_complete for record in records),
    )


def _safe_prompt_text(value: object, limit: int | None = None) -> str:
    text = unicodedata.normalize("NFKC", str(value))
    text = "".join(" " if unicodedata.category(character).startswith("C") else character for character in text)
    text = _MARKDOWN_URL.sub(r"\1", text)
    text = _AUTOLINK_URL.sub("", text)
    text = _BARE_URL.sub("", text)
    text = " ".join(text.split())
    return text[:limit] if limit is not None else text


def _fair_excerpts(items: list[object], budget: int = 800) -> list[str]:
    excerpts = [_safe_prompt_text(_value(item, "excerpt", "")) for item in items]
    populated = sum(bool(excerpt) for excerpt in excerpts)
    if populated == 0:
        return excerpts
    base_quota, remainder = divmod(budget, populated)
    result: list[str] = []
    populated_index = 0
    for excerpt in excerpts:
        if not excerpt:
            result.append("")
            continue
        quota = base_quota + (1 if populated_index < remainder else 0)
        result.append(excerpt[:quota])
        populated_index += 1
    return result


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
            evidence = []
            excerpts = _fair_excerpts(cluster.items)
            for item, excerpt in zip(cluster.items, excerpts, strict=True):
                evidence.append(
                    {
                        "title": _safe_prompt_text(item.title, 240),
                        "source_name": _safe_prompt_text(item.source_name, 120),
                        "published_at": item.published_at.date().isoformat(),
                        "source_type": item.source_type,
                        "category": _safe_prompt_text(item.category, 80),
                        "excerpt": excerpt,
                    }
                )
            payload.append(
                {
                    "cluster_id": cluster.cluster_id,
                    "title": _safe_prompt_text(cluster.title, 240),
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
        repository_labels: list[str] = []
        used_labels: set[str] = set()
        for index, repo in enumerate(repos, start=1):
            label = _safe_prompt_text(repo.snapshot.repository, 180) or f"repository-{index}"
            if label in used_labels:
                label = f"{label} #{index}"
            repository_labels.append(label)
            used_labels.add(label)
        payload = [
            {
                "repository": label,
                "description": _safe_prompt_text(repo.snapshot.description, 300),
                "stars_gained": repo.stars_gained,
                "is_trial": repo.is_trial,
            }
            for repo, label in zip(repos, repository_labels, strict=True)
        ]
        prompt = (
            f"Mode: {mode}. Write every explanation in Simplified Chinese (简体中文). Return JSON only "
            "with items. Each item must contain a supplied repository name and a non-empty explanation. "
            "Include every supplied repository exactly once.\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        response, usage = self._request_with_retry(
            "github",
            prompt,
            lambda parsed: self._validate_repositories(parsed, repository_labels),
        )
        items = self._validate_repository_response(response)
        explanations = {item["repository"]: item["explanation"] for item in items}
        return (
            [
                repo.model_copy(
                    update={"explanation": explanations.get(label, repo.explanation)}
                )
                for repo, label in zip(repos, repository_labels, strict=True)
            ],
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
            if not usage_records[-1].is_complete:
                raise AIEnrichmentError(
                    "AI usage data is invalid",
                    usage=[_aggregate_usage(usage_records)],
                )
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
    def _validate_repositories(
        payload: dict[str, Any], repository_names: list[str]
    ) -> None:
        items = payload.get("items")
        if not isinstance(items, list):
            raise AIEnrichmentError("items must be a list")
        available = set(repository_names)
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
