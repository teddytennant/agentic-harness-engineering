#!/usr/bin/env python3
"""Local OpenAI-compatible proxy backed by wizard's xAI OAuth session.

wizard's `wizard --login xai` flow stores a Bearer token in
~/.wizard/xai_oauth.json and uses it against the OpenAI-compatible Chat
Completions API at https://api.x.ai/v1. This proxy reuses that stored session so
any OpenAI-compatible client — AHE's evolve-agent AND the in-container wizard —
can talk to Grok via your subscription with no API key:

    LLM_BASE_URL=http://localhost:8080/v1   LLM_API_KEY=unused   model=grok-4.3

It keeps the access token fresh (proactive refresh near JWT expiry, plus a forced
refresh after a 401), so long multi-hour runs don't die when the token rotates.
The client's own Authorization header is ignored and replaced with the fresh
bearer, so clients can send any dummy key.

Stdlib only. Forwards exclusively to api.x.ai (never an open proxy). Streaming
(SSE) is passed through unbuffered.

Caveats:
- OAuth API access is gated to certain SuperGrok plans (xAI returns 403 if yours
  lacks it). If you see 403s, you need a plain XAI_API_KEY instead.
- This is your personal subscription: keep n_concurrent modest to avoid rate
  limits, and note that programmatic loop use may differ from interactive terms.
- Binding 0.0.0.0 (needed so Docker containers can reach it via
  host.docker.internal) exposes a token-injecting proxy on your LAN. Use
  HOST=127.0.0.1 if only the host-side evolve-agent needs it.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

UPSTREAM = "https://api.x.ai"  # /v1/... appended from the request path
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"  # wizard's public OAuth client
EXPIRY_LEEWAY = 120  # refresh when the access token expires within this many seconds
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host",
}

_TOKEN_PATH = Path(os.environ.get("XAI_OAUTH_PATH", Path.home() / ".wizard" / "xai_oauth.json"))
_lock = threading.Lock()


def _b64url_json(segment: str) -> dict:
    pad = "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment + pad))


def _jwt_exp(token: str) -> int | None:
    try:
        return int(_b64url_json(token.split(".")[1]).get("exp"))
    except Exception:
        return None


def _load() -> dict:
    if not _TOKEN_PATH.is_file():
        raise SystemExit(
            f"no xAI session at {_TOKEN_PATH}; run `wizard --login xai` first"
        )
    return json.loads(_TOKEN_PATH.read_text())


def _save(tokens: dict) -> None:
    tmp = _TOKEN_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(tokens, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, _TOKEN_PATH)


def _refresh(tokens: dict) -> dict:
    endpoint = tokens["token_endpoint"]
    host = urllib.parse.urlparse(endpoint).hostname or ""
    if not (host == "x.ai" or host.endswith(".x.ai")):
        raise SystemExit(f"refusing to send refresh token to non-x.ai host: {endpoint}")
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise SystemExit("stored xAI session has no refresh token; run `wizard --login xai` again")
    body = urllib.parse.urlencode(
        {"grant_type": "refresh_token", "client_id": CLIENT_ID, "refresh_token": refresh_token}
    ).encode()
    req = urllib.request.Request(
        endpoint, data=body,
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        refreshed = json.loads(resp.read())
    tokens["access_token"] = refreshed["access_token"]
    if refreshed.get("refresh_token"):
        tokens["refresh_token"] = refreshed["refresh_token"]
    if refreshed.get("token_type"):
        tokens["token_type"] = refreshed["token_type"]
    _save(tokens)
    print("[xai-proxy] refreshed access token", file=sys.stderr)
    return tokens


def fresh_bearer(force: bool = False) -> str:
    with _lock:
        tokens = _load()
        exp = _jwt_exp(tokens["access_token"])
        if force or (exp is not None and exp <= time.time() + EXPIRY_LEEWAY):
            tokens = _refresh(tokens)
        return tokens["access_token"]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):  # quieter
        pass

    def _proxy(self, method: str) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        payload = self.rfile.read(length) if length else None

        headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in HOP_BY_HOP and k.lower() != "authorization"
        }
        try:
            self._forward(method, payload, headers, fresh_bearer())
        except urllib.error.HTTPError as e:
            if e.code == 401:  # token rejected mid-flight; force-refresh once and retry
                try:
                    self._forward(method, payload, headers, fresh_bearer(force=True))
                    return
                except urllib.error.HTTPError as e2:
                    e = e2
            self._relay_error(e)
        except Exception as e:  # noqa: BLE001
            self.send_error(502, f"upstream error: {e}")

    def _forward(self, method, payload, headers, bearer) -> None:
        url = UPSTREAM + self.path
        req = urllib.request.Request(url, data=payload, method=method)
        for k, v in headers.items():
            req.add_header(k, v)
        req.add_header("Authorization", f"Bearer {bearer}")
        with urllib.request.urlopen(req, timeout=600) as resp:
            self.send_response(resp.status)
            for k, v in resp.headers.items():
                if k.lower() not in HOP_BY_HOP:
                    self.send_header(k, v)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(b"%X\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")

    def _relay_error(self, e: urllib.error.HTTPError) -> None:
        body = e.read()
        self.send_response(e.code)
        self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._proxy("GET")

    def do_POST(self):
        self._proxy("POST")


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    fresh_bearer()  # fail fast if not logged in / can't refresh
    print(f"[xai-proxy] OpenAI-compatible -> {UPSTREAM}/v1 on http://{host}:{port}/v1", file=sys.stderr)
    print(f"[xai-proxy] token: {_TOKEN_PATH}", file=sys.stderr)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
