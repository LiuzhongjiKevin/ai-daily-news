"""Offline contracts for encrypted delegated Outlook delivery."""

import base64
import traceback
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import ClassVar

import httpx
import pytest
import respx

from ai_daily.mail import EncryptedTokenCache, GraphMailer, MailAuthError, MailSendError
from ai_daily.render import RenderedDigest

TOKEN = "access-token-that-must-never-appear"
RECIPIENT = "reader@example.test"
SENDER = "daily@example.test"


class FakeApplication:
    accounts: ClassVar[list[dict[str, str]]] = [{"home_account_id": "one"}]
    result: ClassVar[dict[str, str]] = {"access_token": TOKEN}
    created: ClassVar[list["FakeApplication"]] = []

    def __init__(self, client_id: str, *, authority: str, token_cache: object) -> None:
        self.client_id = client_id
        self.authority = authority
        self.token_cache = token_cache
        self.silent_calls: list[tuple[list[str], object]] = []
        self.__class__.created.append(self)

    def get_accounts(self) -> list[dict[str, str]]:
        return self.accounts

    def acquire_token_silent(self, scopes: list[str], *, account: object) -> dict[str, str]:
        self.silent_calls.append((scopes, account))
        return self.result


@pytest.fixture(autouse=True)
def reset_fake_application() -> None:
    FakeApplication.accounts = [{"home_account_id": "one"}]
    FakeApplication.result = {"access_token": TOKEN}
    FakeApplication.created = []


@pytest.fixture
def rendered() -> RenderedDigest:
    return RenderedDigest(
        subject="[AI Daily] 2026-08-24｜每日简报",
        text="Plain text\n中文",
        html="<h1>HTML 中文</h1>",
        markdown="# archive",
    )


def _saved_cache(path: Path, key: bytes) -> None:
    cache = EncryptedTokenCache.empty()
    cache.deserialize('{"AccessToken": {}}')
    EncryptedTokenCache.save(cache, path, key)


def _mailer(path: Path, key: bytes) -> GraphMailer:
    return GraphMailer(
        client_id="client-id",
        token_key=key,
        sender=SENDER,
        recipient=RECIPIENT,
        cache_path=path,
        application_factory=FakeApplication,
        http_client=httpx.Client(),
    )


def test_token_cache_round_trip_is_encrypted_and_atomic(tmp_path: Path) -> None:
    key = EncryptedTokenCache.generate_key()
    path = tmp_path / "microsoft-token.enc"
    cache = EncryptedTokenCache.empty()
    cache.deserialize('{"AccessToken": {}}')

    EncryptedTokenCache.save(cache, path, key)

    assert b"AccessToken" not in path.read_bytes()
    assert EncryptedTokenCache.load(path, key).serialize() == cache.serialize()
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize("key", [b"not-a-fernet-key", b""])
def test_token_cache_rejects_malformed_keys(tmp_path: Path, key: bytes) -> None:
    with pytest.raises(MailAuthError, match="authentication material"):
        EncryptedTokenCache.save(EncryptedTokenCache.empty(), tmp_path / "cache.enc", key)


def test_token_cache_redacts_a_malformed_non_ascii_key(tmp_path: Path) -> None:
    malformed_key = "not-a-key-秘密"

    with pytest.raises(MailAuthError) as caught:
        EncryptedTokenCache.save(EncryptedTokenCache.empty(), tmp_path / "cache.enc", malformed_key)

    assert malformed_key not in str(caught.value)


def test_token_cache_rejects_missing_wrong_and_malformed_cache_without_secrets(tmp_path: Path) -> None:
    key = EncryptedTokenCache.generate_key()
    path = tmp_path / "cache.enc"
    secret_plaintext = '{"AccessToken":{"secret":"token-value"}}'

    with pytest.raises(MailAuthError) as missing:
        EncryptedTokenCache.load(path, key)
    path.write_bytes(b"not encrypted")
    with pytest.raises(MailAuthError) as malformed:
        EncryptedTokenCache.load(path, key)
    cache = EncryptedTokenCache.empty()
    cache.deserialize(secret_plaintext)
    EncryptedTokenCache.save(cache, path, key)
    with pytest.raises(MailAuthError) as wrong_key:
        EncryptedTokenCache.load(path, EncryptedTokenCache.generate_key())

    for caught in (missing, malformed, wrong_key):
        assert "token-value" not in str(caught.value)
        assert key.decode() not in str(caught.value)


