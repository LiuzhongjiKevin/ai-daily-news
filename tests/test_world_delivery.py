from datetime import UTC, datetime

import pytest

from ai_daily.models import RawItem
from ai_daily.state import StateStore
from ai_daily.world_finance import deliver, world_mailer

NOW = datetime(2026, 9, 9, 0, tzinfo=UTC)


def rows():
    return [RawItem(source_id=source, source_name=source, source_type="media",
                    title="Trade peace", canonical_url=f"https://example.com/{source}",
                    category="world", published_at=NOW) for source in ("bbc-world", "un-world")]


def test_mail_follows_durable_reservation_and_records_acceptance(tmp_path):
    state = StateStore(tmp_path / "world-state")
    events = []

    def checkpoint():
        current = state.load_run_state()
        events.append("accepted" if current.sent_dates else "reserved")

    class Mailer:
        def send(self, rendered):
            assert events == ["reserved"]
            events.append("mail")
            return "accepted-world-test"

    assert deliver(tmp_path, rows(), NOW, [], Mailer(), checkpoint) == "accepted"
    assert events == ["reserved", "mail", "accepted"]
    assert state.load_run_state().sent_dates == {"2026-09-09": "accepted-world-test"}
    assert not (tmp_path / "data").exists()
    assert deliver(tmp_path, rows(), NOW, [], Mailer(), checkpoint) == "already_sent"
    assert events == ["reserved", "mail", "accepted"]


def test_failed_checkpoint_prevents_mail(tmp_path):
    class Mailer:
        def send(self, rendered):
            pytest.fail("Mail must not run after a failed reservation push")

    def fail():
        raise RuntimeError("push failed")

    with pytest.raises(RuntimeError):
        deliver(tmp_path, rows(), NOW, [], Mailer(), fail)
    assert StateStore(tmp_path / "world-state").load_run_state().delivery_intents


def test_smtp_error_preserves_ambiguous_intent(tmp_path):
    class Mailer:
        def send(self, rendered):
            raise OSError("SMTP disconnected")

    with pytest.raises(OSError):
        deliver(tmp_path, rows(), NOW, [], Mailer(), lambda: None)
    intent = StateStore(tmp_path / "world-state").load_run_state().delivery_intents["2026-09-09"]
    assert intent.status == "ambiguous"


def test_only_new_list_receives_mail(monkeypatch):
    monkeypatch.setenv("SMTP_USERNAME", "sender@qq.com")
    monkeypatch.setenv("SMTP_PASSWORD", "example-secret")
    monkeypatch.setenv("MAIL_TO", "new@example.com,second@example.com,new@example.com")
    mailer = world_mailer()
    assert mailer.recipients == ["new@example.com", "second@example.com"]


def test_one_publisher_is_not_enough_to_send(tmp_path):
    with pytest.raises(ValueError):
        deliver(tmp_path, rows()[:1], NOW, [], None, lambda: None)
    assert not (tmp_path / "world-state" / "state.json").exists()
