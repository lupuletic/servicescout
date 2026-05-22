"""Minimal GitHub REST client for UI onboarding.

Used by the dashboard to turn a user's Personal Access Token into a pick-list
of readable orgs and repos (so they select journey seeds instead of hand-typing
`org/name`). Stdlib only — no new dependencies, no `gh` CLI requirement.

The token is a function argument throughout and is NEVER logged or returned to
the caller. Callers are responsible for not echoing it back to the client.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API = "https://api.github.com"
_TIMEOUT = 15
_MAX_PAGES = 10  # 10 * 100 = 1000 repos; enough for a picker, bounded for safety


class GithubError(Exception):
    """A GitHub API call failed. `status` is the HTTP status (0 = transport)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _request(path: str, token: str, params: dict[str, Any] | None = None) -> tuple[Any, dict[str, str]]:
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "servicescout",
    })
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise GithubError(401, "Token rejected — check it's valid and not expired.") from exc
        if exc.code == 403:
            raise GithubError(403, "Forbidden — the token may lack scope (need repo + read:org) or be rate-limited.") from exc
        if exc.code == 404:
            raise GithubError(404, "Not found — the org may not exist or the token can't see it.") from exc
        raise GithubError(exc.code, f"GitHub API error {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise GithubError(0, f"Could not reach GitHub: {exc}.") from exc


def _next_url(link_header: str) -> str | None:
    """Extract the rel="next" path from a GitHub Link header, or None."""
    for part in link_header.split(","):
        segments = part.split(";")
        if len(segments) < 2:
            continue
        if 'rel="next"' in segments[1]:
            raw = segments[0].strip().strip("<>")
            return raw[len(API):] if raw.startswith(API) else raw
    return None


def _paginate(path: str, token: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    body, headers = _request(path, token, params)
    if isinstance(body, list):
        items.extend(body)
    pages = 1
    nxt = _next_url(headers.get("Link", ""))
    while nxt and pages < _MAX_PAGES:
        body, headers = _request(nxt, token)
        if isinstance(body, list):
            items.extend(body)
        nxt = _next_url(headers.get("Link", ""))
        pages += 1
    return items


def validate_token(token: str) -> dict[str, Any]:
    """Return {login, orgs:[{login}]} for a valid token. Raises GithubError."""
    user, _ = _request("/user", token)
    orgs = _paginate("/user/orgs", token, {"per_page": 100})
    return {
        "login": user.get("login"),
        "orgs": [{"login": o.get("login")} for o in orgs if o.get("login")],
    }


def list_org_repos(token: str, org: str) -> list[dict[str, Any]]:
    """Non-archived repos in an org, newest-updated first, for seed selection."""
    repos = _paginate(f"/orgs/{org}/repos", token, {"per_page": 100, "sort": "updated"})
    return [
        {
            "name": r.get("name"),
            "full_name": r.get("full_name"),
            "description": r.get("description") or "",
            "language": r.get("language") or "",
        }
        for r in repos
        if r.get("full_name") and not r.get("archived")
    ]
