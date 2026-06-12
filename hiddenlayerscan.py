#!/usr/bin/env python3
"""
HiddenLayer Model Security Scanner — demo passthrough
Prints realistic scan logs, writes a self-contained HTML report.

Usage:
    python3 hiddenlayerscan.py [model_name] [--out report.html]
"""

import argparse
import hashlib
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── CLI ───────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="HiddenLayer model security scan (demo)")
parser.add_argument("model", nargs="?", default="fraud-detection-v3.pkl",
                    help="Model name / path to scan")
parser.add_argument("--out", default="hiddenlayer-report.html",
                    help="Output HTML report path (default: hiddenlayer-report.html)")
args = parser.parse_args()

MODEL_NAME = args.model
OUT_PATH   = Path(args.out)
SEED       = int(hashlib.md5(MODEL_NAME.encode()).hexdigest(), 16) % (2**31)
random.seed(SEED)

# ── Colour helpers (stdout only) ──────────────────────────────────────────────
RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RED    = "\033[91m"
YELLOW = "\033[93m"
GREEN  = "\033[92m"
CYAN   = "\033[96m"
WHITE  = "\033[97m"

def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:12]

def log(msg, colour=WHITE, indent=0):
    prefix = "  " * indent
    print(f"{DIM}{ts()}{RESET}  {colour}{prefix}{msg}{RESET}", file=sys.stderr, flush=True)

def section(title):
    print(flush=True, file=sys.stderr)
    print(f"{BOLD}{CYAN}{'─' * 60}{RESET}", file=sys.stderr, flush=True)
    print(f"{BOLD}{CYAN}  {title}{RESET}", file=sys.stderr, flush=True)
    print(f"{BOLD}{CYAN}{'─' * 60}{RESET}", file=sys.stderr, flush=True)

def tick(label, value, colour=WHITE, indent=1):
    log(f"{DIM}{label:<38}{RESET} {colour}{value}{RESET}", indent=indent)

def pause(lo=0.05, hi=0.18):
    time.sleep(random.uniform(lo, hi))

# ── Simulated scan data ───────────────────────────────────────────────────────
MODEL_HASH    = hashlib.sha256((MODEL_NAME * 7).encode()).hexdigest()
MODEL_SIZE_MB = round(random.uniform(18.4, 340.7), 1)
SCAN_START    = datetime.now(timezone.utc)

FRAMEWORK   = random.choice(["PyTorch 2.1", "TensorFlow 2.14", "Scikit-learn 1.4", "ONNX 1.15"])
MODEL_FMT   = {"PyTorch 2.1": "pickle/pt", "TensorFlow 2.14": "SavedModel/h5",
               "Scikit-learn 1.4": "pickle/joblib", "ONNX 1.15": "onnx"}[FRAMEWORK]
LAYERS      = random.randint(12, 187)
PARAMS      = f"{random.uniform(1.2, 340.5):.1f}M"
SIGNATURES  = random.randint(48_200, 52_100)
OPS_SCANNED = random.randint(3_100, 8_400)

