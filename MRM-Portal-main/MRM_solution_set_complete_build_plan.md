# MRM Solution Set — Complete Build Plan (First Draft for Sales)

**Owner**: Nick Goble, Solutions Architect  
**Audience**: Sales team, SEs, AEs — and Claude Code agent teams for build execution  
**Last updated**: April 1, 2026  

---

## EXECUTIVE SUMMARY

This document defines everything Domino needs to ship as a productized MRM solution for financial services. It is organized around the **8-scene demo storyboard** that walks a prospect through the full Model Risk Management lifecycle on Domino. Each scene maps to concrete deliverables across four categories:

| Delivery Type | Description | Who Builds |
|---|---|---|
| **YAML Policies** | Governance policy templates importable into Domino's Policy Builder | Nick Goble / Ian McKenna |
| **Extension/App** | Custom Domino Apps (Flask/React) that extend the platform UI | Claude Code agent teams / hackathon |
| **Scripted Checks** | Python check libraries importable into governance bundles | Nick Goble / FDEs |
| **Product Requests** | Gaps requiring Domino engineering changes | Product team (tracked) |

**Coverage summary**: Domino covers ~65% of MRM requirements out of the box. The remaining ~35% breaks down as: YAML policy content (40% of the gap), Extension/App builds (35%), scripted check libraries (15%), and product requests (10%).

---

## PART 0: THE ARCHITECTURE — HOW IT ALL FITS TOGETHER

### The Domino MRM Stack

```
┌─────────────────────────────────────────────────────────────────┐
│                    MRM SOLUTION SET                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  LAYER 5: Governance Control Tower (Extension)                         │
│    → Model Inventory view (Scene 1)                             │
│    → Model Card view (Scene 2)                                  │
│    → MRM Monitoring Dashboard (Scene 6)                         │
│    → Compliance & Audit Readiness (Scene 8)                     │
│    → Findings Dashboard (cross-cutting)                         │
│                                                                 │
│  LAYER 4: Auto-Documentation (Scene 7)                          │
│    → Domino's Auto-Doc Extension (exists, needs enhancement)    │
│    → Template library for MDD, VR, MR                           │
│                                                                 │
│  LAYER 3: Scripted Check Libraries (Scene 4)                    │
│    → Bias/Fairness suite                                        │
│    → Risk Assessment suite                                      │
│    → Documentation-vs-Code consistency suite                    │
│    → LLM Evaluation suite                                       │
│                                                                 │
│  LAYER 2: YAML Governance Policies (Scene 3)                    │
│    → 13 lifecycle stage policies                                │
│    → Sector overlays (banking, insurance, CRA, UK, EU, Canada)  │
│    → Regulatory cross-walk mappings                             │
│                                                                 │
│  LAYER 1: Domino Platform (exists)                              │
│    → Model Registry (MLflow-based)                              │
│    → Governance Engine (bundles, policies, findings, approvals) │
│    → Model Monitoring (drift, data quality, alerts)             │
│    → Workspaces, Jobs, Flows, Endpoints                         │
│    → Audit Trail (CSV export, REST API)                         │
│    → RBAC, Gates, Gated Deployments                             │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### The Data Model (How Everything Connects)

Understanding this is critical for building any extension or app:

```
Model (MLflow Registry)
  ├── has metadata: name, description, tags, model card
  ├── has versions: v1, v2, v3...
  ├── deployed as: Endpoint(s)
  ├── monitored by: Model Monitoring
  └── governed by: Bundle
        ├── has policies: [Intake Policy, Dev Policy, Validation Policy, ...]
        │     └── each policy has: stages → sections → evidence (questions/answers)
        ├── has findings: [{title, severity, status, assignee, due_date}]
        ├── has approvals: [{approver, status, timestamp, stage}]
        ├── has attachments: [files, reports, artifacts]
        └── has classification: risk_tier (Critical/High/Medium/Low)
