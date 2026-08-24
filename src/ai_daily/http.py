import time

import httpx


class RetryingClient:
    def __init__(self, max_attempts: int = 3, backoff_seconds: float = 1.0) -> None:
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self._client = httpx.Client(timeout=httpx.Timeout(20.0), follow_redirects=True)

    def get(self, url: str, **kwargs) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                response = self._client.get(url, **kwargs)
                if response.status_code not in {429, 500, 502, 503, 504}:
                    response.raise_for_status()
                    return response
                last_error = httpx.HTTPStatusError(
                    f"transient status {response.status_code}",
                    request=response.request,
                    response=response,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
            if attempt + 1 < self.max_attempts:
                time.sleep(self.backoff_seconds * (2**attempt))
        assert last_error is not None
        raise last_error
