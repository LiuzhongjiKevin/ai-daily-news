"""Encrypted delegated Microsoft Graph delivery for rendered digests."""

import base64
import hashlib
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from datetime import date
from email import policy
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import httpx
import msal
from cryptography.fernet import Fernet, InvalidToken

from ai_daily.render import RenderedDigest

_AUTHORITY = "https://login.microsoftonline.com/consumers"
_MAIL_SCOPE = ["Mail.Send"]
_SEND_MAIL_URL = "https://graph.microsoft.com/v1.0/me/sendMail"
_DATE_IN_SUBJECT = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_ADDR = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")


class MailError(RuntimeError):
    """Base class for delivery errors with intentionally safe messages."""


class MailAuthError(MailError):
    """The locally encrypted delegated authorization cannot be used."""


class MailSendError(MailError):
    """Microsoft Graph did not accept a message for queueing."""


class EncryptedTokenCache:
    """Serialize MSAL caches to Fernet-encrypted files only."""

    @staticmethod
    def generate_key() -> bytes:
        return Fernet.generate_key()

    @staticmethod
    def empty() -> msal.SerializableTokenCache:
        return msal.SerializableTokenCache()

    @staticmethod
    def _fernet(key: str | bytes) -> Fernet:
        try:
            return Fernet(key.encode("ascii") if isinstance(key, str) else key)
        except (TypeError, ValueError):
            raise MailAuthError("Microsoft authentication material is invalid") from None

    @classmethod
    def load(cls, path: Path, key: str | bytes) -> msal.SerializableTokenCache:
        fernet = cls._fernet(key)
        try:
            encrypted = path.read_bytes()
            serialized = fernet.decrypt(encrypted).decode("utf-8")
            cache = cls.empty()
            cache.deserialize(serialized)
            return cache
        except (OSError, InvalidToken, UnicodeDecodeError, ValueError, TypeError):
            raise MailAuthError("Microsoft token cache is unavailable") from None

    @classmethod
    def save(
        cls, cache: msal.SerializableTokenCache, path: Path, key: str | bytes, *, replace: bool = True
    ) -> None:
        fernet = cls._fernet(key)
        temporary_path: Path | None = None
        try:
            encrypted = fernet.encrypt(cache.serialize().encode("utf-8"))
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix=f"{path.name}.", suffix=".tmp", delete=False
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(encrypted)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            if replace:
                os.replace(temporary_path, path)
            else:
                # Linking a complete same-directory temporary file creates the final path only
                # when it is still absent; unlike replace(), it cannot overwrite a racing writer.
                os.link(temporary_path, path)
        except (OSError, TypeError, ValueError):
            raise MailAuthError("Microsoft token cache could not be saved") from None
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass


ApplicationFactory = Callable[..., Any]


class GraphMailer:
    """Acquire an existing delegated token and submit a MIME message to Graph."""

    def __init__(
        self,
        *,
        client_id: str,
        token_key: str | bytes,
        sender: str,
        recipient: str,
        cache_path: Path,
        application_factory: ApplicationFactory = msal.PublicClientApplication,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.client_id = self._required(client_id, "client id")
        self.token_key = token_key
        self.sender = self._address(sender)
        self.recipient = self._address(recipient)
        self.cache_path = cache_path
        self.application_factory = application_factory
        self.http_client = http_client or httpx.Client(timeout=httpx.Timeout(20.0))

    @classmethod
    def from_environment(
        cls,
        cache_path: Path,
        environment: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> "GraphMailer":
        environment = environment or os.environ
        try:
            return cls(
                client_id=environment["MS_CLIENT_ID"],
                token_key=environment["MS_TOKEN_KEY"],
                sender=environment["OUTLOOK_SENDER"],
                recipient=environment["MAIL_TO"],
                cache_path=cache_path,
                **kwargs,
            )
        except (KeyError, MailSendError):
            raise MailAuthError("Microsoft mail configuration is unavailable") from None

    @staticmethod
    def _required(value: str, name: str) -> str:
        if not value or "\r" in value or "\n" in value:
            raise MailAuthError(f"Microsoft {name} is unavailable")
        return value

    @staticmethod
    def _address(value: str) -> str:
        if not value or "\r" in value or "\n" in value or not _ADDR.fullmatch(value):
            raise MailSendError("Mail header address is invalid")
        return value

    def _access_token(self) -> str:
        cache = EncryptedTokenCache.load(self.cache_path, self.token_key)
        try:
            application = self.application_factory(
                self.client_id, authority=_AUTHORITY, token_cache=cache
            )
            accounts = application.get_accounts()
            if len(accounts) != 1:
                raise MailAuthError("Microsoft authorization requires exactly one cached account")
            result = application.acquire_token_silent(_MAIL_SCOPE, account=accounts[0])
            if getattr(cache, "has_state_changed", False):
                EncryptedTokenCache.save(cache, self.cache_path, self.token_key)
        except MailAuthError:
            raise
        except Exception:  # noqa: BLE001 - do not expose provider/cache details in auth errors
            raise MailAuthError("Microsoft authorization is unavailable") from None
        token = result.get("access_token") if isinstance(result, dict) else None
        if not isinstance(token, str) or not token:
            raise MailAuthError("Microsoft authorization requires a refreshed sign-in")
        return token

    @staticmethod
    def _validated_subject(subject: str) -> str:
        if not subject or "\r" in subject or "\n" in subject:
            raise MailSendError("Mail header subject is invalid")
        return subject

    def _mime_message(self, rendered: RenderedDigest) -> bytes:
        message = EmailMessage(policy=policy.SMTP)
        message["From"] = self.sender
        message["To"] = self.recipient
        message["Subject"] = self._validated_subject(rendered.subject)
        message.set_content(rendered.text, subtype="plain", charset="utf-8")
        message.add_alternative(rendered.html, subtype="html", charset="utf-8")
        return message.as_bytes()

    def _message_fingerprint(self, rendered: RenderedDigest) -> str:
        subject = self._validated_subject(rendered.subject)
        date_match = _DATE_IN_SUBJECT.search(subject)
        if date_match is None:
            raise MailSendError("Rendered digest subject must contain its local date")
        try:
            local_date = date.fromisoformat(date_match.group(0)).isoformat()
        except ValueError:
            raise MailSendError("Rendered digest subject contains an invalid local date") from None
        material = f"{local_date}\n{self.recipient}\n{subject}".encode()
        return f"accepted-{hashlib.sha256(material).hexdigest()}"

    def send(self, rendered: RenderedDigest) -> str:
        fingerprint = self._message_fingerprint(rendered)
        token = self._access_token()
        payload = base64.b64encode(self._mime_message(rendered))
        try:
            response = self.http_client.post(
                _SEND_MAIL_URL,
                content=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "text/plain",
                },
            )
        except httpx.HTTPError:
            raise MailSendError("Microsoft Graph mail submission failed") from None
        if response.status_code != 202:
            raise MailSendError(
                f"Microsoft Graph did not accept the mail submission (HTTP {response.status_code})"
            )
        return fingerprint