def test_token_cache_cleans_temporary_file_when_replacement_fails(tmp_path: Path, monkeypatch) -> None:
    from ai_daily import mail

    path = tmp_path / "cache.enc"
    key = EncryptedTokenCache.generate_key()
    monkeypatch.setattr(mail.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("blocked")))

    with pytest.raises(MailAuthError, match="save"):
        EncryptedTokenCache.save(EncryptedTokenCache.empty(), path, key)

    assert list(tmp_path.glob("*.tmp")) == []


def test_cached_delegated_token_uses_consumer_authority_single_account_and_mail_send(
    tmp_path: Path, monkeypatch
) -> None:
    from ai_daily import mail

    path = tmp_path / "cache.enc"
    key = EncryptedTokenCache.generate_key()
    _saved_cache(path, key)
    monkeypatch.setattr(mail.msal, "PublicClientApplication", FakeApplication)
    mailer = _mailer(path, key)

    assert mailer._access_token() == TOKEN

    application = FakeApplication.created[-1]
    assert application.client_id == "client-id"
    assert application.authority == "https://login.microsoftonline.com/consumers"
    assert application.silent_calls == [(["Mail.Send"], {"home_account_id": "one"})]


@pytest.mark.parametrize("accounts", [[], [{"one": "1"}, {"two": "2"}]])
def test_cached_token_requires_exactly_one_account(tmp_path: Path, accounts: list[dict[str, str]]) -> None:
    path = tmp_path / "cache.enc"
    key = EncryptedTokenCache.generate_key()
    _saved_cache(path, key)
    FakeApplication.accounts = accounts

    with pytest.raises(MailAuthError, match="exactly one"):
        _mailer(path, key)._access_token()


def test_cached_token_persists_cache_state_after_silent_refresh(tmp_path: Path, monkeypatch) -> None:

    class ChangedCache:
        has_state_changed = True

    changed_cache = ChangedCache()
    saved: list[object] = []
    monkeypatch.setattr(EncryptedTokenCache, "load", lambda *_: changed_cache)
    monkeypatch.setattr(EncryptedTokenCache, "save", lambda cache, *_: saved.append(cache))
    path = tmp_path / "cache.enc"
    mailer = _mailer(path, EncryptedTokenCache.generate_key())

    assert mailer._access_token() == TOKEN
    assert saved == [changed_cache]


def test_cached_token_without_access_token_is_sanitized_auth_error(tmp_path: Path) -> None:
    path = tmp_path / "cache.enc"
    key = EncryptedTokenCache.generate_key()
    _saved_cache(path, key)
    FakeApplication.result = {"error_description": TOKEN}

    with pytest.raises(MailAuthError) as caught:
        _mailer(path, key)._access_token()

    assert TOKEN not in str(caught.value)


def test_cached_token_sanitizes_msal_exceptions(tmp_path: Path) -> None:
    class ExplodingApplication(FakeApplication):
        def get_accounts(self) -> list[dict[str, str]]:
            raise RuntimeError(TOKEN)

    path = tmp_path / "cache.enc"
    key = EncryptedTokenCache.generate_key()
    _saved_cache(path, key)

    with pytest.raises(MailAuthError) as caught:
        GraphMailer(
            client_id="client-id",
            token_key=key,
            sender=SENDER,
            recipient=RECIPIENT,
            cache_path=path,
            application_factory=ExplodingApplication,
        )._access_token()

    assert TOKEN not in str(caught.value)


