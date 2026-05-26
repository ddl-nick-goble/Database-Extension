"""
API Discovery Script — hits every Domino API endpoint we'll use in the Governance Control Tower
and saves raw JSON responses to discovery_output/{endpoint_name}.json.

This tells us the exact field names, nesting, and pagination shapes so our
DominoClient parses correctly.

Run from inside a Domino workspace where localhost:8899 proxies the API.
"""
import json
import os
import requests
import sys
import time

PROXY_URL = os.getenv("DOMINO_API_PROXY", "http://localhost:8899")
DIRECT_URL = os.getenv("DOMINO_API_HOST", "http://nucleus-frontend.domino-platform:80")
API_KEY = os.getenv("DOMINO_USER_API_KEY", "")
OUTPUT_DIR = "discovery_output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

session = requests.Session()
if API_KEY:
    session.headers["X-Domino-Api-Key"] = API_KEY
    print(f"Auth: using DOMINO_USER_API_KEY ({API_KEY[:8]}...)")
else:
    print("Auth: NO API KEY FOUND — requests may fail")

# We'll try proxy first, then direct host for each endpoint
BASE_URL = PROXY_URL

def fetch(path, params=None, label=None, base_url=None):
    """GET an endpoint, save response, return parsed JSON."""
    url = f"{base_url or BASE_URL}{path}"
    label = label or path.strip("/").replace("/", "_")
    print(f"\n{'='*60}")
    print(f"GET {path}")
    if params:
        print(f"  params: {params}")
    try:
        resp = session.get(url, params=params, timeout=30)
        print(f"  status: {resp.status_code}")
        print(f"  content-type: {resp.headers.get('content-type', 'unknown')}")
        print(f"  content-length: {len(resp.content)} bytes")

        out = {
            "url": url,
            "params": params,
            "status_code": resp.status_code,
            "headers": dict(resp.headers),
        }

        if resp.status_code == 200:
            try:
                data = resp.json()
                out["body"] = data
                # Print summary
                if isinstance(data, list):
                    print(f"  response: list with {len(data)} items")
                    if data:
                        print(f"  first item keys: {list(data[0].keys()) if isinstance(data[0], dict) else type(data[0])}")
                elif isinstance(data, dict):
                    print(f"  response keys: {list(data.keys())}")
                    for k, v in data.items():
                        if isinstance(v, list):
                            print(f"    {k}: list[{len(v)}]")
                            if v and isinstance(v[0], dict):
                                print(f"      first item keys: {list(v[0].keys())}")
                        elif isinstance(v, dict):
                            print(f"    {k}: dict with keys {list(v.keys())}")
                        else:
                            val_str = str(v)[:80]
                            print(f"    {k}: {type(v).__name__} = {val_str}")
            except Exception:
                out["body_text"] = resp.text[:2000]
                print(f"  response: not JSON, first 200 chars: {resp.text[:200]}")
        else:
            out["body_text"] = resp.text[:2000]
            print(f"  error body: {resp.text[:300]}")

        outpath = os.path.join(OUTPUT_DIR, f"{label}.json")
        with open(outpath, "w") as f:
            json.dump(out, f, indent=2, default=str)
        print(f"  saved: {outpath}")
        return out.get("body")

    except Exception as e:
        print(f"  FAILED: {e}")
        outpath = os.path.join(OUTPUT_DIR, f"{label}.json")
        with open(outpath, "w") as f:
            json.dump({"url": url, "error": str(e)}, f, indent=2)
        return None


def extract_list(data, keys=("data", "items", "registered_models", "models",
                              "registeredModels", "bundles", "policies",
                              "results", "findings", "projects", "users",
                              "modelProducts", "environments")):
    """Try to get a list from a possibly-wrapped response."""
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            if k in data and isinstance(data[k], list):
                return data[k]
    return []


