import json
from datetime import UTC, datetime

import httpx
import pytest

from ai_daily.models import Digest, NewsCluster, RawItem
from ai_daily.translation import TmtTranslator, translate_digest


def translator(handler, **kwargs):
    return TmtTranslator('test-id', 'test-key',
                         client=httpx.Client(transport=httpx.MockTransport(handler)), **kwargs)


@pytest.mark.parametrize('name', ['TENCENTCLOUD_SECRET_ID', 'TENCENTCLOUD_SECRET_KEY'])
def test_missing_either_credential_disables_translation(monkeypatch, name):
    monkeypatch.setenv('TENCENTCLOUD_SECRET_ID', 'test-id')
    monkeypatch.setenv('TENCENTCLOUD_SECRET_KEY', 'test-key')
    monkeypatch.delenv(name)
    assert TmtTranslator.from_environment() is None


def test_signed_request_translates_and_caches_without_changing_numbers_or_urls():
    requests = []

    def server(request):
        requests.append(request)
        assert str(request.url) == 'https://tmt.tencentcloudapi.com/'
        assert json.loads(request.content) == {
            'SourceText': 'Rate rises 5%', 'Source': 'en', 'Target': 'zh', 'ProjectId': 0}
        assert request.headers['X-TC-Action'] == 'TextTranslate'
        assert request.headers['X-TC-Version'] == '2018-03-21'
        assert 'Credential=test-id/' in request.headers['Authorization']
        assert 'test-key' not in request.headers['Authorization']
        return httpx.Response(200, json={'Response': {'TargetText': '利率上涨5%', 'RequestId': 'id'}})

    with translator(server) as service:
        assert service.translate('Rate rises 5%') == '利率上涨5%'
        assert service.translate('Rate rises 5%') == '利率上涨5%'
        assert service.translate('中文 AI 新闻') == '中文 AI 新闻'
        assert service.translate('https://example.com') == 'https://example.com'
    assert len(requests) == 1


@pytest.mark.parametrize('response', [
    httpx.Response(429), httpx.Response(200, text='<html>Error</html>'),
    httpx.Response(200, json={'Response': {'Error': {'Message': 'secret-value'}}}),
    httpx.Response(200, json={'Response': {'TargetText': ''}}),
    httpx.Response(200, json={'Response': {'TargetText': 123}}),
])
def test_provider_error_opens_circuit_and_keeps_original(response, capsys):
    requests = []
    with translator(lambda request: requests.append(request) or response) as service:
        assert service.translate('Original news') == 'Original news'
        assert service.translate('Next news') == 'Next news'
    assert len(requests) == 1
    assert 'secret-value' not in str(capsys.readouterr())


def test_timeout_returns_original():
    def timeout(request):
        raise httpx.ReadTimeout('sensitive provider info', request=request)
    with translator(timeout) as service:
        assert service.translate('Original news') == 'Original news'


def test_character_budget_prevents_additional_calls():
    calls = []
    with translator(lambda request: calls.append(request) or httpx.Response(
        200, json={'Response': {'TargetText': '新闻'}}), max_characters=4) as service:
        assert service.translate('News') == '新闻'
        assert service.translate('Later') == 'Later'
    assert len(calls) == 1


def test_changed_numbers_fall_back_and_redirects_are_not_followed():
    with translator(lambda request: httpx.Response(
            200, json={'Response': {'TargetText': '利率上涨50%'}})) as service:
        assert service.translate('Rate rises 5%') == 'Rate rises 5%'
    calls = []
    with translator(lambda request: calls.append(request) or httpx.Response(
            302, headers={'Location': 'https://untrusted.example/'})) as service:
        assert service.translate('News') == 'News'
    assert len(calls) == 1


def test_long_field_and_expired_time_budget_do_not_call_api():
    calls = []
    with translator(lambda request: calls.append(request)) as service:
        assert service.translate('x' * 1001) == 'x' * 1001
        service._deadline = 0
        assert service.translate('News') == 'News'
    assert calls == []


def test_digest_translation_retains_source_items_and_does_not_mutate_original(monkeypatch):
    item = RawItem(source_id='bbc', source_name='BBC', source_type='media', title='News',
                   canonical_url='https://example.com/news', published_at=datetime.now(UTC))
    digest = Digest(local_date='2026-09-10', repositories=[], news=[NewsCluster(
        cluster_id='1', title='News', summary='Summary', trust_grade='A', items=[item])])
    service = translator(lambda request: httpx.Response(200, json={'Response': {'TargetText': '译文'}}))
    monkeypatch.setattr(TmtTranslator, 'from_environment', lambda: service)
    result = translate_digest(digest)
    assert result.news[0].title == '译文'
    assert result.news[0].summary == '译文'
    assert result.news[0].items[0].title == 'News'
    assert result.news[0].items[0].canonical_url == item.canonical_url
    assert digest.news[0].title == 'News'
    assert any('机器翻译' in warning for warning in result.warnings)
