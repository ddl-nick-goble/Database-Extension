// Domino Databases — frontend logic.
//
// One file, no build step. Talks to the Flask backend at relative paths so
// the same code works under the Domino app proxy (`/<owner>/<proj>/app/.../`)
// and the workspace dev proxy (`/<owner>/<proj>/notebookSession/<run>/proxy/<port>/`).

const API = "./api";

const state = {
    config: {},
    databases: [],
    summary: {},
    envs: [],
    tiers: [],
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
    const r = await fetch(`${API}${path}`, {
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
    try {
        state.config = await api("/config");
    } catch (e) {
        console.error("config load failed", e);
    }
    await refreshDatabases();
    // Catalogs load lazily when the wizard opens.
}
document.addEventListener("DOMContentLoaded", boot);

// =====================================================================
// Dashboard
// =====================================================================
async function refreshDatabases() {
    const tbody = document.getElementById("db-tbody");
    tbody.innerHTML = `<tr><td colspan="6" class="muted">Loading…</td></tr>`;
    try {
        const data = await api("/databases");
        state.databases = data.databases || [];
        state.summary   = data.summary   || {};
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="6" class="muted">Failed to load: ${escapeHtml(e.message)}</td></tr>`;
        return;
    }
    renderStats();
    renderTable();
}

function renderStats() {
    const s = state.summary;
    const total = (s.total ?? state.databases.length) || 0;
    document.getElementById("stat-total").textContent    = total;
    document.getElementById("stat-postgres").textContent = s.postgres ?? "0";
    document.getElementById("stat-mongo").textContent    = s.mongo    ?? "0";
    document.getElementById("stat-running").textContent  = s.running  ?? "0";
    document.getElementById("stat-stopped").textContent  = Math.max(0, total - (s.running || 0));
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
        tbody.innerHTML = `<tr><td colspan="7" class="muted">No databases yet. Click <b>+ New Database</b> to create one.</td></tr>`;
        return;
    }

    tbody.innerHTML = rows.map(db => {
        const eb = db.engine === "postgres" ? "badge-postgres" : db.engine === "mongo" ? "badge-mongo" : "";
        const sLower = String(db.status).toLowerCase();
        const sb =
            sLower === "running"                                ? "badge-running" :
            ["starting", "pending"].includes(sLower)            ? "badge-starting" :
            ["failed", "error"].includes(sLower)                ? "badge-error" :
            sLower === "never started"                          ? "badge-pending" :
                                                                  "badge-stopped";
        const conn = db.url
            ? `<a href="${escapeHtml(db.url)}" target="_blank" rel="noopener">Open app →</a>`
            : `<span class="muted">—</span>`;
        const created = db.createdAt ? formatDate(db.createdAt) : "<span class=\"muted\">—</span>";
        const isRunning = db.isRunning;
        const actionBtns = isRunning
            ? `<button class="btn btn-secondary btn-small" data-stop="${db.id}">Stop</button>`
            : `<button class="btn btn-secondary btn-small" data-start="${db.id}">Start</button>`;
        return `
            <tr>
                <td><b>${escapeHtml(db.name)}</b></td>
                <td><span class="badge ${eb}">${escapeHtml(db.engine)}</span></td>
                <td><span class="badge ${sb}">${escapeHtml(db.status)}</span></td>
                <td>${escapeHtml(db.owner || "")}</td>
                <td>${created}</td>
                <td>${conn}</td>
                <td class="td-actions">
                    ${actionBtns}
                    <button class="btn btn-secondary btn-small btn-danger" data-delete="${db.id}" title="Delete app">×</button>
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
    state.wizard = { step: 1, engine: null, name: "", environmentId: "", hardwareTierId: "", password: "" };
    renderWizard();
    document.getElementById("wizard-overlay").classList.remove("hidden");
    loadCatalogs();
}

function closeWizard() {
    document.getElementById("wizard-overlay").classList.add("hidden");
}

async function loadCatalogs() {
    if (state.envs.length && state.tiers.length) return;
    const [envs, tiers] = await Promise.all([api("/environments"), api("/hardware-tiers")]);
    state.envs = envs;
    state.tiers = tiers;
    populateCatalogSelects();
    // Re-apply the engine default in case the user already picked an engine
    // before catalogs finished loading (race that previously left the env
    // dropdown unset → app spawned with project default env).
    applyEnvDefault();
}

function populateCatalogSelects() {
    const envSel = document.getElementById("db-env");
    const tierSel = document.getElementById("db-tier");
    envSel.innerHTML = `<option value="">— pick environment —</option>` +
        state.envs.map(e => `<option value="${e.id}">${escapeHtml(e.name)}</option>`).join("");
    tierSel.innerHTML = state.tiers.map(t =>
        `<option value="${t.id}">${escapeHtml(t.name)}</option>`).join("");
}

function applyEnvDefault() {
    const envSel = document.getElementById("db-env");
    if (!envSel) return;
    const def = state.wizard.engine === "postgres"
        ? state.config.postgresEnvId
        : state.config.mongoEnvId;
    if (!def) return;
    const match = envSel.querySelector(`option[value="${def}"]`);
    if (match) envSel.value = def;
}

function renderWizard() {
    const s = state.wizard.step;
    for (let i = 1; i <= 3; i++) {
        const stepEl = document.querySelector(`.step[data-step="${i}"]`);
        const bodyEl = document.getElementById(`step-${i}`);
        stepEl.classList.toggle("active", i === s);
        stepEl.classList.toggle("done", i < s);
        bodyEl.classList.toggle("hidden", i !== s);
    }
    document.getElementById("btn-prev").disabled = s === 1;
    document.getElementById("btn-next").textContent =
        s === 3 ? "Provision" : "Next →";

    document.getElementById("name-prefix").textContent =
        state.wizard.engine === "mongo" ? "mongo-" : "pg-";

    // populate engine cards
    document.querySelectorAll(".engine-card").forEach(c => {
        c.classList.toggle("selected", c.getAttribute("data-engine") === state.wizard.engine);
    });

    if (s === 3) {
        const ws = state.wizard;
        document.getElementById("r-engine").textContent = ws.engine;
        document.getElementById("r-name").textContent =
            (ws.engine === "mongo" ? "mongo-" : "pg-") + ws.name;
        document.getElementById("r-env").textContent =
            (state.envs.find(e => e.id === ws.environmentId) || {}).name || ws.environmentId;
        document.getElementById("r-tier").textContent =
            (state.tiers.find(t => t.id === ws.hardwareTierId) || {}).name || ws.hardwareTierId;
        document.getElementById("r-pw").textContent = "•".repeat(ws.password.length);
    }
}

function readFormToWizard() {
    state.wizard.name           = document.getElementById("db-name").value.trim();
    state.wizard.environmentId  = document.getElementById("db-env").value;
    state.wizard.hardwareTierId = document.getElementById("db-tier").value;
    state.wizard.password       = document.getElementById("db-pw").value;
}

async function next() {
    const s = state.wizard.step;
    if (s === 1) {
        if (!state.wizard.engine) { alert("Pick an engine."); return; }
        state.wizard.step = 2;
        renderWizard();
        return;
    }
    if (s === 2) {
        readFormToWizard();
        const w = state.wizard;
        if (!w.name || !w.environmentId || !w.hardwareTierId || !w.password) {
            alert("All fields required.");
            return;
        }
        // Guard against an env id that's not in the dropdown (shouldn't happen,
        // but if it does we'd send junk to Domino and get a wrong-env spawn).
        if (!state.envs.find(e => e.id === w.environmentId)) {
            alert("Selected environment is not in the catalog. Refresh and try again.");
            return;
        }
        state.wizard.step = 3;
        renderWizard();
        return;
    }
    if (s === 3) {
        await provision();
    }
}

function prev() {
    if (state.wizard.step > 1) {
        state.wizard.step -= 1;
        renderWizard();
    }
}

async function provision() {
    const log = document.getElementById("provision-log");
    log.classList.remove("hidden");
    log.innerHTML = "Creating Domino App…\n";
    document.getElementById("btn-next").disabled = true;
    document.getElementById("btn-prev").disabled = true;
    try {
        const w = state.wizard;
        const body = {
            engine: w.engine,
            name: w.name,
            environmentId: w.environmentId,
            hardwareTierId: w.hardwareTierId,
            password: w.password,
        };
        const result = await api("/databases", { method: "POST", body: JSON.stringify(body) });
        log.innerHTML += `<span class="ok">✓ Created App ${result.id}</span>\n`;
        log.innerHTML += `  Status: ${result.status}\n`;
        if (result.url) {
            log.innerHTML += `  Open: <a href="${result.url}" target="_blank">${result.url}</a>\n`;
        }
        if (result.startError) {
            log.innerHTML += `<span class="err">⚠ Start failed: ${escapeHtml(result.startError)}</span>\n`;
        } else {
            log.innerHTML += "Container is booting (this can take ~1 min)…\n";
        }
        setTimeout(() => { closeWizard(); refreshDatabases(); }, 2000);
    } catch (e) {
        log.innerHTML += `<span class="err">✗ ${escapeHtml(e.message)}</span>\n`;
        document.getElementById("btn-next").disabled = false;
        document.getElementById("btn-prev").disabled = false;
    }
}

// =====================================================================
// UI bindings
// =====================================================================
function bindUi() {
    document.getElementById("btn-new-db").onclick    = openWizard;
    document.getElementById("btn-close-wizard").onclick = closeWizard;
    document.getElementById("btn-prev").onclick      = prev;
    document.getElementById("btn-next").onclick      = next;
    document.getElementById("btn-refresh").onclick   = refreshDatabases;
    document.getElementById("filter-engine").onchange = renderTable;
    document.getElementById("filter-status").onchange = renderTable;
    document.getElementById("filter-search").oninput  = renderTable;

    document.querySelectorAll(".engine-card").forEach(c => {
        c.onclick = () => {
            state.wizard.engine = c.getAttribute("data-engine");
            renderWizard();
            // applyEnvDefault is a no-op until catalogs are loaded; loadCatalogs
            // re-applies it once they arrive, so the dropdown ends up correct either way.
            applyEnvDefault();
        };
    });

    document.getElementById("btn-gen-pw").onclick = () => {
        const r = new Uint8Array(16);
        crypto.getRandomValues(r);
        document.getElementById("db-pw").value =
            btoa(String.fromCharCode(...r)).replace(/[+/=]/g, "").slice(0, 20);
    };
}

function escapeHtml(s) {
    return String(s || "")
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}
