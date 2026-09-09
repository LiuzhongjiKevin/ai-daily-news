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

from ai_daily.collectors.base import load_sources
from ai_daily.collectors.feed import FeedCollector
from ai_daily.http import RetryingClient
from ai_daily.mail import QQMailer
from ai_daily.pipeline import _timezone
from ai_daily.state import StateStore
from ai_daily.world_news import publisher, render_world, select_news

BRANCH = "world-finance-daily"


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
    # A failed push leaves an unresolved reservation locally and MUST prevent SMTP.
    checkpoint()
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

    token = os.environ.get("WORLD_FINANCE_STATE_TOKEN", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or any(character.isspace() for character in token):
        raise RuntimeError("Dedicated state token is unavailable")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise RuntimeError("Repository identity is unavailable")

    def git(*args, environment=None):
        result = subprocess.run(["git", *args], cwd=root, capture_output=True,
                                text=True, timeout=60, check=False, env=environment)
        if result.returncode:
            raise RuntimeError("World-finance state checkpoint failed; do not auto-retry")
        return result.stdout.strip()

    if git("diff", "--cached", "--name-only"):
        raise RuntimeError("Refusing a checkpoint with unrelated staged changes")
    git("add", "--", "world-state/state.json")
    if git("diff", "--cached", "--name-only") != "world-state/state.json":
        raise RuntimeError("Expected exactly one isolated state change")
    git("-c", "user.name=github-actions[bot]", "-c",
        "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "commit", "-m", "chore: update world finance delivery state")
    environment = dict(os.environ)
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    environment.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                       GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {encoded}",
                       GIT_TERMINAL_PROMPT="0")
    for name in ("WORLD_FINANCE_STATE_TOKEN", "SMTP_USERNAME", "SMTP_PASSWORD", "MAIL_TO"):
        environment.pop(name, None)
    git("push", f"https://github.com/{repository}.git", f"HEAD:refs/heads/{BRANCH}",
        environment=environment)


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
