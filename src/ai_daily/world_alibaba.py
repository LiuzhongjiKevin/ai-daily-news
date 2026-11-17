"""Alibaba TranslateGeneral RPC v2; one attempt, fixed endpoint, original-text fallback."""
import base64
import hashlib
import hmac
import http.client
import json
import logging
import os
import time
from collections import Counter
from datetime import UTC, datetime
from urllib.parse import quote, urlencode
from uuid import uuid4

import httpx

from ai_daily.translation_numbers import numbers_equivalent
from ai_daily.world_translation_budget import safe_month, units

ENDPOINT = "https://mt.cn-hangzhou.aliyuncs.com/"
KEY_NAMES = ("ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET")
NOTICE = "采用阿里云通用机器翻译，以原文为准；未翻译或翻译失败的内容保留原文。"


def http_debug_enabled():
    return bool(http.client.HTTPConnection.debuglevel or any(
        logging.getLogger(name).isEnabledFor(logging.DEBUG) for name in ("httpx", "httpcore")))


def sign_rpc(params, secret, *, method="POST"):
    # https://help.aliyun.com/en/sdk/product-overview/rpc-mechanism
    encode = lambda text: quote(str(text), safe="~")
    canonical = "&".join(encode(k) + "=" + encode(v) for k, v in sorted(params.items()))
    message = method + "&%2F&" + encode(canonical)
    return base64.b64encode(hmac.new((secret + "&").encode(), message.encode(),
                                    hashlib.sha1).digest()).decode()


class AlibabaTranslator:
    notice = NOTICE

    def __init__(self, key_id, key_secret, grant, *, client=None, clock=None):
        self._key_id, self._key_secret = key_id, key_secret
        self._grant = grant
        self._client = client or httpx.Client(
            transport=httpx.HTTPTransport(retries=0), timeout=5,
            follow_redirects=False, trust_env=False)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._deadline = time.monotonic() + 60
        self._cache = {}
        self._failed = False
        self.calls = self.characters = self.translated = 0
        self.reasons = Counter()

    @staticmethod
    def credentials():
        # No credential access at all until explicitly enabled.
        if os.environ.get("WORLD_ALIBABA_TRANSLATION_ENABLED") != "true":
            return None
        values = tuple(os.environ.get(name, "").strip() for name in KEY_NAMES)
        if not all(values) or any(any(c.isspace() for c in x) for x in values):
            return None
        return values

    def __enter__(self):
        return self

    def __exit__(self, *args):
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            self.reasons["close_error"] += 1
        print(f"Alibaba translation: requests={self.calls}, "
              f"reserved_submission_units={self.characters}, translated_fields={self.translated}")
        print("Alibaba decisions: " + json.dumps(dict(self.reasons), sort_keys=True))

    def translate(self, text):
        if text in self._cache:
            return self._cache[text]
        result = text
        reason = "not_authorized"
        try:
            if self._failed:
                reason = "circuit_open"
            elif http_debug_enabled():
                reason = "http_debug_disabled"
            elif (self.calls >= 40 or self.characters + units(text) > 12000
                    or time.monotonic() >= self._deadline):
                reason = "run_limit"
            elif self._grant.consume(text, self._clock()):
                params = {
                    "Action": "TranslateGeneral", "Version": "2018-10-12", "Format": "JSON",
                    "FormatType": "text", "Scene": "general",
                    "SourceLanguage": "en", "TargetLanguage": "zh", "SourceText": text,
                    "AccessKeyId": self._key_id, "SignatureMethod": "HMAC-SHA1",
                    "SignatureVersion": "1.0", "SignatureNonce": uuid4().hex,
                    "Timestamp": self._clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                params["Signature"] = sign_rpc(params, self._key_secret)
                remaining = self._deadline - time.monotonic()
                if remaining <= 0 or not safe_month(self._clock(), self._grant.month):
                    reason = "time_or_month_limit"
                else:
                    self.calls += 1
                    self.characters += units(text)
                    response = self._client.post(
                        ENDPOINT, content=urlencode(params),
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                        timeout=min(5, remaining / 4), follow_redirects=False)
                    response.raise_for_status()
                    body = response.json()
                    code = body.get("Code") if isinstance(body, dict) else None
                    if type(code) is not int or code != 200:
                        # Only bounded numeric business codes, never Message/RequestId/raw body.
                        reason = f"business_{code}" if type(code) is int and (
                            10000 <= code <= 19999) else "business_error"
                        self._failed = True
                    else:
                        data = body.get("Data")
                        translated = data.get("Translated") if isinstance(data, dict) else None
                        if (not isinstance(translated, str) or not translated.strip()
                                or len(translated) > 4000):
                            reason = "invalid_response"
                            self._failed = True
                        elif not numbers_equivalent(text, translated):
                            reason = "numeric_mismatch"
                        else:
                            result = translated.strip()
                            reason = "translated" if result != text else "provider_unchanged"
                            self.translated += result != text
        except Exception as error:  # noqa: BLE001 - never print exceptions or provider data
            self._failed = True
            reason = "timeout" if isinstance(error, httpx.TimeoutException) else "provider_error"
        self.reasons[reason] += 1
        self._cache[text] = result
        return result
