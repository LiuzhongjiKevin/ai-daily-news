"""Manual no-mail verification using the AI edition's actual translation adapter."""
import json
import os
import re
from datetime import UTC, datetime

from ai_daily.ai_alibaba import ledger_directory, translate_digest
from ai_daily.models import Digest, NewsCluster, RawItem


def probe(root=None):
    if any(os.environ.get(name) != value for name, value in {
        'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
        'GITHUB_REF': 'refs/heads/master', 'WORLD_FINANCE_ENVIRONMENT': 'ai-daily-production',
        'AI_ALIBABA_PROBE_APPROVED': 'true',
    }.items()):
        raise RuntimeError('AI probe not authorized')
    item = RawItem(source_id='public-sample', source_name='Public sample', source_type='media',
                   title='Hello.', canonical_url='https://example.com/',
                   published_at=datetime.now(UTC))
    original = Digest(local_date=datetime.now(UTC).date().isoformat(), repositories=[], news=[
        NewsCluster(cluster_id='public-sample', title='Hello.', summary='',
                    trust_grade='A', items=[item])])
    result = translate_digest(original, ledger_root=root if root is not None else ledger_directory())
    success = result is not original and bool(re.search(r'[\u3400-\u9fff]', result.news[0].title))
    return {'status': 'translated' if success else 'unverified', 'sample_characters': 6}


def main():
    try:
        result = probe()
        print('AI Alibaba probe: ' + json.dumps(result, sort_keys=True))
        return 0 if result['status'] == 'translated' else 1
    except Exception as error:  # noqa: BLE001
        print(f'AI Alibaba probe stopped: {type(error).__name__}; no automatic retry.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
