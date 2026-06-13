"""Domino Databases — wizard backend (Flask).

Provisions DB Apps (each is a Domino App running dbapp/router.py via
DD_ROLE=postgres|mongo from the env image). Pattern matches MRM-Portal:
serves the static SPA at /, JSON APIs under /api/*.
"""

from __future__ import annotations

import json
import logging
import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

from flask import Flask, Response, jsonify, request, send_from_directory
from flask_cors import CORS

import domino_api as dapi
from dbapp import engines

logging.basicConfig(
    level=os.environ.get("DD_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("wizard")

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

# Per-DB config files live in the project's default dataset so the DB
# Apps (which boot in the same project) can read them without depending
# on /mnt/code being THIS repo. Same default as dbapp.lifecycle.
_DD_CONFIGS_DEFAULT = (
    f"{os.environ.get('DOMINO_DATASETS_DIR', '/mnt/data')}/"
    f"{os.environ.get('DOMINO_PROJECT_NAME', 'default')}/_dd_configs"
)
DBAPPS_DIR = Path(os.environ.get("DD_DBAPPS_DIR", _DD_CONFIGS_DEFAULT))
try:
    DBAPPS_DIR.mkdir(parents=True, exist_ok=True)
except OSError as e:
    log.warning(
        "Could not create DBAPPS_DIR %s: %s — wizard will run without dataset-backed config storage "
        "(DD_CONFIG_JSON is the primary delivery mechanism and does not require this path)",
        DBAPPS_DIR, e,
    )


# --------------------------------------------------------------------------
# Static front-end
# --------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/favicon.ico")
def serve_favicon():
    return send_from_directory(Path(app.static_folder) / "img", "favicon.ico")


@app.route("/architecture.html")
def serve_architecture():
    return send_from_directory(app.static_folder, "architecture.html")



@app.route("/img/<path:filename>")
def serve_img(filename):
    return send_from_directory(Path(app.static_folder) / "img", filename)


@app.route("/<path:_>")
def catch_all(_):
    return send_from_directory(app.static_folder, "index.html")


# --------------------------------------------------------------------------
# Config endpoint
# --------------------------------------------------------------------------
def _req_project_id() -> str:
    """Return the effective project ID for this request.

    Callers can pass ?projectId=<id> to scope operations to any project they
    have access to.  Falls back to the wizard's own PROJECT_ID so it works
    with zero configuration when not accessed as a cross-project extension.
    """
    return (request.args.get("projectId") or "").strip() or dapi.PROJECT_ID


def _resolve_engine_env(adapter, envs_by_name: dict) -> dict:
    """Return resolution info for one engine adapter given a name→id map."""
    explicit = os.environ.get(adapter.env_id_var, "").strip()
    canonical_name = f"dd-{adapter.name}-app"
    resolved = explicit or envs_by_name.get(canonical_name, "")
    source = "envvar" if explicit else ("byname" if resolved else "missing")
    return {
        "envId": resolved,
        "envIdVar": adapter.env_id_var,
        "envIdSource": source,
        "expectedEnvName": canonical_name,
    }


@app.route("/api/config")
def api_config():
    # Engine catalog — drives the wizard's engine cards + auto-resolved
    # env selection. Resolution order per engine:
    #   1. explicit $DD_<ENGINE>_ENV_ID (admin override)
    #   2. Domino env named "dd-<engine>-app" (canonical name match)
    # The second fallback means the wizard "just works" as long as the
    # four envs exist in this Domino install with their canonical names —
    # no project-level env-var configuration required.
    try:
        all_envs = dapi.list_environments()
    except Exception as e:
        log.warning("list_environments failed during /api/config: %s", e)
        all_envs = []
    envs_by_name = {
        e.get("name", ""): e.get("id", "")
        for e in all_envs if isinstance(e, dict)
    }
    img_root = Path(app.static_folder) / "img"
    engine_catalog = []
    for a in engines.all_engines():
        res = _resolve_engine_env(a, envs_by_name)
        icon_path = img_root / f"{a.name}.png"
        engine_catalog.append({
            "name": a.name,
            "label": a.docs_label,
            "icon": a.icon,
            "iconUrl": f"img/{a.name}.png" if icon_path.exists() else "",
            "description": a.description,
            "appPrefix": a.app_prefix,
            "defaultPort": a.default_port,
            **res,
        })
    project_id = _req_project_id()
    return jsonify({
        "owner": dapi.PROJECT_OWNER,
        "project": dapi.PROJECT_NAME,
        "projectId": project_id,
        "publicHost": dapi.PUBLIC_HOST,
        "engines": engine_catalog,
        # Legacy fields — kept so an older static/app.js still works
        # during the rollout. Will remove once the new UI is shipped.
        "postgresEnvId": os.environ.get("DD_POSTGRES_ENV_ID", ""),
        "mongoEnvId": os.environ.get("DD_MONGO_ENV_ID", ""),
    })


# --------------------------------------------------------------------------
# Databases (= filtered Domino Apps)
# --------------------------------------------------------------------------
def _engine_by_prefix() -> dict[str, str]:
    """Map app_prefix → engine name (e.g. 'pg-' → 'postgres'). Built from
    the registry so adding a new engine doesn't require touching this
    file."""
    return {a.app_prefix: a.name for a in engines.all_engines()}


def _is_db_app(a: dict) -> bool:
    name = a.get("name", "")
    return any(name.startswith(p) for p in _engine_by_prefix())


def _engine(a: dict) -> str:
    n = a.get("name", "")
    for prefix, engine in _engine_by_prefix().items():
        if n.startswith(prefix):
            return engine
    return "?"


def _browser_url(a: dict) -> str:
    """Domino modelproducts page — the canonical browser URL for this app."""
    app_id = a.get("id", "")
    host = (dapi.PUBLIC_HOST or "").rstrip("/")
    if app_id and host:
        return f"{host}/modelproducts/{app_id}?scope=project"
    # Fallback for local dev without PUBLIC_HOST.
    running = a.get("runningAppUrl", "")
    return running if running.startswith("http") else dapi.app_url(a)


def _config_url(a: dict) -> str:
    """Domino app details/overview page for configuration."""
    app_id = a.get("id", "")
    host = (dapi.PUBLIC_HOST or "").rstrip("/")
    owner = dapi.PROJECT_OWNER
    project = dapi.PROJECT_NAME
    if app_id and host and owner and project:
        return f"{host}/u/{owner}/{project}/apps/{app_id}/latest/details/overview"
    return _browser_url(a)


def _shape(a: dict) -> dict:
    status = a.get("status") or "Unknown"
    return {
        "id": a.get("id"),
        "name": a.get("name"),
        "engine": _engine(a),
        "status": status,
        "createdAt": a.get("createdAt") or a.get("created"),
        "lastUpdated": a.get("lastUpdated"),
        "owner": (a.get("publisher") or {}).get("name") or dapi.PROJECT_OWNER,
        "description": a.get("description", ""),
        "url": dapi.app_url(a),
        "browserUrl": _browser_url(a),
        "configUrl": _config_url(a),
        "environmentId": a.get("environmentId"),
        "hardwareTierId": a.get("hardwareTierId"),
        "isRunning": status.lower() == "running",
    }


@app.route("/api/databases")
def api_list_databases():
    try:
        apps = dapi.list_apps(project_id=_req_project_id())
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    dbs = [_shape(a) for a in apps if _is_db_app(a)]
    summary: dict = {
        "total": len(dbs),
        "running": sum(1 for d in dbs if d["isRunning"]),
    }
    # Per-engine counters — keys match the engine names in the registry.
    for a in engines.all_engines():
        summary[a.name] = sum(1 for d in dbs if d["engine"] == a.name)
    return jsonify({"databases": dbs, "summary": summary})


@app.route("/api/databases", methods=["POST"])
def api_create_database():
    # Parse + validate at the request-context layer so the generator below
    # is pure-Python (no request access) and survives Flask tearing down
    # the request context once we hand back the streaming Response.
    body = request.get_json(force=True) or {}
    engine = body.get("engine", "postgres")
    name = (body.get("name") or "").strip()
    env_id = (body.get("environmentId") or "").strip()
    hw_id  = (body.get("hardwareTierId") or "").strip()
    password = body.get("password") or ""
    user = body.get("user", "domino")
    snapshot_interval_min = int(body.get("snapshotIntervalMin", 60))
    target_project_id = (body.get("projectId") or "").strip() or _req_project_id()

    def stream():
        def sse(kind, **payload):
            return f"event: {kind}\ndata: {json.dumps(payload)}\n\n"

        def since(t0):
            return int((time.monotonic() - t0) * 1000)

        # Force any L7 proxy in front of us to flush the headers + first
        # bytes before the generator's first real event. SSE comments
        # (": ..." lines) are spec no-ops on the client.
        yield ":" + (" " * 2048) + "\n\n"

        t_total = time.monotonic()

        # 0. Validate
        yield sse("step", msg="Validating inputs", phase="validate")
        if not (name and env_id and hw_id and password):
            yield sse("error", msg="name, environmentId, hardwareTierId, password are required")
            return
        yield sse("ok", msg="Inputs OK", ms=since(t_total))

        try:
            adapter = engines.get(engine)
        except KeyError:
            yield sse("error", msg=f"unknown engine {engine!r}",
                      detail=f"known: {engines.names()}")
            return
        prefix = adapter.app_prefix
        full_name = name if name.startswith(prefix) else prefix + name

        # 1. Name-collision check. If list itself fails, surface that —
        #    don't pretend the name is free.
        t1 = time.monotonic()
        yield sse("step", msg=f"Checking name '{full_name}' against Domino", phase="name")
        try:
            existing = dapi.list_apps()
        except dapi.DominoApiError as e:
            yield sse("error", msg="list_apps failed", detail=e.body[:1500], status=e.status)
            return
        except Exception as e:
            yield sse("error", msg="list_apps failed", detail=str(e))
            return
        if any(a.get("name") == full_name for a in existing):
            yield sse(
                "error",
                msg=f"name '{full_name}' is already in use in this project — stop & delete it, or pick a different name",
            )
            return
        yield sse("ok", msg=f"Name '{full_name}' is available", ms=since(t1))

        # 2. Build config. Written to the dataset as a fallback for the
        #    API-lookup path, but the primary delivery is DD_CONFIG_JSON
        #    passed as an env var at start time — making the DB app
        #    fully project-independent (no file needed in any project).
        t2 = time.monotonic()
        config_path = DBAPPS_DIR / f"{full_name}.json"
        yield sse("step", msg=f"Writing dbapps/{full_name}.json (fallback config)", phase="config")
        cfg = {
            "engine": engine,
            "db_id": full_name,
            "password": password,
            "user": user,
            "port": adapter.default_port,
            "admin_port": 8978,
            "snapshot_interval_min": snapshot_interval_min,
        }
        config_path.write_text(json.dumps(cfg, indent=2))
        os.chmod(config_path, 0o600)
        yield sse("ok", msg="Config ready", ms=since(t2))

        # 3. Create the Domino App.
        t3 = time.monotonic()
        yield sse("step", msg=f"Creating app (engine={engine})", phase="create")
        log.info("provisioning %s engine=%s env=%s hw=%s", full_name, engine, env_id, hw_id)
        try:
            a = dapi.create_app(
                name=full_name,
                description=f"Domino Databases — {engine} ({full_name})",
                environment_id=env_id,
                hardware_tier_id=hw_id,
                entry_point="dd-db-launcher.sh",
                project_id=target_project_id,
            )
        except dapi.DominoApiError as e:
            log.warning("create failed: %s", e)
            try: config_path.unlink()
            except FileNotFoundError: pass
            yield sse("error", msg="create failed", detail=e.body[:1500], status=e.status, path=e.path)
            return
        except Exception as e:
            log.exception("unexpected create failure")
            try: config_path.unlink()
            except FileNotFoundError: pass
            yield sse("error", msg="create failed", detail=str(e), trace=traceback.format_exc()[-1500:])
            return

        app_id_str = a.get("id", "")
        app_version_id = (a.get("currentVersion") or {}).get("id", "")
        yield sse("ok", msg=f"App created (id={app_id_str})", ms=since(t3))
        log.info("created app id=%s version=%s — registering extension", app_id_str, app_version_id)


        # 4. Pin tunnel URL into cfg, then finalise DD_CONFIG_JSON.
        #    The JSON blob is passed as an env var at start time so the
        #    container carries its full config without needing any dataset file.
        t4 = time.monotonic()
        yield sse("step", msg="Finalising config + tunnel URL", phase="pin")
        if app_id_str:
            apps_host = dapi.PUBLIC_HOST.rstrip("/")
            if apps_host.startswith("https://") and not apps_host.startswith("https://apps."):
                apps_host = "https://apps." + apps_host[len("https://"):]
            cfg["tunnel_url"] = f"{apps_host}/apps-internal/{app_id_str}/"
            cfg["app_id"] = app_id_str
            config_path.write_text(json.dumps(cfg, indent=2))
            os.chmod(config_path, 0o600)
        yield sse("ok", msg="Config finalised", ms=since(t4))

        # 5. Start it — create only makes the App object, doesn't launch the
        #    container. Pass env+hw explicitly on the FIRST attempt only;
        #    create's version.environmentId is silently dropped on this Domino
        #    build, so without it the container would launch against the project's
        #    default DSE.
        #
        #    Domino's Apps API is racy here: ~50% of the time the first
        #    /start leaves the App stuck in Stopped indefinitely. A second
        #    /start consistently recovers. We retry up to 3 attempts, and
        #    break the 8s status-probe into 2s ticks so the user sees a
        #    heartbeat at least every 2s.
        #
        #    Passing env+hw on each retry creates an unnecessary new app version.
        #    Retries omit env+hw so Domino re-triggers the instance on the
        #    version already created by attempt 1, avoiding extra versions.
        start_ok = False
        for attempt in (1, 2, 3):
            yield sse("step", msg=f"/start attempt {attempt}", phase="start", attempt=attempt)
            try:
                dapi.start_app(
                    a["id"],
                    environment_id=env_id if attempt == 1 else None,
                    hardware_tier_id=hw_id if attempt == 1 else None,
                    environment_variables={"DD_CONFIG_JSON": json.dumps(cfg)},
                )
                a["status"] = "Starting"
            except dapi.DominoApiError as e:
                log.warning("start attempt %d failed: %s", attempt, e)
                yield sse("warn", msg=f"attempt {attempt} /start raised HTTP {e.status}", detail=e.body[:600])
                continue
            except Exception as e:
                log.exception("unexpected start failure on attempt %d", attempt)
                yield sse("warn", msg=f"attempt {attempt} /start raised: {e}")
                continue

            # 8s wait, emitted as 4×2s heartbeats.
            elapsed = 0
            for _ in range(4):
                time.sleep(2)
                elapsed += 2
                yield sse("tick", msg="waiting for container schedule", elapsed_s=elapsed)

            try:
                current = dapi.get_app(a["id"]) or {}
            except Exception as e:
                yield sse("warn", msg=f"attempt {attempt}: status probe failed: {e}")
                continue
            ci_status = (current.get("currentVersion", {}) or {}).get("currentInstance", {}).get("status", "")
            if ci_status.lower() in ("queued", "pending", "preparing", "running"):
                log.info("attempt %d: instance reached %s", attempt, ci_status)
                yield sse("ok", msg=f"attempt {attempt}: instance status={ci_status}", ms=0, attempt=attempt)
                start_ok = True
                break
            log.warning("attempt %d: instance status=%s — retrying /start", attempt, ci_status)
            yield sse("warn", msg=f"attempt {attempt}: instance status={ci_status or 'unknown'} — retrying")

        if not start_ok:
            a["status"] = "Failed"
            a["startError"] = "all start attempts left the instance stuck in Stopped"
            yield sse("warn", msg="all 3 /start attempts left the instance stuck in Stopped — the App row exists; try /start manually from the table")

        # Final terminal event — same shape as the old JSON response.
        shaped = _shape(a)
        shaped["totalMs"] = since(t_total)
        yield sse("result", **shaped)

    resp = Response(stream(), mimetype="text/event-stream")
    # Defeat L7 buffering on the apps-internal proxy chain.
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@app.route("/api/databases/<app_id>", methods=["DELETE"])
def api_stop_database(app_id: str):
    keep = request.args.get("keep", "1") != "0"
    try:
        if keep:
            dapi.stop_app(app_id)
        else:
            dapi.delete_app(app_id)
    except dapi.DominoApiError as e:
        return jsonify({"error": "stop/delete failed", "status": e.status,
                        "dominoBody": e.body[:1500]}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"ok": True})


@app.route("/api/databases/<app_id>/start", methods=["POST"])
def api_start_database(app_id: str):
    # Resume must re-pass env+hw because /start without them defaults to the
    # project's DSE (same bug as create — see domino_api.start_app). Read
    # from request body if the caller provided overrides; else recover them
    # from the App's currentVersion (last-known good).
    body = request.get_json(silent=True) or {}
    env_id = body.get("environmentId")
    hw_id = body.get("hardwareTierId")
    if not (env_id and hw_id):
        try:
            app_doc = dapi.get_app(app_id)
            cv = app_doc.get("currentVersion", {})
            env_id = env_id or cv.get("environmentId")
            hw_id = hw_id or cv.get("hardwareTierId")
        except Exception:
            pass
    # Re-inject DD_CONFIG_JSON from the fallback file so the resumed container
    # still gets its config even if the original start env vars weren't persisted.
    env_vars: dict | None = None
    app_name = None
    try:
        app_doc = app_doc if 'app_doc' in dir() else dapi.get_app(app_id)  # type: ignore[name-defined]
        app_name = app_doc.get("name", "")
    except Exception:
        pass
    if app_name:
        cfg_path = DBAPPS_DIR / f"{app_name}.json"
        if cfg_path.exists():
            try:
                env_vars = {"DD_CONFIG_JSON": cfg_path.read_text()}
            except OSError:
                pass
    try:
        result = dapi.start_app(app_id, environment_id=env_id, hardware_tier_id=hw_id,
                                environment_variables=env_vars)
    except dapi.DominoApiError as e:
        return jsonify({"error": "start failed", "status": e.status,
                        "dominoBody": e.body[:1500]}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"ok": True, "result": result})