```

### Key Domino API Endpoints (for Extension/App Builds)

All extensions use `localhost:8899` as the API proxy inside containers. Auth is auto-injected.

| API | Endpoint Pattern | Purpose |
|---|---|---|
| **Models** | `GET /api/modelregistry/v2/models` | List all registered models |
| **Model Versions** | `GET /api/modelregistry/v2/models/{id}/versions` | Get model version history |
| **Bundles** | `GET /api/governance/v1/bundles` | List all governance bundles |
| **Bundle Detail** | `GET /api/governance/v1/bundles/{id}` | Get bundle with policies, evidence, findings |
| **Bundle Findings** | `GET /api/governance/v1/bundles/{id}/findings` | Get findings for a bundle |
| **Bundle Approvals** | `GET /api/governance/v1/bundles/{id}/approvals` | Get approval history |
| **Bundle Report** | `GET /api/governance/v1/bundles/{id}/report` | Download PDF report |
| **Results (Evidence)** | `GET /api/governance/v1/results` | Get evidence answers (THE KEY ENDPOINT) |
| **Latest Results** | `GET /api/governance/v1/results/latest` | Get most recent evidence for a bundle |
| **Compute Policy** | `POST /api/governance/v1/rpc/compute-policy` | Get computed policy state for a bundle |
| **Policies** | `GET /api/governance/v1/policy-overviews` | List all published policies |
| **Policy Definition** | `GET /api/governance/v1/policies/{id}/definition` | Get policy YAML |
| **Create Finding** | `POST /api/governance/v1/findings` | Create a new finding |
| **Audit Trail** | `GET /auditevents` | Query audit events |
| **Model Monitoring** | `GET /api/modelmonitor/v1/...` | Drift, data quality metrics |
| **Projects** | `GET /v4/projects` | List all projects |
| **Jobs** | `POST /v4/jobs/start` | Start a job (for compute) |
| **Users** | `GET /v4/users` | List users (for owner lookups) |
| **Endpoints** | `GET /v4/modelProducts` | List deployed model endpoints |

> **Note**: Exact endpoint paths may vary by Domino version. Always verify against the target instance's API docs at `/api/index.html`. The governance API is the newest and may have undocumented endpoints. Use browser dev tools on the Domino UI to discover real API calls.

### The Evidence Search Pattern (Critical Architecture)

MRM metadata like "model ID", "risk tier", "model owner" is NOT stored as fields on the model object. It lives as **evidence answers** inside governance policies applied to bundles. To retrieve this data, you search evidence.

The flow:
```
Model → find its Bundle (by registeredModelId) → get Results for that Bundle → 
search through artifact labels for keywords matching what you need
```

The `EvidenceSearcher` class (see `mrm-dashboard/CLAUDE.md`) implements fuzzy search using a keyword alias table:

```python
EVIDENCE_ALIASES = {
    "model_id": ["model id", "model identifier", "institution id"],
    "model_owner": ["model owner", "owner", "responsible party"],
    "risk_tier": ["risk tier", "risk classification", "risk level"],
    # ...
}
```

When the app needs "model owner" for a bundle, it calls `searcher.find_evidence_value(bundle_id, "model_owner")`, which:
1. Gets all results for the bundle via `GET /api/governance/v1/results?bundleId={id}`
2. Iterates through every artifact (question-answer pair)
3. Checks if the artifact's label contains any of the keywords for "model_owner"
4. Returns the first matching value

This approach works regardless of how the customer has worded their policy questions, because it searches by keyword rather than expecting exact field names. The alias table is configurable per customer deployment.

The full `DominoClient` and `EvidenceSearcher` implementations are in `mrm-dashboard/CLAUDE.md`.

---

## PART 1: SCENE-BY-SCENE BUILD SPECIFICATIONS

---

### SCENE 1: MODEL INVENTORY (The Single Pane of Glass)

**Talk track**: "Every model your institution owns or uses is in one place. Internal models, vendor models, Excel models. Each one has a risk tier that drives every downstream governance decision."

**This is the single most important screen in the demo.**

#### What Exists Today
- Model Registry with name, version history, model card, tags
- Bundle overview page showing governed bundles with compliance status
- Global models list (publicly discoverable models across projects)

#### What Must Be Built

| Req ID | Requirement | Build Type | Priority | Effort |
|---|---|---|---|---|
| 1.1 | Model ID field (e.g., ACME-001) | YAML Policy + App | P0 | 2 days |
| 1.2 | Risk tier badge (Critical/High/Medium/Low) | YAML Policy + App | P0 | 2 days |
| 1.3 | Health status indicator (traffic light) | App (DMM integration) | P0 | 3 days |
| 1.4 | Lifecycle stage column | Derived from MDLC policies | P0 | 3 days |
| 1.5 | Model owner field | YAML Policy evidence | P1 | 1 day |
| 1.6 | Last validation date / next review date | Derived from bundle timestamps | P1 | 2 days |
| 1.7 | Single-pane view of ALL model types | App (query all sources) | P0 | 3 days |

#### Solution Architecture for Scene 1

**The Governance Control Tower** is a Domino App (Flask backend + React/HTML frontend) that:

1. Queries the Model Registry API for all registered models
2. Queries the Governance API for all bundles attached to those models
3. For each model, extracts from its bundle's evidence:
   - **Model ID**: From evidence field in the Intake policy (e.g., question: "What is the institution's model ID?")
   - **Risk Tier**: From the bundle's classification (Domino's native risk classification feature)
   - **Lifecycle Stage**: Derived from which MDLC policy stages have completed approvals. If Intake is approved but Development is in-progress → stage = "Development"
   - **Owner**: From evidence field or from the model's creator metadata
   - **Last Validation Date**: Timestamp of the most recent approval on the Validation policy stage
   - **Next Review Date**: Last validation date + revalidation frequency (collected as evidence or defaulted by risk tier: Critical=6mo, High=12mo, Medium=18mo, Low=36mo)
4. Queries Model Monitoring API for health status per model
5. Renders a filterable, sortable table with:
   - Color-coded risk tier badges
   - Traffic-light health indicators (green/amber/red)
   - Lifecycle stage pills
   - Filter sidebar: by risk tier, lifecycle stage, owner, health status, model type
   - Search bar
   - CSV export button
   - Click-through to Model Card (Scene 2)

#### YAML Policy: Model Intake & Registration

This policy is applied when a model first enters the MRM framework. It collects the metadata that populates the inventory.

```yaml
# model-intake-registration.yaml (SKELETON — full version to be authored)
name: "Model Intake & Registration"
description: "Captures model metadata at initial registration for MRM inventory"
version: "1.0"

classification:
  alias: "risk_classification"
  rules:
    - if: "risk_classification == 'Critical'"
      then: "critical"
    - if: "risk_classification == 'High'"
      then: "high"
    - if: "risk_classification == 'Medium'"
      then: "medium"
    - if: "risk_classification == 'Low'"
      then: "low"

stages:
  - name: "Model Registration"
    sections:
      - name: "Model Identification"
        evidence:
          - type: textinput
            label: "Institution Model ID"
            placeholder: "e.g., ACME-CRS-001"
            helpText: "Unique identifier per your institution's naming convention"
            required: true
          - type: textinput
            label: "Model Name"
            required: true
          - type: radio
            label: "Model Type"
            options:
              - label: "Internal (Python/R/SAS)"
                value: "internal"
              - label: "Vendor/Third-Party"
                value: "vendor"
              - label: "Excel/EUC"
                value: "excel"
              - label: "GenAI/LLM"
                value: "genai"
              - label: "Agent/Agentic System"
                value: "agent"
          - type: textinput
            label: "Model Owner"
            required: true
          - type: textinput
            label: "Business Sponsor"
          - type: textarea
            label: "Model Purpose / Use Case Description"
            required: true
          - type: radio
            label: "Business Line"
            options:
              - label: "Credit Risk"
                value: "credit_risk"
              - label: "Market Risk"
                value: "market_risk"
              - label: "Operational Risk"
                value: "operational_risk"
              - label: "Compliance/AML"
                value: "compliance"
              - label: "Insurance Pricing/Underwriting"
                value: "insurance_pricing"
              - label: "Claims"
                value: "claims"
              - label: "Reserving"
                value: "reserving"
              - label: "Other"
                value: "other"
      - name: "Risk Classification"
        evidence:
          - type: radio
            label: "Financial Materiality"
            alias: "materiality"
            options:
              - label: "High (>$100M impact)"
                value: "high"
              - label: "Medium ($10M-$100M)"
                value: "medium"
              - label: "Low (<$10M)"
                value: "low"
          - type: radio
            label: "Model Complexity"
            alias: "complexity"
            options:
              - label: "High (deep learning, ensemble, LLM)"
                value: "high"
              - label: "Medium (random forest, gradient boosting)"
                value: "medium"
              - label: "Low (linear regression, scorecard, rules)"
                value: "low"
          - type: radio
            label: "Regulatory Exposure"
            alias: "regulatory_exposure"
            options:
              - label: "High (CCAR/DFAST, fair lending, pricing)"
                value: "high"
              - label: "Medium (internal risk, reserving)"
                value: "medium"
              - label: "Low (informational, internal analytics)"
                value: "low"
          - type: radio
            label: "Overall Risk Tier"
            alias: "risk_classification"
            options:
              - label: "Critical"
                value: "Critical"
              - label: "High"
                value: "High"
              - label: "Medium"
                value: "Medium"
              - label: "Low"
                value: "Low"
    approvals:
      - name: "MRM Function Acknowledgment"
        roles: ["GovernanceAdmin"]
        required: true
