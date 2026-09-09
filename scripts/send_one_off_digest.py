"""Explicit QQ resend with isolated state; never alters scheduled delivery records."""

import os
import tempfile
from pathlib import Path

from ai_daily.cli import build_pipeline
from ai_daily.pipeline import RunOptions
from ai_daily.state import StateStore


def main():
    if os.environ.get("MAIL_PROVIDER") != "qq":
        print("One-off delivery requires QQ mail configuration.")
        return 1
    pipeline = build_pipeline()
    state_parent = pipeline.output_root / "preview" / "one-off-state"
    state_parent.mkdir(parents=True, exist_ok=True)
    pipeline.state_store = StateStore(Path(tempfile.mkdtemp(dir=state_parent)))
    try:
        result = pipeline.run(RunOptions(send=True, ai_mode="off"))
    except Exception as error:  # noqa: BLE001 - never expose credentials in provider errors
        print(f"One-off delivery failed: {type(error).__name__}; do not retry automatically.")
        return 1
    print("One-off delivery accepted by mail server." if result.sent else "No email was sent.")
    return 0 if result.sent else 1


if __name__ == "__main__":
    raise SystemExit(main())
