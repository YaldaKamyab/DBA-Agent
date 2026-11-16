"""LLM client (Ollama + OpenAI-compatible) using only the standard library."""
import json
import re
import time
import urllib.error
import urllib.request

import math
import socket
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from .errors import InvalidResponseError, LLMError, MalformedResponseError, ProviderError

_QUOTA_WORDS = ("quota", "insufficient_quota", "billing", "insufficient balance", "insufficient_balance",
                "insufficient credit", "out of credit", "exceeded your current", "resource_exhausted", "credit balance")
# explicit, permanent account exhaustion (retrying cannot help). A bare "quota"/RESOURCE_EXHAUSTED may be a
# per-minute limit, so it is NOT enough to call a 429 permanent.
_PERMANENT_QUOTA_WORDS = ("insufficient_quota", "insufficient quota", "billing", "insufficient balance", "insufficient_balance",
                          "insufficient credit", "out of credit", "credit balance", "exceeded your current quota",
                          "quota exhausted", "balance is not enough", "payment required")
_TRANSIENT_HINTS = ("per minute", "per second", "retry in", "retry after", "try again in", "rate limit", "too many requests")
_REQUEST_ID_HEADERS = ("x-request-id", "request-id", "x-goog-request-id", "x-amzn-requestid", "openai-request-id")


# Longest Retry-After we will honour by sleeping. A larger request means "come back much later": retrying
# now would only waste attempts, so the call stops and reports the wait instead of sleeping.
MAX_RETRY_AFTER_S = 60.0

_CRED_FIELD = (r"api[-_ ]?key|x-api-key|x-goog-api-key|authorization|proxy-authorization|access[_-]?token|"
               r"auth[_-]?token|refresh[_-]?token|client[_-]?secret|secret[_-]?key|private[_-]?key")
_TOKEN_SHAPES = re.compile(r"AIza[0-9A-Za-z_\-]{30,}|\bsk-[A-Za-z0-9_\-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}")


def redact(text, *secrets):
    """Remove credentials (exact configured keys, bearer tokens, auth headers/fields, key= URL params,
    well-known key shapes) from text that may be shown or logged. Ordinary business text is left alone."""
    text = str(text)
    for s in secrets:
        if s and len(s) >= 4:
            text = text.replace(s, "[REDACTED]")
    text = re.sub(r"(?i)(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}", r"\1 [REDACTED]", text)
    text = re.sub(r"(?i)([?&](?:key|api_?key|access_token)=)[^&\s\"']+", r"\1[REDACTED]", text)
    text = re.sub(r"""(?i)(?<![A-Za-z0-9])(""" + _CRED_FIELD + r""")(["']?\s*[:=]\s*["']?)(?:(?:bearer|basic)\s+)?[^\s"',}]+""",
                  r"\1\2[REDACTED]", text)
    return _TOKEN_SHAPES.sub("[REDACTED]", text)


def _header(headers, name):
    """Case-insensitive header lookup that works for http.client.HTTPMessage and plain dicts."""
    if headers is None:
        return None
    try:
        items = headers.items()
    except AttributeError:
        return None
    for k, v in items:
        if str(k).lower() == name:
            return v
    return None


def parse_retry_after(value, now=None):
    """Seconds to wait from a Retry-After header (delay-seconds or HTTP-date); None if absent/invalid."""
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    try:
        secs = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        secs = (when - (now or datetime.now(timezone.utc))).total_seconds()
        return max(0.0, secs)
    if math.isnan(secs) or math.isinf(secs) or secs < 0:
        return None
    return secs


def _host(url):
    return urlsplit(url).netloc or url


def parse_provider_error(body):
    """Best-effort (message, code, request_id) from an OpenAI/Gemini-style error body (dict or [dict])."""
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return (str(body or "").strip()[:300], "", "")
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        return (str(body)[:300], "", "")
    err = data.get("error", data)
    if isinstance(err, str):
        return (err[:300], "", str(data.get("request_id") or ""))
    if not isinstance(err, dict):
        return (str(body)[:300], "", "")
    code = err.get("code") or err.get("type") or err.get("status") or ""
    msg = err.get("message") or data.get("message") or ""
    return (str(msg)[:300], str(code), str(err.get("request_id") or data.get("request_id") or ""))


