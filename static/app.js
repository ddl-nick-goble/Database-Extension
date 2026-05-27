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
        // Engine catalog drives the entire UI — paint it before anything
        // else so the filter dropdown / engine cards / stat tiles all
        // exist before refreshDatabases() tries to populate them.
        renderEngineDropdown();
        renderStatTiles();
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

function renderStatTiles() {
    // Insert per-engine stat tiles after the TOTAL tile, before RUNNING.
    const row = document.getElementById("stats-row");
    if (!row) return;
    // Strip any previously-injected tiles (idempotent reload).
    row.querySelectorAll("[data-engine-stat]").forEach(el => el.remove());
    const runningTile = row.querySelector(".stat-value.stat-running")?.closest(".stat");
    enginesCatalog().forEach(e => {
        const tile = document.createElement("div");
        tile.className = "stat";
        tile.setAttribute("data-engine-stat", e.name);
        tile.innerHTML = `
            <div class="stat-label">${escapeHtml(e.label.toUpperCase())}</div>
            <div class="stat-value" id="stat-engine-${e.name}">—</div>
        `;
        row.insertBefore(tile, runningTile);
    });
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
            applyEnvDefault();
        };
    });
}

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
    enginesCatalog().forEach(e => {
        const el = document.getElementById(`stat-engine-${e.name}`);
        if (el) el.textContent = s[e.name] ?? "0";
    });
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
            ? `<a href="${escapeHtml(db.url)}" target="_blank" rel="noopener">Open app →</a>`
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
    // We only need hw tiers now — the compute env is resolved from the
    // engine catalog (each engine has exactly one image, baked in by an
    // admin via DD_<ENGINE>_ENV_ID on the wizard project).
    if (state.tiers.length) return;
    const tiers = await api("/hardware-tiers");
    state.tiers = tiers;
    const tierSel = document.getElementById("db-tier");
    tierSel.innerHTML = state.tiers.map(t =>
        `<option value="${t.id}">${escapeHtml(t.name)}</option>`).join("");
}

// Returns the env id the wizard will submit for the currently-picked
// engine, or "" if the admin hasn't configured one — caller surfaces
// that as a user-visible error.
function resolveEnvId() {
    const eng = enginesCatalog().find(e => e.name === state.wizard.engine);
    return eng?.envId || "";
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

    const adapter = enginesCatalog().find(e => e.name === state.wizard.engine);
    document.getElementById("name-prefix").textContent =
        adapter?.appPrefix ?? "pg-";

    // populate engine cards
    document.querySelectorAll(".engine-card").forEach(c => {
        c.classList.toggle("selected", c.getAttribute("data-engine") === state.wizard.engine);
    });

    if (s === 3) {
        const ws = state.wizard;
        document.getElementById("r-engine").textContent =
            adapter ? adapter.label : ws.engine;
        document.getElementById("r-name").textContent =
            (adapter?.appPrefix ?? "pg-") + ws.name;
        document.getElementById("r-tier").textContent =
            (state.tiers.find(t => t.id === ws.hardwareTierId) || {}).name || ws.hardwareTierId;
        document.getElementById("r-pw").textContent = "•".repeat(ws.password.length);
    }
}

function readFormToWizard() {
    state.wizard.name           = document.getElementById("db-name").value.trim();
    state.wizard.environmentId  = resolveEnvId();
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
        if (!w.name || !w.hardwareTierId || !w.password) {
            alert("Name, hardware tier, and password are required.");
            return;
        }
        if (!w.environmentId) {
            // resolveEnvId() returned "" — the admin hasn't built / wired
            // up the env image for this engine yet. Tell the user how to
            // fix it instead of letting Domino spawn against a wrong env.
            const eng = enginesCatalog().find(e => e.name === w.engine);
            alert(
                `No compute environment is configured for ${eng?.label || w.engine}.\n\n` +
                `Ask an admin to build envs/dd-${w.engine}-app and set ` +
                `${eng?.envIdVar || 'the env id'} on the wizard project.`
            );
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
    log.innerHTML = "";
    document.getElementById("btn-next").disabled = true;
    document.getElementById("btn-prev").disabled = true;

    const append = (cls, marker, msg, extra) => {
        const tail = extra ? ` <span class="muted">(${escapeHtml(extra)})</span>` : "";
        const cleanCls = cls ? ` class="${cls}"` : "";
        log.innerHTML += `<span${cleanCls}>${escapeHtml(marker)} ${escapeHtml(msg)}</span>${tail}\n`;
        log.scrollTop = log.scrollHeight;
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
                log.innerHTML += `<span class="ok">→</span> Open: <a href="${escapeHtml(data.url)}" target="_blank">${escapeHtml(data.url)}</a>\n`;
                log.scrollTop = log.scrollHeight;
            }
            if (data.startError) {
                append("warn", "⚠", `Start: ${data.startError}`);
            }
        }
    };

    let terminal = null;  // { kind: 'result'|'error', data }
    try {
        const w = state.wizard;
        const body = JSON.stringify({
            engine: w.engine,
            name: w.name,
            environmentId: w.environmentId,
            hardwareTierId: w.hardwareTierId,
            password: w.password,
        });
        const resp = await fetch(`${API}/databases`, {
            method: "POST",
            headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
            body,
        });
        const ct = resp.headers.get("Content-Type") || "";
        // Non-SSE error response (pre-stream failure, e.g. malformed JSON).
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
            // SSE records separated by blank line.
            let idx;
            while ((idx = buf.indexOf("\n\n")) !== -1) {
                const record = buf.slice(0, idx);
                buf = buf.slice(idx + 2);
                // Skip SSE comments (lines starting with ':') — used for padding/flush.
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
        append("err", "✗", e.message || String(e));
    }

    if (terminal && terminal.kind === "result") {
        setTimeout(() => { closeWizard(); refreshDatabases(); }, 1800);
    } else {
        // error, or stream ended without a terminal event — let the user retry.
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
