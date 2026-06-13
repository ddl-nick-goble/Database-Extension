// Domino Databases — frontend logic.
//
// One file, no build step. Talks to the Flask backend at relative paths so
// the same code works under the Domino app proxy (`/<owner>/<proj>/app/.../`)
// and the workspace dev proxy (`/<owner>/<proj>/notebookSession/<run>/proxy/<port>/`).

const API = "./api";

// Project scoping — read from ?projectId= so the wizard can be opened
// from any project and create databases there instead of its own project.
const _urlParams = new URLSearchParams(location.search);
const _scopedProjectId = _urlParams.get("projectId") || "";

const state = {
    config: {},
    databases: [],
    summary: {},
    tiers: [],
    envsLoaded: false,
    wizard: {
        step: 1,
        engine: null,
        name: "",
        environmentId: "",
        hardwareTierId: "",
        password: "",
    },
};

// =====================================================================
// API helpers
// =====================================================================
async function api(path, opts = {}) {
    const sep = path.includes("?") ? "&" : "?";
    const url = _scopedProjectId
        ? `${API}${path}${sep}projectId=${encodeURIComponent(_scopedProjectId)}`
        : `${API}${path}`;
    const r = await fetch(url, {
        headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
        ...opts,
    });
    const ct = r.headers.get("Content-Type") || "";
    const body = ct.includes("application/json") ? await r.json() : await r.text();
    if (!r.ok) {
        // Surface as much detail as the backend gave us — status, path, Domino's response body.
        let msg = (body && body.error) || `${r.status} ${r.statusText}`;
        if (body && body.status)   msg += ` (Domino HTTP ${body.status})`;
        if (body && body.path)     msg += ` ${body.path}`;
        if (body && body.dominoBody) msg += `\n${body.dominoBody}`;
        if (body && body.detail)   msg += `\n${body.detail}`;
        const err = new Error(msg);
        err.body = body;
        throw err;
    }
    return body;
}

// =====================================================================
// Boot
// =====================================================================
async function boot() {
    bindUi();
    if (_scopedProjectId) {
        const badge = document.getElementById("project-scope-badge");
        if (badge) {
            badge.textContent = `Project: ${_scopedProjectId}`;
            badge.classList.remove("hidden");
        }
    }
    try {
        state.config = await api("/config");
        if (_scopedProjectId) {
            const badge = document.getElementById("project-scope-badge");
            if (badge && state.config.project) {
                badge.textContent = `Project: ${state.config.project}`;
            }
        }
        renderEngineDropdown();
        renderEngineCards();
    } catch (e) {
        console.error("config load failed", e);
    }
    await refreshDatabases();
    // Catalogs (envs / hw tiers) load lazily when the wizard opens.
}
document.addEventListener("DOMContentLoaded", boot);

// engines() returns the catalog the backend resolved from the registry.
// Empty array if the backend response was malformed — UI degrades to a
// generic database list without engine-specific affordances.
function enginesCatalog() {
    return Array.isArray(state.config.engines) ? state.config.engines : [];
}

function renderEngineDropdown() {
    const sel = document.getElementById("filter-engine");
    if (!sel) return;
    const head = `<option value="">All Engines</option>`;
    sel.innerHTML = head + enginesCatalog().map(e =>
        `<option value="${escapeHtml(e.name)}">${escapeHtml(e.label)}</option>`
    ).join("");
}


function renderEngineCards() {
    const grid = document.getElementById("engine-grid");
    if (!grid) return;
    grid.innerHTML = enginesCatalog().map(e => {
        const icon = e.iconUrl
            ? `<img class="engine-icon-img" src="${escapeHtml(e.iconUrl)}" alt="${escapeHtml(e.label)}">`
            : e.icon
                ? `<div class="engine-icon">${escapeHtml(e.icon)}</div>`
                : "";
        return `
            <div class="engine-card" data-engine="${escapeHtml(e.name)}">
                ${icon}
                <h3>${escapeHtml(e.label)}</h3>
                <p>${escapeHtml(e.description)}</p>
            </div>
        `;
    }).join("");
    // Re-bind click handlers on the freshly rendered cards.
    grid.querySelectorAll(".engine-card").forEach(c => {
        c.onclick = () => {
            state.wizard.engine = c.getAttribute("data-engine");
            renderWizard();
        };
    });
}