def main():
    print(f"API Discovery — base URL: {BASE_URL}")
    print(f"Output directory: {OUTPUT_DIR}/")
    print(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    # ══════════════════════════════════════════════════════════════
    # 1. GOVERNANCE — Bundles
    # ══════════════════════════════════════════════════════════════
    bundles_raw = fetch("/api/governance/v1/bundles", label="governance_bundles")
    bundles = extract_list(bundles_raw)

    first_bundle_id = None
    if bundles:
        first_bundle = bundles[0]
        first_bundle_id = first_bundle.get("id") or first_bundle.get("_id")
        print(f"\n  >>> First bundle ID: {first_bundle_id}")
        print(f"  >>> First bundle name: {first_bundle.get('name')}")

    # Single bundle detail
    if first_bundle_id:
        fetch(f"/api/governance/v1/bundles/{first_bundle_id}",
              label="governance_bundle_detail")

        # Bundle approvals
        fetch(f"/api/governance/v1/bundles/{first_bundle_id}/approvals",
              label="governance_bundle_approvals")

        # Bundle findings
        fetch(f"/api/governance/v1/bundles/{first_bundle_id}/findings",
              label="governance_bundle_findings")

        # Evidence / results for bundle
        fetch("/api/governance/v1/results",
              params={"bundleId": first_bundle_id},
              label="governance_results_by_bundle")

        # Latest results for bundle
        fetch("/api/governance/v1/results/latest",
              params={"bundleId": first_bundle_id},
              label="governance_results_latest")

    # Try a second bundle too (different data shape possible)
    if len(bundles) > 1:
        second_id = bundles[1].get("id") or bundles[1].get("_id")
        if second_id:
            fetch(f"/api/governance/v1/bundles/{second_id}",
                  label="governance_bundle_detail_2")
            fetch(f"/api/governance/v1/bundles/{second_id}/findings",
                  label="governance_bundle_findings_2")
            fetch("/api/governance/v1/results",
                  params={"bundleId": second_id},
                  label="governance_results_by_bundle_2")

    # ══════════════════════════════════════════════════════════════
    # 2. GOVERNANCE — Policies
    # ══════════════════════════════════════════════════════════════
    policies_raw = fetch("/api/governance/v1/policy-overviews",
                         label="governance_policy_overviews")
    policies = extract_list(policies_raw)

    if policies:
        first_policy_id = policies[0].get("id") or policies[0].get("_id")
        if first_policy_id:
            fetch(f"/api/governance/v1/policies/{first_policy_id}",
                  label="governance_policy_detail")
            fetch(f"/api/governance/v1/policies/{first_policy_id}/definition",
                  label="governance_policy_definition")

    # ══════════════════════════════════════════════════════════════
    # 3. MODEL REGISTRY
    # ══════════════════════════════════════════════════════════════
    models_raw = fetch("/api/modelregistry/v2/models",
                       label="modelregistry_models")
    models = extract_list(models_raw)

    if models:
        first_model = models[0]
        first_model_id = first_model.get("id") or first_model.get("_id")
        print(f"\n  >>> First model ID: {first_model_id}")
        print(f"  >>> First model name: {first_model.get('name')}")

        if first_model_id:
            fetch(f"/api/modelregistry/v2/models/{first_model_id}",
                  label="modelregistry_model_detail")
            fetch(f"/api/modelregistry/v2/models/{first_model_id}/versions",
                  label="modelregistry_model_versions")

    # ══════════════════════════════════════════════════════════════
    # 4. MODEL ENDPOINTS (modelProducts / model APIs)
    # ══════════════════════════════════════════════════════════════
    fetch("/v4/modelProducts", label="model_products")
    # Alternative endpoint paths
    fetch("/api/modelProducts/v1", label="model_products_v1")

    # ══════════════════════════════════════════════════════════════
    # 5. MODEL MONITORING
    # ══════════════════════════════════════════════════════════════
    # We need a model monitoring ID — check if any model has one
    fetch("/api/modelmonitor/v1", label="model_monitor_list")

    # ══════════════════════════════════════════════════════════════
    # 6. PROJECTS
    # ══════════════════════════════════════════════════════════════
    projects_raw = fetch("/v4/projects", label="projects_list")
    projects = extract_list(projects_raw)

    if projects:
        first_project = projects[0]
        first_project_id = first_project.get("id") or first_project.get("_id")
        if first_project_id:
            fetch(f"/v4/projects/{first_project_id}",
                  label="project_detail")

    # Also try the gateway/projects endpoint
    fetch("/v4/gateway/projects", label="gateway_projects")

    # ══════════════════════════════════════════════════════════════
    # 7. USERS
    # ══════════════════════════════════════════════════════════════
    fetch("/v4/users", label="users_list")
    # Current user / self
    fetch("/v4/users/self", label="users_self")

    # ══════════════════════════════════════════════════════════════
    # 8. ENVIRONMENTS
    # ══════════════════════════════════════════════════════════════
    envs_raw = fetch("/v4/environments", label="environments_list")
    # Alternative
    fetch("/api/environments/v1", label="environments_v1")
    envs = extract_list(envs_raw)
    if envs:
        first_env_id = envs[0].get("id") or envs[0].get("_id")
        if first_env_id:
            fetch(f"/v4/environments/{first_env_id}",
                  label="environment_detail")

    # ══════════════════════════════════════════════════════════════
    # 9. JOBS / RUNS
    # ══════════════════════════════════════════════════════════════
    # Recent jobs for current project
    if projects:
        for p in projects:
            if p.get("name") == "mrm-playground" or "mrm" in p.get("name", "").lower():
                pid = p.get("id") or p.get("_id")
                if pid:
                    fetch(f"/v4/projects/{pid}/runs",
                          label="project_runs")
                    break

    # Also try generic runs endpoint
    fetch("/v4/jobs", label="jobs_list")
    fetch("/v4/runs", label="runs_list")

    # ══════════════════════════════════════════════════════════════
    # 10. DATASETS
    # ══════════════════════════════════════════════════════════════
    fetch("/v4/datasetrw", label="datasets_list")
    fetch("/api/datasetrw/v2", label="datasets_v2")

    # ══════════════════════════════════════════════════════════════
    # 11. HARDWARE TIERS
    # ══════════════════════════════════════════════════════════════
    fetch("/v4/hardwareTiers", label="hardware_tiers")

    # ══════════════════════════════════════════════════════════════
    # 12. DATA SOURCES
    # ══════════════════════════════════════════════════════════════
    fetch("/api/datasources/v2", label="datasources_list")
    fetch("/v4/datasources", label="datasources_v4")

    # ══════════════════════════════════════════════════════════════
    # 13. AUDIT EVENTS
    # ══════════════════════════════════════════════════════════════
    fetch("/auditevents", params={"limit": 5}, label="audit_events")

    # ══════════════════════════════════════════════════════════════
    # 14. COMPUTE GRID / CLUSTERS
    # ══════════════════════════════════════════════════════════════
    fetch("/v4/sparkClusters", label="spark_clusters")
    fetch("/v4/rayClusters", label="ray_clusters")

    # ══════════════════════════════════════════════════════════════
    # 15. TAGS (used on models/bundles)
    # ══════════════════════════════════════════════════════════════
    fetch("/v4/tags", label="tags_list")

    # ══════════════════════════════════════════════════════════════
    # 16. RETRY FAILED ENDPOINTS VIA DIRECT HOST
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*60}")
    print(f"RETRYING FAILED ENDPOINTS VIA DIRECT HOST: {DIRECT_URL}")
    print(f"{'='*60}")

    # Governance endpoints — try direct host
    bundles_raw = fetch("/api/governance/v1/bundles",
                        label="direct_governance_bundles", base_url=DIRECT_URL)
    bundles = extract_list(bundles_raw)

    if bundles:
        first_bundle_id = bundles[0].get("id") or bundles[0].get("_id")
        if first_bundle_id:
            fetch(f"/api/governance/v1/bundles/{first_bundle_id}",
                  label="direct_governance_bundle_detail", base_url=DIRECT_URL)
            fetch(f"/api/governance/v1/bundles/{first_bundle_id}/approvals",
                  label="direct_governance_bundle_approvals", base_url=DIRECT_URL)
            fetch(f"/api/governance/v1/bundles/{first_bundle_id}/findings",
                  label="direct_governance_bundle_findings", base_url=DIRECT_URL)
            fetch("/api/governance/v1/results",
                  params={"bundleId": first_bundle_id},
                  label="direct_governance_results", base_url=DIRECT_URL)
            fetch("/api/governance/v1/results/latest",
                  params={"bundleId": first_bundle_id},
                  label="direct_governance_results_latest", base_url=DIRECT_URL)

        if len(bundles) > 1:
            second_id = bundles[1].get("id") or bundles[1].get("_id")
            if second_id:
                fetch(f"/api/governance/v1/bundles/{second_id}",
                      label="direct_governance_bundle_detail_2", base_url=DIRECT_URL)
                fetch("/api/governance/v1/results",
                      params={"bundleId": second_id},
                      label="direct_governance_results_2", base_url=DIRECT_URL)

    # Governance policies via direct
    policies_raw = fetch("/api/governance/v1/policy-overviews",
                         label="direct_governance_policy_overviews", base_url=DIRECT_URL)
    policies = extract_list(policies_raw)
    if policies:
        first_policy_id = policies[0].get("id") or policies[0].get("_id")
        if first_policy_id:
            fetch(f"/api/governance/v1/policies/{first_policy_id}",
                  label="direct_governance_policy_detail", base_url=DIRECT_URL)
            fetch(f"/api/governance/v1/policies/{first_policy_id}/definition",
                  label="direct_governance_policy_definition", base_url=DIRECT_URL)

    # Model registry via direct
    models_raw = fetch("/api/modelregistry/v2/models",
                       label="direct_modelregistry_models", base_url=DIRECT_URL)
    models = extract_list(models_raw)
    if models:
        first_model_id = models[0].get("id") or models[0].get("_id")
        if first_model_id:
            fetch(f"/api/modelregistry/v2/models/{first_model_id}",
                  label="direct_modelregistry_model_detail", base_url=DIRECT_URL)
            fetch(f"/api/modelregistry/v2/models/{first_model_id}/versions",
                  label="direct_modelregistry_model_versions", base_url=DIRECT_URL)

    # Model monitoring via direct
    fetch("/api/modelmonitor/v1", label="direct_model_monitor", base_url=DIRECT_URL)

    # Environments via direct
    fetch("/v4/environments", label="direct_environments", base_url=DIRECT_URL)
    fetch("/api/environments/v1", label="direct_environments_v1", base_url=DIRECT_URL)
    fetch("/api/environments/beta", label="direct_environments_beta", base_url=DIRECT_URL)

    # Hardware tiers via direct
    fetch("/v4/hardwareTiers", label="direct_hardware_tiers", base_url=DIRECT_URL)

    # Datasets via direct
    fetch("/v4/datasetrw", label="direct_datasets", base_url=DIRECT_URL)
    fetch("/api/datasetrw/v2", label="direct_datasets_v2", base_url=DIRECT_URL)

    # Jobs/Runs via direct
    fetch("/v4/jobs", label="direct_jobs", base_url=DIRECT_URL)
    project_id = os.getenv("DOMINO_PROJECT_ID", "")
    if project_id:
        fetch(f"/v4/projects/{project_id}/runs",
              label="direct_project_runs", base_url=DIRECT_URL)
        fetch(f"/v4/projects/{project_id}/executions",
              label="direct_project_executions", base_url=DIRECT_URL)

    # Data sources via direct
    fetch("/api/datasources/v2", label="direct_datasources", base_url=DIRECT_URL)

    # Audit events via direct
    fetch("/auditevents", params={"limit": 3},
          label="direct_audit_events", base_url=DIRECT_URL)

    # ══════════════════════════════════════════════════════════════
    # 17. ALTERNATIVE API PATHS (some APIs moved between versions)
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*60}")
    print("TRYING ALTERNATIVE API PATHS")
    print(f"{'='*60}")

    for base in [PROXY_URL, DIRECT_URL]:
        tag = "proxy" if base == PROXY_URL else "direct"
        # Model registry alternatives
        fetch("/api/registeredmodels/v1", label=f"{tag}_registeredmodels_v1", base_url=base)
        fetch("/api/registeredmodels", label=f"{tag}_registeredmodels", base_url=base)
        # Governance alternatives
        fetch("/api/governance/v2/bundles", label=f"{tag}_governance_v2_bundles", base_url=base)
        fetch("/governance/api/v1/bundles", label=f"{tag}_governance_alt_bundles", base_url=base)
        # Environments
        fetch("/api/environments/v2", label=f"{tag}_environments_v2", base_url=base)
        # Datasets
        fetch("/api/datasets/v1", label=f"{tag}_datasets_v1", base_url=base)
        # Model endpoints / model APIs
        fetch("/api/modelApis/v1", label=f"{tag}_modelApis_v1", base_url=base)

    # ══════════════════════════════════════════════════════════════
    # 18. DEEP DIVE ON WORKING ENDPOINTS
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*60}")
    print("DEEP DIVE ON WORKING ENDPOINTS")
    print(f"{'='*60}")

    # Registered models v1 — get individual model details
    for base in [PROXY_URL]:
        tag = "proxy"
        rm_raw = fetch("/api/registeredmodels/v1", label=f"{tag}_rm_v1_list", base_url=base)
        rm_items = extract_list(rm_raw)
        if rm_items:
            first_rm = rm_items[0]
            rm_name = first_rm.get("name", "")
            print(f"\n  >>> First registered model name: {rm_name}")
            # Try to get detail by name
            if rm_name:
                fetch(f"/api/registeredmodels/v1/{rm_name}",
                      label=f"{tag}_rm_detail_by_name", base_url=base)
                fetch(f"/api/registeredmodels/v1/{rm_name}/versions",
                      label=f"{tag}_rm_versions", base_url=base)

            # Get detail of second model too
            if len(rm_items) > 1:
                rm2_name = rm_items[1].get("name", "")
                if rm2_name:
                    fetch(f"/api/registeredmodels/v1/{rm2_name}",
                          label=f"{tag}_rm_detail_2", base_url=base)

    # Model Products — drill into first one
    mp_raw = fetch("/v4/modelProducts", label="mp_list_drill", base_url=PROXY_URL)
    mp_items = extract_list(mp_raw)
    if mp_items:
        mp_id = mp_items[0].get("id")
        if mp_id:
            fetch(f"/v4/modelProducts/{mp_id}",
                  label="mp_detail", base_url=PROXY_URL)

    # Try more governance paths
    for base in [PROXY_URL, DIRECT_URL]:
        tag = "proxy" if base == PROXY_URL else "direct"
        # Governance bundles — try various prefixes
        for path in [
            "/api/governance/bundles",
            "/api/governance/v1/bundle-overviews",
            "/api/governance/v1/bundle",
            "/api/mrm/v1/bundles",
            "/api/mrm/bundles",
            "/api/governancebundles/v1",
            "/api/bundles/v1",
        ]:
            fetch(path, label=f"{tag}_gov_{path.replace('/','_').strip('_')}", base_url=base)

    # Try governance via registered model association
    for base in [PROXY_URL]:
        rm_raw2 = fetch("/api/registeredmodels/v1", label="rm_for_gov_lookup", base_url=base)
        rm_items2 = extract_list(rm_raw2)
        if rm_items2:
            rm_name = rm_items2[0].get("name", "")
            if rm_name:
                # Some Domino versions expose governance on the model itself
                fetch(f"/api/registeredmodels/v1/{rm_name}/governance",
                      label="rm_governance_link", base_url=base)
                fetch(f"/api/registeredmodels/v1/{rm_name}/bundle",
                      label="rm_bundle_link", base_url=base)

    # Environments — try more paths
    for base in [PROXY_URL]:
        for path in [
            "/api/environments/v1/environments",
            "/v4/environments/list",
            f"/v4/projects/{os.getenv('DOMINO_PROJECT_ID','')}/environments",
        ]:
            fetch(path, label=f"env_{path.replace('/','_').strip('_')}", base_url=base)

    # Hardware tiers — try with project context
    pid = os.getenv("DOMINO_PROJECT_ID", "")
    if pid:
        fetch(f"/v4/projects/{pid}/hardwareTiers",
              label="project_hw_tiers", base_url=PROXY_URL)

    # Jobs — try project-scoped patterns
    if pid:
        for path in [
            f"/v4/projects/{pid}/jobs",
            f"/v4/projects/{pid}/runs/recent",
            f"/api/jobs/v1",
            f"/api/runs/v1",
        ]:
            fetch(path, label=f"jobs_{path.replace('/','_').strip('_')}", base_url=PROXY_URL)

    # Datasets — try project-scoped
    if pid:
        fetch(f"/v4/projects/{pid}/datasets",
              label="project_datasets", base_url=PROXY_URL)

    # ══════════════════════════════════════════════════════════════
    # SUMMARY
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*60}")
    print("DISCOVERY COMPLETE")
    print(f"{'='*60}")
    files = sorted(os.listdir(OUTPUT_DIR))
    print(f"Saved {len(files)} response files to {OUTPUT_DIR}/:")
    for f in files:
        fpath = os.path.join(OUTPUT_DIR, f)
        size = os.path.getsize(fpath)
        # Quick peek at status
        try:
            with open(fpath) as fh:
                d = json.load(fh)
                status = d.get("status_code", "?")
                has_body = "body" in d
                print(f"  {f:50s}  HTTP {status}  {'OK' if has_body else 'NO JSON BODY'}  ({size:,} bytes)")
        except Exception:
            print(f"  {f:50s}  (parse error)  ({size:,} bytes)")


if __name__ == "__main__":
    main()
