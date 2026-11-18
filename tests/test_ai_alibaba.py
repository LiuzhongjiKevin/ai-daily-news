from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest
from test_world_translation_budget import install, ledger

from ai_daily import world_finance as world
from ai_daily.models import Digest, NewsCluster, RawItem
from ai_daily.state import StateStore
from ai_daily.world_alibaba import AlibabaTranslator

NOW = datetime(2026, 11, 18, 7, tzinfo=UTC)


def digest():
    item = RawItem(source_id='example', source_name='Example', source_type='media',
                   title='Source title', canonical_url='https://example.com/story',
                   published_at=NOW)
    return Digest(local_date='2026-11-18', repositories=[], news=[NewsCluster(
        cluster_id='one', title='Hello.', summary='Summary ' + 'x' * 300,
        trust_grade='A', items=[item])])


def setup(monkeypatch, tmp_path):
    for name, value in {'GITHUB_ACTIONS': 'true', 'GITHUB_REF': 'refs/heads/master',
                        'WORLD_FINANCE_ENVIRONMENT': 'ai-daily-production',
                        'WORLD_ALIBABA_TRANSLATION_ENABLED': 'true',
                        'ALIBABA_CLOUD_ACCESS_KEY_ID': 'fake-id',
                        'ALIBABA_CLOUD_ACCESS_KEY_SECRET': 'fake-secret',
                        'GITHUB_RUN_ID': '789', 'GITHUB_RUN_ATTEMPT': '1'}.items():
        monkeypatch.setenv(name, value)
    store = StateStore(tmp_path / 'world-state')
    install(store, ledger(baseline=100000))
    monkeypatch.setattr(world, 'translation_now', lambda: NOW)
    monkeypatch.setattr(world, 'verify_budget_history', lambda root: None)
    return store


def test_ai_adapter_uses_shared_durable_budget_and_bounded_fields(tmp_path, monkeypatch):
    from ai_daily.ai_alibaba import translate_digest

    store = setup(monkeypatch, tmp_path)
    checkpoints, requests = [], []
    def checkpoint(root):
        assert root == tmp_path
        assert store.load_run_state().translation_budget['months']['2026-11']['reserved'] == 100226
        checkpoints.append(True)
        return 'a' * 40
    monkeypatch.setattr(world, 'checkpoint_state', checkpoint)
    def handler(request):
        assert checkpoints == [True]
        text = parse_qs(request.content.decode())['SourceText'][0]
        requests.append(text)
        return httpx.Response(200, json={'Code': 200, 'Data': {'Translated': '\u4f60\u597d\u3002'}})
    real_init = AlibabaTranslator.__init__
    monkeypatch.setattr(AlibabaTranslator, '__init__', lambda self, *a, **k: real_init(
        self, *a, **k, client=httpx.Client(transport=httpx.MockTransport(handler))))
    original = digest()
    translated = translate_digest(original, ledger_root=tmp_path)
    assert translated.news[0].title == '\u4f60\u597d\u3002'
    assert translated.news[0].summary == '\u4f60\u597d\u3002\u2026'
    assert requests == ['Hello.', original.news[0].summary[:220]]
    assert original.news[0].title == 'Hello.'
    assert translated.news[0].items[0].title == 'Source title'
    assert translated.warnings == [AlibabaTranslator.notice]


@pytest.mark.parametrize('failure', ['disabled', 'wrong_environment', 'history', 'budget', 'push'])
def test_ai_failure_uses_original_without_tencent_or_paid_call(tmp_path, monkeypatch, failure):
    from ai_daily.ai_alibaba import translate_digest

    store = setup(monkeypatch, tmp_path)
    if failure == 'disabled':
        monkeypatch.setenv('WORLD_ALIBABA_TRANSLATION_ENABLED', 'false')
    elif failure == 'wrong_environment':
        monkeypatch.setenv('WORLD_FINANCE_ENVIRONMENT', 'world-finance-production')
    elif failure == 'budget':
        install(store, None)
    def fail(*a):
        raise RuntimeError('private-token provider response')
    if failure == 'history':
        monkeypatch.setattr(world, 'verify_budget_history', fail)
    monkeypatch.setattr(world, 'checkpoint_state', fail)
    monkeypatch.setattr(AlibabaTranslator, '__init__', lambda *a, **k: pytest.fail('No paid provider'))
    original = digest()
    assert translate_digest(original, ledger_root=tmp_path) is original


