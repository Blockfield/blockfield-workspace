"""Thin CP v2 §7 HTTP client (stdlib only) shared by the CLI and site-mcp.

Authority always comes from the per-call bearer; body `actor`/`role`/`via`
are refused client-side so a caller can never smuggle them (E3, §7.1).
No token, secret or DSN is stored in this module.
"""

import http.client
import json
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cp import contract  # noqa: E402


def build_request(method, path, body=None):
    """Validate one §7 call; raise ValueError on forbidden body fields."""
    body = dict(body or {})
    smuggled = sorted(set(body) & set(contract.FORBIDDEN_BODY_FIELDS))
    if smuggled:
        raise ValueError(f"forbidden body fields, authority is bearer-only: {smuggled}")
    return {"method": method.upper(), "path": path, "body": body}


def send(base_url, request, bearer, timeout=10):
    """Send a built request; return (status, parsed JSON body)."""
    target = urllib.parse.urlsplit(base_url)
    if target.scheme not in ("http", "https"):
        raise ValueError("base_url must be http(s)")
    connection = (
        http.client.HTTPSConnection(target.hostname, target.port, timeout=timeout)
        if target.scheme == "https"
        else http.client.HTTPConnection(target.hostname, target.port, timeout=timeout)
    )
    with connection:
        payload = json.dumps(request["body"], ensure_ascii=False, sort_keys=True)
        connection.request(
            request["method"],
            target.path.rstrip("/") + request["path"],
            body=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {bearer}",
            },
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"null")