def classify_http_error(status, body, headers=None, host="", model="", key_env="", secrets=()):
    """Turn an HTTP failure into a concise, actionable ProviderError (no credentials in the text)."""
    msg, code, rid = parse_provider_error(redact(body or "", *secrets))   # redact before truncating
    for h in _REQUEST_ID_HEADERS:
        v = _header(headers, h)
        if v:
            rid = rid or str(v)
            break
    retry_after = parse_retry_after(_header(headers, "retry-after")) if status in (429, 503) else None
    evidence = f"{msg} {code}".lower()
    quota = any(w in evidence for w in _QUOTA_WORDS)
    permanent = any(w in evidence for w in _PERMANENT_QUOTA_WORDS) and not (
        status == 429 and any(h in evidence for h in _TRANSIENT_HINTS) and "insufficient_quota" not in evidence)
    if _header(headers, "retry-after") is not None and not permanent:
        quota = False                                   # the provider says to try again later: a transient limit
    where = f" ({host}" + (f", model {model}" if model else "") + ")" if host else ""
    if status == 401:
        kind, retry = "auth", False
        text = ("API authentication failed (HTTP 401): the provider did not accept the API key" + where +
                (f". Check the key stored in the {key_env} environment variable." if key_env else ". Check the configured API key."))
    elif status == 403:
        retry = False
        if quota or permanent:
            kind = "quota"
            text = f"API quota/billing problem reported by the provider (HTTP 403){where}. Check the account's quota or balance."
        else:
            kind = "forbidden"
            text = (f"API request refused by the provider (HTTP 403, not authorized){where}. The key may lack access to this "
                    "model or endpoint, or the account/region may be blocked. This is not a design-revision result.")
    elif status == 429:
        if permanent:
            kind, retry = "quota", False
            text = (f"API quota or balance exhausted according to the provider (HTTP 429){where}. "
                    "Retrying will not help; check the account's quota, billing or balance.")
        else:
            kind, retry = "rate_limit", True
            text = f"API rate limit reached (HTTP 429){where}. Wait a little and try again."
            if quota:
                text += " (The provider mentions a quota; it does not say the account is exhausted.)"
    elif status == 503:
        kind, retry = "unavailable", True
        text = f"The provider is temporarily unavailable (HTTP 503){where}. Try again later."
    elif status == 404:
        kind, retry = "http", False
        text = f"API endpoint or model not found (HTTP 404){where}. Check 'base_url' and 'model' in the config."
    elif status >= 500:
        kind, retry = "server", True
        text = f"The provider reported a server error (HTTP {status}){where}. Try again later."
    else:
        kind, retry = "http", status == 408
        text = f"API request failed (HTTP {status}){where}."
    if msg:
        text += f" Provider message: {msg}"
    if code and code.lower() not in msg.lower():
        text += f" [code: {redact(code, *secrets)}]"
    if rid:
        text += f" [request id: {redact(rid, *secrets)}]"
    if kind == "quota":
        retry_after = None
    return ProviderError(text, retryable=retry, kind=kind, status=status, request_id=rid or None, retry_after=retry_after)


def extract_json(text):
    """Parse a JSON object from model output (tolerates fences / extra prose)."""
    if not text or not text.strip():
        raise MalformedResponseError("Model returned an empty response.")
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL | re.IGNORECASE)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise MalformedResponseError("Model response contains no JSON object.")
        try:
            data = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError as e:
            raise MalformedResponseError(f"Model response is not valid JSON: {e}")
    if not isinstance(data, dict):
        raise MalformedResponseError("Model response JSON is not an object.")
    return data


