import base64
import hashlib
import hmac
from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest

from ai_daily.world_translation_budget import Grant, make_manifest

NOW = datetime(2026, 11, 17, 4, tzinfo=UTC)


def service(handler, text="Trade rises 5%", clock=lambda: NOW):
    from ai_daily.world_alibaba import AlibabaTranslator
    grant = Grant(make_manifest([text]), "2026-11")
    grant.confirm("a" * 40)
    return AlibabaTranslator("fake-id", "fake-secret", grant,
        client=httpx.Client(transport=httpx.MockTransport(handler)), clock=clock)


@pytest.mark.parametrize("success_code", [200, "200"])
def test_official_rpc_request_and_success(success_code):
    def handler(request):
        assert str(request.url) == "https://mt.cn-hangzhou.aliyuncs.com/"
        params = parse_qs(request.content.decode())
        assert params["Action"] == ["TranslateGeneral"]
        assert params["Version"] == ["2018-10-12"]
        assert params["FormatType"] == ["text"]
        assert params["Scene"] == ["general"]
        assert params["SourceLanguage"] == ["en"]
        assert params["TargetLanguage"] == ["zh"]
        assert params["SourceText"] == ["Trade rises 5%"]
        assert params["AccessKeyId"] == ["fake-id"]
        assert params["SignatureMethod"] == ["HMAC-SHA1"]
        assert params["Signature"][0]
        return httpx.Response(200, json={"Code": success_code, "Message": "success",
            "RequestId": "example", "Data": {"Translated": "贸易增长5%", "WordCount": 14}})
    with service(handler) as translator:
        assert translator.translate("Trade rises 5%") == "贸易增长5%"
        assert translator.translate("Trade rises 5%") == "贸易增长5%"
        assert translator.calls == 1


def test_official_rpc_signature_vector():
    from ai_daily.world_alibaba import sign_rpc
    # Published Alibaba RPC v2 reference vector, independent of production encoder.
    params = {"AccessKeyId": "testid", "Action": "DescribeDedicatedHosts",
        "Format": "JSON", "RegionId": "cn-beijing", "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": "edb2b34af0af9a6d14deaf7c1a5315eb",
        "SignatureVersion": "1.0", "Timestamp": "2023-03-13T08:34:30Z",
        "Version": "2014-05-26"}
    assert sign_rpc(params, "testsecret", method="GET") == "9NaGiOspFP5UPcwX8Iwt2YJXXuk="
    literal = "POST&%2F&SourceText%3DA%2520%252B%2520%25F0%259F%2598%2580%2521"
    expected = base64.b64encode(hmac.new(b"testsecret&", literal.encode(),
                                         hashlib.sha1).digest()).decode()
    assert sign_rpc({"SourceText": "A + 😀!"}, "testsecret") == expected


@pytest.mark.parametrize("payload", [
    {"Code": 10013, "Message": "fake-secret SOURCE private provider text"},
    {"Code": 200, "Data": {"Translated": ""}},
    {"Code": "0200", "Data": {"Translated": "贸易增长5%"}},
    {"Code": 200, "Data": []},
    {"Data": {"Translated": "贸易增长5%"}},
])
def test_business_error_and_malformed_response_fall_back_without_leaks(payload, capsys):
    with service(lambda _: httpx.Response(200, json=payload)) as translator:
        assert translator.translate("Trade rises 5%") == "Trade rises 5%"
        assert translator.translate("Trade rises 5%") == "Trade rises 5%"
        assert translator.calls == 1
    output = capsys.readouterr().out
    for forbidden in ("fake-secret", "fake-id", "SOURCE", "Trade rises", "private provider"):
        assert forbidden not in output


@pytest.mark.parametrize(("status", "code", "marker"), [
    (403, "Forbidden.RAM", "http_403_code_string_Forbidden.RAM"),
    (403, "200", "http_403_code_string_200"),
    (200, "10013", "http_200_code_string_10013"),
    (200, "private-detail", "http_200_code_string_other"),
    (200, True, "http_200_code_other_other"),
])
def test_failure_metadata_is_allowlisted_and_stops_requests(status, code, marker, capsys):
    body = {"Code": code, "Message": "private-detail", "Data": {"Translated": "贸易增长5%"}}
    with service(lambda _: httpx.Response(status, json=body)) as translator:
        assert translator.translate("Trade rises 5%") == "Trade rises 5%"
        assert translator.translate("Other title") == "Other title"
        assert translator.calls == 1
    output = capsys.readouterr().out
    assert marker in output and "circuit_open" in output
    assert "private-detail" not in output


def test_numeric_difference_keeps_original():
    with service(lambda _: httpx.Response(200, json={
            "Code": 200, "Data": {"Translated": "贸易增长50%"}})) as translator:
        assert translator.translate("Trade rises 5%") == "Trade rises 5%"
        assert translator.reasons["numeric_mismatch"] == 1


def test_timeout_is_one_attempt_and_does_not_leak(capsys):
    attempts = []
    def timeout(request):
        attempts.append(request)
        raise httpx.ReadTimeout("fake-secret signed request", request=request)
    with service(timeout) as translator:
        assert translator.translate("Trade rises 5%") == "Trade rises 5%"
        assert translator.translate("Other title") == "Other title"
    assert len(attempts) == 1
    assert "fake-secret" not in capsys.readouterr().out


def test_missing_credentials_and_default_disabled(monkeypatch):
    from ai_daily.world_alibaba import AlibabaTranslator
    monkeypatch.delenv("WORLD_ALIBABA_TRANSLATION_ENABLED", raising=False)
    assert AlibabaTranslator.credentials() is None
    monkeypatch.setenv("WORLD_ALIBABA_TRANSLATION_ENABLED", "true")
    monkeypatch.delenv("ALIBABA_CLOUD_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", raising=False)
    assert AlibabaTranslator.credentials() is None


def test_no_call_without_grant_or_after_month_boundary():
    attempts = []
    def handler(request):
        attempts.append(request)
        return httpx.Response(500)
    with service(handler, clock=lambda: datetime(2026, 11, 30, 16, tzinfo=UTC)) as translator:
        assert translator.translate("Trade rises 5%") == "Trade rises 5%"
    assert not attempts