```

#### Customer Requirements Mapped

| Customer | Their Requirement | How Scene 1 Addresses It |
|---|---|---|
| **Fitch** | "Comprehensive model inventory" (Row 1 of AI Gov CSV) | The Governance Control Tower shows all models with governance metadata |
| **Hanover** | Q3.14: "Comprehensive model inventory with sign-off tracking" | Bundle approval history visible per model |
| **USAA** | Model lifecycle management across 750 users | Lifecycle stage derived from MDLC policies visible in inventory |
| **Freddie Mac** | "Centralized control plane" for SageMaker + Domino models | Req 1.7 single-pane view of all model types including external |
| **FRBNY** | Deepest governance adopter — inventory is table stakes | Full inventory with regulatory cross-walk |

---

### SCENE 2: MODEL CARD (Drill into a Model)

**Talk track**: "This model was registered six months ago. It went through development, was independently validated, approved by the risk committee, and is now in production monitoring. Every piece of evidence is attached. Every approval is logged."

#### What Exists Today
- Model card with metadata, lineage, code/data/environment tracking
- Governance bundle page with evidence notebooks, findings, approvals
- Model monitoring metrics

#### What Must Be Built

| Req ID | Requirement | Build Type | Priority | Effort |
|---|---|---|---|---|
| 2.1 | MDLC stage progress visualization | App (React stepper) | P0 | 3 days |
| 2.2 | Evidence, policies, findings, approvals in one view | App | P0 | 5 days |
| 2.3 | Model use case and business context fields | YAML Policy | P1 | 1 day |

#### Solution Architecture for Scene 2

Clicking a model row in Scene 1 opens the **Model Detail View** within the Governance Control Tower. This view assembles data from multiple APIs:

**Header**: Model name, ID, risk tier badge, health indicator, current lifecycle stage

**Lifecycle Stepper** (horizontal progress bar):
```
[Intake ✓] → [Development ✓] → [Validation ●] → [Approval ○] → [Deployment ○] → [Monitoring ○] → [Retirement ○]
```
- Each stage is derived from the corresponding MDLC policy's approval status
- Green check = all approvals complete for that stage
- Filled circle = in progress (evidence being collected)
- Empty circle = not started
- Clicking a stage expands to show: evidence collected, findings at that stage, approvals, timestamps

**Tabs below the stepper**:

1. **Overview**: Model purpose, owner, business line, risk rationale, upstream/downstream dependencies (all from Intake policy evidence)
2. **Evidence**: All evidence across all policy stages, organized by policy. Each evidence item shows: question, answer, attachments, timestamp, who submitted
3. **Findings**: All findings on this model's bundle. Filterable by severity, status. Shows: title, severity, status, assignee, created date, due date
4. **Approvals**: Timeline of all approvals across all policies. Shows: approver name, role, action (approved/rejected/conditional), timestamp, comments
5. **Monitoring**: Embedded model monitoring charts (drift over time, data quality metrics, prediction volume). Links to full DMM dashboard
6. **Audit Trail**: All audit events for this model filtered from the global audit trail

**Deep links**: Every view should link back to the native Domino UI where applicable (e.g., clicking "Evidence Notebook" opens the actual governance bundle page, clicking "Monitoring" opens the DMM dashboard for that model).

---

### SCENE 3: GOVERNANCE POLICY ENGINE (Already Strong — Content Needed)

**Talk track**: "These are your governance workflows codified as policy. Your development team fills out the Model Development Document. Your validation team fills out the Validation Report. The fields, the routing, the approvals are all configured once and applied consistently."

#### What Exists Today
- Full Policy Builder (visual drag-and-drop, YAML, code editor)
- Off-the-shelf templates (EU AI Act, NIST AI RMF, SR 11-7 referenced)
- Multiple policies per bundle
- Sequential workflows, conditional logic, classification
- Gates and gated deployments
- Policy versioning

**This is Domino's strongest MRM asset. No product changes needed. Only content.**

#### What Must Be Built: 13 Lifecycle Policies + Sector Overlays

| Policy | Regulatory Basis | Priority | Effort |
|---|---|---|---|
| 1. Model Intake & Registration | SR 11-7 (inventory), SS1/23 §4 | P0 | 3 days |
| 2. Risk Tiering & Classification | SR 11-7 (risk-based), OSFI E-23 §3 | P0 | 3 days |
| 3. Model Development Standards | SR 11-7 (documentation), SS1/23 §5 | P0 | 5 days |
| 4. Initial Validation | SR 11-7 (effective challenge), SS1/23 §6 | P0 | 5 days |
| 5. Model Approval & Certification | SR 11-7 (governance), SS1/23 §7 | P0 | 3 days |
| 6. Deployment & Promotion Gates | SR 11-7 (implementation) | P1 | 3 days |
| 7. Ongoing Monitoring | SR 11-7 (ongoing validation), SS1/23 §8 | P0 | 3 days |
| 8. Periodic Review / Recertification | SR 11-7 (annual review) | P1 | 2 days |
| 9. Model Change Management | SS1/23 §9, OSFI E-23 §5 | P1 | 3 days |
| 10. Model Retirement | SR 11-7 (recently retired), SS1/23 §10 | P2 | 2 days |
| 11. Third-Party/Vendor Model Governance | SR 11-7 (third-party), OSFI E-23 §6 | P1 | 3 days |
| 12. Shadow AI Detection & Response | IBM 2025 research, EU AI Act Art. 26 | P2 | 2 days |
| 13. Challenger Model Requirements | SR 11-7 (outcomes analysis) | P2 | 2 days |

**Sector overlay policies** (applied in addition to base policies):

| Overlay | Target Sector | Key Additions | Priority | Effort |
|---|---|---|---|---|
| NAIC/Insurance | Insurance | Protected variable analysis, DI testing, ASOP 56 fields | P0 | 3 days |
| UK/PRA SS1/23 | UK Banking | PMA governance, named senior manager accountability | P1 | 2 days |
| EU AI Act | All (EU) | Annex IV technical documentation, conformity assessment | P0 | 3 days |
| OSFI E-23 | Canadian Banking | AI-specific risk ratings, usage limits, explainability | P1 | 2 days |
| CRA/SEC | Credit Rating Agencies | SEC examination requirements, methodology governance | P1 | 2 days |
| US Treasury FS AI RMF | US Banking | 230 control objectives mapping | P0 | 5 days |
| FHFA AB 2022-02 | GSEs (Fannie/Freddie) | Fair lending integration, explainability imperative | P1 | 2 days |
| NY DFS CL 7 | Insurance (NY) | Six statistical tests for disparate impact | P1 | 2 days |

**Delivery format**: Each policy is a `.yaml` file importable via Domino's Policy Builder or uploadable via the Governance API. Shipped as an email attachment or downloadable from a GitHub repo until Domino has a policy marketplace.

---

### SCENE 4: VALIDATION WORKFLOW (The #1 Bottleneck)

**Talk track**: "Your validator doesn't rebuild anything. They open the developer's exact environment, versioned and reproducible, and run their tests. Findings are classified by severity and route automatically."

#### What Exists Today
- Workspace duplication (validator reproduces developer environment)
- Scripted checks on governed bundles (automated pre-flight checks)
- Governance Findings with severity, assignee, due date
- Conditional approval (findings block advancement)
- Gated Deployments

#### What Must Be Built

| Req ID | Requirement | Build Type | Priority | Effort |
|---|---|---|---|---|
| 4.1 | Central findings view across all models | App | P0 | 3 days |
| 4.2 | Custom finding fields (custom severity, metadata) | Product Request | P1 | — |
| 4.3 | Pre-built validation test suites | Scripted Checks | P0 | 10 days |
| 4.4 | LLM evaluation tools | Scripted Checks | P1 | 5 days |

#### Scripted Check Library Architecture (Req 4.3 — THE DIFFERENTIATOR)

This is the single most important technical deliverable. ValidMind ships 200+ checks. Domino needs a comparable library. Each check is a Python function that can be imported into a governance bundle as a scripted check.

**Suite 1: Bias & Fairness**
```python
# bias_fairness_checks.py
# Importable as scripted checks in governance bundles

