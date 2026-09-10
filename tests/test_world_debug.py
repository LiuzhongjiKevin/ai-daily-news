from datetime import UTC, datetime

import pytest

from ai_daily.models import RawItem
from ai_daily.world_debug import send_debug


def rows():
    return [RawItem(source_id=source, source_name=source, source_type='media', title='Trade peace',
                    canonical_url=f'https://example.com/{source}', category='world',
                    published_at=datetime(2026, 9, 10, tzinfo=UTC)) for source in ('bbc', 'un')]


def test_debug_resends_without_touching_any_production_state(tmp_path, monkeypatch):
    monkeypatch.delenv('TENCENTCLOUD_SECRET_ID', raising=False)
    folder = tmp_path / 'world-state'
    folder.mkdir()
    state = folder / 'state.json'
    state.write_text('existing sent and ambiguous records must remain untouched', encoding='utf-8')
    sent = []

    class Mailer:
        def send(self, rendered):
            sent.append(rendered)
            return 'accepted-debug-test'

    for _ in range(2):
        assert send_debug(tmp_path, rows(), datetime(2026, 9, 10, tzinfo=UTC), [], Mailer()) == 'accepted'
    assert len(sent) == 2
    assert all('调试' in item.subject for item in sent)
    assert state.read_text(encoding='utf-8') == 'existing sent and ambiguous records must remain untouched'
    assert not (tmp_path / 'data').exists()
    assert (tmp_path / 'world-debug' / 'digest.html').exists()


def test_debug_does_not_retry_ambiguous_smtp_failure(tmp_path, monkeypatch):
    monkeypatch.delenv('TENCENTCLOUD_SECRET_ID', raising=False)
    calls = []

    class Mailer:
        def send(self, rendered):
            calls.append(rendered)
            raise OSError('SMTP disconnected')

    with pytest.raises(OSError):
        send_debug(tmp_path, rows(), datetime(2026, 9, 10, tzinfo=UTC), [], Mailer())
    assert len(calls) == 1
    assert 'ambiguous' in (tmp_path / 'world-debug' / 'report.json').read_text()


def test_debug_rejects_insufficient_source_diversity(tmp_path):
    with pytest.raises(ValueError):
        send_debug(tmp_path, rows()[:1], datetime(2026, 9, 10, tzinfo=UTC), [], None)


@pytest.mark.parametrize('key,value', [('GITHUB_EVENT_NAME', 'schedule'),
                                     ('GITHUB_REF', 'refs/heads/world-finance-daily'),
                                     ('WORLD_FINANCE_DEBUG_CONFIRM', 'false')])
def test_debug_entry_rejects_unconfirmed_or_nonmanual_runs(monkeypatch, key, value):
    from ai_daily.world_debug import main
    for name, expected in {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
                           'GITHUB_REF': 'refs/heads/master',
                           'WORLD_FINANCE_ENVIRONMENT': 'world-finance-production',
                           'WORLD_FINANCE_DEBUG_CONFIRM': 'true'}.items():
        monkeypatch.setenv(name, expected)
    monkeypatch.setenv(key, value)
    assert main() == 2
