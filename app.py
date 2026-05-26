"""Domino Databases — wizard backend (Flask).

Provisions DB Apps (each is a Domino App running dbapp/router.py via
DD_ROLE=postgres|mongo from the env image). Pattern matches MRM-Portal:
serves the static SPA at /, JSON APIs under /api/*.
"""

from __future__ import annotations

import json
import logging
import os
import traceback
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

import domino_api as dapi

logging.basicConfig(
    level=os.environ.get("DD_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("wizard")

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

DBAPPS_DIR = Path(os.environ.get("DD_DBAPPS_DIR", "/mnt/code/dbapps"))
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
    return jsonify({
        "owner": dapi.PROJECT_OWNER,
        "project": dapi.PROJECT_NAME,
        "projectId": dapi.PROJECT_ID,
        "publicHost": dapi.PUBLIC_HOST,
        "postgresEnvId": os.environ.get("DD_POSTGRES_ENV_ID", ""),
        "mongoEnvId": os.environ.get("DD_MONGO_ENV_ID", ""),
    })


# --------------------------------------------------------------------------
# Databases (= filtered Domino Apps)
# --------------------------------------------------------------------------
def _is_db_app(a: dict) -> bool:
    name = a.get("name", "")
    return name.startswith("pg-") or name.startswith("mongo-")


def _engine(a: dict) -> str:
    n = a.get("name", "")
    if n.startswith("pg-"): return "postgres"
    if n.startswith("mongo-"): return "mongo"
    return "?"


def _shape(a: dict) -> dict:
    return {
        "id": a.get("id"),
        "name": a.get("name"),
        "engine": _engine(a),
        "status": a.get("status", "unknown"),
        "createdAt": a.get("createdAt"),
        "owner": a.get("ownerName") or a.get("creator") or dapi.PROJECT_OWNER,
        "description": a.get("description", ""),
        "url": dapi.app_url(a),
    }


@app.route("/api/databases")
def api_list_databases():
    try:
        apps = dapi.list_apps()
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    dbs = [_shape(a) for a in apps if _is_db_app(a)]
    summary = {
        "total":    len(dbs),
        "postgres": sum(1 for d in dbs if d["engine"] == "postgres"),
        "mongo":    sum(1 for d in dbs if d["engine"] == "mongo"),
        "running":  sum(1 for d in dbs if str(d["status"]).lower() == "running"),
    }
    return jsonify({"databases": dbs, "summary": summary})


@app.route("/api/databases", methods=["POST"])
def api_create_database():
    body = request.get_json(force=True) or {}
    engine = body.get("engine", "postgres")
    name = (body.get("name") or "").strip()
    env_id = (body.get("environmentId") or "").strip()
    hw_id  = (body.get("hardwareTierId") or "").strip()
    password = body.get("password") or ""
    if not (name and env_id and hw_id and password):
        return jsonify({"error": "name, environmentId, hardwareTierId, password are required"}), 400

    prefix = "pg-" if engine == "postgres" else "mongo-"
    full_name = name if name.startswith(prefix) else prefix + name

    # 1. Write the per-DB config file BEFORE creating the app. The DB app's
    #    lifecycle.find_config() picks it up by app name (or most-recent fallback).
    config_path = DBAPPS_DIR / f"{full_name}.json"
    cfg = {
        "engine": engine,
        "db_id": full_name,
        "password": password,
        "user": body.get("user", "domino"),
        "port": 5432 if engine == "postgres" else 27017,
        "cloudbeaver_port": 8978,
        "snapshot_interval_min": int(body.get("snapshotIntervalMin", 60)),
    }
    config_path.write_text(json.dumps(cfg, indent=2))
    os.chmod(config_path, 0o600)

    # 2. Create the Domino App. Its DD_ROLE env (set by the chosen env image)
    #    routes /mnt/code/app.sh to dbapp/app.sh on boot.
    log.info("provisioning %s engine=%s env=%s hw=%s",
             full_name, engine, env_id, hw_id)
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
        return jsonify({
            "error": "create failed",
            "method": e.method, "path": e.path,
            "status": e.status, "dominoBody": e.body[:1500],
        }), 502
    except Exception as e:
        log.exception("unexpected create failure")
        try: config_path.unlink()
        except FileNotFoundError: pass
        return jsonify({"error": "create failed", "detail": str(e),
                        "trace": traceback.format_exc()[-1500:]}), 502

    log.info("created app id=%s — starting", a.get("id"))

    # 3. Start it — create only makes the App object, doesn't launch the container.
    try:
        dapi.start_app(a["id"])
        a["status"] = "Starting"
    except dapi.DominoApiError as e:
        log.warning("start failed: %s", e)
        a["status"] = "Failed"
        a["startError"] = f"{e.status}: {e.body[:500]}"
    except Exception as e:
        log.exception("unexpected start failure")
        a["status"] = "Failed"
        a["startError"] = str(e)

    return jsonify(_shape(a)), 201


@app.route("/api/databases/<app_id>", methods=["DELETE"])
def api_stop_database(app_id: str):
    keep = request.args.get("keep", "1") != "0"
    try:
        if keep:
            dapi.stop_app(app_id)
        else:
            dapi.delete_app(app_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"ok": True})


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
