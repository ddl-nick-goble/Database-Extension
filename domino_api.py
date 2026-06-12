"""Thin Domino REST client — Apps API edition.

We talk to Domino's APIs through the auth proxy at $DOMINO_API_PROXY
(localhost:8899 inside any execution) which auto-injects the calling
user's identity. Falls back to direct nucleus URL if needed.

This module replaces the previous workspace-based variant: each "database"
is now a Domino App (not a workspace).
"""

from __future__ import annotations

import html as _html
import logging
import os
import re
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


def _put(path: str, json: dict | None = None) -> Any:
    return _request("PUT", path, json=json)


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


def list_datasets(project_id: str = "") -> list[dict]:
    """List datasets for a project. Returns the inner `dataset` dicts."""
    r = _get("/api/datasetrw/v2/datasets",
             params={"projectIdsToInclude": project_id or PROJECT_ID})
    return [w.get("dataset", w) for w in (r.get("datasets", []) if isinstance(r, dict) else [])]


def create_dataset(name: str, project_id: str = "") -> dict:
    """Create a new Domino dataset in the project. Returns the dataset object.
    Raises DominoApiError on failure."""
    r = _post("/api/datasetrw/v2/datasets", json={
        "name": name,
        "projectId": project_id or PROJECT_ID,
    })
    # API may return {"dataset": {...}} or the object directly
    if isinstance(r, dict) and "dataset" in r:
        return r["dataset"]
    return r if isinstance(r, dict) else {}


def default_environment_image() -> str:
    """Return the dockerImage of the default Domino Standard Environment."""
    data = _get("/v4/environments/defaultEnvironment")
    base = data.get("base") or {}
    img = base.get("dockerImage", "")
    if not img:
        raise RuntimeError("Could not find dockerImage on defaultEnvironment.base")
    return img


def find_environment_by_name(name: str) -> str | None:
    """Return the env id for the first environment with exactly this name, or None."""
    for e in list_environments():
        if isinstance(e, dict) and e.get("name") == name:
            return e.get("id")
    return None


def set_environment_visibility(env_id: str, owner_id: str, visibility: str = "Organization") -> None:
    """Set the visibility of an environment.  Raises DominoApiError on failure."""
    _post(f"/v4/environments/{env_id}/visibility", json={
        "visibility": visibility,
        "ownerId": owner_id,
    })


def create_environment(name: str, image: str, visibility: str = "Global") -> str:
    """Create a new compute environment and return its id."""
    r = _post("/api/environments/beta/environments", json={
        "name": name,
        "visibility": visibility,
        "image": image,
        "addBaseDependencies": True,
    })
    env_id = (r.get("environment") or r).get("id", "")
    if not env_id:
        raise RuntimeError(f"create_environment returned no id: {r}")
    return env_id


def add_environment_revision(env_id: str, dockerfile: str, image: str, summary: str = "") -> dict:
    """Add a new revision. Returns the full API response dict so callers can
    extract revision id, build id, etc."""
    body = {
        "dockerfileInstructions": dockerfile,
        "environmentVariables": [],
        "image": image,
        "postRunScript": "",
        "postSetupScript": "",
        "preRunScript": "",
        "preSetupScript": "",
        "skipCache": False,
        "summary": summary,
        "supportedClusters": [],
        "tags": [],
        "useVpn": False,
        "workspaceTools": [],
    }
    r = _post(f"/api/environments/beta/environments/{env_id}/revisions", json=body)
    return r if isinstance(r, dict) else {}


def _revision_id_from_resp(rev_resp: dict) -> str:
    """Extract the revision id from an add_environment_revision response."""
    return (rev_resp.get("revision") or rev_resp).get("id", "")


def _build_id_from_resp(rev_resp: dict) -> str:
    """Try to extract the build id from an add_environment_revision response.

    Domino may embed it as buildId, or under a 'build' sub-object."""
    rev = rev_resp.get("revision") or rev_resp
    return (
        rev.get("buildId")
        or (rev.get("build") or {}).get("id")
        or rev_resp.get("buildId")
        or ""
    )


def _build_id_from_env(env_data: dict) -> str:
    """Try to pull a build ID out of a v4 environment object.

    Domino may put the active build ID in various places — check them all."""
    rev = env_data.get("latestRevision") or {}
    details = env_data.get("latestRevisionDetails") or {}
    return (
        rev.get("buildId")
        or (rev.get("build") or {}).get("id")
        or details.get("buildId")
        or (details.get("build") or {}).get("id")
        or ""
    )


def wait_for_build_id(env_id: str, revision_id: str, max_wait_s: int = 45) -> str:
    """Poll the v4 env endpoint until a build ID appears or max_wait_s expires.

    Domino creates the build record asynchronously after the revision is queued,
    so the initial add_environment_revision response often omits it.  We see it
    appear in GET /v4/environments/{envId} within a few seconds.

    Returns the build ID string, or "" if it never shows up.
    """
    import time as _time
    deadline = _time.monotonic() + max_wait_s
    interval = 3
    while _time.monotonic() < deadline:
        _time.sleep(interval)
        try:
            env_data = _get(f"/v4/environments/{env_id}")
        except Exception:
            continue

        # Confirm it's for the right revision before trusting the build ID.
        rev = env_data.get("latestRevision") or {}
        if revision_id and rev.get("id") != revision_id:
            continue  # Domino hasn't caught up yet

        build_id = _build_id_from_env(env_data)
        if build_id:
            return build_id
    return ""


