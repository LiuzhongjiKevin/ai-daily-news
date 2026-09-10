"""Manual-only finance delivery with no reads or writes to production delivery state."""

import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from ai_daily.translation import TmtTranslator
from ai_daily.world_finance import collect, world_mailer
from ai_daily.world_news import publisher, render_world


def send_debug(root, items, now, diagnostics, mailer):
    if len({publisher(item) for item in items}) < 2:
        raise ValueError('At least two publishers are required')
    rendered = render_world(items, now, diagnostics)
    translator = TmtTranslator.from_environment()
    if translator is not None:
        with translator:
            rendered = render_world(items, now, diagnostics, translator=translator)
    rendered = replace(rendered, subject=f'[调试发送] {rendered.subject}')
    output = root / 'world-debug'
    output.mkdir(exist_ok=True)
    for suffix, content in (('html', rendered.html), ('txt', rendered.text), ('md', rendered.markdown)):
        (output / f'digest.{suffix}').write_text(content, encoding='utf-8')
    report = {'mode': 'debug-send', 'generated_at': now.isoformat(), 'selected': len(items),
              'delivery': 'ambiguous', 'sources': diagnostics}
    report_path = output / 'report.json'
    # This is a diagnostic artifact, not a production sent marker. Do not retry SMTP.
    report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    mailer.send(rendered)
    print('World finance debug delivery: accepted.')
    report['delivery'] = 'accepted'
    report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    return 'accepted'


def main():
    if not (
        os.environ.get('GITHUB_ACTIONS') == 'true'
        and os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch'
        and os.environ.get('GITHUB_REF') == 'refs/heads/master'
        and os.environ.get('WORLD_FINANCE_ENVIRONMENT') == 'world-finance-production'
        and os.environ.get('WORLD_FINANCE_DEBUG_CONFIRM') == 'true'
    ):
        print('Debug sending requires an explicitly confirmed manual run from master.')
        return 2
    try:
        root = Path(__file__).resolve().parents[2]
        mailer = world_mailer()
        now = datetime.now(UTC)
        items, diagnostics = collect(root, now)
        send_debug(root, items, now, diagnostics, mailer)
        return 0
    except Exception as error:  # noqa: BLE001 - never expose provider responses or credentials
        print(f'World finance debug stopped: {type(error).__name__}; check delivery before retrying.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