def test_ai_probe_uses_actual_adapter_and_only_six_units_without_email(tmp_path, monkeypatch):
    from ai_daily.ai_probe import probe

    store = setup(monkeypatch, tmp_path)
    monkeypatch.setenv('GITHUB_EVENT_NAME', 'workflow_dispatch')
    monkeypatch.setenv('AI_ALIBABA_PROBE_APPROVED', 'true')
    def checkpoint(root):
        assert store.load_run_state().translation_budget['months']['2026-11']['reserved'] == 100006
        return 'a' * 40
    monkeypatch.setattr(world, 'checkpoint_state', checkpoint)
    calls = []
    def handler(request):
        calls.append(parse_qs(request.content.decode())['SourceText'])
        return httpx.Response(200, json={'Code': 200, 'Data': {'Translated': '\u4f60\u597d\u3002'}})
    real_init = AlibabaTranslator.__init__
    monkeypatch.setattr(AlibabaTranslator, '__init__', lambda self, *a, **k: real_init(
        self, *a, **k, client=httpx.Client(transport=httpx.MockTransport(handler))))
    assert probe(tmp_path)['status'] == 'translated'
    assert calls == [['Hello.']]
    assert not store.load_run_state().sent_dates
    monkeypatch.setenv('GITHUB_RUN_ATTEMPT', '2')
    assert probe(tmp_path)['status'] == 'unverified'
    assert calls == [['Hello.']]


def test_ai_workflows_require_manual_probe_and_shared_ledger_without_mail():
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / '.github/workflows/ai-translation-probe.yml').read_text('utf-8'))
    assert set(workflow[True]) == {'workflow_dispatch'}
    assert workflow[True]['workflow_dispatch']['inputs']['confirm']['default'] is False
    assert workflow['concurrency']['group'] == 'world-finance-delivery'
    job = workflow['jobs']['probe']
    assert "github.ref == 'refs/heads/master'" in job['if']
    assert 'inputs.confirm == true' in job['if']
    assert job['environment'] == 'ai-daily-production'
    assert 'secrets.' not in str(workflow['jobs']['verify'])
    assert not any(key in str(job) for key in ('SMTP_', 'MAIL_TO', 'TENCENTCLOUD'))
    assert job['steps'][0]['with']['ref'] == '${{ needs.verify.outputs.source_sha }}'
    ledger_step = next(s for s in job['steps'] if s.get('with', {}).get('path') == '.translation-ledger')
    assert ledger_step['with']['ref'] == 'world-finance-daily'
    assert ledger_step['with']['fetch-depth'] == 0
    env = next(s['env'] for s in job['steps'] if s.get('run') == 'python -m ai_daily.ai_probe')
    assert env['WORLD_FINANCE_STATE_TOKEN'] == '${{ secrets.AI_DAILY_STATE_TOKEN }}'
    daily = yaml.safe_load((root / '.github/workflows/daily.yml').read_text('utf-8'))['jobs']['daily']
    step = next(s for s in daily['steps'] if s.get('name') == 'Run daily digest')
    assert step['env']['WORLD_ALIBABA_TRANSLATION_ENABLED'] == '${{ vars.AI_ALIBABA_TRANSLATION_ENABLED }}'
    assert 'TENCENTCLOUD' not in str(step)
    ledger_step = next(s for s in daily['steps'] if s.get('with', {}).get('path') == '.translation-ledger')
    assert "steps.reserve.outputs.reservation_status == 'reserved'" in ledger_step['if']
