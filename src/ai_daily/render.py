"""Render digest data into Outlook-safe HTML plus text and Markdown archives."""

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ai_daily.cost import CostReport
from ai_daily.models import Digest

_GITHUB_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]<>#()])")


@dataclass(frozen=True)
class RenderedDigest:
    subject: str
    html: str
    text: str
    markdown: str


def _safe_url(value: object) -> str | None:
    """Return only absolute HTTP(S) URLs suitable for an output link."""
    url = str(value).strip()
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return url


def _github_url(repository: object) -> str | None:
    repository_name = str(repository).strip()
    if not _GITHUB_REPOSITORY.fullmatch(repository_name):
        return None
    return f"https://github.com/{repository_name}"


def _markdown_text(value: object) -> str:
    """Keep untrusted values from becoming Markdown links or raw HTML."""
    return _MARKDOWN_SPECIAL.sub(r"\\\1", str(value))


def _environment(templates_dir: Path, *, autoescape: bool) -> Environment:
    environment = Environment(
        loader=FileSystemLoader(templates_dir),
        autoescape=autoescape,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.filters["safe_url"] = _safe_url
    environment.filters["github_url"] = _github_url
    environment.filters["markdown_text"] = _markdown_text
    return environment


def render_digest(
    digest: Digest, cost_report: CostReport, templates_dir: Path
) -> RenderedDigest:
    """Render all digest formats without trusting fetched or model-provided content."""
    context = {"digest": digest, "cost": cost_report}
    subject = (
        f"[AI Daily] {digest.local_date}｜{len(digest.news)} 条 AI 要闻 "
        f"+ GitHub 周榜 Top {len(digest.repositories)}"
    )
    html_environment = _environment(templates_dir, autoescape=True)
    text_environment = _environment(templates_dir, autoescape=False)
    return RenderedDigest(
        subject=subject,
        html=html_environment.get_template("daily.html.j2").render(context),
        text=text_environment.get_template("daily.txt.j2").render(context),
        markdown=text_environment.get_template("daily.md.j2").render(context),
    )
