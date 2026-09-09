"""Rule-based world/finance selection and original-language digest rendering."""

import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from html import escape
from urllib.parse import urlsplit

from rapidfuzz.fuzz import ratio

from ai_daily.models import RawItem
from ai_daily.news import canonicalize_url
from ai_daily.pipeline import _timezone
from ai_daily.render import RenderedDigest, _markdown_text

TOPICS = {
    "world": (
        "war", "peace", "ceasefire", "diplomatic", "summit", "sanction", "trade", "tariff",
        "election", "president", "military", "conflict", "missile", "nuclear", "united nations",
        "humanitarian", "refugee", "treaty", "security council", "withdrawal", "strike",
    ),
    "finance": (
        "inflation", "interest rate", "central bank", "federal reserve", "ecb", "economy",
        "economic", "gdp", "employment", "unemployment", "jobs", "tariff", "trade", "oil",
        "energy", "bond", "yield", "currency", "dollar", "stock", "market", "recession",
        "bank", "monetary", "fiscal", "budget", "debt", "sanction", "gold", "export",
    ),
}
LABELS = {"world": "国际形势", "finance": "宏观经济与财经"}
OPINION = re.compile(r"\b(opinion|editorial|analysis)\b|\|\s*letters?\s*$", re.IGNORECASE)


def publisher(item: RawItem) -> str:
    # Source ids group separate sections from the same publisher.
    return item.source_id.split("-", 1)[0]


def select_news(items: list[RawItem], now: datetime) -> list[RawItem]:
    if now.tzinfo is None:
        raise ValueError("Selection clock must be timezone-aware")
    ranked = []
    for item in items:
        if item.category not in TOPICS or item.published_at.tzinfo is None:
            continue
        if not now - timedelta(hours=24) <= item.published_at <= now:
            continue
        url = urlsplit(str(item.canonical_url))
        if url.scheme not in {"http", "https"} or url.username or url.password:
            continue
        text = (item.title + " " + item.excerpt).casefold()
        hits = sum(bool(re.search(r"\b" + re.escape(word) + r"\b", text))
                   for word in TOPICS[item.category])
        if not hits:
            continue
        score = hits * 10 + (12 if item.source_type == "official" else 0)
        if OPINION.search(item.title):
            score -= 20
        ranked.append((score, item))
    ranked.sort(key=lambda pair: (-pair[0], -pair[1].published_at.timestamp(),
                                 str(pair[1].canonical_url)))
    candidates = []
    urls = set()
    titles = []
    for _, item in ranked:
        url = canonicalize_url(str(item.canonical_url))
        title = re.sub(r"\W+", " ", item.title.casefold()).strip()
        if url in urls or any(ratio(title, previous) >= 94 for previous in titles):
            continue
        candidates.append(item)
        urls.add(url)
        titles.append(title)
    selected = []
    # Round-robin publishers within each section before taking additional stories.
    for category in LABELS:
        pool = [item for item in candidates if item.category == category]
        counts = Counter()
        for _ in range(6):
            if not pool:
                break
            index = min(range(len(pool)), key=lambda n: (counts[publisher(pool[n])], n))
            item = pool.pop(index)
            selected.append(item)
            counts[publisher(item)] += 1
    return selected


def render_world(items: list[RawItem], now: datetime, diagnostics: list[dict]) -> RenderedDigest:
    local = now.astimezone(_timezone("Asia/Shanghai"))
    day = local.date().isoformat()
    subject = f"国际形势与财经日报 {day}"
    start = (local - timedelta(hours=24)).strftime("%m-%d %H:%M")
    end = local.strftime("%m-%d %H:%M")
    failed = sum(row["status"] == "failed" for row in diagnostics)
    note = (f"采集窗口：{start}—{end}（北京时间）。原文标题与短摘要，未自动翻译。"
            f"来源检查 {len(diagnostics)} 个，失败 {failed} 个。"
            "本版非全球全量覆盖；选稿优先级不是事实可信度或投资建议。")
    text = [subject, note]
    html = [f'<html><body><h1>{escape(subject)}</h1><p>{escape(note)}</p>']
    markdown = [f"# {subject}", note]
    for category, label in LABELS.items():
        rows = [item for item in items if item.category == category]
        text.append(f"\n{label}（{len(rows)} 条）")
        html.append(f"<h2>{label}（{len(rows)} 条）</h2>")
        markdown.append(f"\n## {label}")
        if not rows:
            message = "本时段未取得符合筛选条件的条目，不代表没有相关新闻。"
            text.append(message)
            html.append(f"<p>{message}</p>")
            markdown.append(message)
        for item in rows:
            stamp = item.published_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
            source = "官方公告" if item.source_type == "official" else "媒体报道"
            if OPINION.search(item.title):
                source = "评论/分析"
            meta = f"{item.source_name} · {source} · {stamp}"
            excerpt = item.excerpt[:220].strip()
            url = canonicalize_url(str(item.canonical_url))
            text.extend([item.title, meta, excerpt, url, ""])
            html.append(f'<h3><a href="{escape(url, quote=True)}">{escape(item.title)}</a></h3>'
                        f'<p>{escape(meta)}</p><p>{escape(excerpt)}</p>')
            markdown.extend([f"### {_markdown_text(item.title)}", meta,
                             _markdown_text(excerpt), url, ""])
    html.append("</body></html>")
    return RenderedDigest(subject=subject, text="\n".join(text),
                          html="\n".join(html), markdown="\n".join(markdown))
