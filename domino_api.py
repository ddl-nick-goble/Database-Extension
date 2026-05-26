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
DIRECT_URL = os.getenv("DOMINO_API_HOST", "")
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

    Strategy:
      - Try PROXY_URL first; on network error or 5xx, fall back to DIRECT_URL.
      - 4xx is the *server's* answer — don't retry, just surface it.
      - Always log status + (truncated) body so the wizard's stdout has real info.
    """
    last_exc = None
    bases = [b for b in (PROXY_URL, DIRECT_URL) if b]
    for base in bases:
        url = f"{base}{path}"
        try:
            r = _session().request(method, url, timeout=30, **kwargs)
        except requests.RequestException as e:
            last_exc = e
            log.warning("[domino_api] %s %s → network error via %s: %s", method, path, base, e)
            continue

        body_preview = r.text[:1000] if r.text else ""
        log.info("[domino_api] %s %s → %s via %s", method, path, r.status_code, base)
        if r.status_code >= 500:
            log.warning("[domino_api]   server error body: %s", body_preview)
            last_exc = DominoApiError(method, path, r.status_code, r.text)
            continue  # try next base
        if r.status_code >= 400:
            log.warning("[domino_api]   client error body: %s", body_preview)
            raise DominoApiError(method, path, r.status_code, r.text)
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return r.text

    if last_exc:
        raise last_exc
    raise RuntimeError(f"no bases configured for {method} {path}")


def _get(path: str, params: dict | None = None) -> Any:
    return _request("GET", path, params=params)


def _post(path: str, json: dict | None = None) -> Any:
    return _request("POST", path, json=json)


def _delete(path: str) -> None:
    try:
        _request("DELETE", path)
    except DominoApiError as e:
        if e.status not in (404,):  # ok if already gone
            log.warning("[domino_api] DELETE %s → %s", path, e.status)


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
    # Confirmed: /v4/projects/{id}/hardwareTiers → 200 on this instance.
    try:
        return _unwrap_list(_get(f"/v4/projects/{PROJECT_ID}/hardwareTiers"))
    except Exception:
        return _unwrap_list(_get("/api/hardwaretiers/v1/hardwaretiers"))


# --------------------------------------------------------------------------
# Apps
# --------------------------------------------------------------------------
def list_apps(status: str | None = None) -> list[dict]:
    params = {"projectId": PROJECT_ID, "limit": 200}
    if status:
        params["status"] = status
    # Apps API uses {items: [...], metadata: {...}} on this instance.
    return _unwrap_list(_get("/api/apps/beta/apps", params=params))


def create_app(
    name: str,
    description: str,
    environment_id: str,
    hardware_tier_id: str,
    visibility: str = "GRANT_BASED",
) -> dict:
    """Create the App object. NOT started yet — call start_app(id) next."""
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
    """Best-effort URL where the user can open the app."""
    url = app.get("url")
    if url:
        if url.startswith("http"):
            return url
        if PUBLIC_HOST:
            return f"{PUBLIC_HOST.rstrip('/')}{url}"
    name = app.get("name", "")
    app_id = app.get("id", "")
    if PUBLIC_HOST and PROJECT_OWNER and PROJECT_NAME and (name or app_id):
        slug = name or app_id
        return f"{PUBLIC_HOST.rstrip('/')}/{PROJECT_OWNER}/{PROJECT_NAME}/app/{slug}/"
    return ""
