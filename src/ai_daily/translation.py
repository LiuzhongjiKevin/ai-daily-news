"""Optional, bounded English-to-Chinese TMT translation; never required for delivery."""

import hashlib
import hmac
import json
import os
import re
import time
from collections import Counter
from datetime import UTC, datetime

import httpx

from ai_daily.models import Digest
from ai_daily.translation_numbers import numbers_equivalent

HOST = 'tmt.tencentcloudapi.com'
NOTICE = '部分内容为腾讯云机器翻译，以原文为准；翻译费用不包含在 AI 用量统计中。'


def _authorization(payload, timestamp, secret_id, secret_key, *, host=HOST,
                   service='tmt', action='TextTranslate'):
    """TC3 procedure: https://cloud.tencent.com/document/api/551/30636."""
    day = datetime.fromtimestamp(timestamp, UTC).strftime('%Y-%m-%d')
    headers = (f'content-type:application/json; charset=utf-8\nhost:{host}\n'
               f'x-tc-action:{action.lower()}\n')
    signed = 'content-type;host;x-tc-action'
    canonical = f'POST\n/\n\n{headers}\n{signed}\n{hashlib.sha256(payload).hexdigest()}'
    scope = f'{day}/{service}/tc3_request'
    message = (f'TC3-HMAC-SHA256\n{timestamp}\n{scope}\n'
               f'{hashlib.sha256(canonical.encode()).hexdigest()}')
    key = ('TC3' + secret_key).encode()
    for value in (day, service, 'tc3_request'):
        key = hmac.new(key, value.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, message.encode(), hashlib.sha256).hexdigest()
    return (f'TC3-HMAC-SHA256 Credential={secret_id}/{scope}, '
            f'SignedHeaders={signed}, Signature={signature}')


class TmtTranslator:
    def __init__(self, secret_id, secret_key, *, client=None, max_characters=12000):
        self._secret_id, self._secret_key = secret_id, secret_key
        self._client = client or httpx.Client(timeout=5, follow_redirects=False)
        self._cache = {}
        self._failed = False
        self._deadline = time.monotonic() + 60
        self._next_request = 0.0
        self._max_characters = max_characters
        self.characters = 0
        self.calls = 0
        self.translated = 0
        self.reasons = Counter()

    @classmethod
    def from_environment(cls):
        values = [os.environ.get(name, '').strip() for name in
                  ('TENCENTCLOUD_SECRET_ID', 'TENCENTCLOUD_SECRET_KEY')]
        if not all(values) or any(any(c.isspace() for c in value) for value in values):
            return None
        try:
            return cls(*values)
        except Exception:  # noqa: BLE001 - optional provider initialization must not block SMTP
            return None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 - optional transport cleanup
            self._failed = True
        # No credentials, source text, provider bodies, or exception messages in logs.
        print(f'TMT: requests={self.calls}, submitted_characters={self.characters}, '
              f'translated_fields={self.translated}, fallback={self._failed}')
        print('TMT decisions: ' + json.dumps(dict(self.reasons), sort_keys=True))

    def _keep_original(self, text, reason):
        self._cache[text] = text
        self.reasons[reason] += 1
        return text

    def translate(self, text):
        if text in self._cache:
            self.reasons['cached'] += 1
            return self._cache[text]
        # Short English fields only. Preserve Chinese/mixed Chinese, URLs and long fields.
        for condition, reason in (
            (not text, 'empty'), (len(text) > 1000, 'field_too_long'),
            (bool(re.search(r'[\u3400-\u9fff]', text)), 'already_chinese_or_mixed'),
            (not re.search(r'[A-Za-z]', text), 'no_latin_text'), ('://' in text, 'contains_url'),
            (self._failed, 'circuit_open'), (self.calls >= 40, 'request_limit'),
            (self.characters + len(text) > self._max_characters, 'character_limit'),
            (time.monotonic() >= self._deadline, 'time_limit'),
        ):
            if condition:
                return self._keep_original(text, reason)
        try:
            time.sleep(max(0, self._next_request - time.monotonic()))
            self._next_request = time.monotonic() + 0.25
            payload = json.dumps({'SourceText': text, 'Source': 'en', 'Target': 'zh',
                                  'ProjectId': 0}, ensure_ascii=False).encode('utf-8')
            timestamp = int(time.time())
            headers = {
                'Authorization': _authorization(payload, timestamp, self._secret_id, self._secret_key),
                'Content-Type': 'application/json; charset=utf-8', 'Host': HOST,
                'X-TC-Action': 'TextTranslate', 'X-TC-Version': '2018-03-21',
                'X-TC-Timestamp': str(timestamp), 'X-TC-Region': 'ap-guangzhou',
            }
            self.characters += len(text)
            self.calls += 1
            response = self._client.post(f'https://{HOST}/', content=payload, headers=headers,
                                         timeout=5, follow_redirects=False)
            response.raise_for_status()
            body = response.json()['Response']
            result = body.get('TargetText')
            if 'Error' in body or not isinstance(result, str) or not result.strip() or len(result) > 4000:
                raise ValueError('Invalid translation response')
            # A simple guard, not a guarantee of financial or linguistic accuracy.
            if not numbers_equivalent(text, result):
                return self._keep_original(text, 'numeric_mismatch')
            result = result.strip()
            self._cache[text] = result
            self.translated += result != text
            self.reasons['translated' if result != text else 'provider_unchanged'] += 1
            return result
        except Exception:  # noqa: BLE001 - fail open to ORIGINAL TEXT, never to repeated API calls
            self._failed = True
            return self._keep_original(text, 'provider_error')


def translate_digest(digest: Digest) -> Digest:
    service = TmtTranslator.from_environment()
    if service is None:
        return digest
    result = digest.model_copy(deep=True)
    with service:
        for cluster in result.news:
            cluster.title = service.translate(cluster.title)
            cluster.summary = service.translate(cluster.summary)
        for repo in result.repositories:
            repo.snapshot.description = service.translate(repo.snapshot.description)
        if service.translated:
            result.warnings.append(NOTICE)
    return result
