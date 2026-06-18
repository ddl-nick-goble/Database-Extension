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
    """List datasets for a project. Returns the inner dataset detail dicts.

    Uses the v1 datasetrw API — the only datasets endpoint exposed through the
    public auth proxy on this Domino (v2 returns 404 "endpoint not found").
    The v1 GET returns {datasets: [DatasetRwDetailsV1...], metadata}.
    """
    r = _get("/api/datasetrw/v1/datasets",
             params={"projectId": project_id or PROJECT_ID})
    return [w.get("dataset", w) for w in (r.get("datasets", []) if isinstance(r, dict) else [])]


def create_dataset(name: str, project_id: str = "") -> dict:
    """Create a new Domino dataset in the project. Returns the dataset object.
    Raises DominoApiError on failure.

    Uses the v1 datasetrw API (v2 is not exposed through the public proxy).
    Body schema NewDatasetRwV1 = {name, projectId, description?}; the response
    is the DatasetRwEnvelopeV1 wrapper {dataset: {...}, metadata: {...}}.
    """
    r = _post("/api/datasetrw/v1/datasets", json={
        "name": name,
        "projectId": project_id or PROJECT_ID,
    })
    # Envelope {"dataset": {...}} on v1; tolerate a bare object defensively.
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


def set_environment_visibility(env_id: str, owner_id: str | None = None,
                               visibility: str = "Organization") -> None:
    """Set the visibility of an environment.  Raises DominoApiError on failure.

    ownerId is optional (the API marks it nullable) — it scopes Organization
    visibility to a specific org. Omit it for Global/Private. Valid values:
    "Global", "Organization", "Private".
    """
    body: dict = {"visibility": visibility}
    if owner_id:
        body["ownerId"] = owner_id
    _post(f"/v4/environments/{env_id}/visibility", json=body)


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


DEFAULT_WORKSPACE_TOOLS = [
    {
        "id": "jupyter",
        "name": "jupyter",
        "title": "Jupyter (Python, R, Julia)",
        "iconUrl": "/assets/images/workspace-logos/Jupyter.svg",
        "startScripts": ["/opt/domino/workspaces/jupyter/start"],
        "supportedFileExtensions": [".ipynb"],
        "proxyConfig": {
            "internalPath": (
                "/{{ownerUsername}}/{{projectName}}/{{sessionPathComponent}}"
                "/{{runId}}/{{#if pathToOpen}}tree/{{pathToOpen}}{{/if}}"
            ),
            "port": 8888,
            "rewrite": False,
            "requireSubdomain": False,
        },
    },
    {
        "id": "jupyterlab",
        "name": "jupyterlab",
        "title": "JupyterLab",
        "iconUrl": "/assets/images/workspace-logos/jupyterlab.svg",
        "startScripts": ["/opt/domino/workspaces/jupyterlab/start"],
        "supportedFileExtensions": [],
        "proxyConfig": {
            "internalPath": (
                "/{{ownerUsername}}/{{projectName}}/{{sessionPathComponent}}"
                "/{{runId}}/{{#if pathToOpen}}tree/{{pathToOpen}}{{/if}}"
            ),
            "port": 8888,
            "rewrite": False,
            "requireSubdomain": False,
        },
    },
    {
        "id": "vscode",
        "name": "vscode",
        "title": "vscode",
        "iconUrl": "/assets/images/workspace-logos/vscode.svg",
        "startScripts": ["/opt/domino/workspaces/vscode/start"],
        "supportedFileExtensions": [],
        "proxyConfig": {
            "internalPath": "/",
            "port": 8888,
            "rewrite": False,
            "requireSubdomain": False,
        },
    },
    {
        "id": "rstudio",
        "name": "rstudio",
        "title": "RStudio",
        "iconUrl": "/assets/images/workspace-logos/Rstudio.svg",
        "startScripts": ["/opt/domino/workspaces/rstudio/start"],
        "supportedFileExtensions": [],
        "proxyConfig": {
            "internalPath": "/",
            "port": 8888,
            "rewrite": False,
            "requireSubdomain": False,
        },
    },
]