def disparate_impact_ratio(predictions, protected_attribute, threshold=0.8):
    """SR 11-7 / ECOA / NY DFS CL 7 compliance check.
    Returns PASS if DIR >= threshold for all protected groups."""
    # Implementation: P(favorable|minority) / P(favorable|majority)
    pass

def psi_check(baseline_distribution, current_distribution, threshold=0.25):
    """Population Stability Index — model stability check.
    Green < 0.10, Amber 0.10-0.25, Red > 0.25"""
    pass

def csi_check(baseline_feature, current_feature, threshold=0.25):
    """Characteristic Stability Index — feature drift check."""
    pass

def equalized_odds_check(predictions, actuals, protected_attribute):
    """Check equal TPR/FPR across protected groups."""
    pass

def demographic_parity_check(predictions, protected_attribute, threshold=0.1):
    """Check prediction rate equality across groups."""
    pass

def adverse_impact_ratio(outcomes, protected_attribute):
    """4/5ths rule check per EEOC guidelines."""
    pass
```

**Suite 2: Risk Assessment / Model Performance**
```python
# risk_assessment_checks.py

def gini_coefficient(actuals, predictions, threshold=0.3):
    """Discriminatory power check. Banking standard: Gini > 0.3 for credit models."""
    pass

def ks_statistic(actuals, predictions, threshold=0.2):
    """Kolmogorov-Smirnov test for discrimination between classes."""
    pass

def auc_roc_check(actuals, predictions, threshold=0.7):
    """Area Under ROC Curve check."""
    pass

def hosmer_lemeshow_test(actuals, predictions, groups=10, alpha=0.05):
    """Calibration test for logistic regression models."""
    pass

def var_backtesting(var_predictions, actual_pnl, confidence=0.99):
    """Basel traffic-light backtesting for VaR models.
    Green: 0-4 exceptions, Yellow: 5-9, Red: 10+"""
    pass

def accuracy_degradation_check(baseline_accuracy, current_accuracy, threshold=0.05):
    """Check if model accuracy has degraded beyond threshold."""
    pass
```

**Suite 3: Documentation-vs-Code Consistency (UNIQUE DIFFERENTIATOR)**
```python
# doc_code_consistency_checks.py
# NO COMPETITOR SHIPS THIS

def mdd_code_feature_mismatch(mdd_text, code_features):
    """Compares features listed in Model Development Document against
    features actually used in training code. Flags mismatches.
    Uses NLP/LLM to extract feature names from MDD text."""
    pass

def mdd_code_algorithm_mismatch(mdd_text, code_algorithm):
    """Checks if algorithm described in MDD matches what's in the code.
    E.g., MDD says 'logistic regression' but code uses XGBoost."""
    pass

def parameter_documentation_check(mdd_text, model_params):
    """Verifies all model parameters are documented."""
    pass

def data_source_documentation_check(mdd_text, actual_data_sources):
    """Checks if data sources in MDD match actual training data."""
    pass
```

**Suite 4: LLM Evaluation (GenAI/Agentic)**
```python
# llm_evaluation_checks.py

def hallucination_detection(responses, ground_truth, threshold=0.1):
    """Check factual accuracy of LLM outputs against known ground truth."""
    pass

def toxicity_check(responses, threshold=0.05):
    """Screen LLM outputs for toxic content."""
    pass

