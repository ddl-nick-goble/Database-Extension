#!/usr/bin/env python3
"""
HiddenLayer Model Security Scanner — demo passthrough
Prints realistic scan logs, writes a PDF report.

Usage:
    python3 hiddenlayerscan.py [model_name] [--out report.pdf]
"""

import argparse
import hashlib
import io
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Install weasyprint if not present — runs once, silent on subsequent runs.
try:
    import weasyprint as _wp  # noqa: F401
except ImportError:
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "-q", "weasyprint"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

# ── CLI ───────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="HiddenLayer model security scan (demo)")
parser.add_argument("model", nargs="?", default="fraud-detection-v3.pkl",
                    help="Model name / path to scan")
parser.add_argument("--out", default="hiddenlayer-report.pdf",
                    help="Output PDF path (default: hiddenlayer-report.pdf)")
args = parser.parse_args()

MODEL_NAME = args.model
OUT_PATH   = Path(args.out)
SEED       = int(hashlib.md5(MODEL_NAME.encode()).hexdigest(), 16) % (2**31)
random.seed(SEED)

# ── Colour helpers — all output to stderr so stdout stays clean ───────────────
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
        "title": "Deprecated pickle protocol 2 (policy violation)",
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
                  "pattern (*.internal.corp). Not executable but leaks topology in published artifacts.",
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
for msg, colour in [
    ("Loading signature database …", GREEN),
    (f"Signatures loaded: {SIGNATURES:,}", WHITE),
    ("Connecting to HiddenLayer cloud telemetry …", GREEN),
    ("Session token acquired (TTL 3600s)", WHITE),
    (f"Target: {MODEL_NAME}", CYAN),
]:
    log(msg, colour, indent=1); pause()

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

section("GENERATING REPORT")
log("Rendering PDF …", indent=1); pause(0.1, 0.2)

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
     RED if RISK_LABEL in ("CRITICAL", "HIGH") else YELLOW)

# ── HTML → PDF ────────────────────────────────────────────────────────────────

SEV_STYLE = {
    # label_color, bg_color, border_color
    "CRITICAL": ("#b91c1c", "#fef2f2", "#fca5a5"),
    "HIGH":     ("#c2410c", "#fff7ed", "#fdba74"),
    "MEDIUM":   ("#a16207", "#fefce8", "#fde047"),
    "LOW":      ("#1d4ed8", "#eff6ff", "#93c5fd"),
    "INFO":     ("#374151", "#f9fafb", "#d1d5db"),
}

RISK_SCORE_COLOUR = {
    "CRITICAL": "#b91c1c",
    "HIGH":     "#c2410c",
    "MEDIUM":   "#a16207",
    "LOW":      "#1d4ed8",
}[RISK_LABEL]

scan_dt    = SCAN_START.strftime("%B %d, %Y  %H:%M UTC")
report_id  = f"HL-{SCAN_START.strftime('%Y%m%d')}-{MODEL_HASH[:8].upper()}"


def sev_pill(sev):
    fg, bg, border = SEV_STYLE[sev]
    return (f'<span style="display:inline-block;background:{bg};color:{fg};'
            f'border:1px solid {border};border-radius:4px;'
            f'font-size:10px;font-weight:700;letter-spacing:.07em;'
            f'padding:2px 8px;white-space:nowrap">{sev}</span>')


def meta_row(label, value, mono=False):
    val_style = 'font-family:monospace;font-size:11px;color:#374151;word-break:break-all' if mono \
                else 'font-size:12px;color:#111827'
    return (f'<tr>'
            f'<td style="padding:7px 0;color:#6b7280;font-size:12px;'
            f'white-space:nowrap;padding-right:20px;vertical-align:top">{label}</td>'
            f'<td style="{val_style};padding:7px 0">{value}</td>'
            f'</tr>')


findings_rows = ""
for i, f in enumerate(FINDINGS):
    fg, bg, border = SEV_STYLE[f["severity"]]
    row_bg = "#fafafa" if i % 2 == 0 else "#ffffff"
    conf_pct = f["confidence"]
    conf_bar = (
        f'<div style="width:80px;background:#e5e7eb;border-radius:2px;height:4px;display:inline-block;vertical-align:middle">'
        f'<div style="width:{conf_pct}%;background:{fg};height:4px;border-radius:2px"></div></div>'
        f'<span style="font-size:10px;color:#6b7280;margin-left:5px">{conf_pct}%</span>'
    )
    findings_rows += f"""
    <tr style="background:{row_bg}">
      <td style="padding:12px 14px;border-bottom:1px solid #f3f4f6;
                 vertical-align:top;white-space:nowrap">
        <code style="font-size:10px;color:#6b7280">{f["id"]}</code>
      </td>
      <td style="padding:12px 14px;border-bottom:1px solid #f3f4f6;vertical-align:top">
        {sev_pill(f["severity"])}
      </td>
      <td style="padding:12px 14px;border-bottom:1px solid #f3f4f6;vertical-align:top">
        <div style="font-weight:600;color:#111827;font-size:12px;margin-bottom:4px">{f["title"]}</div>
        <div style="font-size:11px;color:#4b5563;line-height:1.5">{f["detail"]}</div>
        <div style="font-size:11px;color:#6b7280;margin-top:5px">
          <span style="font-weight:600;color:#374151">Remediation:</span> {f["remediation"]}
        </div>
      </td>
      <td style="padding:12px 14px;border-bottom:1px solid #f3f4f6;
                 vertical-align:top;white-space:nowrap">
        <div style="font-size:11px;color:#374151">{f["category"]}</div>
      </td>
      <td style="padding:12px 14px;border-bottom:1px solid #f3f4f6;vertical-align:top">
        <code style="font-size:10px;color:#6b7280">{f["layer"]}</code>
      </td>
      <td style="padding:12px 14px;border-bottom:1px solid #f3f4f6;vertical-align:top">
        {conf_bar}
      </td>
    </tr>"""