def get_environment(env_id: str) -> dict:
    """Full environment object from the v4 API.

    Returns richer data than /v1/environments:  latestRevisionDetails
    (dockerImage, compressedImageSize, number, summary…), owner, visibility,
    lastUpdated, projectsCount.  Returns {} on any error.
    """
    try:
        return _get(f"/v4/environments/{env_id}")
    except Exception:
        return {}


def environment_latest_revision(env_id: str) -> dict:
    """Return the latestRevision dict for the given env id, or {} if not found."""
    for e in list_environments():
        if isinstance(e, dict) and e.get("id") == env_id:
            return e.get("latestRevision") or {}
    return {}


# Regex to extract (nanotime, log_line) from the fetchBuildLogsSince HTML response.
_BUILD_LOG_ROW_RE = re.compile(
    r'<tr[^>]*\bdata-timeNano="(\d+)"[^>]*>.*?<td[^>]*\bclass="line"[^>]*>(.*?)</td>',
    re.DOTALL,
)


def fetch_build_logs(
    env_id: str, revision_id: str, build_id: str, since_nano: int = 0
) -> tuple[list[str], int]:
    """Fetch Docker build log lines since `since_nano` (nanosecond timestamp).

    URL pattern discovered from the Domino UI:
      GET /environments/{envId}/revisions/{revId}/build/{buildId}/fetchBuildLogsSince
      ?since=<nanotime>   (0 = fetch all)

    Returns (log_lines, last_nanotime_seen).  Returns ([], since_nano) on any error
    so the caller can fall back gracefully.
    """
    path = (
        f"/environments/{env_id}/revisions/{revision_id}"
        f"/build/{build_id}/fetchBuildLogsSince"
    )
    params = {"since": since_nano}
    try:
        r = _session().request("GET", f"{PROXY_URL}{path}", params=params, timeout=30)
        if r.status_code >= 400:
            return [], since_nano
        content = r.text
    except Exception:
        return [], since_nano

    lines: list[str] = []
    last_nano = since_nano
    for m in _BUILD_LOG_ROW_RE.finditer(content):
        nano = int(m.group(1))
        if nano <= since_nano:
            continue
        text = _html.unescape(m.group(2)).strip()
        lines.append(text)
        if nano > last_nano:
            last_nano = nano
    return lines, last_nano


def list_hardware_tiers(project_id: str = "") -> list[dict]:
    return _unwrap_list(_get(f"/v4/projects/{project_id or PROJECT_ID}/hardwareTiers"))


# --------------------------------------------------------------------------
# Apps
# --------------------------------------------------------------------------
def list_apps(status: str | None = None, project_id: str = "") -> list[dict]:
    """Use /v4/modelProducts which gives us status + runningAppUrl in one call."""
    data = _get("/v4/modelProducts", params={"projectId": project_id or PROJECT_ID})
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
    entry_point: str = "/opt/dd/app.sh",
    project_id: str | None = None,
) -> dict:
    """Create the App and bind it to the chosen environment.

    entry_point="/opt/dd/app.sh" is the script baked into every dd-*-app
    environment image, making DB apps project-independent — they carry
    their full runtime in the image and need nothing from the project repo.

    project_id overrides the wizard's own project so DB apps can be created
    in any project the caller has access to.
    """
    return _post("/api/apps/beta/apps", json={
        "projectId": project_id or PROJECT_ID,
        "name": name,
        "description": description,
        "visibility": visibility,
        "entryPoint": entry_point,
        "version": {
            "hardwareTierId": hardware_tier_id,
            "environmentId": environment_id,
            "extendedIdentityPropagationToAppsEnabled": True,
        },
    })


def register_extension(app_id: str, app_version_id: str, name: str) -> dict:
    """Register the app as a project-sidebar extension visible across all projects."""
    return _post("/api/extensions/beta/extensions", json={
        "name": name,
        "appId": app_id,
        "appVersionId": app_version_id,
        "enabled": True,
        "uiMountPointTypeConfigs": {
            "projectSidebar": {
                "allProjects": True,
                "enabled": True,
                "mountPoints": [],
                "urlConfig": {
                    "contextualQueryParams": ["projectId"],
                },
            },
            "datasetFileContext":       {"enabled": False},
            "netAppVolumeFileContext":  {"enabled": False},
            "dataset":                 {"enabled": False},
            "netAppVolume":            {"enabled": False},
            "modelDetails":            {"enabled": False},
            "adminPanel":              {"enabled": False},
        },
    })


def start_app(
    app_id: str,
    environment_id: str | None = None,
    hardware_tier_id: str | None = None,
    environment_variables: dict | None = None,
) -> dict:
    """Launch the app instance.

    Critical: /api/apps/beta/apps create() ignores the version.environmentId
    we pass — the auto-created currentVersion uses the project's default
    env (DSE). We MUST override at start time by passing environmentId +
    hardwareTierId in the /v4/modelProducts/<id>/start body; the start
    endpoint then provisions a new currentInstance bound to the right env.
    Confirmed empirically against cloud-dogfood; the bare `{}` form silently
    starts with the DSE.

    environment_variables are merged into the container environment.  We use
    this to pass DD_CONFIG_JSON — the DB app's full config as a JSON blob —
    so the app is self-contained and needs no config file in the project
    dataset.  Confirmed the v4 start endpoint accepts this field.

    The v4 path is used (not /api/apps/beta/.../start) because the latter
    is feature-flag-gated on this Domino build.
    """
    body: dict = {}
    if environment_id:
        body["environmentId"] = environment_id
    if hardware_tier_id:
        body["hardwareTierId"] = hardware_tier_id
    if environment_variables:
        body["environmentVariables"] = environment_variables
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