# --------------------------------------------------------------------------
# Catalogs
# --------------------------------------------------------------------------
@app.route("/api/environments")
def api_environments():
    try:
        envs = dapi.list_environments()
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify([
        {"id": e.get("id"), "name": e.get("name"), "visibility": e.get("visibility")}
        for e in envs if isinstance(e, dict) and e.get("id")
    ])


@app.route("/api/hardware-tiers")
def api_hardware_tiers():
    try:
        tiers = dapi.list_hardware_tiers(project_id=_req_project_id())
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    out = []
    for t in tiers:
        if not isinstance(t, dict):
            continue
        # Domino wraps each tier as {"hardwareTier": {...}} on this instance.
        ht = t.get("hardwareTier", t)
        if not (ht and ht.get("id")):
            continue
        flags = ht.get("hwtFlags", {})
        if flags.get("isArchived") or not flags.get("isVisible", True):
            continue
        out.append({"id": ht.get("id"), "name": ht.get("name") or ht.get("id")})
    return jsonify(out)


# --------------------------------------------------------------------------
# Environment management (admin)
# --------------------------------------------------------------------------
def _fmt_bytes(n: int) -> str:
    """Human-readable byte count, e.g. 3.2 GB."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return str(n)


@app.route("/api/environments/status")
def api_environments_status():
    """Per-engine environment status for the Environments admin tab."""
    try:
        all_envs = dapi.list_environments()
    except Exception as e:
        log.warning("list_environments failed during /api/environments/status: %s", e)
        all_envs = []

    id_map = {
        e.get("name", ""): e.get("id", "")
        for e in all_envs if isinstance(e, dict)
    }
    img_root = Path(app.static_folder) / "img"

    # Build spec list — no API calls yet
    specs = []
    for a in engines.all_engines():
        specs.append(dict(
            name=a.name, label=a.docs_label,
            canonical_name=f"dd-{a.name}-app",
            icon=a.icon, icon_png=f"{a.name}.png",
            env_id_var=a.env_id_var,
        ))
    specs.append(dict(
        name="dsc-db", label="DSE + DB",
        canonical_name="dd-dse-db",
        icon="", icon_png="", env_id_var="",
    ))

    # Resolve env_id for each spec from env-var or by canonical name
    for s in specs:
        env_id = os.environ.get(s["env_id_var"], "").strip() if s["env_id_var"] else ""
        source = "envvar" if env_id else ""
        if not env_id:
            env_id = id_map.get(s["canonical_name"], "")
            source = "byname" if env_id else "missing"
        s["env_id"] = env_id
        s["source"] = source

    # Fetch v4 details for all envs that have an id — in parallel
    def _fetch_v4(env_id: str) -> dict:
        try:
            return dapi.get_environment(env_id) or {}
        except Exception as exc:
            log.warning("get_environment(%s) failed: %s", env_id, exc)
            return {}

    unique_ids = list({s["env_id"] for s in specs if s["env_id"]})
    v4_by_id: dict[str, dict] = {}
    if unique_ids:
        with ThreadPoolExecutor(max_workers=min(len(unique_ids), 8)) as pool:
            fut_map = {pool.submit(_fetch_v4, eid): eid for eid in unique_ids}
            for fut in as_completed(fut_map):
                v4_by_id[fut_map[fut]] = fut.result()

    def _build_row(s: dict) -> dict:
        env_v4 = v4_by_id.get(s["env_id"], {})
        latest_rev = env_v4.get("latestRevision") or {}
        rev_details = env_v4.get("latestRevisionDetails") or {}
        docker_image = rev_details.get("dockerImage", "")
        image_display = docker_image.split("/")[-1] if "/" in docker_image else docker_image
        size_obj = rev_details.get("compressedImageSize") or {}
        size_bytes = size_obj.get("value")
        image_size = _fmt_bytes(int(size_bytes)) if size_bytes else ""
        owner_obj = env_v4.get("owner") or {}

        canonical_name = s["canonical_name"]
        df_path = REPO_ROOT / "envs" / canonical_name / "Dockerfile"
        dockerfile_text = ""
        dockerfile_exists = df_path.exists()
        if dockerfile_exists:
            try:
                dockerfile_text = df_path.read_text()
            except OSError:
                pass

        icon_png = s["icon_png"]
        icon_path = img_root / icon_png if icon_png else None
        env_id = s["env_id"]
        return {
            "name": s["name"],
            "label": s["label"],
            "icon": s["icon"],
            "iconUrl": f"img/{icon_png}" if (icon_path and icon_path.exists()) else "",
            "expectedEnvName": canonical_name,
            "envId": env_id,
            "envIdVar": s["env_id_var"],
            "envIdSource": s["source"],
            "latestRevision": latest_rev,
            "revisionNumber": latest_rev.get("number"),
            "revisionSummary": rev_details.get("summary", ""),
            "dockerImage": docker_image,
            "imageDisplay": image_display,
            "imageSize": image_size,
            "visibility": env_v4.get("visibility", ""),
            "owner": owner_obj.get("username", ""),
            "lastUpdated": env_v4.get("lastUpdated", ""),
            "projectsCount": env_v4.get("projectsCount"),
            "envUrl": f"{dapi.PUBLIC_HOST.rstrip('/')}/environment/{env_id}" if env_id and dapi.PUBLIC_HOST else "",
            "dockerfile": dockerfile_text,
            "dockerfileExists": dockerfile_exists,
        }

    return jsonify([_build_row(s) for s in specs])


@app.route("/api/environments/<engine>/build", methods=["POST"])
def api_build_environment(engine: str):
    """SSE stream that builds (or rebuilds) a dd-* environment.

    Works for both engine environments (dd-<engine>-app) and the
    DSE + DB workspace environment (engine="dsc-db" → dd-dse-db).
    """
    # Validate + read Dockerfile before entering the generator so we have no
    # request-context access inside the stream (same pattern as api_create_database).
    if engine == "dsc-db":
        canonical_name = "dd-dse-db"
        pre_run_script = (
            "curl -fsSL https://raw.githubusercontent.com/ddl-nick-goble/"
            "Database-Extension/main/client/dd-workspace-setup.py | python3 -"
        )
    else:
        try:
            adapter = engines.get(engine)
        except KeyError:
            return jsonify({"error": f"unknown engine {engine!r}"}), 400
        canonical_name = f"dd-{adapter.name}-app"
        # Always write dd-db-launcher.sh (never conflicts with a project's own
        # app.sh) so this works in any project, not just Database-Extension.
        pre_run_script = (
            "echo '#!/usr/bin/env bash' > /mnt/code/dd-db-launcher.sh"
            " && echo 'exec /opt/dd/app.sh \"$@\"' >> /mnt/code/dd-db-launcher.sh"
            " && chmod +x /mnt/code/dd-db-launcher.sh"
        )

    df_path = REPO_ROOT / "envs" / canonical_name / "Dockerfile"
    if not df_path.exists():
        return jsonify({"error": f"Dockerfile not found at {df_path}"}), 400
    dockerfile = df_path.read_text()

    def stream():
        def sse(kind, **payload):
            return f"event: {kind}\ndata: {json.dumps(payload)}\n\n"

        def since(t0):
            return int((time.monotonic() - t0) * 1000)

        # Flush bytes to defeat L7 proxy buffering.
        yield ":" + (" " * 2048) + "\n\n"

        t_total = time.monotonic()

        # 1. Fetch DSE base image
        yield sse("step", msg="Fetching DSE base image")
        try:
            image = dapi.default_environment_image()
        except Exception as e:
            yield sse("error", msg="Could not fetch default environment image", detail=str(e))
            return
        yield sse("ok", msg=f"Base image: {image}", ms=since(t_total))

        # 2. Find or create the environment
        t2 = time.monotonic()
        yield sse("step", msg=f"Looking up environment '{canonical_name}'")
        try:
            existing_id = dapi.find_environment_by_name(canonical_name)
        except Exception as e:
            yield sse("error", msg="Could not list environments", detail=str(e))
            return

        action = "revised"
        if existing_id:
            env_id = existing_id
            yield sse("ok", msg=f"Found existing env {env_id} — adding new revision", ms=since(t2))
        else:
            yield sse("step", msg=f"Creating new environment '{canonical_name}'")
            try:
                env_id = dapi.create_environment(canonical_name, image, visibility="Private")
            except Exception as e:
                yield sse("error", msg="create_environment failed", detail=str(e))
                return
            action = "created"
            yield sse("ok", msg=f"Created env {env_id}", ms=since(t2))

        # 3. Add the Dockerfile revision
        t3 = time.monotonic()
        yield sse("step", msg="Queuing Dockerfile revision build")
        try:
            rev_resp = dapi.add_environment_revision(
                env_id, dockerfile, image,
                summary=f"{canonical_name} — built via Environments tab",
                pre_run_script=pre_run_script,
            )
        except Exception as e:
            yield sse("error", msg="add_environment_revision failed", detail=str(e))
            return
        revision_id = dapi._revision_id_from_resp(rev_resp)
        build_id = dapi._build_id_from_resp(rev_resp)
        yield sse("ok", msg=f"Revision queued (rev={revision_id or '?'})", ms=since(t3))

        # 4. If the revision response didn't include a build ID (common — Domino
        #    creates the build record asynchronously), poll the v4 env endpoint
        #    for up to 45 s while emitting progress ticks so the UI stays alive.
        if revision_id and not build_id:
            yield sse("step", msg="Waiting for Domino to assign a build ID…")
            _wait_start = time.monotonic()
            _wait_deadline = _wait_start + 45
            _tick_interval = 3
            while time.monotonic() < _wait_deadline and not build_id:
                time.sleep(_tick_interval)
                try:
                    env_data = dapi.get_environment(env_id) or {}
                    rev = env_data.get("latestRevision") or {}
                    details = env_data.get("latestRevisionDetails") or {}
                    build_id = (
                        rev.get("buildId")
                        or (rev.get("build") or {}).get("id")
                        or details.get("buildId")
                        or (details.get("build") or {}).get("id")
                        or ""
                    )
                    if not build_id:
                        waited = int(time.monotonic() - _wait_start)
                        yield sse("tick", msg=f"waiting for build ID… ({waited}s)", elapsed_s=waited)
                except Exception as e:
                    yield sse("warn", msg=f"env probe failed: {e}")
            if build_id:
                yield sse("ok", msg=f"Build ID found: {build_id}")
            else:
                yield sse("warn", msg="Build ID not found after 45 s — falling back to status polling")

        # 5. Stream real Docker build logs when we have the build_id; otherwise
        #    fall back to periodic status polling (10s ticks).
        yield sse("step", msg="Streaming build log (this takes ~3–5 min)…")
        deadline = time.monotonic() + 20 * 60
        elapsed_s = 0
        poll_interval = 3    # seconds between log fetches when logs are streaming
        status_interval = 10  # seconds between status polls when falling back
        final_status = "Unknown"
        since_nano = 0

        use_log_api = bool(revision_id and build_id)

        while time.monotonic() < deadline:
            sleep_s = poll_interval if use_log_api else status_interval
            time.sleep(sleep_s)
            elapsed_s += sleep_s

            # Check build status
            try:
                rev_info = dapi.environment_latest_revision(env_id)
                final_status = rev_info.get("status", "Unknown")
            except Exception as e:
                yield sse("warn", msg=f"status probe failed: {e}")
                final_status = "Unknown"

            if use_log_api:
                # Fetch real log lines and stream them
                try:
                    new_lines, since_nano = dapi.fetch_build_logs(
                        env_id, revision_id, build_id, since_nano
                    )
                    emitted = 0
                    for line in new_lines:
                        if line:
                            yield sse("tick", msg=line, elapsed_s=elapsed_s)
                            emitted += 1
                    # Always emit a heartbeat so the UI knows we're still alive
                    if emitted == 0:
                        yield sse("tick", msg=f"status={final_status} ({elapsed_s}s)", elapsed_s=elapsed_s)
                except Exception as e:
                    yield sse("warn", msg=f"build log fetch failed: {e}")
                    use_log_api = False  # fall back to status ticks
            else:
                yield sse("tick", msg=f"status={final_status} ({elapsed_s}s)", elapsed_s=elapsed_s)

            if final_status.lower() == "succeeded":
                break
            if final_status.lower() == "failed":
                yield sse("error", msg="Domino build FAILED — check the environment's revision log in the Domino UI",
                          detail=f"env_id={env_id}")
                return

        total_ms = since(t_total)
        if final_status.lower() == "succeeded":
            yield sse("result", engine=engine, envId=env_id, status=final_status,
                      action=action, totalMs=total_ms)
        else:
            yield sse("warn", msg=f"Build timed out or status unknown ({final_status}) — "
                      f"check the Domino UI for env {env_id}")
            yield sse("result", engine=engine, envId=env_id, status=final_status,
                      action=action, totalMs=total_ms)

    resp = Response(stream(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8888"))
    app.run(host="0.0.0.0", port=port, debug=False)