def sev_summary_cell(sev):
    count = SEV_COUNTS[sev]
    fg, bg, border = SEV_STYLE[sev]
    active = count > 0
    return (
        f'<td style="padding:12px 18px;text-align:center;border-right:1px solid #e5e7eb">'
        f'<div style="font-size:26px;font-weight:700;color:{fg if active else "#9ca3af"}">{count}</div>'
        f'<div style="font-size:10px;font-weight:700;letter-spacing:.07em;'
        f'color:{fg if active else "#9ca3af"};margin-top:2px">{sev}</div>'
        f'</td>'
    )


dist_segments = "".join(
    f'<div title="{s}: {SEV_COUNTS[s]}" style="flex:{SEV_COUNTS[s] or 0.15};'
    f'background:{SEV_STYLE[s][0]};opacity:{1.0 if SEV_COUNTS[s] else 0.15};'
    f'min-width:3px"></div>'
    for s in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")
)

dist_legend = "".join(
    f'<span style="display:inline-flex;align-items:center;gap:5px;'
    f'font-size:11px;color:#374151;margin-right:14px">'
    f'<span style="display:inline-block;width:10px;height:10px;border-radius:2px;'
    f'background:{SEV_STYLE[s][0]};opacity:{1.0 if SEV_COUNTS[s] else 0.3}"></span>'
    f'{s.title()} ({SEV_COUNTS[s]})</span>'
    for s in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")
)

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
  @page {{
    size: A4;
    margin: 14mm 14mm 16mm 14mm;
    @bottom-center {{
      content: "CONFIDENTIAL  ·  HiddenLayer Model Security Report  ·  {report_id}  ·  Page " counter(page) " of " counter(pages);
      font-size: 8px;
      color: #9ca3af;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: .05em;
    }}
  }}

  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    font-size: 12px;
    color: #111827;
    background: #ffffff;
    line-height: 1.5;
  }}

  h1  {{ font-size: 20px; font-weight: 700; color: #111827; }}
  h2  {{ font-size: 13px; font-weight: 700; color: #111827; margin-bottom: 12px; }}

  table {{ border-collapse: collapse; width: 100%; }}
  th {{
    text-align: left;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: .08em;
    color: #6b7280;
    padding: 9px 14px;
    background: #f9fafb;
    border-bottom: 1px solid #e5e7eb;
    border-top: 1px solid #e5e7eb;
  }}

  .card {{
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    overflow: hidden;
    margin-bottom: 18px;
    page-break-inside: avoid;
  }}

  .card-header {{
    padding: 11px 18px;
    background: #f9fafb;
    border-bottom: 1px solid #e5e7eb;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: .08em;
    color: #6b7280;
    text-transform: uppercase;
  }}

  .card-body {{ padding: 16px 18px; }}

  code {{
    font-family: ui-monospace, "SF Mono", "Cascadia Code", Menlo, monospace;
    font-size: 11px;
  }}
</style>
</head>
<body>

<!-- ── HEADER ── -->
<div style="display:flex;align-items:flex-start;justify-content:space-between;
            padding-bottom:16px;border-bottom:2px solid #111827;margin-bottom:22px">
  <div>
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
      <svg width="28" height="28" viewBox="0 0 28 28" fill="none">
        <rect width="28" height="28" rx="6" fill="#1d4ed8"/>
        <path d="M8 14l4.5 4.5L20 9" stroke="white" stroke-width="2.5"
              stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <span style="font-size:15px;font-weight:700;color:#111827;letter-spacing:-.01em">HiddenLayer</span>
      <span style="font-size:11px;color:#6b7280;border-left:1px solid #d1d5db;
                   padding-left:10px;margin-left:2px">Model Security Scanner</span>
    </div>
    <h1>Model Security Assessment Report</h1>
    <div style="font-size:11px;color:#6b7280;margin-top:4px">
      Report ID: <code>{report_id}</code> &nbsp;·&nbsp; Generated: {scan_dt} &nbsp;·&nbsp; Scanner v2.9.1
    </div>
  </div>
  <div style="text-align:right;flex-shrink:0">
    <div style="font-size:10px;font-weight:700;letter-spacing:.08em;
                color:#6b7280;margin-bottom:4px">OVERALL RISK SCORE</div>
    <div style="font-size:40px;font-weight:800;color:{RISK_SCORE_COLOUR};
                line-height:1">{RISK_SCORE}</div>
    <div style="margin-top:4px">{sev_pill(RISK_LABEL)}</div>
  </div>
</div>

<!-- ── EXECUTIVE SUMMARY BAR ── -->
<div class="card">
  <div class="card-header">Executive Summary</div>
  <table>
    <tr>
      {sev_summary_cell("CRITICAL")}
      {sev_summary_cell("HIGH")}
      {sev_summary_cell("MEDIUM")}
      {sev_summary_cell("LOW")}
      {sev_summary_cell("INFO")}
      <td style="padding:12px 18px;text-align:center">
        <div style="font-size:26px;font-weight:700;color:#111827">{len(FINDINGS)}</div>
        <div style="font-size:10px;font-weight:700;letter-spacing:.07em;color:#374151;margin-top:2px">TOTAL</div>
      </td>
    </tr>
  </table>
  <!-- distribution bar -->
  <div style="padding:0 18px 14px">
    <div style="display:flex;height:6px;border-radius:3px;overflow:hidden;gap:2px;margin-bottom:8px">
      {dist_segments}
    </div>
    <div style="display:flex;flex-wrap:wrap">{dist_legend}</div>
  </div>
</div>

<!-- ── TWO-COL METADATA ── -->
<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:18px">

  <div class="card">
    <div class="card-header">Model Metadata</div>
    <div class="card-body">
      <table>
        {meta_row("Name", MODEL_NAME)}
        {meta_row("Framework", FRAMEWORK)}
        {meta_row("Format", MODEL_FMT)}
        {meta_row("File size", f"{MODEL_SIZE_MB} MB")}
        {meta_row("Layers", str(LAYERS))}
        {meta_row("Parameters", PARAMS)}
        {meta_row("SHA-256", MODEL_HASH, mono=True)}
      </table>
    </div>
  </div>

  <div class="card">
    <div class="card-header">Scan Details</div>
    <div class="card-body">
      <table>
        {meta_row("Scanner", "HiddenLayer v2.9.1")}
        {meta_row("Policy set", "HL-POL-0012 (Enterprise)")}
        {meta_row("Scan started", SCAN_START.strftime("%Y-%m-%d %H:%M:%S UTC"))}
        {meta_row("Scan completed", SCAN_END.strftime("%Y-%m-%d %H:%M:%S UTC"))}
        {meta_row("Duration", f"{SCAN_DURATION:.2f}s")}
        {meta_row("Signatures checked", f"{SIGNATURES:,}")}
        {meta_row("Operations analysed", f"{OPS_SCANNED:,}")}
      </table>
    </div>
  </div>

</div>

<!-- ── FINDINGS TABLE ── -->
<div class="card">
  <div class="card-header" style="display:flex;justify-content:space-between;align-items:center">
    <span>Findings</span>
    <span style="font-weight:400;text-transform:none;letter-spacing:0;color:#9ca3af">
      {len(FINDINGS)} findings · sorted by severity
    </span>
  </div>
  <table>
    <thead>
      <tr>
        <th style="width:100px">ID</th>
        <th style="width:90px">SEVERITY</th>
        <th>FINDING &amp; REMEDIATION</th>
        <th style="width:160px">CATEGORY</th>
        <th style="width:160px">LAYER / LOCATION</th>
        <th style="width:110px">CONFIDENCE</th>
      </tr>
    </thead>
    <tbody>{findings_rows}</tbody>
  </table>
</div>

<!-- ── DISCLAIMER ── -->
<div style="border-top:1px solid #e5e7eb;margin-top:8px;padding-top:10px;
            font-size:10px;color:#9ca3af;line-height:1.6">
  This report is generated for authorised security assessment purposes only. Findings are based on
  static analysis and signature matching; they do not constitute a guarantee of the absence of
  additional vulnerabilities. Classification: <strong>CONFIDENTIAL</strong> — distribute only to
  personnel with a need to know.
</div>

</body>
</html>"""

# ── Render HTML → PDF via WeasyPrint ─────────────────────────────────────────
import weasyprint  # imported late so scan logs print before the ~1s import cost

pdf_bytes = weasyprint.HTML(string=html).write_pdf()

# Write to stdout
sys.stdout.buffer.write(pdf_bytes)
sys.stdout.buffer.flush()

# Save to --out path
OUT_PATH.write_bytes(pdf_bytes)

# Save to /mnt/artifacts
ARTIFACTS_DIR = Path(os.environ.get("DOMINO_ARTIFACTS_DIR", "/mnt/artifacts"))
artifacts_path = ARTIFACTS_DIR / OUT_PATH.name
try:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    artifacts_path.write_bytes(pdf_bytes)
    artifacts_saved = str(artifacts_path)
except OSError as e:
    artifacts_saved = f"(skipped: {e})"

print(flush=True, file=sys.stderr)
log(f"Report written → {OUT_PATH.resolve()}", GREEN)
log(f"Artifacts copy  → {artifacts_saved}", GREEN)
print(flush=True, file=sys.stderr)