def add_environment_revision(env_id: str, dockerfile: str, image: str, summary: str = "",
                              pre_run_script: str = "",
                              workspace_tools: list | None = None,
                              skip_cache: bool = False) -> dict:
    """Add a new revision. Returns the full API response dict so callers can
    extract revision id, build id, etc.

    skip_cache=True forces a no-cache build — essential for the dd-*-app envs
    because their code-pull RUN line is identical every build, so Docker would
    otherwise reuse a cached layer and bake STALE code from a previous `main`.
    """
    body = {
        "dockerfileInstructions": dockerfile,
        "environmentVariables": [],
        "image": image,
        "postRunScript": "",
        "postSetupScript": "",
        "preRunScript": pre_run_script,
        "preSetupScript": "",
        "skipCache": skip_cache,
        "summary": summary,
        "supportedClusters": [],
        "tags": [],
        "useVpn": False,
        "workspaceTools": DEFAULT_WORKSPACE_TOOLS if workspace_tools is None else workspace_tools,
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
    """Return the latestRevision dict for the given env id, or {} if not found.

    Uses the v4 per-env endpoint so the status field reflects the live build
    state (Queued / Building / Succeeded / Failed), not the stale v1 list value.
    """
    env_data = get_environment(env_id)
    return env_data.get("latestRevision") or {}


# Regex to extract (nanotime, log_line) from the fetchBuildLogsSince HTML response.
_BUILD_LOG_ROW_RE = re.compile(
    r'<tr[^>]*\bdata-timeNano="(\d+)"[^>]*>.*?<td[^>]*\bclass="line"[^>]*>(.*?)</td>',
    re.DOTALL,
)


def fetch_build_logs(
    env_id: str, revision_id: str, build_id: str, since_nano: int = 0
) -> tuple[list[str], int]:
    """Fetch Docker build log lines since `since_nano` (nanosecond timestamp).

    URL pattern confirmed from Domino UI DevTools:
      GET /environments/{envId}/revisions/{revId}/build/{buildId}/fetchBuildLogsSince
      ?sinceTimeNano=<nanotime>&tail=true
      204 = no new lines yet (keep polling), 200 = HTML with <tr data-timeNano="...">

    Returns (log_lines, last_nanotime_seen).  Returns ([], since_nano) on any error
    so the caller can fall back gracefully.
    """
    path = (
        f"/environments/{env_id}/revisions/{revision_id}"
        f"/build/{build_id}/fetchBuildLogsSince"
    )
    params = {"sinceTimeNano": since_nano, "tail": "true"}
    try:
        r = _session().request("GET", f"{PROXY_URL}{path}", params=params, timeout=30)
        if r.status_code == 204:
            return [], since_nano
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


def get_project(project_id: str) -> dict:
    """Return project metadata (name, owner.username) for the given project ID."""
    return _get(f"/v4/projects/{project_id}")


def set_project_env_var(project_id: str, name: str, value: str) -> dict:
    """Create or overwrite a project-level environment variable."""
    return _post(f"/v4/projects/{project_id}/environmentVariables",
                 json={"name": name, "value": value})


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
    entry_point: str = "dd-db-launcher.sh",
    project_id: str | None = None,
    pre_run_script: str = "",
) -> dict:
    """Create the App and bind it to the chosen environment.

    pre_run_script is embedded in the version definition so it runs before
    the entry script on every start — this is the only reliable cross-project
    config delivery mechanism (start-time environmentVariables/preRunScript
    are silently ignored by some Domino versions).
    """
    version: dict = {
        "hardwareTierId": hardware_tier_id,
        "environmentId": environment_id,
    }
    if pre_run_script:
        version["preRunScript"] = pre_run_script
    return _post("/api/apps/beta/apps", json={
        "projectId": project_id or PROJECT_ID,
        "name": name,
        "description": description,
        "visibility": visibility,
        "entryPoint": entry_point,
        "version": version,
    })



def start_app(
    app_id: str,
    environment_id: str | None = None,
    hardware_tier_id: str | None = None,
) -> dict:
    """Launch the app instance.

    environmentId + hardwareTierId must be passed on the first start — the
    version created by create_app() has them, but Domino silently overrides
    to the project default DSE when starting. Passing them here forces the
    right environment. Retries omit them to reuse the version already created.
    """
    body: dict = {}
    if environment_id:
        body["environmentId"] = environment_id
    if hardware_tier_id:
        body["hardwareTierId"] = hardware_tier_id
    return _post(f"/v4/modelProducts/{app_id}/start", json=body)


def get_app(app_id: str) -> dict:
    return _get(f"/api/apps/beta/apps/{app_id}")


def fetch_app_logs(
    app_id: str, version_id: str, instance_id: str, offset: int = 0, limit: int = 10000
) -> dict:
    """Real-time instance logs for a running/spinning-up app.

    URL pattern confirmed from Domino UI DevTools:
      GET /api/apps/beta/apps/{appId}/versions/{versionId}/instances/{instanceId}/realTimeLogs
      ?limit=<n>&offset=<n>
    Returns the raw payload: {logContent: [{timestamp, logType, log, size}], isComplete, pagination}.
    """
    path = (
        f"/api/apps/beta/apps/{app_id}"
        f"/versions/{version_id}/instances/{instance_id}/realTimeLogs"
    )
    return _get(path, params={"limit": limit, "offset": offset})


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
