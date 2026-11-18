"""Explicit manual six-unit translation verification; never collect or send mail."""
import json
import os
import re
from pathlib import Path

from ai_daily import world_finance as world
from ai_daily.state import StateStore
from ai_daily.world_alibaba import AlibabaTranslator
from ai_daily.world_translation_budget import (
    BudgetUnavailable,
    make_manifest,
    reserve,
    safe_error_code,
)

SAMPLE = "Hello."


def probe(root):
    required = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/master",
        "WORLD_FINANCE_ENVIRONMENT": "world-finance-production",
        "WORLD_ALIBABA_PROBE_APPROVED": "true",
    }
    if any(os.environ.get(name) != value for name, value in required.items()):
        raise BudgetUnavailable("probe_not_authorized")
    credentials = AlibabaTranslator.credentials()
    if credentials is None:
        raise BudgetUnavailable("probe_credentials_unavailable")
    world.verify_budget_history(root)
    store = StateStore(root / "world-state")
    manifest = make_manifest([SAMPLE])
    grant = reserve(store, manifest, world.translation_now(),
                    os.environ.get("GITHUB_RUN_ID", ""),
                    os.environ.get("GITHUB_RUN_ATTEMPT", ""))
    # The existing checkpoint confirms a normal push and its exact remote commit.
    # Nothing constructs a provider until this new reservation is durable.
    grant.confirm(world.checkpoint_state(root))
    with AlibabaTranslator(*credentials, grant, clock=world.translation_now) as translator:
        result = translator.translate(SAMPLE)
        succeeded = (translator.calls == 1 and translator.translated == 1
                     and bool(re.search(r"[\u3400-\u9fff]", result)))
        return {"status": "translated" if succeeded else "unverified",
                "reserved_characters": manifest.characters, "requests": translator.calls}


def main():
    try:
        report = probe(Path(__file__).resolve().parents[2])
        print("Alibaba probe: " + json.dumps(report, sort_keys=True))
        return 0 if report["status"] == "translated" else 1
    except Exception as error:  # noqa: BLE001 - do not emit provider/credential/response content
        print(f"Alibaba probe stopped: {type(error).__name__}; "
              f"code={safe_error_code(error)}; no automatic retry.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
