# Author: joelsnl and Anthropic Claude
"""How GoogleTranslator talks to each engine: Google (New / HTML / Old), Microsoft Edge,
LibreTranslate and a local Ollama. One method per engine, all returning the translated string
for one piece of text. Throttling, retries and caching stay in core/translator.py.
"""

from __future__ import annotations

import base64
import json
import time
from html import unescape
from typing import Any, Dict, Optional, Tuple

from core.gtx_throttle import RateLimitedError, parse_retry_after
from core.ollama_setup import list_ollama_models, ollama_infer_options
from core.parser import CHROME_UA

# Widget keys shipped in Google's Translate Element / the Calibre plugin.
# Not a user Cloud Translation secret.
_GOOGLE_PA_KEY = "AIzaSyDLEeFI5OtFBwYBIoK_jj5m32rZK5CkCXA"
_GOOGLE_HTML_KEY = "AIzaSyATBXajvzQLTDHEQbcpq0Ihe0vWDHmO520"


class EngineRequestsMixin:
    """The ``_request_*`` methods of GoogleTranslator (it supplies the settings they read)."""

    ENDPOINT = 'https://translate.googleapis.com/translate_a/single'
    ENDPOINT_PA = 'https://translate-pa.googleapis.com/v1/translate'
    ENDPOINT_HTML = 'https://translate-pa.googleapis.com/v1/translateHtml'
    ENDPOINT_EDGE_AUTH = 'https://edge.microsoft.com/translate/auth'
    ENDPOINT_EDGE = 'https://api-edge.cognitive.microsofttranslator.com/translate'
    USER_AGENT = CHROME_UA
    _OLLAMA_SYSTEM = (
        "You are a literary translator. Translate the user's Chinese web-novel "
        "text into fluent natural English. Keep names and terms consistent. "
        "Do not add notes, titles, or commentary. Output only the translation."
    )

    def _raise_if_rate_limited(self, response: Any) -> None:
        status = int(getattr(response, "status_code", 0) or 0)
        if status == 429:
            wait = parse_retry_after(response)
            raise RateLimitedError(wait)

    def _request_http(
        self,
        method: str,
        url: str,
        *,
        allow_http: bool = False,
        resolve_dns: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Session + UA + SSRF wrapper + 429/status. Caller parses the body."""
        from core.security import safe_http_request

        session = self._get_http_session()
        headers = dict(kwargs.pop("headers", None) or {})
        headers.setdefault("User-Agent", self.USER_AGENT)
        kwargs.setdefault("timeout", self._timeout)
        response = safe_http_request(
            session,
            method,
            url,
            allow_http=allow_http,
            resolve_dns=resolve_dns,
            headers=headers,
            **kwargs,
        )
        self._raise_if_rate_limited(response)
        response.raise_for_status()
        return response

    def _request_google(self, text: str) -> str:
        """Translate via the selected free Google engine."""
        if self.backend == "google_html":
            return self._request_google_html(text)
        if self.backend == "google_gtx":
            return self._request_google_gtx(text)
        return self._request_google_pa(text)

    def _request_google_gtx(self, text: str) -> str:
        """Legacy translate_a/single?client=gtx (walled for many IPs since 2026)."""
        params = {
            'client': 'gtx',
            'sl': self.source_lang,
            'tl': self.target_lang,
            'dt': 't',
            'dj': '1',
            'q': text
        }
        if len(text) <= 1800:
            response = self._request_http(
                "GET", self.ENDPOINT, params=params,
            )
        else:
            response = self._request_http(
                "POST", self.ENDPOINT, data=params,
            )
        data = response.json()
        return ''.join(
            s.get('trans', '')
            for s in data.get('sentences', [])
            if 'trans' in s
        )

    def _request_google_pa(self, text: str) -> str:
        """Google (Free) New — same as Calibre Ebook Translator v2.4+."""
        params = {
            "params.client": "gtx",
            "query.source_language": self.source_lang or "zh-CN",
            "query.target_language": self.target_lang or "en",
            "query.display_language": "en-US",
            "data_types": "TRANSLATION",
            "key": _GOOGLE_PA_KEY,
            "query.text": text,
        }
        response = self._request_http(
            "GET", self.ENDPOINT_PA, params=params,
        )
        data = response.json() if hasattr(response, "json") else {}
        out = unescape(str((data or {}).get("translation") or "")).strip()
        if not out:
            raise ValueError("Empty Google (New) translation")
        return out

    def _request_google_html(self, text: str) -> str:
        """Google (Free) HTML — translateHtml widget endpoint."""
        escaped = (
            (text or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        body = json.dumps(
            [[[escaped], self.source_lang or "zh-CN", self.target_lang or "en"], "wt_lib"],
            ensure_ascii=False,
        )
        response = self._request_http(
            "POST", self.ENDPOINT_HTML,
            data=body.encode("utf-8"),
            headers={
                "Content-Type": "application/json+protobuf",
                "X-Goog-Api-Key": _GOOGLE_HTML_KEY,
            },
        )
        data = response.json() if hasattr(response, "json") else None
        chunk = data[0][0] if isinstance(data, list) and data and data[0] else ""
        if isinstance(chunk, list):
            chunk = "".join(str(p) for p in chunk)
        out = unescape(str(chunk or "")).strip()
        if not out:
            raise ValueError("Empty Google (HTML) translation")
        return out

    def _microsoft_lang(self, code: str) -> str:
        raw = (code or "en").replace("_", "-")
        mapped = {
            "zh-cn": "zh-Hans",
            "zh": "zh-Hans",
            "zh-tw": "zh-Hant",
            "zh-hk": "zh-Hant",
        }
        return mapped.get(raw.lower(), raw)

    def _edge_auth_token(self) -> str:
        now = time.time()
        if self._edge_token and now < self._edge_token_exp - 60:
            return self._edge_token
        response = self._request_http("GET", self.ENDPOINT_EDGE_AUTH)
        token = (getattr(response, "text", None) or "").strip().strip('"')
        if not token or token.count(".") < 2:
            raise ValueError("Microsoft Edge auth did not return a token")
        exp = now + 300
        try:
            payload = token.split(".")[1]
            pad = "=" * (-len(payload) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload + pad).decode("utf-8"))
            exp = float(data.get("exp") or exp)
        except Exception:
            pass
        self._edge_token = token
        self._edge_token_exp = exp
        return token

    def _request_microsoft(self, text: str) -> str:
        """Microsoft Edge (Free) — same unofficial path as the Calibre plugin."""
        from urllib.parse import urlencode

        token = self._edge_auth_token()
        query = {
            "to": self._microsoft_lang(self.target_lang),
            "api-version": "3.0",
            "includeSentenceLength": True,
            "from": self._microsoft_lang(self.source_lang),
        }
        url = f"{self.ENDPOINT_EDGE}?{urlencode(query)}"
        response = self._request_http(
            "POST", url,
            data=json.dumps([{"text": text}], ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        data = response.json() if hasattr(response, "json") else []
        try:
            out = unescape(str(data[0]["translations"][0]["text"])).strip()
        except (IndexError, KeyError, TypeError):
            out = ""
        if not out:
            raise ValueError("Empty Microsoft Edge translation")
        return out

    def _request_libretranslate(self, text: str) -> str:
        """Translate via a LibreTranslate server."""
        from core.security import UnsafeURLError
        # LibreTranslate uses plain ISO codes ('zh', not 'zh-CN')
        source = self.source_lang.split('-')[0]
        try:
            response = self._request_http(
                "POST",
                f'{self.libretranslate_url}/translate',
                allow_http=True,
                resolve_dns=True,
                json={
                    'q': text,
                    'source': source,
                    'target': self.target_lang,
                    'format': 'text',
                },
            )
        except UnsafeURLError as e:
            raise ValueError(f"Blocked LibreTranslate URL: {e}") from e
        return response.json().get('translatedText', '')

    def _request_ollama(
        self,
        text: str,
        *,
        system: Optional[str] = None,
        timeout: Optional[Tuple[int, int]] = None,
        temperature: float = 0.2,
        extra_options: Optional[Dict[str, Any]] = None,
        allow_empty: bool = False,
        think: Optional[bool] = None,
    ) -> str:
        """Chat with a local Ollama instance (loopback only)."""
        from core.security import UnsafeURLError, safe_http_request, validate_ollama_url
        if not self.ollama_model:
            raise ValueError(
                "Ollama model is empty. Set a model (e.g. qwen2.5:3b) and run: "
                "ollama pull qwen2.5:3b"
            )
        try:
            base = validate_ollama_url(self.ollama_url or self.DEFAULT_OLLAMA_URL)
        except UnsafeURLError as e:
            raise ValueError(f"Blocked Ollama URL: {e}") from e
        session = self._get_http_session()
        options: Dict[str, Any] = {'temperature': temperature}
        options.update(ollama_infer_options())
        if extra_options:
            options.update(extra_options)
        payload: Dict[str, Any] = {
            'model': self.ollama_model,
            'stream': False,
            'keep_alive': '10m',
            'messages': [
                {'role': 'system', 'content': system or self._OLLAMA_SYSTEM},
                {'role': 'user', 'content': text},
            ],
            'options': options,
        }
        if think is not None:
            payload['think'] = think
        try:
            response = safe_http_request(
                session,
                "POST",
                f'{base}/api/chat',
                allow_http=True,
                allow_loopback=True,
                timeout=timeout or self._timeout,
                json=payload,
                headers={
                    'User-Agent': self.USER_AGENT,
                    'Content-Type': 'application/json',
                },
            )
        except UnsafeURLError as e:
            raise ValueError(f"Blocked Ollama URL: {e}") from e
        except Exception as e:
            err = str(e).lower()
            if any(s in err for s in ('connection', 'refused', '10061', 'timed out', 'timeout')):
                raise ValueError(
                    "Ollama is not running. Install from https://ollama.com "
                    f"then run: ollama pull {self.ollama_model}"
                ) from e
            raise
        if getattr(response, 'status_code', 0) == 404:
            installed = list_ollama_models(base, timeout=1.5)
            hint = (
                f"Installed: {', '.join(installed)}. Pick one in Translator, or run: "
                f"ollama pull {self.ollama_model}"
                if installed
                else f"Run: ollama pull {self.ollama_model}"
            )
            raise ValueError(
                f"Ollama model '{self.ollama_model}' is not installed. {hint}"
            )
        response.raise_for_status()
        data = response.json() if hasattr(response, 'json') else {}
        if isinstance(data, dict) and data.get('error'):
            err = str(data['error'])
            if 'not found' in err.lower():
                installed = list_ollama_models(base, timeout=1.5)
                hint = (
                    f"Installed: {', '.join(installed)}. Pick one in Translator, or run: "
                    f"ollama pull {self.ollama_model}"
                    if installed
                    else f"Run: ollama pull {self.ollama_model}"
                )
                raise ValueError(
                    f"Ollama model '{self.ollama_model}' is not installed. {hint}"
                )
            raise ValueError(f"Ollama: {err}")
        msg = ((data.get('message') or {}).get('content') or '').strip()
        if not msg:
            if allow_empty:
                return ""
            raise ValueError("Ollama returned an empty translation")
        return msg

    def _request_translation(self, text: str) -> str:
        if self.backend == 'libretranslate':
            return self._request_libretranslate(text)
        if self.backend == 'ollama':
            return self._request_ollama(text)
        if self.backend == 'microsoft':
            return self._request_microsoft(text)
        return self._request_google(text)