def faithfulness_check(responses, source_context):
    """Check if RAG responses are faithful to source documents."""
    pass

def output_consistency_check(prompt, n_runs=10, similarity_threshold=0.8):
    """Statistical test for output non-determinism.
    Run same prompt n times, measure output variance."""
    pass

def prompt_injection_resistance(model_endpoint, injection_prompts):
    """Test model's resistance to common prompt injection attacks."""
    pass
```

#### Findings Dashboard (Req 4.1)

A view in the Governance Control Tower that queries `GET /api/governance/v1/findings` across all bundles and renders:

- **Summary cards**: Total findings, by severity (Critical/High/Medium/Low), overdue findings count
- **Filterable table**: Model name, finding title, severity, status (Open/In Progress/Resolved/Won't Fix), owner, created date, due date, age in days
- **Export to CSV** button
- Click-through to finding detail in native Domino UI

---

### SCENE 5: REGISTER AN EXCEL MODEL

**Talk track**: "This is the model your regulator is most worried about. It lives in a spreadsheet on someone's laptop. No version control. No audit trail. No governance. With one click, it's in your inventory, versioned, and governed."

#### What Exists Today
- Excel add-in (Nick Goble built it) with:
  - Domino API connection from Excel
  - Model registration via MLflow
  - Workbook versioning as artifact upload
  - Excel-DNA UDFs calling Domino model endpoints
  - Base64-encoded job execution pattern for registration

#### What Must Be Built

| Req ID | Requirement | Build Type | Priority | Effort |
|---|---|---|---|---|
| 5.1 | Registration creates enhanced inventory entry | Excel Add-in enhancement | P1 | 2 days |
| 5.2 | Version control and audit trail | Already built | Done | — |
| 5.3 | Governed compute via UDF endpoints | Already built | Done | — |

**Key gap**: OAuth handling needs to be production-ready. The add-in currently uses a workaround for authentication that won't scale to customer deployments.

**Demo flow**: Open Excel → Show Domino add-in ribbon → Select workbook → Click "Register in Domino" → Model appears in Scene 1 inventory with model ID, risk tier, owner, type="Excel/EUC" → Workbook is versioned, governed by same policies as Python/R models.

---

### SCENE 6: MRM MONITORING DASHBOARD

**Talk track**: "You have 200 models in production. Three of them are drifting. One is past its annual review date. The dashboard shows you exactly what needs attention."

#### What Exists Today
- Domino Model Monitoring with drift detection, data quality checks
- Traffic-light alerting and email alerts
- Monitoring checks integrated with governance (findings auto-created)

#### What Must Be Built

| Req ID | Requirement | Build Type | Priority | Effort |
|---|---|---|---|---|
| 6.1 | MRM portfolio dashboard | App | P0 | 5 days |
| 6.2 | Segment-level monitoring with custom thresholds | Product Request | P1 | — |
| 6.3 | Automated revalidation trigger | App + Policy | P1 | 3 days |

#### Portfolio Dashboard Architecture (Req 6.1)

A view in the Governance Control Tower showing:

**Summary row** (card-style):
- Total models: 247
- Healthy: 198 (green)
- Approaching threshold: 32 (amber)
- Threshold breached: 11 (red)
- Overdue for review: 6

**Charts**:
- Models by risk tier (donut chart)
- Models by lifecycle stage (horizontal bar)
- Health distribution over time (stacked area)
- Upcoming reviews calendar (next 30/60/90 days)

**Alert feed** (real-time):
- "Model CRS-042 drift score exceeded 2.3% threshold at 14:32 UTC"
- "Model FRD-015 annual review overdue by 12 days"
- "Finding F-089 auto-created: Data quality degradation on PRC-007"

**Automated revalidation** (Req 6.3): When Domino Model Monitoring triggers an alert that breaches configured thresholds, the monitoring integration automatically:
1. Creates a finding on the model's governance bundle via `POST /api/governance/v1/findings`
2. Sets severity based on breach magnitude
3. Assigns to model owner
4. Logs the event in the audit trail

This already ships with Winter 2026 release — "Governance now responds automatically to Domino Model Monitoring alerts."

---

### SCENE 7: AUTO-GENERATED DOCUMENTATION

**Talk track**: "Your validators spend more time writing reports than actually validating. The auto-documentation agent pulls the model's full context, drafts the report, and gives your validator a starting point, not a blank page."

#### What Exists Today
- Auto-documentation Extension (GA, uses LLM to generate documents from model context)
- AI on governed bundles
- Evidence notebooks

#### What Must Be Built

| Req ID | Requirement | Build Type | Priority | Effort |
|---|---|---|---|---|
| 7.1 | Auto-doc sees FULL context (model + bundle + findings + policies) | Extension enhancement | P0 | 5 days |
| 7.2 | Policy generation from regulatory PDF | App/Launcher | P1 | 5 days |
| 7.3 | Agent-based doc generation from model + template | Extension enhancement | P0 | 5 days |
| 7.4 | Embed model results and charts in documents | Extension enhancement | P1 | 3 days |

#### Template Library (to ship alongside auto-doc)

| Template | Sections | Regulatory Mapping |
|---|---|---|
| **Model Development Document (MDD)** | Problem statement, data sourcing, EDA, feature engineering, model selection rationale, champion/challenger, parameter estimation, testing results, performance by segment, assumptions register, limitations, implementation specs, monitoring plan | SR 11-7 recreatability standard |
| **Validation Report** | Executive summary (Pass/Conditional Pass/Fail), scope, model description, conceptual soundness, data quality, performance analysis, implementation review, findings, sign-off | SR 11-7 effective challenge |
| **Monitoring Report** | Performance drift metrics, data quality assessment, threshold breaches, remediation actions, recommendations | SR 11-7 ongoing monitoring |
| **Model Card (FSI-extended)** | ID, owner, purpose, risk tier, performance metrics disaggregated by demographics, fairness metrics, training data description, limitations, regulatory applicability, validation status, monitoring thresholds | Google Model Cards + FSI extensions |
| **Board/Committee Report** | Total models by status, risk tier distribution, validation compliance rates, open findings by severity, aggregate risk assessment, KRI trends | Board reporting best practice |
| **Incident Management Report** | Incident ID, affected model, description, root cause, impact assessment, containment actions, remediation plan, lessons learned | Operational risk standards |

---

### SCENE 8: COMPLIANCE AND AUDIT READINESS

**Talk track**: "When your regulator walks in, you don't spend three weeks assembling binders. You pull a compliance report. Every model, every approval, every finding, every piece of evidence is here."

#### What Exists Today
- Unified Audit Trail with CSV export
- REST API for audit events
- Evidence notebooks exportable as governance reports

#### What Must Be Built

| Req ID | Requirement | Build Type | Priority | Effort |
|---|---|---|---|---|
| 8.1 | Quarterly risk report PDF (one-click) | App/Scheduled Job | P0 | 5 days |
| 8.2 | Regulatory-to-lifecycle mapping views | App | P0 | 5 days |
| 8.3 | Role-based dashboard views | App | P1 | 3 days |

#### Regulatory Cross-Walk Matrix (Req 8.2)

This is the view QBE specifically requested. It's a matrix where:
- **Rows** = Regulatory requirements (e.g., SR 11-7 Section III.A, SS1/23 §5.1, EU AI Act Art. 9)
- **Columns** = Lifecycle stages (Intake → Retirement)
- **Cells** = Compliance status:
  - 🟢 Green = Evidence present and approved
  - 🟡 Amber = Evidence submitted but not yet approved
  - 🔴 Red = No evidence collected
  - Link to specific evidence item

Implementation: Hardcoded mapping of regulatory section → policy stage → evidence question ID. When the user selects a regulation (e.g., "SR 11-7"), the app queries all bundles for a given model, checks which evidence questions are answered, and renders the matrix.

#### Quarterly Risk Report PDF (Req 8.1)

A scheduled Domino job or on-demand App function that generates a PDF containing:
1. Executive summary: Total models, distribution by risk tier, overall compliance rate
2. Model inventory summary table
3. Validation compliance: % on schedule, overdue models, overdue by severity
4. Open findings summary: by severity, by age, by model
5. New registrations and retirements this quarter
6. Monitoring alerts summary
7. Appendix: Full model list with governance status

Uses Python (reportlab or weasyprint) to generate the PDF from API data.

---

## PART 2: THE MRM DASHBOARD APP — UNIFIED BUILD SPEC

The scenes above converge on a single **Governance Control Tower** — a Domino App that serves as the primary MRM user interface. This is the "Wormhole" from the hackathon context, productized for sales.

### Technology Stack

| Component | Choice | Rationale |
|---|---|---|
| **Backend** | Flask (Python) | Standard Domino App pattern, team familiarity |
| **Frontend** | React (via CDN, no build step) or HTML/HTMX | Fast iteration, no webpack required |
| **Graph Visualization** | Cytoscape.js or D3.js | For lineage/dependency graphs |
| **Charts** | ECharts or Chart.js | For monitoring dashboards |
| **PDF Generation** | ReportLab or WeasyPrint | For quarterly reports |
| **API Client** | Python `requests` via localhost:8899 | Auto-injected auth |

### Views (Routes)

| Route | Scene | Description |
|---|---|---|
| `/` | Scene 1 | Model Inventory — the home screen |
| `/model/<id>` | Scene 2 | Model Card / Detail View |
| `/findings` | Scene 4 | Findings Dashboard (cross-model) |
| `/monitoring` | Scene 6 | MRM Monitoring Portfolio Dashboard |
| `/compliance` | Scene 8 | Compliance & Audit Dashboard |
| `/compliance/crosswalk/<regulation>` | Scene 8 | Regulatory cross-walk matrix |
| `/reports/quarterly` | Scene 8 | Generate quarterly risk report |
| `/api/*` | Internal | Backend API routes (proxy to Domino API) |

### Data Layer

The app needs a caching layer to avoid hitting the Domino API on every page load. Options:

1. **In-memory cache** (simplest): Python dict refreshed every 60 seconds via background thread. Good enough for demo and small deployments.
2. **SQLite** (persistent): Write API results to a local SQLite DB. Enables historical trending. Better for production.
3. **Redis** (scalable): If the app needs to serve many concurrent users. Overkill for most MRM teams.

**Recommendation for first draft**: In-memory cache with 60-second TTL. Simple, demo-able, no external dependencies.

### Agent Team Assignments (Claude Code)

**Team 1: Foundation (4 hours)**
- Agent 1A: Build `DominoClient` — Python class wrapping all API calls above. Handles auth, pagination, error handling, response parsing. Every other agent imports this.
- Agent 1B: Build Flask app skeleton, base HTML template, routing, CORS, health check endpoint.

**Team 2: Model Inventory + Model Card (8 hours)**
- Agent 2A: Backend — `/api/models` endpoint that assembles inventory data from Model Registry + Governance + Monitoring APIs. Returns JSON.
- Agent 2B: Frontend — Inventory table view with filters, search, sorting, CSV export.
- Agent 2C: Backend — `/api/models/<id>` endpoint that assembles full model detail data.
- Agent 2D: Frontend — Model Card view with lifecycle stepper, tabbed detail view.

**Team 3: Findings + Monitoring (6 hours)**
- Agent 3A: Backend — `/api/findings` endpoint aggregating findings across all bundles.
- Agent 3B: Frontend — Findings dashboard with summary cards, filterable table.
- Agent 3C: Backend — `/api/monitoring` endpoint assembling portfolio health data.
- Agent 3D: Frontend — Monitoring dashboard with charts, alert feed, upcoming reviews.

**Team 4: Compliance + Reports (6 hours)**
- Agent 4A: Backend — Regulatory cross-walk data assembly (hardcoded mapping + live evidence status).
- Agent 4B: Frontend — Cross-walk matrix view, compliance dashboard.
- Agent 4C: Backend — Quarterly report PDF generation (reportlab).

**Dependency order**: Team 1 → Teams 2/3/4 in parallel.

---

## PART 3: COMPLETE REQUIREMENTS REGISTRY

Every requirement from the storyboard, tracked with status:

| Req ID | Scene | Requirement | Build Type | Status | Owner | Notes |
|---|---|---|---|---|---|---|
| 1.1 | 1 | Model ID field | YAML + App | TODO | Nick | Use Domino tags or evidence |
| 1.2 | 1 | Risk tier badge | YAML + App | TODO | Nick | Native classification feature |
| 1.3 | 1 | Health status indicator | App | TODO | Agent 2B | DMM API integration |
| 1.4 | 1 | Lifecycle stage column | Derived | TODO | Agent 2A | Derive from MDLC policy approvals |
| 1.5 | 1 | Model owner field | YAML | TODO | Nick | Evidence in Intake policy |
| 1.6 | 1 | Last validation/next review dates | Derived | TODO | Agent 2A | Bundle timestamps + frequency |
| 1.7 | 1 | Single-pane all model types | App | TODO | Agent 2A | Query all registry sources |
| 2.1 | 2 | MDLC stage progress visualization | App | TODO | Agent 2D | React stepper component |
| 2.2 | 2 | Evidence/policies/findings/approvals in one view | App | TODO | Agent 2D | Tabbed detail view |
| 2.3 | 2 | Model use case and business context | YAML | TODO | Nick | Evidence in Intake policy |
| 3.1 | 3 | Prebuilt MRM policy templates (3 core) | YAML | TODO | Nick/Ian | MDD, VR, MR templates |
| 3.2 | 3 | Sector-specific overlays | YAML | TODO | Nick | Insurance, UK, EU, Canada, etc. |
| 3.3 | 3 | LLM-generated policy from regulatory PDF | App | TODO | Nick | v1 prototype launcher |
| 4.1 | 4 | Central findings view | App | TODO | Agent 3A/3B | Cross-bundle findings query |
| 4.2 | 4 | Custom finding fields | Product Request | BLOCKED | Product | Won't-do irreversibility, custom fields |
| 4.3 | 4 | Pre-built validation test suites | Scripted Checks | IN PROGRESS | Nick | 4 suites defined above |
| 4.4 | 4 | LLM evaluation tools | Scripted Checks | TODO | Nick | Suite 4 above |
| 5.1 | 5 | Excel registration → enhanced inventory | Excel Add-in | DONE (partial) | Nick | OAuth gap remains |
| 5.2 | 5 | Version control for Excel workbooks | Excel Add-in | DONE | Nick | — |
| 5.3 | 5 | Governed compute via UDFs | Excel Add-in | DONE | Nick | — |
| 6.1 | 6 | MRM portfolio dashboard | App | TODO | Agent 3C/3D | Charts + alert feed |
| 6.2 | 6 | Segment-level monitoring thresholds | Product Request | BLOCKED | Product | DMM enhancement needed |
| 6.3 | 6 | Automated revalidation trigger | Platform | DONE (Winter 2026) | Product | Monitoring → Findings auto-creation |
| 7.1 | 7 | Auto-doc sees full context | Extension | TODO | Product/Nick | Extension enhancement |
| 7.2 | 7 | Policy from regulatory PDF | App | TODO | Nick | LLM-powered launcher |
| 7.3 | 7 | Agent-based doc from model + template | Extension | TODO | Product/Nick | Auto-doc extension enhancement |
| 7.4 | 7 | Embed charts in documents | Extension | TODO | Nick | Server-side chart generation |
| 8.1 | 8 | Quarterly risk report PDF | App/Job | TODO | Agent 4C | ReportLab generation |
| 8.2 | 8 | Regulatory cross-walk matrix | App | TODO | Agent 4A/4B | Hardcoded mapping + live status |
| 8.3 | 8 | Role-based dashboard views | App | TODO | Agent 4B | Pre-built query templates |

---

## PART 4: PRODUCT REQUESTS (BLOCKERS)

These require Domino engineering changes and cannot be solved with content or extensions:

| ID | Description | Impact | Accounts Requesting | Severity |
|---|---|---|---|---|
| PR-1 | **Finding field editability**: Custom severity levels, custom statuses, custom metadata fields on findings | Blocks institutions with non-standard finding taxonomies | Fitch, Hanover, QBE | HIGH |
| PR-2 | **Segment-level monitoring**: Monitor model performance by segment (region, product, demographic) with custom thresholds per segment | Blocks insurance pricing model governance | Early Warning, USAA, QBE | HIGH |
| PR-3 | **Model ID as first-class field**: Currently no way to enforce uniqueness on a model ID field in the registry | All MRM clients need institution-specific IDs | All accounts | MEDIUM |
| PR-4 | **Lifecycle stage as first-class field**: Currently must be derived from policy/bundle status | Would simplify inventory filtering | All accounts | MEDIUM |
| PR-5 | **Revalidation scheduling**: Recurring evidence collection tied to calendar dates | Currently no way to auto-trigger periodic recertification | Hanover (Q3.32), USAA | MEDIUM |

---

## PART 5: COMPETITIVE POSITIONING

### How This Solution Set Compares

| Capability | Domino + Solution Set | ValidMind | SAS MRM | IBM OpenPages |
|---|---|---|---|---|
| Model development embedded | ✅ Native | ❌ External | ⚠️ SAS-only | ❌ External |
| Policy engine | ✅ Visual builder + YAML | ⚠️ Pre-built only | ✅ Workflow templates | ✅ Deep GRC |
| Pre-built test library | 🔨 Building (target: 50+) | ✅ 200+ tests | ⚠️ SAS-native tests | ❌ None |
| Auto-documentation | ✅ LLM-powered | ✅ 70-80% automation | ⚠️ Templates only | ⚠️ Templates only |
| Model monitoring | ✅ DMM (drift, quality) | ❌ External | ✅ Integrated | ❌ External |
| Multi-language (Python/R/SAS/Excel) | ✅ All | ✅ Python/R | ⚠️ SAS-centric | ❌ Platform-agnostic |
| Regulatory coverage | 🔨 Building (10 frameworks) | ✅ 4 frameworks | ✅ Banking-deep | ✅ Broad GRC |
| Doc-vs-code consistency checks | 🔨 UNIQUE | ❌ | ❌ | ❌ |
| Agentic AI governance (ADLC) | ✅ Winter 2026 | ❌ | ❌ | ❌ |
| Pricing | Per-user subscription | Per-model ($$) | Enterprise ($$$$) | Enterprise ($$$$) |

### The Pitch

**For the Head of MRM**: "Domino is the only platform where your developers build models AND your validators govern them in the same environment. No handoffs, no version mismatches, no documentation lag. The MRM solution set ships pre-built policies mapped to SR 11-7, the EU AI Act, and 8 other frameworks. Your team configures once and enforces consistently. And when your regulator walks in, every piece of evidence is one click away."

**Against SAS**: "SAS is optimized for SAS models. Your team uses Python, R, and Excel. Domino governs all of them. And our agentic AI governance is two years ahead."

**Against ValidMind**: "ValidMind is documentation-only. It doesn't run your models, doesn't monitor them, doesn't provide compute. Domino is the full stack — development through retirement — with governance built in, not bolted on. And we're open to integrating ValidMind where their documentation automation adds value."

---

## PART 6: DELIVERY TIMELINE

### Phase 1: Sales-Ready MVP (4 weeks)

| Week | Deliverable |
|---|---|
| Week 1 | 3 core YAML policies (Intake, Development, Validation) authored and tested. Governance Control Tower skeleton with Model Inventory view (hardcoded demo data). |
| Week 2 | 3 more policies (Approval, Monitoring, Retirement). App connected to live Domino APIs. Model Card view functional. |
| Week 3 | Findings Dashboard. Monitoring Portfolio Dashboard. 2 scripted check suites (Bias/Fairness, Risk Assessment). |
| Week 4 | Compliance cross-walk matrix. Quarterly report PDF. Sector overlays (Insurance, EU AI Act). Demo rehearsal with sales. |

### Phase 2: Customer-Ready (8 weeks)

| Week | Deliverable |
|---|---|
| Week 5-6 | Remaining 7 policies. Remaining scripted check suites. Auto-doc template library. |
| Week 7-8 | Role-based views. LLM policy generator (v1 prototype). Customer pilot preparation. Documentation and sales enablement materials. |

### Phase 3: Scale (Ongoing)

- Expand scripted check library toward 200+ checks (parity with ValidMind)
- Customer feedback incorporation from USAA design partnership
- Additional sector overlays as customer demand dictates
- Product requests (PR-1 through PR-5) addressed in partnership with engineering

---

## APPENDIX A: CUSTOMER REQUIREMENT TRACEABILITY

| Customer | Key Requirement | Mapped To |
|---|---|---|
| **Fitch** | AI governance as platform's weakest area | Entire solution set addresses this |
| **Fitch** | Regulatory compliance, automatic tracking | Req 8.2 (cross-walk matrix), Req 3.2 (sector overlays) |
| **Fitch** | Automated risk scoring | Req 1.2 (risk tier), Req 4.3 (scripted checks) |
| **Hanover** | Q3.1: NIST, ASOP, NAIC, NY DFS compliance | Policies + sector overlays |
| **Hanover** | Q3.6: Explainability, bias detection | Req 4.3 bias/fairness suite |
| **Hanover** | Q3.9: Risk tiering and controls | Req 1.2 + classification in policies |
| **Hanover** | Q3.10: Monitoring (bias, drift, degradation) | Scene 6 + DMM |
| **Hanover** | Q3.14: Comprehensive model inventory with sign-off | Scene 1 + Scene 2 |
| **Hanover** | Q3.19: Compliance reports, audit-ready documentation | Scene 8 |
| **Hanover** | Q3.31: Automated stakeholder notifications for recertification | Existing (email + tasks) + PR-5 |
| **USAA** | Model lifecycle management at scale (750 users) | Scene 1 inventory + Scene 6 monitoring |
| **USAA** | Design partner for MRM capabilities | Solution set is the deliverable |
| **Freddie Mac** | Centralized control plane (SageMaker ingestion) | Req 1.7 single-pane view |
| **Freddie Mac** | LLM evaluation and monitoring | Req 4.4 + Agentic AI lifecycle (ADLC) |
| **FRBNY** | Deepest governance adopter | Validation through entire solution set |
| **MSRB** | Regulatory compliance, audit risk | Scene 8 compliance dashboard |
| **QBE** | SR 11-7 cross-walk | Req 8.2 regulatory cross-walk matrix |
| **Early Warning** | Segment-level monitoring | PR-2 (product request) |

---

## APPENDIX B: AUDITOR PERSONA (STRATEGIC GAP)

**The auditor persona has zero documented presence across all customer engagements.** This is a strategic positioning gap. Auditors (3rd line of defense) need:

1. **Read-only dashboard access**: View all models, findings, compliance status without ability to modify
2. **Pre-built audit queries**: "Show me all Critical models with overdue reviews," "Show me all findings open > 90 days," "Show me models registered without risk tier"
3. **Evidence packages**: One-click download of all evidence for a given model as a ZIP/PDF
4. **Regulatory sampling**: Random sample of models by risk tier for examination
5. **Trend analysis**: Year-over-year metrics (finding resolution times, validation compliance rates)

**Action item**: Scene 8 and Req 8.3 (role-based views) should explicitly include an Auditor view with pre-built queries and read-only access patterns.

---

## APPENDIX C: REGULATORY FRAMEWORK QUICK REFERENCE

| Framework | Jurisdiction | Effective | Key MRM Requirements |
|---|---|---|---|
| SR 11-7 / OCC 2011-12 | US Banking | 2011 | Inventory, validation (effective challenge), documentation (recreatability), board oversight |
| PRA SS1/23 | UK Banking | May 2024 | Model lifecycle, PMA governance, senior manager accountability |
| OSFI E-23 | Canada | May 2027 | Risk ratings drive limits, AI-specific requirements, explainability |
| EU AI Act | EU | Aug 2026 (high-risk) | Conformity assessment, technical documentation (Annex IV), risk management system |
| NAIC Model Bulletin | US Insurance (24+ states) | Rolling | AI/ML governance in insurance, fairness, transparency |
| NY DFS CL 7 | New York Insurance | 2024 | Six statistical tests for disparate impact |
| US Treasury FS AI RMF | US Financial Services | Feb 2026 | 230 control objectives across model lifecycle |
| NIST AI RMF | US (voluntary) | Jan 2023 | Map/Measure/Manage/Govern framework |
| ISO 42001 | International | Dec 2023 | AI Management System standard (Plan-Do-Check-Act) |
| FHFA AB 2022-02 | US GSEs | 2022 | Fair lending integration, explainability for mortgage models |

---

*This document is the master reference for the Domino MRM Solution Set build. All updates flow through this document. All customer-facing materials derive from it.*