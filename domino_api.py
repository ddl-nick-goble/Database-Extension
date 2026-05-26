"""Thin Domino REST client — Apps API edition.

We talk to Domino's APIs through the auth proxy at $DOMINO_API_PROXY
(localhost:8899 inside any execution) which auto-injects the calling
user's identity. Falls back to direct nucleus URL if needed.

This module replaces the previous workspace-based variant: each "database"
is now a Domino App (not a workspace).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests

log = logging.getLogger("domino_api")

PROXY_URL = os.getenv("DOMINO_API_PROXY", "http://localhost:8899")
API_KEY = os.getenv("DOMINO_USER_API_KEY", "")
PROJECT_ID = os.getenv("DOMINO_PROJECT_ID", "")
PROJECT_OWNER = os.getenv("DOMINO_PROJECT_OWNER", "")
PROJECT_NAME = os.getenv("DOMINO_PROJECT_NAME", "")
PUBLIC_HOST = os.getenv("DOMINO_PUBLIC_HOST", "")


def _session() -> requests.Session:
    s = requests.Session()
    if API_KEY:
        s.headers["X-Domino-Api-Key"] = API_KEY
    s.headers["Accept"] = "application/json"
    return s


class DominoApiError(RuntimeError):
    """Carries the real status code + response body from Domino."""
    def __init__(self, method: str, path: str, status: int, body: str):
        self.method = method
        self.path = path
        self.status = status
        self.body = body
        super().__init__(f"{method} {path} → {status}: {body[:500]}")


def _request(method: str, path: str, **kwargs) -> Any:
    """One shared helper for GET/POST/DELETE.

    All calls go through the in-pod auth proxy at PROXY_URL
    (localhost:8899). No alternate-base fallback — if the proxy is
    unreachable, something is very wrong and we want the loud failure.
    Any non-2xx raises DominoApiError carrying status + body.
    """
    url = f"{PROXY_URL}{path}"
    r = _session().request(method, url, timeout=30, **kwargs)

    body_preview = r.text[:1000] if r.text else ""
    log.info("[domino_api] %s %s → %s", method, path, r.status_code)
    if r.status_code >= 400:
        log.warning("[domino_api]   error body: %s", body_preview)
        raise DominoApiError(method, path, r.status_code, r.text)
    if not r.content:
        return {}
    try:
        return r.json()
    except ValueError:
        return r.text


def _get(path: str, params: dict | None = None) -> Any:
    return _request("GET", path, params=params)


def _post(path: str, json: dict | None = None) -> Any:
    return _request("POST", path, json=json)


def _delete(path: str) -> None:
    """Raises DominoApiError on any non-2xx (including 404). Callers decide
    whether 404 is fatal — we don't swallow it here."""
    _request("DELETE", path)


def _unwrap_list(d) -> list:
    """Domino APIs vary: some return {data: [...]}, some {items: [...]},
    some a raw list. Normalize to a list."""
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for key in ("data", "items", "results"):
            v = d.get(key)
            if isinstance(v, list):
                return v
    return []


# --------------------------------------------------------------------------
# Catalogs
# --------------------------------------------------------------------------
def list_environments() -> list[dict]:
    # Confirmed against this Domino: /v1/environments → {objectType, data: [...]}
    return _unwrap_list(_get("/v1/environments"))


def list_hardware_tiers() -> list[dict]:
    # Confirmed working endpoint on this Domino. If it stops working,
    # surface the failure rather than silently switching paths.
    return _unwrap_list(_get(f"/v4/projects/{PROJECT_ID}/hardwareTiers"))


# --------------------------------------------------------------------------
# Apps
# --------------------------------------------------------------------------
def list_apps(status: str | None = None) -> list[dict]:
    """Use /v4/modelProducts which gives us status + runningAppUrl in one call.
    (apps/beta/apps lists apps but doesn't include lifecycle status.)"""
    data = _get("/v4/modelProducts", params={"projectId": PROJECT_ID})
    apps = data if isinstance(data, list) else _unwrap_list(data)
    if status:
        apps = [a for a in apps if str(a.get("status", "")).lower() == status.lower()]
    return apps


def create_app(
    name: str,
    description: str,
    environment_id: str,
    hardware_tier_id: str,
    visibility: str = "GRANT_BASED",
) -> dict:
    """Create the App and bind it to the chosen environment.

    The `version` sub-object on this Domino instance IS respected on initial
    create — the auto-created currentVersion inherits the env+hw we set
    here. Call start_app(id) afterward to launch the container.
    """
    return _post("/api/apps/beta/apps", json={
        "projectId": PROJECT_ID,
        "name": name,
        "description": description,
        "visibility": visibility,
        "version": {
            "hardwareTierId": hardware_tier_id,
            "environmentId": environment_id,
        },
    })


def start_app(app_id: str) -> dict:
    """Launch the app instance. On this Domino, /api/apps/beta/...start is
    gated by a feature flag; the v4 modelProducts endpoint is the working
    path (apps and modelProducts share IDs)."""
    return _post(f"/v4/modelProducts/{app_id}/start", json={})


def get_app(app_id: str) -> dict:
    return _get(f"/api/apps/beta/apps/{app_id}")


def stop_app(app_id: str) -> dict:
    """Stop the running instance of an app via the v4 modelProducts API
    (same reasoning as start_app)."""
    return _post(f"/v4/modelProducts/{app_id}/stop", json={})


def delete_app(app_id: str) -> None:
    """Stop instances and delete the app object."""
    stop_app(app_id)
    _delete(f"/api/apps/beta/apps/{app_id}")


def app_url(app: dict) -> str:
    """Best-effort URL where the user can open the app.

    Preference order (works for both Apps API + modelProducts shapes):
      1. runningAppUrl  — present when an instance is live (modelProducts)
      2. openUrl        — always present on modelProducts; opens the
                          launcher even if the app isn't running
      3. url            — present on /api/apps/beta/apps responses
    Relative paths are prefixed with PUBLIC_HOST.
    """
    for key in ("runningAppUrl", "openUrl", "url"):
        u = app.get(key)
        if not u:
            continue
        if u.startswith("http"):
            return u
        if PUBLIC_HOST:
            return f"{PUBLIC_HOST.rstrip('/')}{u}"
    return ""