// =====================================================================
// Dashboard
// =====================================================================
async function refreshDatabases() {
    const tbody = document.getElementById("db-tbody");
    tbody.innerHTML = `<tr><td colspan="8" class="muted">Loading…</td></tr>`;
    try {
        const data = await api("/databases");
        state.databases = data.databases || [];
        state.summary   = data.summary   || {};
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="8" class="muted">Failed to load: ${escapeHtml(e.message)}</td></tr>`;
        return;
    }
    renderTable();
}

function renderTable() {
    const tbody = document.getElementById("db-tbody");
    const engineFilter = document.getElementById("filter-engine").value;
    const statusFilter = document.getElementById("filter-status").value;
    const q = document.getElementById("filter-search").value.toLowerCase();

    const rows = state.databases.filter(db => {
        if (engineFilter && db.engine !== engineFilter) return false;
        const s = String(db.status).toLowerCase();
        if (statusFilter === "running" && !["running", "started", "active"].includes(s)) return false;
        if (statusFilter === "stopped" && ["running", "started", "active"].includes(s)) return false;
        if (q && !(`${db.name} ${db.owner} ${db.id}`.toLowerCase().includes(q))) return false;
        return true;
    });

    if (!rows.length) {
        tbody.innerHTML = `<tr><td colspan="8" class="muted">No databases yet. Click <b>+ New Database</b> to create one.</td></tr>`;
        return;
    }

    tbody.innerHTML = rows.map(db => {
        // Use a uniform `badge-<engine>` class — style.css has rules for
        // each registered engine name (postgres / mongo / mysql / redis).
        const eb = `badge-${db.engine}`;
        const sLower = String(db.status).toLowerCase();
        const sb =
            sLower === "running"                                ? "badge-running" :
            ["starting", "pending"].includes(sLower)            ? "badge-starting" :
            ["failed", "error"].includes(sLower)                ? "badge-error" :
            sLower === "never started"                          ? "badge-pending" :
                                                                  "badge-stopped";
        const conn = db.url
            ? `<a href="${escapeHtml(db.browserUrl || db.url)}" target="_blank" rel="noopener">Open DB ↗</a>`
            : `<span class="muted">—</span>`;
        const configLink = db.configUrl
            ? `<a href="${escapeHtml(db.configUrl)}" target="_blank" rel="noopener">Open Setup ↗</a>`
            : `<span class="muted">—</span>`;
        const created = db.createdAt ? formatDate(db.createdAt) : "<span class=\"muted\">—</span>";
        const isRunning = db.isRunning;
        const isTransitioning = ["pending", "starting", "preparing", "queued"].includes(sLower);
        const actionBtns = isRunning
            ? `<button class="btn btn-secondary btn-small" data-stop="${db.id}">Stop</button>`
            : isTransitioning
                ? `<button class="btn btn-secondary btn-small" disabled title="DB is ${escapeHtml(db.status)} — wait for it to settle">Start</button>`
                : `<button class="btn btn-secondary btn-small" data-start="${db.id}">Start</button>`;
        return `
            <tr>
                <td><b>${escapeHtml(db.name)}</b></td>
                <td><span class="badge ${eb}">${escapeHtml(db.engine)}</span></td>
                <td><span class="badge ${sb}">${escapeHtml(db.status)}</span></td>
                <td>${escapeHtml(db.owner || "")}</td>
                <td>${created}</td>
                <td>${conn}</td>
                <td>${configLink}</td>
                <td class="td-actions">
                    ${actionBtns}
                    <button class="btn btn-secondary btn-small btn-danger" data-delete="${db.id}">Delete</button>
                </td>
            </tr>
        `;
    }).join("");

    tbody.querySelectorAll("[data-stop]").forEach(btn => {
        btn.onclick = () => stopDb(btn.getAttribute("data-stop"));
    });
    tbody.querySelectorAll("[data-start]").forEach(btn => {
        btn.onclick = () => startDb(btn.getAttribute("data-start"));
    });
    tbody.querySelectorAll("[data-delete]").forEach(btn => {
        btn.onclick = () => deleteDb(btn.getAttribute("data-delete"));
    });
}

function formatDate(s) {
    try {
        const d = new Date(s);
        if (isNaN(d.getTime())) return "<span class=\"muted\">—</span>";
        const y = d.getFullYear();
        if (y < 2000) return "<span class=\"muted\">just now</span>";
        const m = d.toLocaleString("en-US", { month: "short" });
        return `${m} ${d.getDate()}, ${d.getHours().toString().padStart(2,'0')}:${d.getMinutes().toString().padStart(2,'0')}`;
    } catch (e) {
        return "<span class=\"muted\">—</span>";
    }
}

async function startDb(id) {
    try {
        await api(`/databases/${id}/start`, { method: "POST", body: "{}" });
    } catch (e) {
        alert("Start failed: " + e.message);
    }
    refreshDatabases();
}

async function deleteDb(id) {
    if (!confirm("Delete this app entirely? This removes the App object from Domino. Dataset snapshots are preserved.")) return;
    try {
        await api(`/databases/${id}?keep=0`, { method: "DELETE" });
    } catch (e) {
        alert("Delete failed: " + e.message);
    }
    refreshDatabases();
}

async function stopDb(id) {
    if (!confirm("Stop this database? Data is preserved on /mnt/db and in snapshots.")) return;
    try {
        await api(`/databases/${id}`, { method: "DELETE" });
    } catch (e) {
        alert("Stop failed: " + e.message);
    }
    refreshDatabases();
}

// =====================================================================
// Wizard
// =====================================================================
function openWizard() {
    state.wizard = { engine: null, name: "", environmentId: "", hardwareTierId: "", password: "" };
    // Reset form fields so a re-opened wizard starts clean.
    document.getElementById("db-name").value = "";
    document.getElementById("db-pw").value = "";
    document.getElementById("provision-log").classList.add("hidden");
    document.getElementById("provision-log").innerHTML = "";
    // provision() disables btn-next to prevent double-submits during a
    // create stream; the success path doesn't re-enable it because it
    // auto-closes the wizard. Re-enable on every open so a 2nd DB works.
    document.getElementById("btn-next").disabled = false;
    renderWizard();
    document.getElementById("wizard-overlay").classList.remove("hidden");
    loadCatalogs();
}

function closeWizard() {
    document.getElementById("wizard-overlay").classList.add("hidden");
}

async function loadCatalogs() {
    // We only need hw tiers now — the compute env is resolved from the
    // engine catalog (each engine has exactly one image, baked in by an
    // admin via DD_<ENGINE>_ENV_ID on the wizard project).
    if (state.tiers.length) return;
    const tiers = await api("/hardware-tiers");
    state.tiers = tiers;
    const tierSel = document.getElementById("db-tier");
    tierSel.innerHTML = state.tiers.map(t =>
        `<option value="${t.id}">${escapeHtml(t.name)}</option>`).join("");
    // Tiers arrive after the first renderWizard(); re-check now that the
    // <select> has a real value so the Provision button reflects it.
    updateProvisionEnabled();
}

// Returns the env id the wizard will submit for the currently-picked
// engine, or "" if the admin hasn't configured one — caller surfaces
// that as a user-visible error.
function resolveEnvId() {
    const eng = enginesCatalog().find(e => e.name === state.wizard.engine);
    return eng?.envId || "";
}

// All three wizard panes are visible at once now. renderWizard() just
// reflects current state.wizard into the engine cards (selected ring),
// the name-prefix code chip in Configure, and the Review table.
function renderWizard() {
    const ws = state.wizard;
    const adapter = enginesCatalog().find(e => e.name === ws.engine);

    document.getElementById("name-prefix").textContent =
        adapter?.appPrefix ?? "pg-";

    document.querySelectorAll(".engine-card").forEach(c => {
        c.classList.toggle("selected", c.getAttribute("data-engine") === ws.engine);
    });

    document.getElementById("r-engine").textContent =
        adapter ? adapter.label : (ws.engine || "—");
    document.getElementById("r-name").textContent =
        ws.name ? (adapter?.appPrefix ?? "pg-") + ws.name : "—";
    const tier = state.tiers.find(t => t.id === ws.hardwareTierId);
    document.getElementById("r-tier").textContent =
        tier?.name || ws.hardwareTierId || "—";
    document.getElementById("r-pw").textContent =
        ws.password ? "•".repeat(ws.password.length) : "—";

    updateProvisionEnabled();
}

// The Provision button stays disabled until all four required choices are
// made: an engine card is picked, and Name / Hardware tier / Password are
// filled. We read the live DOM values (not just state.wizard) so the
// hardware-tier <select>'s auto-selected option counts even before the
// user touches it. renderWizard() — called on every engine pick, field
// edit, and wizard open — is the single place this is kept in sync.
function updateProvisionEnabled() {
    const btn = document.getElementById("btn-next");
    if (!btn) return;
    const engine = state.wizard.engine;
    const name   = (document.getElementById("db-name").value || "").trim();
    const tier   = document.getElementById("db-tier").value;
    const pw     = document.getElementById("db-pw").value;
    btn.disabled = !(engine && name && tier && pw);
}

function readFormToWizard() {
    state.wizard.name           = document.getElementById("db-name").value.trim();
    state.wizard.environmentId  = resolveEnvId();
    state.wizard.hardwareTierId = document.getElementById("db-tier").value;
    state.wizard.password       = document.getElementById("db-pw").value;
}

// Provision-button click: validate everything, then stream the create.
async function submitWizard() {
    readFormToWizard();
    const w = state.wizard;
    if (!w.engine) { alert("Pick an engine."); return; }
    if (!w.name || !w.hardwareTierId || !w.password) {
        alert("Name, hardware tier, and password are required.");
        return;
    }
    if (!w.environmentId) {
        // resolveEnvId() returned "" — the admin hasn't built / wired the
        // env image for this engine yet. Tell the user how to fix it
        // instead of letting Domino spawn against a wrong env.
        const eng = enginesCatalog().find(e => e.name === w.engine);
        alert(
            `No compute environment is configured for ${eng?.label || w.engine}.\n\n` +
            `Ask an admin to build envs/dd-${w.engine}-app and set ` +
            `${eng?.envIdVar || 'the env id'} on the wizard project.`
        );
        return;
    }
    await provision();
}

// =====================================================================
// SSE streaming — shared by provision() and buildEnv()
// =====================================================================

// Consume an SSE stream from url (POST with body), calling renderEvent(kind, data) for
// each event. Returns { terminal: { kind, data } | null }.
async function streamSse(url, body, renderEvent, onAppend) {
    let terminal = null;
    try {
        const resp = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
            body,
        });
        const ct = resp.headers.get("Content-Type") || "";
        if (!ct.includes("event-stream")) {
            const text = await resp.text();
            let detail = text;
            try { detail = (JSON.parse(text).error) || text; } catch {}
            throw new Error(`${resp.status} ${resp.statusText}: ${detail}`.trim());
        }

        const reader = resp.body.getReader();
        const dec = new TextDecoder();
        let buf = "";
        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buf += dec.decode(value, { stream: true });
            let idx;
            while ((idx = buf.indexOf("\n\n")) !== -1) {
                const record = buf.slice(0, idx);
                buf = buf.slice(idx + 2);
                const lines = record.split("\n").filter(l => l && !l.startsWith(":"));
                if (lines.length === 0) continue;
                let kind = "message", dataStr = "";
                for (const line of lines) {
                    if (line.startsWith("event:")) kind = line.slice(6).trim();
                    else if (line.startsWith("data:")) dataStr += (dataStr ? "\n" : "") + line.slice(5).trimStart();
                }
                let data = {};
                try { data = JSON.parse(dataStr); } catch {}
                renderEvent(kind, data);
                if (kind === "result" || kind === "error") terminal = { kind, data };
            }
        }
    } catch (e) {
        onAppend("err", "✗", e.message || String(e));
    }
    return terminal;
}

async function provision() {
    const logEl = document.getElementById("provision-log");
    logEl.classList.remove("hidden");
    logEl.innerHTML = "";
    document.getElementById("btn-next").disabled = true;

    const append = (cls, marker, msg, extra) => {
        const tail = extra ? ` <span class="muted">(${escapeHtml(extra)})</span>` : "";
        const cleanCls = cls ? ` class="${cls}"` : "";
        logEl.innerHTML += `<span${cleanCls}>${escapeHtml(marker)} ${escapeHtml(msg)}</span>${tail}\n`;
        logEl.scrollTop = logEl.scrollHeight;
    };
    const renderEvent = (kind, data) => {
        const msg = data.msg || "";
        const ms = (typeof data.ms === "number" && data.ms > 0) ? `${data.ms}ms` : "";
        if (kind === "step")        append("step",  "→", msg, ms);
        else if (kind === "ok")     append("ok",    "✓", msg, ms);
        else if (kind === "tick")   append("muted", "·", `${msg} (${data.elapsed_s}s)`);
        else if (kind === "warn")   append("warn",  "⚠", msg + (data.detail ? ` — ${data.detail}` : ""));
        else if (kind === "error")  append("err",   "✗", msg + (data.detail ? ` — ${data.detail}` : ""));
        else if (kind === "result") {
            append("ok", "✓", `Created App ${data.id} — status ${data.status}`,
                   (typeof data.totalMs === "number") ? `total ${data.totalMs}ms` : "");
            if (data.url) {
                logEl.innerHTML += `<span class="ok">→</span> Open: <a href="${escapeHtml(data.url)}" target="_blank">${escapeHtml(data.url)}</a>\n`;
                logEl.scrollTop = logEl.scrollHeight;
            }
            if (data.startError) {
                append("warn", "⚠", `Start: ${data.startError}`);
            }
        }
    };

    const w = state.wizard;
    const body = JSON.stringify({
        engine: w.engine,
        name: w.name,
        environmentId: w.environmentId,
        hardwareTierId: w.hardwareTierId,
        password: w.password,
        ...(_scopedProjectId ? { projectId: _scopedProjectId } : {}),
    });
    const terminal = await streamSse(`${API}/databases`, body, renderEvent, append);

    if (terminal && terminal.kind === "result") {
        setTimeout(() => { closeWizard(); refreshDatabases(); }, 1800);
    } else {
        document.getElementById("btn-next").disabled = false;
    }
}

// =====================================================================
// Environments tab
// =====================================================================
async function loadEnvironments() {
    const container = document.getElementById("env-cards");
    container.innerHTML = `<p class="muted">Loading environments…</p>`;
    let envs;
    try {
        envs = await api("/environments/status");
    } catch (e) {
        container.innerHTML = `<p class="muted">Failed to load: ${escapeHtml(e.message)}</p>`;
        return;
    }
    state.envsLoaded = true;
    renderEnvCards(envs);
}

function envStatusBadge(envIdSource, latestRevision) {
    const revStatus = (latestRevision?.status || "").toLowerCase();
    if (envIdSource === "missing") {
        return `<span class="badge badge-pending">Not Built</span>`;
    }
    if (revStatus === "succeeded") {
        return `<span class="badge badge-running">Succeeded</span>`;
    }
    if (["queued", "building"].includes(revStatus)) {
        return `<span class="badge badge-starting">${latestRevision.status}</span>`;
    }
    if (revStatus === "failed") {
        return `<span class="badge badge-error">Failed</span>`;
    }
    if (revStatus) {
        return `<span class="badge badge-stopped">${escapeHtml(latestRevision.status)}</span>`;
    }
    return `<span class="badge badge-pending">Pending</span>`;
}

function fmtEnvDate(ts) {
    if (!ts) return "";
    try {
        const d = new Date(ts);
        return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
    } catch { return ts; }
}

function renderEnvCards(envs) {
    const container = document.getElementById("env-cards");
    if (!envs || !envs.length) {
        container.innerHTML = `<p class="muted">No engines registered.</p>`;
        return;
    }
    container.innerHTML = envs.map(e => {
        const icon = e.iconUrl
            ? `<img class="engine-icon-img" src="${escapeHtml(e.iconUrl)}" alt="${escapeHtml(e.label)}" style="width:36px;height:36px;">`
            : e.icon
                ? `<span class="engine-icon" style="font-size:32px;">${escapeHtml(e.icon)}</span>`
                : "";
        const badge = envStatusBadge(e.envIdSource, e.latestRevision);
        const revNum = e.revisionNumber != null ? `v${e.revisionNumber}` : "";
        const envIdLine = e.envId
            ? `<span class="env-meta-val"><code>${escapeHtml(e.envId)}</code></span>`
            : `<span class="muted">—</span>`;
        const sourceLine = {
            envvar: `<span class="env-source-chip env-source-envvar">env var</span>`,
            byname: `<span class="env-source-chip env-source-byname">resolved by name</span>`,
            missing: `<span class="env-source-chip env-source-missing">not found</span>`,
        }[e.envIdSource] || "";
        const dfBtn = e.dockerfileExists
            ? `<button class="btn btn-secondary btn-small" onclick="toggleDockerfile('${escapeHtml(e.name)}')">View Dockerfile</button>`
            : `<span class="muted" title="Dockerfile not present in repo">No Dockerfile</span>`;
        const buildLabel = e.envIdSource === "missing" ? "Build" : "Rebuild";
        const dfId = `df-${e.name}`;
        const logId = `env-log-${e.name}`;
        const dfEscaped = escapeHtml(e.dockerfile || "(no Dockerfile found)");

        // Rich v4 fields
        const baseImageTag = e.imageDisplay
            ? e.imageDisplay.includes(":") ? e.imageDisplay.split(":")[1] : e.imageDisplay
            : "";
        const baseImageRow = e.imageDisplay ? `
            <div class="env-meta-row">
                <span class="env-meta-key">Base image</span>
                <span class="env-meta-val env-image-tag" title="${escapeHtml(e.dockerImage || "")}">${escapeHtml(baseImageTag)}</span>
            </div>` : "";
        const sizeRow = e.imageSize ? `
            <div class="env-meta-row">
                <span class="env-meta-key">Image size</span>
                <span class="env-meta-val">${escapeHtml(e.imageSize)}</span>
            </div>` : "";
        const ownerRow = e.owner ? `
            <div class="env-meta-row">
                <span class="env-meta-key">Owner</span>
                <span class="env-meta-val">${escapeHtml(e.owner)}${e.visibility ? ` <span class="env-source-chip">${escapeHtml(e.visibility)}</span>` : ""}</span>
            </div>` : "";
        const updatedRow = e.lastUpdated ? `
            <div class="env-meta-row">
                <span class="env-meta-key">Last updated</span>
                <span class="env-meta-val">${escapeHtml(fmtEnvDate(e.lastUpdated))}</span>
            </div>` : "";

        return `
        <div class="env-card" id="env-card-${escapeHtml(e.name)}">
            <div class="env-card-header">
                <div class="env-card-identity">
                    ${icon}
                    <div>
                        <div class="env-card-label">${escapeHtml(e.label)}</div>
                        <code class="env-card-name">${escapeHtml(e.expectedEnvName)}</code>
                    </div>
                </div>
                <div class="env-card-status">
                    ${badge}
                    ${revNum ? `<span class="env-rev-chip">${escapeHtml(revNum)}</span>` : ""}
                    ${e.envUrl ? `<a class="btn btn-secondary btn-small env-open-btn" href="${escapeHtml(e.envUrl)}" target="_blank" rel="noopener">Open Environment</a>` : ""}
                </div>
            </div>
            <div class="env-card-meta">
                ${baseImageRow}
                ${sizeRow}
                ${ownerRow}
                ${updatedRow}
                <div class="env-meta-row">
                    <span class="env-meta-key">Env ID</span>
                    ${envIdLine}
                    ${sourceLine}
                </div>
                <div class="env-meta-row">
                    <span class="env-meta-key">Resolved via</span>
                    <code class="env-meta-val">${e.envIdSource === "envvar" ? escapeHtml(e.envIdVar) : escapeHtml(e.expectedEnvName)}</code>
                </div>
            </div>
            <div class="env-card-actions">
                ${dfBtn}
                <button class="btn btn-primary btn-small" id="btn-build-${escapeHtml(e.name)}"
                    onclick="buildEnv('${escapeHtml(e.name)}')">${buildLabel}</button>
            </div>
            <pre class="provision-log dockerfile-view hidden" id="${dfId}">${dfEscaped}</pre>
            <pre class="provision-log hidden" id="${logId}"></pre>
        </div>
        `;
    }).join("");
}

function toggleDockerfile(engine) {
    const el = document.getElementById(`df-${engine}`);
    if (!el) return;
    el.classList.toggle("hidden");
}

async function buildEnv(engine) {
    const logId = `env-log-${engine}`;
    const logEl = document.getElementById(logId);
    const btn = document.getElementById(`btn-build-${engine}`);
    if (!logEl || !btn) return;

    logEl.classList.remove("hidden");
    logEl.innerHTML = "";
    btn.disabled = true;

    const append = (cls, marker, msg, extra) => {
        const tail = extra ? ` <span class="muted">(${escapeHtml(extra)})</span>` : "";
        const cleanCls = cls ? ` class="${cls}"` : "";
        logEl.innerHTML += `<span${cleanCls}>${escapeHtml(marker)} ${escapeHtml(msg)}</span>${tail}\n`;
        logEl.scrollTop = logEl.scrollHeight;
    };
    const renderEvent = (kind, data) => {
        const msg = data.msg || "";
        const ms = (typeof data.ms === "number" && data.ms > 0) ? `${data.ms}ms` : "";
        if (kind === "step")        append("step",  "→", msg, ms);
        else if (kind === "ok")     append("ok",    "✓", msg, ms);
        else if (kind === "tick")   append("muted", "·", `${msg} (${data.elapsed_s}s)`);
        else if (kind === "warn")   append("warn",  "⚠", msg + (data.detail ? ` — ${data.detail}` : ""));
        else if (kind === "error")  append("err",   "✗", msg + (data.detail ? ` — ${data.detail}` : ""));
        else if (kind === "result") {
            const statusWord = data.status || "done";
            const action = data.action || "built";
            append("ok", "✓", `Environment ${action} — build status: ${statusWord}`,
                   (typeof data.totalMs === "number") ? `total ${data.totalMs}ms` : "");
        }
    };

    const terminal = await streamSse(
        `${API}/environments/${encodeURIComponent(engine)}/build`,
        "{}",
        renderEvent,
        append,
    );

    btn.disabled = false;
    btn.textContent = "Rebuild";

    if (terminal && terminal.kind === "result") {
        // Refresh just this card's status without blowing away the whole list.
        try {
            const envs = await api("/environments/status");
            renderEnvCards(envs);
            // Re-reveal the log for the engine we just built.
            const newLog = document.getElementById(`env-log-${engine}`);
            if (newLog) {
                newLog.classList.remove("hidden");
                newLog.innerHTML = logEl.innerHTML;
                newLog.scrollTop = newLog.scrollHeight;
            }
        } catch (_) {}
    }
}

// =====================================================================
// UI bindings
// =====================================================================
function bindTabs() {
    document.querySelectorAll(".tab[data-tab]").forEach(btn => {
        btn.onclick = () => {
            document.querySelectorAll(".tab[data-tab]").forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            const tab = btn.getAttribute("data-tab");
            document.querySelectorAll("[id^='panel-']").forEach(p => p.classList.add("hidden"));
            const panel = document.getElementById(`panel-${tab}`);
            if (panel) panel.classList.remove("hidden");
            if (tab === "environments" && !state.envsLoaded) {
                loadEnvironments();
            }
        };
    });
}

function bindUi() {
    bindTabs();
    document.getElementById("btn-new-db").onclick    = openWizard;
    document.getElementById("btn-close-wizard").onclick = closeWizard;
    document.getElementById("btn-cancel").onclick    = closeWizard;
    document.getElementById("btn-next").onclick      = submitWizard;
    document.getElementById("btn-refresh").onclick   = refreshDatabases;
    document.getElementById("btn-refresh-envs").onclick = loadEnvironments;
    document.getElementById("filter-engine").onchange = renderTable;
    document.getElementById("filter-status").onchange = renderTable;
    document.getElementById("filter-search").oninput  = renderTable;

    // Live wiring — every form-field edit immediately flows into
    // state.wizard and is reflected in the Review pane.
    const liveUpdate = () => { readFormToWizard(); renderWizard(); };
    document.getElementById("db-name").oninput  = liveUpdate;
    document.getElementById("db-tier").onchange = liveUpdate;
    document.getElementById("db-pw").oninput    = liveUpdate;

    // (Engine-card click handlers are wired up in renderEngineCards()
    // once the catalog has been fetched — at boot time the grid is empty.)

    document.getElementById("btn-gen-pw").onclick = () => {
        const r = new Uint8Array(16);
        crypto.getRandomValues(r);
        document.getElementById("db-pw").value =
            btoa(String.fromCharCode(...r)).replace(/[+/=]/g, "").slice(0, 20);
        // Reflect generated password into state + Review pane immediately.
        readFormToWizard();
        renderWizard();
    };
}

function escapeHtml(s) {
    return String(s || "")
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}
