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


def start_app(
    app_id: str,
    environment_id: str | None = None,
    hardware_tier_id: str | None = None,
) -> dict:
    """Launch the app instance.

    Critical: /api/apps/beta/apps create() ignores the version.environmentId
    we pass — the auto-created currentVersion uses the project's default
    env (DSE). We MUST override at start time by passing environmentId +
    hardwareTierId in the /v4/modelProducts/<id>/start body; the start
    endpoint then provisions a new currentInstance bound to the right env.
    Confirmed empirically against cloud-dogfood; the bare `{}` form silently
    starts with the DSE.

    The v4 path is used (not /api/apps/beta/.../start) because the latter
    is feature-flag-gated on this Domino build.
    """
    body: dict = {}
    if environment_id:
        body["environmentId"] = environment_id
    if hardware_tier_id:
        body["hardwareTierId"] = hardware_tier_id
    return _post(f"/v4/modelProducts/{app_id}/start", json=body)


def get_app(app_id: str) -> dict:
    return _get(f"/api/apps/beta/apps/{app_id}")


def stop_app(app_id: str) -> dict:
    """Stop the running instance of an app via the v4 modelProducts API
    (same reasoning as start_app)."""
    return _post(f"/v4/modelProducts/{app_id}/stop", json={})


def delete_app(app_id: str) -> None:
    """Stop instances and delete the app object. Tolerates "no active run"
    (404) from stop — that just means the App is already stopped."""
    try:
        stop_app(app_id)
    except DominoApiError as e:
        if e.status != 404:
            raise
    _delete(f"/api/apps/beta/apps/{app_id}")


def app_url(app: dict) -> str:
    """URL where clients (including our /wire WS tunnel) can reach the App.

    On this Domino there are TWO surfaces:

      `https://apps.<domain>/apps-internal/<appId>/`
         → app's response directly; honors X-Domino-Api-Key; WS upgrades work.

      `https://<domain>/u/<owner>/<project>/apps/<instId>/latest`
         → browser-only; Domino wraps with a 70KB HTML auth/loading page.
           HTTP-level WS upgrade still hits the wrapper, not the app.

    We need the first one for programmatic clients (psql tunnel, healthchecks,
    etc.) so the preference order is:
      1. `url`         — already the apps.<domain> absolute URL on this build
      2. `openUrl`     — relative `/apps-internal/<id>/`, prepended with
                          PUBLIC_HOST's apps-subdomain form
      3. `runningAppUrl` — absolute browser URL (last resort)
    """
    direct = app.get("url")
    if direct and direct.startswith("http"):
        return direct.rstrip("/")

    open_path = app.get("openUrl")
    if open_path and PUBLIC_HOST:
        # PUBLIC_HOST = https://cloud-dogfood.domino.tech
        # apps-host    = https://apps.cloud-dogfood.domino.tech
        host = PUBLIC_HOST.rstrip("/")
        if host.startswith("https://") and not host.startswith("https://apps."):
            host = "https://apps." + host[len("https://"):]
        elif host.startswith("http://") and not host.startswith("http://apps."):
            host = "http://apps." + host[len("http://"):]
        return f"{host}{open_path}".rstrip("/")

    running = app.get("runningAppUrl")
    if running:
        if running.startswith("http"):
            return running.rstrip("/")
        if PUBLIC_HOST:
            return f"{PUBLIC_HOST.rstrip('/')}{running}".rstrip("/")
    return ""