FINDINGS_RAW = [
    {
        "id": "HL-2024-0041",
        "title": "Serialized lambda / eval gadget chain",
        "severity": "CRITICAL",
        "category": "Arbitrary Code Execution",
        "layer": "__reduce__ / __reduce_ex__",
        "confidence": 97,
        "cve": "CVE-2024-3094 (analogue)",
        "detail": "Pickle opcode sequence matches known ACE gadget pattern. "
                  "Deserializing this object on an unpatched runtime executes arbitrary shell commands.",
        "remediation": "Re-serialize using safetensors or torch.save with weights_only=True.",
    },
    {
        "id": "HL-2024-0107",
        "title": "Obfuscated tensor payload (base64 stego)",
        "severity": "HIGH",
        "category": "Data Exfiltration / Backdoor",
        "layer": "embedding.weight [layer 3]",
        "confidence": 88,
        "cve": "N/A",
        "detail": "High-entropy bytes embedded in unused weight rows. Pattern consistent with "
                  "steganographic data encoding observed in supply-chain backdoor samples.",
        "remediation": "Audit weight provenance; compare checksum against upstream training artifact.",
    },
    {
        "id": "HL-2024-0203",
        "title": "Unsafe custom __getattr__ hook",
        "severity": "HIGH",
        "category": "Privilege Escalation",
        "layer": "model.classifier",
        "confidence": 81,
        "cve": "N/A",
        "detail": "Custom attribute accessor triggers file-system reads outside the model's working "
                  "directory at inference time. Exploitable in multi-tenant serving environments.",
        "remediation": "Remove __getattr__ override; use explicit property accessors.",
    },
    {
        "id": "HL-2024-0318",
        "title": "Dependency on deprecated pickle protocol 2",
        "severity": "MEDIUM",
        "category": "Compliance / Supply Chain",
        "layer": "Global header",
        "confidence": 100,
        "cve": "N/A",
        "detail": "Model uses pickle protocol 2 (Python 2.3 era). Protocol 5 is required by policy "
                  "HL-POL-0012 for models deployed in regulated environments.",
        "remediation": "Re-export with protocol=5; validate checksum after re-export.",
    },
    {
        "id": "HL-2024-0419",
        "title": "Unverified third-party operator: torch_xops.fused_matmul",
        "severity": "MEDIUM",
        "category": "Supply Chain Integrity",
        "layer": "layers.4 / layers.11",
        "confidence": 74,
        "cve": "N/A",
        "detail": "Custom C++ extension operator not in HiddenLayer approved operator registry. "
                  "Origin package (torch_xops 0.3.1) has no published SBOM.",
        "remediation": "Pin to a registry-approved operator; open variance ticket if business-critical.",
    },
    {
        "id": "HL-2024-0512",
        "title": "Hardcoded internal endpoint in model config",
        "severity": "LOW",
        "category": "Information Disclosure",
        "layer": "model.config / metadata blob",
        "confidence": 95,
        "cve": "N/A",
        "detail": "Model metadata blob contains a hardcoded URL matching internal MLOps infra "
                  "pattern (*.internal.corp). Not executable but leaks topology in public artifacts.",
        "remediation": "Scrub metadata before publishing; use relative or environment-resolved URIs.",
    },
    {
        "id": "HL-2024-0601",
        "title": "Missing model card / transparency metadata",
        "severity": "INFO",
        "category": "Governance",
        "layer": "N/A",
        "confidence": 100,
        "cve": "N/A",
        "detail": "No model card (README.md / modelcard.json) found. Required by NIST AI RMF "
                  "MAP-1.5 and EU AI Act Article 13 for high-risk system categories.",
        "remediation": "Add a model card documenting training data, intended use, and limitations.",
    },
]

