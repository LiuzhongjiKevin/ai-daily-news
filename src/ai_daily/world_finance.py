"""Independent world/finance preview and explicitly authorized daily delivery."""

import argparse
import base64
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx

from ai_daily.collectors.base import load_sources
from ai_daily.collectors.feed import FeedCollector
from ai_daily.http import RetryingClient
from ai_daily.mail import QQMailer
from ai_daily.pipeline import _timezone
from ai_daily.state import StateStore
from ai_daily.world_alibaba import AlibabaTranslator
from ai_daily.world_news import publisher, render_world, select_news
from ai_daily.world_translation_budget import (
    BudgetUnavailable,
    make_manifest,
    reserve,
    validate_history,
)

BRANCH = "world-finance-daily"


def translation_now():
    return datetime.now(UTC)


def _git_environment(authenticated=False):
    environment = {k: v for k, v in os.environ.items()
                   if not k.startswith(("GIT_CONFIG_", "GIT_TRACE", "GIT_CURL_VERBOSE"))}
    for name in ("WORLD_FINANCE_STATE_TOKEN", "SMTP_USERNAME", "SMTP_PASSWORD", "MAIL_TO",
                 "TENCENTCLOUD_SECRET_ID", "TENCENTCLOUD_SECRET_KEY", "GITHUB_TOKEN",
                 "AI_MEMORY_AUTH_TOKEN",
                 "ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET"):
        environment.pop(name, None)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    if authenticated:
        token = os.environ.get("WORLD_FINANCE_STATE_TOKEN", "")
        if not token or any(c.isspace() for c in token):
            raise RuntimeError("Dedicated state token is unavailable")
        encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
        environment.update(GIT_CONFIG_COUNT="1",
                           GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                           GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {encoded}")
    return environment


def _git(root, *args, authenticated=False):
    try:
        result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                                timeout=60, check=False, env=_git_environment(authenticated))
        if result.returncode:
            raise RuntimeError("World-finance checkpoint failed; do not auto-retry")
        return result.stdout.strip()
    except subprocess.SubprocessError:
        raise RuntimeError("World-finance checkpoint outcome unknown; do not auto-retry") from None


def _remote():
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise RuntimeError("Repository identity is unavailable")
    return f"https://github.com/{repository}.git"


def _remote_tip(root):
    rows = _git(root, "ls-remote", _remote(), f"refs/heads/{BRANCH}",
                authenticated=True).splitlines()
    if len(rows) != 1 or rows[0].split()[1:] != [f"refs/heads/{BRANCH}"]:
        raise RuntimeError("Remote state tip unavailable")
    return rows[0].split()[0]


def verify_budget_history(root):
    """Verify full append-only ledger lineage and a fresh remote tip before reserving.

    Anchor is an operator-reviewed initialization commit, never inferred from missing state.
    Administrative rewrites of the entire history require independent access protection.
    """
    try:
        verify_append_only_protection()
        anchor = os.environ.get("WORLD_TRANSLATION_LEDGER_ANCHOR", "")
        if not re.fullmatch(r"[0-9a-f]{40}", anchor):
            raise BudgetUnavailable("budget_anchor_missing")
        if _git(root, "rev-parse", "--is-shallow-repository") != "false":
            raise BudgetUnavailable("budget_history_shallow")
        if _git(root, "rev-parse", "HEAD") != _remote_tip(root):
            raise BudgetUnavailable("budget_stale_checkout")
        _git(root, "merge-base", "--is-ancestor", anchor, "HEAD")
        # Reject merges: one auditable linear ledger lineage, no hidden side histories.
        if _git(root, "rev-list", "--merges", f"{anchor}..HEAD"):
            raise BudgetUnavailable("budget_history_merge")
        commits = [anchor] + _git(root, "rev-list", "--reverse", f"{anchor}..HEAD",
                                 "--", "world-state/state.json").splitlines()
        snapshots = [json.loads(_git(root, "show", f"{sha}:world-state/state.json"))
                     .get("translation_budget") for sha in commits]
        validate_history(snapshots)
        if StateStore(root / "world-state").load_run_state().translation_budget != snapshots[-1]:
            raise BudgetUnavailable("budget_local_mismatch")
    except Exception:  # noqa: BLE001 - no git output or state content in logs
        raise BudgetUnavailable("budget_history_unverified") from None


def verify_append_only_protection(*, client=None):
    """Unverifiable/disabled GitHub protection means no paid API calls.

    A repository administrator can still remove protection; account compromise or
    deliberate removal of all safeguards is outside this program's cost guarantee.
    """
    from ai_daily.world_alibaba import http_debug_enabled
    if http_debug_enabled():
        raise BudgetUnavailable("budget_guard_debug")
    owned = client is None
    transport = client or httpx.Client(transport=httpx.HTTPTransport(retries=0),
                                      follow_redirects=False, trust_env=False, timeout=5)
    try:
        repository = _remote().removeprefix("https://github.com/").removesuffix(".git")
        token = os.environ.get("WORLD_FINANCE_STATE_TOKEN", "")
        if not token or any(c.isspace() for c in token):
            raise BudgetUnavailable("budget_guard_auth")
        response = transport.get(
            f"https://api.github.com/repos/{repository}/branches/{BRANCH}/protection",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json"},
            timeout=5, follow_redirects=False)
        response.raise_for_status()
        body = response.json()
        if (body.get("enforce_admins", {}).get("enabled") is not True
                or body.get("allow_force_pushes", {}).get("enabled") is not False
                or body.get("allow_deletions", {}).get("enabled") is not False):
            raise BudgetUnavailable("budget_guard_unprotected")
    except Exception:  # noqa: BLE001 - no headers, response bodies or provider messages
        raise BudgetUnavailable("budget_guard_unverified") from None
    finally:
        if owned:
            transport.close()


