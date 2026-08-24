import httpx
import pytest
import respx

from ai_daily.http import RetryingClient


@respx.mock
def test_retries_transient_status_then_returns_response() -> None:
    route = respx.get("https://example.test/feed").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, text="ok")]
    )
    client = RetryingClient(max_attempts=2, backoff_seconds=0)
    assert client.get("https://example.test/feed").text == "ok"
    assert route.call_count == 2


@respx.mock
def test_permanent_status_is_attempted_once() -> None:
    route = respx.get("https://example.test/missing").mock(
        return_value=httpx.Response(404)
    )
    client = RetryingClient(max_attempts=3, backoff_seconds=0)
    with pytest.raises(httpx.HTTPStatusError):
        client.get("https://example.test/missing")
    assert route.call_count == 1
