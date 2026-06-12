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
from pathlib import Path

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
DBAPPS_DIR.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# Static front-end
# --------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/<path:_>")
def catch_all(_):
    return send_from_directory(app.static_folder, "index.html")


# --------------------------------------------------------------------------
# Config endpoint
# --------------------------------------------------------------------------
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
        explicit = os.environ.get(a.env_id_var, "").strip()
        canonical_name = f"dd-{a.name}-app"
        resolved = explicit or envs_by_name.get(canonical_name, "")
        icon_path = img_root / f"{a.name}.png"
        engine_catalog.append({
            "name": a.name,
            "label": a.docs_label,
            "icon": a.icon,
            "iconUrl": f"img/{a.name}.png" if icon_path.exists() else "",
            "description": a.description,
            "appPrefix": a.app_prefix,
            "defaultPort": a.default_port,
            "envId": resolved,
            "envIdVar": a.env_id_var,
            # Surfaced for the wizard UI to render a helpful tooltip if
            # something is unresolved.
            "envIdSource": (
                "envvar" if explicit
                else ("byname" if resolved else "missing")
            ),
            "expectedEnvName": canonical_name,
        })
    return jsonify({
        "owner": dapi.PROJECT_OWNER,
        "project": dapi.PROJECT_NAME,
        "projectId": dapi.PROJECT_ID,
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
        "environmentId": a.get("environmentId"),
        "hardwareTierId": a.get("hardwareTierId"),
        "isRunning": status.lower() == "running",
    }


@app.route("/api/databases")
def api_list_databases():
    try:
        apps = dapi.list_apps()
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

        # 2. Write per-DB config BEFORE creating the app. The DB app's
        #    lifecycle.find_config() picks it up by app name on first boot.
        t2 = time.monotonic()
        config_path = DBAPPS_DIR / f"{full_name}.json"
        yield sse("step", msg=f"Writing dbapps/{full_name}.json", phase="config")
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
        yield sse("ok", msg="Config written", ms=since(t2))

        # 3. Create the Domino App. Its DD_ROLE env (baked into the
        #    chosen env image) routes /mnt/code/app.sh → dbapp/app.sh.
        t3 = time.monotonic()
        yield sse("step", msg=f"Calling Domino /v4/modelProducts (create {engine})", phase="create")
        log.info("provisioning %s engine=%s env=%s hw=%s", full_name, engine, env_id, hw_id)
        try:
            a = dapi.create_app(
                name=full_name,
                description=f"Domino Databases — {engine} ({full_name})",
                environment_id=env_id,
                hardware_tier_id=hw_id,
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
        yield sse("ok", msg=f"App created (id={app_id_str})", ms=since(t3))
        log.info("created app id=%s — starting", app_id_str)

        # 4. Pin the apps-subdomain URL into the config now that we know
        #    the App ID. The DB-app's status page reads this to render
        #    a copy-pasteable tunnel command.
        t4 = time.monotonic()
        yield sse("step", msg="Pinning tunnel URL into config", phase="pin")
        if app_id_str:
            apps_host = dapi.PUBLIC_HOST.rstrip("/")
            if apps_host.startswith("https://") and not apps_host.startswith("https://apps."):
                apps_host = "https://apps." + apps_host[len("https://"):]
            cfg["tunnel_url"] = f"{apps_host}/apps-internal/{app_id_str}/"
            cfg["app_id"] = app_id_str
            config_path.write_text(json.dumps(cfg, indent=2))
            os.chmod(config_path, 0o600)
        yield sse("ok", msg="Config updated with app_id + tunnel_url", ms=since(t4))

        # 5. Start it — create only makes the App object, doesn't launch the
        #    container. Pass env+hw explicitly; create's version.environmentId
        #    is silently dropped on this Domino build, so without it the
        #    container would launch against the project's default DSE.
        #
        #    Domino's Apps API is racy here: ~50% of the time the first
        #    /start leaves the App stuck in Stopped indefinitely. A second
        #    /start consistently recovers. We retry up to 3 attempts, and
        #    break the 8s status-probe into 2s ticks so the user sees a
        #    heartbeat at least every 2s.
        start_ok = False
        for attempt in (1, 2, 3):
            yield sse("step", msg=f"/start attempt {attempt}", phase="start", attempt=attempt)
            try:
                dapi.start_app(a["id"], environment_id=env_id, hardware_tier_id=hw_id)
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
    try:
        result = dapi.start_app(app_id, environment_id=env_id, hardware_tier_id=hw_id)
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
        tiers = dapi.list_hardware_tiers()
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
# Entrypoint
# --------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8888"))
    app.run(host="0.0.0.0", port=port, debug=False)