def collect(root, now):
    sources = load_sources(root / "config" / "world-sources.yaml")

    def read(source):
        client = RetryingClient(max_attempts=2)
        try:
            rows = FeedCollector(client).collect(source, now - timedelta(hours=24))
            rows = [item for item in rows if item.published_at <= now]
            return rows, {"source": source.id, "status": "ok", "count": len(rows)}
        except Exception as error:  # noqa: BLE001 - report source failure without response bodies
            return [], {"source": source.id, "status": "failed", "count": 0,
                        "error": type(error).__name__}
        finally:
            client._client.close()

    items, diagnostics = [], []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for rows, status in pool.map(read, sources):
            items.extend(rows)
            diagnostics.append(status)
    return select_news(items, now), diagnostics


def world_mailer():
    # Deliberately do not use from_environment(): the AI edition adds its sender to MAIL_TO.
    return QQMailer(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"],
                    os.environ["MAIL_TO"])


def deliver(root, items, now, diagnostics, mailer, checkpoint):
    if len({publisher(item) for item in items}) < 2:
        raise ValueError("At least two publishers with eligible stories are required")
    rendered = render_world(items, now, diagnostics)
    store = StateStore(root / "world-state")
    day = now.astimezone(_timezone("Asia/Shanghai")).date().isoformat()
    attempt = "world-" + uuid4().hex
    status = store.reserve_delivery(day, attempt, now)
    if status == "already_sent":
        return "already_sent"
    grant = None
    credentials = AlibabaTranslator.credentials()
    if credentials is not None:
        try:
            verify_budget_history(root)
            fields = [text for item in items if item.category in {"world", "finance"}
                      for text in (item.title, item.excerpt[:220].strip())]
            grant = reserve(store, make_manifest(fields), translation_now(),
                            os.environ.get("GITHUB_RUN_ID", ""),
                            os.environ.get("GITHUB_RUN_ATTEMPT", ""))
        except BudgetUnavailable:
            print("Alibaba translation: budget unavailable; keeping original text.")
    # A failed push leaves an unresolved reservation locally and MUST prevent SMTP.
    receipt = checkpoint()
    # Only spend translation quota once a new, durable send is authorized.
    if grant is not None:
        try:
            grant.confirm(receipt)
            with AlibabaTranslator(*credentials, grant) as translator:
                rendered = render_world(items, now, diagnostics, translator=translator)
        except Exception:  # noqa: BLE001 - original rendering remains usable
            print("Alibaba translation: unavailable; keeping original text.")
    with store.delivery_operation(day, attempt) as operation:
        operation.mark_ambiguous()
        try:
            message_id = mailer.send(rendered)
        except Exception:
            # Remote 'reserved' and local 'ambiguous' both block unattended retry.
            checkpoint()
            raise
        operation.complete(message_id, datetime.now(UTC))
    checkpoint()
    return "accepted"


def checkpoint_state(root):
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Durable delivery requires the dedicated GitHub workflow")
    if os.environ.get("WORLD_FINANCE_ENVIRONMENT") != "world-finance-production":
        raise RuntimeError("Dedicated world-finance environment is required")

    def git(*args, **kwargs):
        return _git(root, *args, **kwargs)

    if git("diff", "--cached", "--name-only"):
        raise RuntimeError("Refusing a checkpoint with unrelated staged changes")
    git("add", "--", "world-state/state.json")
    if git("diff", "--cached", "--name-only") != "world-state/state.json":
        raise RuntimeError("Expected exactly one isolated state change")
    git("-c", "user.name=github-actions[bot]", "-c",
        "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "commit", "-m", "chore: update world finance delivery state")
    commit = git("rev-parse", "HEAD")
    git("push", _remote(), f"HEAD:refs/heads/{BRANCH}", authenticated=True)
    if _remote_tip(root) != commit:
        raise RuntimeError("Remote checkpoint unconfirmed; do not auto-retry")
    return commit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("preview", "send"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    now = datetime.now(UTC)
    try:
        mailer = None
        if args.mode == "send":
            if os.environ.get("WORLD_FINANCE_ENVIRONMENT") != "world-finance-production":
                raise ValueError("Dedicated environment is required")
            mailer = world_mailer()  # Validate all addresses before collection or reservation.
        items, diagnostics = collect(root, now)
        rendered = render_world(items, now, diagnostics)
        output = root / "world-preview"
        output.mkdir(exist_ok=True)
        report = {"generated_at": now.isoformat(), "sources": diagnostics,
                  "selected": len(items), "publishers": sorted({publisher(x) for x in items}),
                  "mode": args.mode}
        for suffix, content in (("html", rendered.html), ("txt", rendered.text),
                                ("md", rendered.markdown)):
            (output / f"digest.{suffix}").write_text(content, encoding="utf-8")
        (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        if len(report["publishers"]) < 2:
            print("Insufficient eligible source diversity; no mail sent.")
            return 1
        if args.mode == "send":
            outcome = deliver(root, items, now, diagnostics, mailer, lambda: checkpoint_state(root))
            print(f"World finance delivery: {outcome}.")
        return 0
    except Exception as error:  # noqa: BLE001 - never leak provider messages or secret values
        print(f"World finance stopped: {type(error).__name__}; no automatic resend.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