@respx.mock
def test_graph_mailer_posts_base64_multipart_alternatives_and_returns_fingerprint(
    tmp_path: Path, rendered: RenderedDigest
) -> None:
    path = tmp_path / "cache.enc"
    key = EncryptedTokenCache.generate_key()
    _saved_cache(path, key)
    route = respx.post("https://graph.microsoft.com/v1.0/me/sendMail").respond(202)

    message_id = _mailer(path, key).send(rendered)

    request = route.calls[0].request
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["content-type"].split(";")[0] == "text/plain"
    mime = BytesParser(policy=policy.default).parsebytes(base64.b64decode(request.content))
    assert mime.get_content_type() == "multipart/alternative"
    assert mime["To"] == RECIPIENT
    assert mime["From"] == SENDER
    assert mime["Subject"] == rendered.subject
    assert [part.get_content_type() for part in mime.iter_parts()] == ["text/plain", "text/html"]
    assert "中文" in mime.get_body(("plain",)).get_content()
    assert "HTML 中文" in mime.get_body(("html",)).get_content()
    assert message_id == _mailer(path, key)._message_fingerprint(rendered)


@pytest.mark.parametrize("status", [200, 201, 204, 401, 429, 500])
@respx.mock
def test_graph_mailer_rejects_every_non_202_status_without_leaking_response(
    tmp_path: Path, rendered: RenderedDigest, status: int
) -> None:
    path = tmp_path / "cache.enc"
    key = EncryptedTokenCache.generate_key()
    _saved_cache(path, key)
    respx.post("https://graph.microsoft.com/v1.0/me/sendMail").respond(status, text=TOKEN)

    with pytest.raises(MailSendError) as caught:
        _mailer(path, key).send(rendered)

    assert str(status) in str(caught.value)
    assert TOKEN not in str(caught.value)
    assert RECIPIENT not in str(caught.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [("recipient", "reader@example.test\r\nBcc: attacker@example.test"), ("sender", "a\nb@example.test")],
)
def test_graph_mailer_rejects_header_injection(tmp_path: Path, field: str, value: str) -> None:
    path = tmp_path / "cache.enc"
    key = EncryptedTokenCache.generate_key()
    _saved_cache(path, key)
    kwargs = {field: value}

    with pytest.raises(MailSendError, match="header"):
        GraphMailer(
            client_id="client-id",
            token_key=key,
            sender=kwargs.get("sender", SENDER),
            recipient=kwargs.get("recipient", RECIPIENT),
            cache_path=path,
            application_factory=FakeApplication,
            http_client=httpx.Client(),
        )


def test_auth_traceback_redacts_keys_tokens_and_full_addresses(tmp_path: Path) -> None:
    path = tmp_path / "cache.enc"
    key = EncryptedTokenCache.generate_key()
    path.write_bytes(b"bad")

    try:
        _mailer(path, key)._access_token()
    except MailAuthError as exc:
        output = "".join(traceback.format_exception(exc))
    else:  # pragma: no cover - guard for test clarity
        pytest.fail("expected authentication error")

    for secret in (TOKEN, key.decode(), RECIPIENT, SENDER):
        assert secret not in output


def test_setup_refuses_existing_cache_without_replace_and_only_prints_device_code(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from scripts import setup_outlook

    class DeviceApplication:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def initiate_device_flow(self, *, scopes: list[str]) -> dict[str, str]:
            assert scopes == ["Mail.Send"]
            return {"verification_uri": "https://microsoft.example/verify", "user_code": "ABCD-EFGH"}

        def acquire_token_by_device_flow(self, flow: dict[str, str]) -> dict[str, str]:
            assert flow["user_code"] == "ABCD-EFGH"
            return {"access_token": TOKEN}

    key = EncryptedTokenCache.generate_key().decode()
    path = tmp_path / "microsoft-token.enc"
    path.write_bytes(b"existing")
    monkeypatch.setenv("MS_CLIENT_ID", "client-id")
    monkeypatch.setenv("MS_TOKEN_KEY", key)
    monkeypatch.setattr(setup_outlook.msal, "PublicClientApplication", DeviceApplication)

    assert setup_outlook.main(["--cache-path", str(path)]) == 2
    assert capsys.readouterr().out == ""
    assert setup_outlook.main(["--cache-path", str(path), "--replace"]) == 0
    output = capsys.readouterr().out
    assert "https://microsoft.example/verify" in output
    assert "ABCD-EFGH" in output
    assert TOKEN not in output
    assert b"access-token" not in path.read_bytes()