SEV_ORDER  = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
FINDINGS   = sorted(FINDINGS_RAW, key=lambda f: SEV_ORDER[f["severity"]])
SEV_COUNTS = {s: sum(1 for f in FINDINGS if f["severity"] == s)
              for s in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")}

RISK_SCORE = 9.1 if SEV_COUNTS["CRITICAL"] else \
             7.4 if SEV_COUNTS["HIGH"]     else \
             4.8 if SEV_COUNTS["MEDIUM"]   else 2.1

RISK_LABEL = "CRITICAL" if RISK_SCORE >= 9 else \
             "HIGH"      if RISK_SCORE >= 7 else \
             "MEDIUM"    if RISK_SCORE >= 4 else "LOW"

# ── Scan simulation ───────────────────────────────────────────────────────────
print(flush=True, file=sys.stderr)
print(f"{BOLD}{'═' * 60}{RESET}", file=sys.stderr, flush=True)
print(f"{BOLD}  HiddenLayer  ·  Model Security Scanner  ·  v2.9.1{RESET}", file=sys.stderr, flush=True)
print(f"{BOLD}{'═' * 60}{RESET}", file=sys.stderr, flush=True)

section("INITIALISING")
for msg in [
    ("Loading signature database …", GREEN),
    (f"Signatures loaded: {SIGNATURES:,}", WHITE),
    ("Connecting to HiddenLayer cloud telemetry …", GREEN),
    ("Session token acquired (TTL 3600s)", WHITE),
    (f"Target: {MODEL_NAME}", CYAN),
]:
    log(*msg, indent=1); pause()

section("FILE ANALYSIS")
tick("Path",           MODEL_NAME)
tick("SHA-256",        MODEL_HASH)
tick("Size",           f"{MODEL_SIZE_MB} MB")
tick("Format",         MODEL_FMT)
tick("Framework",      FRAMEWORK)
pause(0.1, 0.2)
log("Decompressing and mapping tensor shards …",   indent=1); pause(0.1, 0.25)
log("Resolving pickle opcode graph …",             indent=1); pause(0.1, 0.25)
log("Extracting embedded metadata blobs …",        indent=1); pause(0.05, 0.15)
log("Computing layer fingerprints …",              indent=1); pause(0.1, 0.2)

section("STATIC ANALYSIS")
tick("Layers identified",   f"{LAYERS}")
tick("Parameters",          PARAMS)
tick("Ops scanned",         f"{OPS_SCANNED:,}")
tick("Custom extensions",   str(random.randint(1, 4)))
tick("Pickle opcodes",      str(random.randint(220, 480)))
pause(0.1, 0.2)

log("Running opcode sequence classifier …",                    indent=1); pause(0.1, 0.2)
log("Checking gadget-chain signatures …",                      indent=1); pause(0.1, 0.3)
log(f"{RED}  !! Gadget chain match — HL-2024-0041 (CRITICAL){RESET}",  indent=1); pause(0.05, 0.1)
log("Scanning weight tensors for anomalies …",                 indent=1); pause(0.2, 0.4)
log(f"{YELLOW}  !! Stego pattern in embedding.weight — HL-2024-0107{RESET}", indent=1); pause(0.05, 0.1)
log("Analysing custom operator registry …",                    indent=1); pause(0.1, 0.2)
log(f"{YELLOW}  !! Unverified operator torch_xops — HL-2024-0419{RESET}",    indent=1); pause(0.05, 0.1)
log("Scanning attribute hook implementations …",               indent=1); pause(0.1, 0.2)
log(f"{YELLOW}  !! Unsafe __getattr__ — HL-2024-0203{RESET}",                indent=1); pause(0.05, 0.1)

section("POLICY & COMPLIANCE CHECKS")
log("Checking serialisation protocol policy (HL-POL-0012) …",  indent=1); pause(0.1, 0.2)
log(f"{YELLOW}  !! Protocol 2 violation — HL-2024-0318{RESET}",              indent=1); pause(0.05, 0.1)
log("Scanning metadata for sensitive strings …",               indent=1); pause(0.1, 0.2)
log(f"{DIM}  !! Hardcoded endpoint — HL-2024-0512 (LOW){RESET}",             indent=1); pause(0.05, 0.1)
log("Verifying model card presence (NIST AI RMF MAP-1.5) …",   indent=1); pause(0.1, 0.2)
log(f"{DIM}  !! No model card found — HL-2024-0601 (INFO){RESET}",           indent=1); pause(0.05, 0.1)

section("SCAN COMPLETE")
SCAN_END      = datetime.now(timezone.utc)
SCAN_DURATION = (SCAN_END - SCAN_START).total_seconds()

tick("Duration",    f"{SCAN_DURATION:.2f}s")
tick("Findings",    str(len(FINDINGS)))
tick("Critical",    str(SEV_COUNTS["CRITICAL"]), RED)
tick("High",        str(SEV_COUNTS["HIGH"]),     YELLOW)
tick("Medium",      str(SEV_COUNTS["MEDIUM"]),   YELLOW)
tick("Low",         str(SEV_COUNTS["LOW"]),       DIM + WHITE)
tick("Info",        str(SEV_COUNTS["INFO"]),      DIM)
tick("Risk score",  f"{RISK_SCORE}  [{RISK_LABEL}]",
     RED if RISK_LABEL in ("CRITICAL","HIGH") else YELLOW)

# ── HTML report ───────────────────────────────────────────────────────────────
SEV_COLOUR = {
    "CRITICAL": ("#ff3b3b", "#3d0a0a"),
    "HIGH":     ("#ff8c00", "#3d1f00"),
    "MEDIUM":   ("#f5c518", "#2e2600"),
    "LOW":      ("#4fc3f7", "#0a1f2e"),
    "INFO":     ("#9e9e9e", "#1e1e1e"),
}

def sev_badge(sev):
    fg, bg = SEV_COLOUR[sev]
    return (f'<span style="background:{bg};color:{fg};border:1px solid {fg}22;'
            f'font-size:11px;font-weight:700;letter-spacing:.06em;'
            f'padding:2px 8px;border-radius:3px">{sev}</span>')

risk_fg, risk_bg = SEV_COLOUR[RISK_LABEL]

findings_rows = ""
for f in FINDINGS:
    fg, bg = SEV_COLOUR[f["severity"]]
    conf_bar = (f'<div style="width:100%;background:#ffffff14;border-radius:2px;height:5px;margin-top:4px">'
                f'<div style="width:{f["confidence"]}%;background:{fg};height:5px;border-radius:2px"></div>'
                f'</div><div style="font-size:10px;color:#6b7280;margin-top:2px">{f["confidence"]}% confidence</div>')
    findings_rows += f"""
        <tr>
            <td style="padding:14px 16px;border-bottom:1px solid #1f2937;white-space:nowrap">
                <code style="font-size:11px;color:#6b7280">{f["id"]}</code>
            </td>
            <td style="padding:14px 16px;border-bottom:1px solid #1f2937">{sev_badge(f["severity"])}</td>
            <td style="padding:14px 16px;border-bottom:1px solid #1f2937">
                <div style="font-weight:600;color:#f3f4f6;margin-bottom:2px">{f["title"]}</div>
                <div style="font-size:12px;color:#9ca3af">{f["detail"]}</div>
                <div style="font-size:11px;color:#4b5563;margin-top:6px">
                    <span style="color:#374151">Remediation:</span> {f["remediation"]}
                </div>
            </td>
            <td style="padding:14px 16px;border-bottom:1px solid #1f2937;white-space:nowrap">
                <div style="font-size:12px;color:#9ca3af">{f["category"]}</div>
            </td>
            <td style="padding:14px 16px;border-bottom:1px solid #1f2937;white-space:nowrap">
                <code style="font-size:11px;color:#6b7280">{f["layer"]}</code>
            </td>
            <td style="padding:14px 16px;border-bottom:1px solid #1f2937">
                {conf_bar}
            </td>
        </tr>"""

def sev_card(label, count, fg, bg):
    border = f"2px solid {fg}55" if count else "1px solid #1f2937"
    return (f'<div style="background:{bg if count else "#111827"};border:{border};'
            f'border-radius:8px;padding:16px 22px;min-width:90px;text-align:center">'
            f'<div style="font-size:28px;font-weight:700;color:{fg if count else "#374151"}">{count}</div>'
            f'<div style="font-size:11px;font-weight:600;letter-spacing:.07em;color:{fg if count else "#4b5563"}'
            f';margin-top:2px">{label}</div></div>')

cards = "".join([
    sev_card("CRITICAL", SEV_COUNTS["CRITICAL"], *SEV_COLOUR["CRITICAL"]),
    sev_card("HIGH",     SEV_COUNTS["HIGH"],     *SEV_COLOUR["HIGH"]),
    sev_card("MEDIUM",   SEV_COUNTS["MEDIUM"],   *SEV_COLOUR["MEDIUM"]),
    sev_card("LOW",      SEV_COUNTS["LOW"],      *SEV_COLOUR["LOW"]),
    sev_card("INFO",     SEV_COUNTS["INFO"],      *SEV_COLOUR["INFO"]),
])

scan_dt = SCAN_START.strftime("%Y-%m-%d %H:%M:%S UTC")

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HiddenLayer Scan — {MODEL_NAME}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
          background: #030712; color: #d1d5db; min-height: 100vh; }}
  a {{ color: #6366f1; text-decoration: none; }}
  code {{ font-family: ui-monospace, "SF Mono", Menlo, monospace; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th {{ text-align: left; font-size: 11px; font-weight: 700; letter-spacing: .08em;
        color: #4b5563; padding: 10px 16px; border-bottom: 1px solid #1f2937;
        background: #0a0f1a; }}
  tr:last-child td {{ border-bottom: none !important; }}
</style>
</head>
<body>

<!-- TOP BAR -->
<div style="background:#0a0f1a;border-bottom:1px solid #1f2937;padding:0 40px">
  <div style="max-width:1200px;margin:0 auto;height:56px;display:flex;align-items:center;gap:16px">
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
      <rect width="24" height="24" rx="5" fill="#6366f1"/>
      <path d="M7 12l3.5 3.5L17 8" stroke="white" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>
    <span style="font-weight:700;font-size:16px;color:#f9fafb;letter-spacing:-.01em">HiddenLayer</span>
    <span style="color:#374151;font-size:14px">Model Security Scanner</span>
    <div style="margin-left:auto;display:flex;align-items:center;gap:8px">
      <span style="font-size:12px;color:#4b5563">v2.9.1</span>
      <span style="font-size:12px;color:#374151">·</span>
      <span style="font-size:12px;color:#4b5563">{scan_dt}</span>
    </div>
  </div>
</div>

<div style="max-width:1200px;margin:0 auto;padding:32px 40px">

  <!-- HERO -->
  <div style="background:#0d1117;border:1px solid #1f2937;border-radius:10px;
              padding:28px 32px;margin-bottom:28px;display:flex;align-items:center;gap:32px">
    <div style="background:{risk_bg};border:2px solid {risk_fg}44;border-radius:10px;
                padding:20px 28px;text-align:center;flex-shrink:0">
      <div style="font-size:44px;font-weight:800;color:{risk_fg};line-height:1">{RISK_SCORE}</div>
      <div style="font-size:11px;font-weight:700;letter-spacing:.1em;color:{risk_fg};
                  margin-top:4px">RISK SCORE</div>
      <div style="margin-top:8px">{sev_badge(RISK_LABEL)}</div>
    </div>
    <div style="flex:1;min-width:0">
      <div style="font-size:22px;font-weight:700;color:#f9fafb;margin-bottom:4px;
                  overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
           title="{MODEL_NAME}">{MODEL_NAME}</div>
      <div style="font-size:14px;color:#6b7280;margin-bottom:16px">{FRAMEWORK} · {MODEL_FMT}</div>
      <div style="display:flex;gap:10px;flex-wrap:wrap">
        {cards}
      </div>
    </div>
    <div style="flex-shrink:0;text-align:right;font-size:13px;color:#4b5563;line-height:2">
      <div><span style="color:#6b7280">Scan duration</span> &nbsp;{SCAN_DURATION:.2f}s</div>
      <div><span style="color:#6b7280">Signatures</span> &nbsp;{SIGNATURES:,}</div>
      <div><span style="color:#6b7280">Ops scanned</span> &nbsp;{OPS_SCANNED:,}</div>
      <div><span style="color:#6b7280">Parameters</span> &nbsp;{PARAMS}</div>
    </div>
  </div>

  <!-- TWO-COL: model metadata + scan metadata -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:28px">

    <div style="background:#0d1117;border:1px solid #1f2937;border-radius:10px;overflow:hidden">
      <div style="padding:14px 20px;border-bottom:1px solid #1f2937;
                  font-size:11px;font-weight:700;letter-spacing:.08em;color:#4b5563">MODEL METADATA</div>
      <table>
        <tr><td style="padding:10px 20px;color:#6b7280;font-size:13px;width:140px">Name</td>
            <td style="padding:10px 20px;font-size:13px;color:#f3f4f6;word-break:break-all">{MODEL_NAME}</td></tr>
        <tr><td style="padding:10px 20px;color:#6b7280;font-size:13px">Framework</td>
            <td style="padding:10px 20px;font-size:13px;color:#f3f4f6">{FRAMEWORK}</td></tr>
        <tr><td style="padding:10px 20px;color:#6b7280;font-size:13px">Format</td>
            <td style="padding:10px 20px;font-size:13px;color:#f3f4f6">{MODEL_FMT}</td></tr>
        <tr><td style="padding:10px 20px;color:#6b7280;font-size:13px">Size</td>
            <td style="padding:10px 20px;font-size:13px;color:#f3f4f6">{MODEL_SIZE_MB} MB</td></tr>
        <tr><td style="padding:10px 20px;color:#6b7280;font-size:13px">Layers</td>
            <td style="padding:10px 20px;font-size:13px;color:#f3f4f6">{LAYERS}</td></tr>
        <tr><td style="padding:10px 20px;color:#6b7280;font-size:13px">Parameters</td>
            <td style="padding:10px 20px;font-size:13px;color:#f3f4f6">{PARAMS}</td></tr>
        <tr><td style="padding:10px 20px;color:#6b7280;font-size:13px;vertical-align:top">SHA-256</td>
            <td style="padding:10px 20px"><code style="font-size:11px;color:#6b7280;word-break:break-all">{MODEL_HASH}</code></td></tr>
      </table>
    </div>

    <div style="background:#0d1117;border:1px solid #1f2937;border-radius:10px;overflow:hidden">
      <div style="padding:14px 20px;border-bottom:1px solid #1f2937;
                  font-size:11px;font-weight:700;letter-spacing:.08em;color:#4b5563">SCAN DETAILS</div>
      <table>
        <tr><td style="padding:10px 20px;color:#6b7280;font-size:13px;width:160px">Scanner</td>
            <td style="padding:10px 20px;font-size:13px;color:#f3f4f6">HiddenLayer v2.9.1</td></tr>
        <tr><td style="padding:10px 20px;color:#6b7280;font-size:13px">Policy set</td>
            <td style="padding:10px 20px;font-size:13px;color:#f3f4f6">HL-POL-0012 (Enterprise)</td></tr>
        <tr><td style="padding:10px 20px;color:#6b7280;font-size:13px">Scan started</td>
            <td style="padding:10px 20px;font-size:13px;color:#f3f4f6">{SCAN_START.strftime("%Y-%m-%d %H:%M:%S UTC")}</td></tr>
        <tr><td style="padding:10px 20px;color:#6b7280;font-size:13px">Scan ended</td>
            <td style="padding:10px 20px;font-size:13px;color:#f3f4f6">{SCAN_END.strftime("%Y-%m-%d %H:%M:%S UTC")}</td></tr>
        <tr><td style="padding:10px 20px;color:#6b7280;font-size:13px">Duration</td>
            <td style="padding:10px 20px;font-size:13px;color:#f3f4f6">{SCAN_DURATION:.2f}s</td></tr>
        <tr><td style="padding:10px 20px;color:#6b7280;font-size:13px">Signatures checked</td>
            <td style="padding:10px 20px;font-size:13px;color:#f3f4f6">{SIGNATURES:,}</td></tr>
        <tr><td style="padding:10px 20px;color:#6b7280;font-size:13px">Ops analysed</td>
            <td style="padding:10px 20px;font-size:13px;color:#f3f4f6">{OPS_SCANNED:,}</td></tr>
      </table>
    </div>
  </div>

  <!-- FINDINGS TABLE -->
  <div style="background:#0d1117;border:1px solid #1f2937;border-radius:10px;overflow:hidden;margin-bottom:28px">
    <div style="padding:16px 20px;border-bottom:1px solid #1f2937;
                display:flex;align-items:center;justify-content:space-between">
      <div>
        <span style="font-size:11px;font-weight:700;letter-spacing:.08em;color:#4b5563">FINDINGS</span>
        <span style="font-size:12px;color:#374151;margin-left:10px">{len(FINDINGS)} total</span>
      </div>
      <div style="font-size:12px;color:#374151">Sorted by severity</div>
    </div>
    <div style="overflow-x:auto">
      <table>
        <thead>
          <tr>
            <th style="width:120px">ID</th>
            <th style="width:100px">SEVERITY</th>
            <th>FINDING</th>
            <th style="width:180px">CATEGORY</th>
            <th style="width:180px">LAYER / LOCATION</th>
            <th style="width:130px">CONFIDENCE</th>
          </tr>
        </thead>
        <tbody>{findings_rows}</tbody>
      </table>
    </div>
  </div>

  <!-- RISK BREAKDOWN BAR -->
  <div style="background:#0d1117;border:1px solid #1f2937;border-radius:10px;
              padding:20px 24px;margin-bottom:28px">
    <div style="font-size:11px;font-weight:700;letter-spacing:.08em;color:#4b5563;margin-bottom:14px">
      FINDING DISTRIBUTION
    </div>
    <div style="display:flex;height:10px;border-radius:4px;overflow:hidden;gap:2px">
      {"".join(
        f'<div title="{s}: {SEV_COUNTS[s]}" style="background:{SEV_COLOUR[s][0]};'
        f'flex:{SEV_COUNTS[s] or 0.2};min-width:{2 if SEV_COUNTS[s] else 0}px"></div>'
        for s in ("CRITICAL","HIGH","MEDIUM","LOW","INFO")
      )}
    </div>
    <div style="display:flex;gap:20px;margin-top:10px;flex-wrap:wrap">
      {"".join(
        f'<div style="display:flex;align-items:center;gap:6px;font-size:12px;color:#6b7280">'
        f'<div style="width:10px;height:10px;border-radius:2px;background:{SEV_COLOUR[s][0]}"></div>'
        f'{s.title()} ({SEV_COUNTS[s]})</div>'
        for s in ("CRITICAL","HIGH","MEDIUM","LOW","INFO")
      )}
    </div>
  </div>

  <!-- FOOTER -->
  <div style="text-align:center;font-size:12px;color:#374151;padding:16px 0 8px">
    HiddenLayer Model Security Scanner · Report generated {scan_dt} ·
    <span style="color:#4b5563">This report is for authorised use only</span>
  </div>

</div>
</body>
</html>
"""

# Write to stdout (allows piping / capture)
sys.stdout.write(html)
sys.stdout.flush()

# Save to --out path
OUT_PATH.write_text(html, encoding="utf-8")

# Save to /mnt/artifacts (Domino artifacts dir, or override via $DOMINO_ARTIFACTS_DIR)
ARTIFACTS_DIR = Path(os.environ.get("DOMINO_ARTIFACTS_DIR", "/mnt/artifacts"))
artifacts_path = ARTIFACTS_DIR / OUT_PATH.name
try:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    artifacts_path.write_text(html, encoding="utf-8")
    artifacts_saved = str(artifacts_path)
except OSError as e:
    artifacts_saved = f"(skipped: {e})"

print(flush=True, file=sys.stderr)
log(f"Report written → {OUT_PATH.resolve()}", GREEN)
log(f"Artifacts copy  → {artifacts_saved}", GREEN)
print(flush=True, file=sys.stderr)
