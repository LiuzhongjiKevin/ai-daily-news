"""Verify QQ TLS delivery, error redaction, and CLI configuration selection."""

import smtplib
import ssl
import traceback
from email import policy
from email.parser import BytesParser
from unittest.mock import Mock

import pytest

from ai_daily.cli import _LazyMailer, required_send_secret_names
from ai_daily.mail import MailAuthError, MailSendError, QQMailer
from ai_daily.render import RenderedDigest


@pytest.fixture
def smtp(monkeypatch):
    connection = Mock()
    connection.sendmail.return_value = {}
    factory = Mock(return_value=connection)
    monkeypatch.setattr("ai_daily.mail.smtplib.SMTP_SSL", factory)
    return factory, connection


def digest():
    return RenderedDigest(subject="AI Daily 2026-09-07", text="中文日报",
                          html="<p>中文日报</p>", markdown="# 日报")


def test_qq_sends_tls_multipart_and_closes(smtp):
    factory, connection = smtp
    result = QQMailer("sender@qq.com", "secret", "reader@outlook.com").send(digest())
    assert result.startswith("accepted-")
    assert factory.call_args.args == ("smtp.qq.com", 465)
    context = factory.call_args.kwargs["context"]
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    connection.login.assert_called_once_with("sender@qq.com", "secret")
    sender, recipients, payload = connection.sendmail.call_args.args
    assert (sender, recipients) == ("sender@qq.com", ["reader@outlook.com"])
    message = BytesParser(policy=policy.default).parsebytes(payload)
    assert message.get_body(preferencelist=("plain",)).get_content().strip() == "中文日报"
    assert "中文日报" in message.get_body(preferencelist=("html",)).get_content()
    connection.close.assert_called_once()


@pytest.mark.parametrize("error,expected", [
    (smtplib.SMTPAuthenticationError(535, b"secret"), MailAuthError),
    (smtplib.SMTPServerDisconnected("secret"), MailSendError),
    (OSError("secret"), MailSendError),
])
def test_qq_redacts_errors_without_retry(smtp, error, expected):
    _, connection = smtp
    connection.login.side_effect = error
    mailer = QQMailer("sender@qq.com", "secret", "reader@outlook.com")
    with pytest.raises(expected) as caught:
        mailer.send(digest())
    assert "secret" not in "".join(traceback.format_exception(caught.value))
    connection.login.assert_called_once()
    connection.sendmail.assert_not_called()


def test_qq_recipient_refusal_is_not_success(smtp):
    smtp[1].sendmail.return_value = {"reader@outlook.com": (550, b"refused")}
    with pytest.raises(MailSendError):
        QQMailer("sender@qq.com", "secret", "reader@outlook.com").send(digest())


def test_qq_cli_requires_only_smtp_secrets(monkeypatch, smtp, tmp_path):
    monkeypatch.setenv("MAIL_PROVIDER", "qq")
    monkeypatch.setenv("SMTP_USERNAME", "sender@qq.com")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setenv("MAIL_TO", "reader@outlook.com")
    assert required_send_secret_names("off") == ("SMTP_USERNAME", "SMTP_PASSWORD", "MAIL_TO")
    assert _LazyMailer(tmp_path / "absent.enc").send(digest()).startswith("accepted-")
    assert smtp[1].sendmail.call_args.args[1] == ["reader@outlook.com", "sender@qq.com"]


def test_duplicate_recipients_only_receive_once(smtp):
    QQMailer("sender@qq.com", "secret", "sender@qq.com, sender@qq.com").send(digest())
    assert smtp[1].sendmail.call_args.args[1] == ["sender@qq.com"]


def test_all_recipient_addresses_are_validated(smtp):
    with pytest.raises(MailSendError):
        QQMailer("sender@qq.com", "secret", "reader@outlook.com, bad\r\nBcc: other@qq.com")
    smtp[0].assert_not_called()


def test_qq_cleanup_failure_does_not_undo_acceptance(smtp):
    smtp[1].close.side_effect = OSError("closed")
    assert QQMailer("sender@qq.com", "secret", "reader@outlook.com").send(digest())