class LLMClient:
    def __init__(self, config, logger=None):
        self.config = config
        self.logger = logger

    # ---- transport (patched in tests) ----------------------------------
    def _post(self, url, payload, headers):
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        for k, v in headers.items():
            req.add_header(k, v)
        c = self.config
        secrets = (c.api_key,) + tuple(v for k, v in headers.items() if k.lower() in ("authorization", "x-goog-api-key"))
        host = _host(url)
        try:
            with urllib.request.urlopen(req, timeout=c.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:2000]
            except Exception:
                pass
            raise classify_http_error(e.code, detail, getattr(e, "headers", None), host, c.model,
                                      getattr(c, "api_key_env", ""), secrets)
        except urllib.error.URLError as e:
            if isinstance(e.reason, (TimeoutError, socket.timeout)):
                raise ProviderError(f"API request timed out after {c.timeout:g}s ({host}). Try again or raise 'timeout' in the config.",
                                    retryable=True, kind="timeout")
            raise ProviderError(redact(f"Cannot connect to {host}: {e.reason}. Check the network connection and 'base_url'.", *secrets),
                                retryable=True, kind="network")
        except (TimeoutError, socket.timeout):
            raise ProviderError(f"API request timed out after {c.timeout:g}s ({host}). Try again or raise 'timeout' in the config.",
                                retryable=True, kind="timeout")
        except OSError as e:
            raise ProviderError(redact(f"Network problem talking to {host}: {e}", *secrets), retryable=True, kind="network")
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise MalformedResponseError(f"The provider returned a non-JSON response body ({host}): {e}")

    # ---- provider calls -------------------------------------------------
    def _chat(self, system, user):
        c = self.config
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        headers = dict(c.extra_headers)
        if c.provider == "ollama":
            payload = {
                "model": c.model,
                "messages": messages,
                "stream": False,
                "options": {"temperature": c.temperature},
            }
            if c.json_mode:
                payload["format"] = "json"
            data = self._post(f"{c.base_url}/api/chat", payload, headers)
            try:
                return data["message"]["content"]
            except (KeyError, TypeError):
                raise MalformedResponseError(f"Unexpected Ollama response shape: {redact(str(data)[:200], self.config.api_key)}")
        # openai_compatible
        if c.api_key:
            headers["Authorization"] = f"Bearer {c.api_key}"
        payload = {"model": c.model, "messages": messages, "temperature": c.temperature}
        if c.json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = self._post(f"{c.base_url}/chat/completions", payload, headers)
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise MalformedResponseError(f"Unexpected API response shape: {redact(str(data)[:200], self.config.api_key)}")

    # ---- public API -------------------------------------------------------
    def _log(self, stage, attempt, system, user, output, error, started):
        if self.logger:
            k = self.config.api_key
            self.logger.log(
                stage=stage, attempt=attempt, provider=self.config.provider,
                model=self.config.model, system=redact(system, k), input=redact(user, k),
                output=None if output is None else redact(output, k),
                error=None if error is None else redact(error, k), duration_s=time.time() - started,
            )

    def _retry_wait(self, e, attempt):
        """Seconds to sleep before the next attempt after provider error e; None = stop retrying now."""
        ra = getattr(e, "retry_after", None)
        if ra is None:
            return self.config.retry_delay * attempt        # unchanged default back-off
        if ra > MAX_RETRY_AFTER_S:
            return None
        return ra

    def complete_json(self, stage, system, user, validator=None):
        """Call the LLM and return a validated dict.

        Retries up to config.max_attempts on transient provider errors (honouring a bounded Retry-After) and on
        invalid/unvalidatable responses. Permanent errors (401/403/quota/404...) are never retried.
        Every attempt is logged with credentials redacted.
        """
        max_attempts = self.config.max_attempts
        last_error = None
        prompt = user
        for attempt in range(1, max_attempts + 1):
            started = time.time()
            output = None
            try:
                output = self._chat(system, prompt)
                data = extract_json(output)
                if validator:
                    data = validator(data)
                self._log(stage, attempt, system, prompt, output, None, started)
                return data
            except LLMError as e:
                last_error = e
                self._log(stage, attempt, system, prompt, output, str(e), started)
                if not e.retryable:
                    raise
                if isinstance(e, InvalidResponseError):
                    prompt = (
                        user + "\n\nYour previous answer was rejected: "
                        f"{e}\nReturn ONLY a corrected, complete JSON object."
                    )
                elif attempt < max_attempts:
                    wait = self._retry_wait(e, attempt)
                    if wait is None:
                        raise ProviderError(
                            f"LLM call for stage '{stage}' stopped after {attempt} attempt(s): the provider asked to wait "
                            f"{e.retry_after:.0f}s (more than the {MAX_RETRY_AFTER_S:.0f}s limit); not retrying. {e}",
                            retryable=False, kind=e.kind, status=e.status, request_id=e.request_id, retry_after=e.retry_after)
                    if wait:
                        time.sleep(wait)
        msg = f"LLM call for stage '{stage}' failed after {max_attempts} attempt(s): {last_error}"
        # keep the failure class so callers can tell provider failures from rejected/malformed answers
        if isinstance(last_error, ProviderError):
            raise ProviderError(msg, retryable=False, kind=last_error.kind, status=last_error.status,
                                request_id=last_error.request_id, retry_after=last_error.retry_after)
        if isinstance(last_error, MalformedResponseError):
            raise MalformedResponseError(msg, retryable=False)
        if isinstance(last_error, InvalidResponseError):
            raise InvalidResponseError(msg, retryable=False)
        raise LLMError(msg, retryable=False)
