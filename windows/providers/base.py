"""Tiny HTTP/JSON helper shared by the quota providers."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request

DEFAULT_UA = "AgentHUD-Windows/1.0"
_LOOPBACK_CTX = ssl.create_default_context()
_LOOPBACK_CTX.check_hostname = False
_LOOPBACK_CTX.verify_mode = ssl.CERT_NONE


class ProviderError(Exception):
    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind


def http_json(url: str, *, headers: dict | None = None, body=None, method: str | None = None,
              timeout: float = 20, insecure: bool = False, raw_body: bytes | None = None):
    """Return (status, parsed_json_or_None, text). Never raises for HTTP errors."""
    data = raw_body
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    request.add_header("User-Agent", DEFAULT_UA)
    for key, value in (headers or {}).items():
        if value is not None:
            request.add_header(key, str(value))
    context = _LOOPBACK_CTX if insecure else None
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            text = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        status = exc.code
    except Exception as exc:
        return -1, None, f"{type(exc).__name__}: {exc}"
    try:
        return status, json.loads(text), text
    except Exception:
        return status, None, text


def walk(value, *keys, default=None):
    """Safe nested lookup: walk(doc, 'a', 'b') reads doc['a']['b']."""
    current = value
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        elif isinstance(current, list) and isinstance(key, int) and -len(current) <= key < len(current):
            current = current[key]
        else:
            return default
        if current is None:
            return default
    return current


def number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except Exception:
            return None
    return None


def iso_to_epoch(text):
    """Parse an ISO-8601 timestamp (with Z or offset) into epoch seconds."""
    if not isinstance(text, str) or not text.strip():
        return None
    import datetime
    value = text.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()
