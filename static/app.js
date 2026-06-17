// Domino Databases — frontend logic.
//
// One file, no build step. Talks to the Flask backend at relative paths so
// the same code works under the Domino app proxy (`/<owner>/<proj>/app/.../`)
// and the workspace dev proxy (`/<owner>/<proj>/notebookSession/<run>/proxy/<port>/`).

const API = "./api";

// Project scoping — Domino appends these query params when loading the app
// as a sidebar extension so it can scope itself to the active project.
const _urlParams    = new URLSearchParams(location.search);
const _scopedProjectId = _urlParams.get("projectId") || "";
const _scopedOwner     = _urlParams.get("projectOwner") || _urlParams.get("ownerUsername") || "";
const _scopedProject   = _urlParams.get("projectName") || "";

const state = {
    config: {},
    databases: [],
    summary: {},
    tiers: [],
    envsLoaded: false,
    envCards: {},      // engine name -> latest row data (skeleton spec, then v4 detail)
    envPollers: {},    // engine name -> setTimeout id for in-progress build polling
    envLoadToken: 0,   // bumped each (re)load so stale in-flight fetches drop themselves
    dbPollers: {},     // app id -> setTimeout id for spinning-up DB status/log polling
    dbLogOffsets: {},  // app id -> realTimeLogs offset already fetched
    dbLogSeen: {},     // app id -> latest log timestamp shown (dedupe guard)
    dbLoadToken: 0,    // bumped on full DB (re)render so stale per-card work drops itself
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
    try {
        const configParams = new URLSearchParams();
        if (_scopedProjectId) configParams.set("projectId", _scopedProjectId);
        if (_scopedOwner)     configParams.set("ownerName", _scopedOwner);
        if (_scopedProject)   configParams.set("projectName", _scopedProject);
        const configQs = configParams.toString();
        state.config = await fetch(`${API}/config${configQs ? "?" + configQs : ""}`, {
            headers: { "Content-Type": "application/json" },
        }).then(r => r.json());
        const badge = document.getElementById("project-scope-badge");
        if (badge) {
            const resolvedProject = state.config.project || "";
            const isScoped = _scopedProjectId && resolvedProject && resolvedProject !== state.config.deployProject;
            if (isScoped) {
                badge.textContent = `Project: ${resolvedProject}`;
                badge.classList.remove("hidden");
            } else {
                badge.classList.add("hidden");
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
const DB_POLL_MS = 3500;  // status/log cadence for a DB that's still spinning up

// Statuses that mean "still settling" — these cards poll status + stream logs
// until they reach a terminal state (running / stopped / failed).
const DB_TRANSITION_STATES = [
    "pending", "starting", "preparing", "queued", "building", "pulling", "stopping",
];
function dbIsTransitioning(status) {
    return DB_TRANSITION_STATES.includes(String(status).toLowerCase());
}
function dbStatusBadgeClass(status) {
    const s = String(status).toLowerCase();
    if (s === "running") return "badge-running";
    if (dbIsTransitioning(s)) return "badge-starting";
    if (["failed", "error"].includes(s)) return "badge-error";
    if (s === "never started") return "badge-pending";
    return "badge-stopped";
}

async function refreshDatabases() {
    const container = document.getElementById("db-cards");
    stopAllDbPolling();
    state.dbLoadToken++;
    container.innerHTML = `<p class="muted">Loading…</p>`;
    try {
        const data = await api("/databases");
        state.databases = data.databases || [];
        state.summary   = data.summary   || {};
    } catch (e) {
        container.innerHTML = `<p class="muted">Failed to load: ${escapeHtml(e.message)}</p>`;
        return;
    }
    renderDbCards();
}

// Full render of the DB grid (applies filters). Resets per-card log tracking,
// then arms async status/log polling for every visible spinning-up card.
function renderDbCards() {
    const container = document.getElementById("db-cards");
    if (!container) return;
    stopAllDbPolling();
    state.dbLogOffsets = {};
    state.dbLogSeen = {};

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
        container.innerHTML = `<p class="muted">No databases yet. Click <b>+ New Database</b> to create one.</p>`;
        return;
    }

    container.innerHTML = rows.map(dbCardShell).join("");
    bindDbCardActions(container);
    rows.forEach(db => maybePollDbCard(db.id));
}

// Stable card wrapper: a replaceable body plus a sibling <pre> for live logs
// that survives body re-renders (mirrors the environment cards).
function dbCardShell(db) {
    const id = escapeHtml(db.id);
    return `
    <div class="db-card" id="db-card-${id}">
        <div class="db-card-body" id="db-body-${id}">
            ${dbCardBodyHtml(db)}
        </div>
        <pre class="provision-log hidden" id="db-log-${id}"></pre>
    </div>`;
}

function dbCardBodyHtml(db) {
    const id = escapeHtml(db.id);
    const eb = `badge-${db.engine}`;
    const sb = dbStatusBadgeClass(db.status);
    const transitioning = dbIsTransitioning(db.status);
    const statusInner = transitioning
        ? `<span class="db-spin"></span>${escapeHtml(db.status)}`
        : escapeHtml(db.status);

    const conn = db.url
        ? `<a class="btn btn-secondary btn-small" href="${escapeHtml(db.browserUrl || db.url)}" target="_blank" rel="noopener">Open DB ↗</a>`
        : `<span class="muted">—</span>`;
    const configLink = db.configUrl
        ? `<a class="btn btn-secondary btn-small" href="${escapeHtml(db.configUrl)}" target="_blank" rel="noopener">Open Setup ↗</a>`
        : `<span class="muted">—</span>`;
    const created = db.createdAt ? formatDate(db.createdAt) : `<span class="muted">—</span>`;

    const isRunning = String(db.status).toLowerCase() === "running" || db.isRunning;
    // Mirror the environment cards: the launch action is the primary (filled)
    // button; Stop/Logs are secondary; Delete keeps the danger treatment.
    const actionBtns = isRunning
        ? `<button class="btn btn-secondary btn-small" data-stop="${id}">Stop</button>`
        : transitioning
            ? `<button class="btn btn-primary btn-small" disabled title="DB is ${escapeHtml(db.status)} — wait for it to settle">Start</button>`
            : `<button class="btn btn-primary btn-small" data-start="${id}">Start</button>`;

    return `
        <div class="db-card-header">
            <div class="db-card-identity">
                <span class="db-card-name">${escapeHtml(db.name)}</span>
                <span class="badge ${eb}">${escapeHtml(db.engine)}</span>
            </div>
            <span class="badge ${sb} db-status-badge">${statusInner}</span>
        </div>
        <div class="db-card-meta">
            <div class="env-meta-row"><span class="env-meta-key">Owner</span><span class="env-meta-val">${escapeHtml(db.owner || "—")}</span></div>
            <div class="env-meta-row"><span class="env-meta-key">Created</span><span class="env-meta-val">${created}</span></div>
            <div class="env-meta-row"><span class="env-meta-key">Connection</span><span class="env-meta-val">${conn}</span></div>
            <div class="env-meta-row"><span class="env-meta-key">Configuration</span><span class="env-meta-val">${configLink}</span></div>
        </div>
        <div class="db-card-actions">
            ${actionBtns}
            <button class="btn btn-secondary btn-small" data-logs="${id}">Logs</button>
            <button class="btn btn-secondary btn-small btn-danger" data-delete="${id}">Delete</button>
        </div>`;
}

// Re-render only one card's body, preserving its sibling log <pre>.
function renderDbCardBody(id) {
    const body = document.getElementById(`db-body-${id}`);
    const db = state.databases.find(d => d.id === id);
    if (!body || !db) return;
    body.innerHTML = dbCardBodyHtml(db);
    bindDbCardActions(body);
}

function bindDbCardActions(scope) {
    scope.querySelectorAll("[data-stop]").forEach(b => b.onclick = () => stopDb(b.getAttribute("data-stop")));
    scope.querySelectorAll("[data-start]").forEach(b => b.onclick = () => startDb(b.getAttribute("data-start")));
    scope.querySelectorAll("[data-delete]").forEach(b => b.onclick = () => deleteDb(b.getAttribute("data-delete")));
    scope.querySelectorAll("[data-logs]").forEach(b => b.onclick = () => toggleDbLogs(b.getAttribute("data-logs")));
}

// ---- per-card async status + live logs --------------------------------

// Arm polling for a card only if it's mid-transition; reveals the live log
// and kicks the first tick immediately.
function maybePollDbCard(id, token = state.dbLoadToken) {
    stopDbPolling(id);
    if (!document.getElementById(`db-card-${id}`)) return;  // not currently rendered
    const db = state.databases.find(d => d.id === id);
    if (!db || !dbIsTransitioning(db.status)) return;
    const logEl = document.getElementById(`db-log-${id}`);
    if (logEl) logEl.classList.remove("hidden");
    pollDbCardTick(id, token);
}

async function pollDbCardTick(id, token) {
    if (token !== state.dbLoadToken) return;
    await refreshDbCardStatus(id, token);
    await fetchDbLogs(id, token);
    if (token !== state.dbLoadToken) return;
    const db = state.databases.find(d => d.id === id);
    if (db && dbIsTransitioning(db.status)) {
        state.dbPollers[id] = setTimeout(() => pollDbCardTick(id, token), DB_POLL_MS);
    } else {
        stopDbPolling(id);  // reached a terminal state — settle the card
    }
}

// Fetch one DB's live status and merge the volatile fields into state, then
// re-render just that card. Transient errors are swallowed — next tick retries.
async function refreshDbCardStatus(id, token) {
    let st;
    try {
        st = await api(`/databases/${encodeURIComponent(id)}/status`);
    } catch (e) {
        return;
    }
    if (token !== state.dbLoadToken || !st || st.error) return;
    const db = state.databases.find(d => d.id === id);
    if (!db) return;
    if (st.status) db.status = st.status;
    if (st.instanceStatus !== undefined) db.instanceStatus = st.instanceStatus;
    db.isRunning = !!st.isRunning;
    if (st.versionId) db.versionId = st.versionId;
    if (st.instanceId) db.instanceId = st.instanceId;
    if (st.url) db.url = st.url;
    renderDbCardBody(id);
}

// Pull new real-time log lines (incremental by offset, deduped by timestamp)
// and append them to the card's log panel.
async function fetchDbLogs(id, token) {
    const db = state.databases.find(d => d.id === id);
    const logEl = document.getElementById(`db-log-${id}`);
    if (!db || !logEl) return;
    const offset = state.dbLogOffsets[id] || 0;
    const params = new URLSearchParams({ offset: String(offset) });
    if (db.versionId)  params.set("versionId", db.versionId);
    if (db.instanceId) params.set("instanceId", db.instanceId);
    let data;
    try {
        data = await api(`/databases/${encodeURIComponent(id)}/logs?${params.toString()}`);
    } catch (e) {
        return;
    }
    if (token !== state.dbLoadToken) return;
    const lines = (data && data.logContent) || [];
    const lastSeen = state.dbLogSeen[id] || 0;
    const fresh = lines.filter(l => (l.timestamp || 0) > lastSeen);
    if (fresh.length) {
        appendDbLogLines(logEl, fresh);
        state.dbLogSeen[id] = Math.max(lastSeen, ...fresh.map(l => l.timestamp || 0));
        logEl.classList.remove("hidden");
    }
    state.dbLogOffsets[id] = offset + lines.length;
}

function appendDbLogLines(logEl, lines) {
    lines.forEach(item => {
        // Logs carry carriage returns / progress redraws — strip to one clean line.
        const text = String(item.log || "").replace(/\r/g, "").replace(/\n+$/, "");
        if (!text.trim()) return;
        const span = document.createElement("span");
        if (item.logType === "stderr") span.className = "err";
        span.textContent = text;
        logEl.appendChild(span);
        logEl.appendChild(document.createTextNode("\n"));
    });
    logEl.scrollTop = logEl.scrollHeight;
}

async function toggleDbLogs(id) {
    const logEl = document.getElementById(`db-log-${id}`);
    if (!logEl) return;
    const willShow = logEl.classList.contains("hidden");
    logEl.classList.toggle("hidden");
    if (willShow && !logEl.childNodes.length) {
        await fetchDbLogs(id, state.dbLoadToken);
        if (!logEl.childNodes.length) logEl.textContent = "(no logs available yet)";
    }
}

function stopDbPolling(id) {
    if (state.dbPollers[id]) {
        clearTimeout(state.dbPollers[id]);
        delete state.dbPollers[id];
    }
}
function stopAllDbPolling() {
    Object.keys(state.dbPollers).forEach(stopDbPolling);
}
// Re-entering the databases tab: resume polling any still-spinning-up cards.
function rearmDbPolling() {
    state.databases.forEach(db => maybePollDbCard(db.id));
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
const ENV_POLL_MS = 4000;  // re-check cadence for cards with an in-progress build

// Two-phase load: fetch the cheap spec list, render every card shell at once,
// then fetch each card's v4 detail independently so cards fill in (and fail,
// and poll) on their own timeline instead of all-at-once.
async function loadEnvironments() {
    const container = document.getElementById("env-cards");
    stopAllEnvPolling();                 // cancel pollers from a previous load
    const token = ++state.envLoadToken;  // invalidate any in-flight per-card fetches

    container.innerHTML = `<p class="muted">Loading environments…</p>`;
    let specs;
    try {
        specs = await api("/environments/specs");
    } catch (e) {
        container.innerHTML = `<p class="muted">Failed to load: ${escapeHtml(e.message)}</p>`;
        return;
    }
    if (token !== state.envLoadToken) return;  // a newer load superseded us
    state.envsLoaded = true;

    if (!specs || !specs.length) {
        container.innerHTML = `<p class="muted">No engines registered.</p>`;
        return;
    }

    // Phase 1 — render every card shell immediately in a "loading" state.
    state.envCards = {};
    container.innerHTML = specs.map(s => {
        state.envCards[s.name] = s;
        return envCardShell(s);
    }).join("");

    // Phase 2 — fetch each card's detail in parallel; each updates on arrival.
    specs.forEach(s => refreshEnvCard(s.name, token));
}

// Fetch one engine's v4 detail and update only that card. Fault-isolated:
// a failure renders a per-card error+retry, never disturbing other cards.
async function refreshEnvCard(engine, token = state.envLoadToken) {
    let detail;
    try {
        detail = await api(`/environments/${encodeURIComponent(engine)}/status`);
    } catch (e) {
        if (token !== state.envLoadToken) return;
        renderEnvCardBody(engine, { error: e.message });
        return;
    }
    if (token !== state.envLoadToken) return;  // stale load — drop the result
    state.envCards[engine] = detail;
    if (detail.error) {
        renderEnvCardBody(engine, { error: detail.error });
        return;
    }
    renderEnvCardBody(engine, {});
    maybePollEnvCard(engine, token);
}

// Arm a one-shot poll if this card's build is still queued/building. Each tick
// re-fetches and re-arms via refreshEnvCard until the build reaches a terminal
// state, so only in-progress cards poll and they stop on their own.
function maybePollEnvCard(engine, token = state.envLoadToken) {
    stopEnvPolling(engine);
    const e = state.envCards[engine];
    const st = ((e && e.latestRevision && e.latestRevision.status) || "").toLowerCase();
    if (!["queued", "building"].includes(st)) return;
    state.envPollers[engine] = setTimeout(() => {
        if (token !== state.envLoadToken) return;
        refreshEnvCard(engine, token);
    }, ENV_POLL_MS);
}

function stopEnvPolling(engine) {
    if (state.envPollers[engine]) {
        clearTimeout(state.envPollers[engine]);
        delete state.envPollers[engine];
    }
}

function stopAllEnvPolling() {
    Object.keys(state.envPollers).forEach(stopEnvPolling);
}

// Re-entering an already-loaded tab: resume polling for any in-progress cards.
function rearmEnvPolling() {
    Object.keys(state.envCards).forEach(engine => maybePollEnvCard(engine));
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

// Full card shell: a stable wrapper holding the dynamic body plus the two
// <pre> panels (dockerfile + build log). The body is rebuilt on every update,
// but the <pre>s are siblings we never touch — so an open dockerfile view or a
// streaming build log survives status refreshes untouched.
function envCardShell(e) {
    const dfId = `df-${e.name}`;
    const logId = `env-log-${e.name}`;
    const dfEscaped = escapeHtml(e.dockerfile || "(no Dockerfile found)");
    return `
    <div class="env-card" id="env-card-${escapeHtml(e.name)}">
        <div class="env-card-body" id="env-body-${escapeHtml(e.name)}">
            ${envBodyHtml(e, { loading: true })}
        </div>
        <pre class="provision-log dockerfile-view hidden" id="${dfId}">${dfEscaped}</pre>
        <pre class="provision-log hidden" id="${logId}"></pre>
    </div>`;
}

// Replace only one card's body — identity, status, meta, actions — leaving its
// sibling <pre> panels (and every other card) alone.
function renderEnvCardBody(engine, opts = {}) {
    const body = document.getElementById(`env-body-${engine}`);
    if (!body) return;
    body.innerHTML = envBodyHtml(state.envCards[engine] || {}, opts);
}

// Renders the card body for one of three states:
//   loading — skeleton fields known from the specs call, detail pending
//   error   — detail fetch failed; show message + Retry
//   ready   — full v4 detail present
function envBodyHtml(e, opts = {}) {
    const loading = !!opts.loading;
    const error = opts.error || "";

    const icon = e.iconUrl
        ? `<img class="engine-icon-img" src="${escapeHtml(e.iconUrl)}" alt="${escapeHtml(e.label)}" style="width:36px;height:36px;">`
        : e.icon
            ? `<span class="engine-icon" style="font-size:32px;">${escapeHtml(e.icon)}</span>`
            : "";

    let statusEl;
    if (error)        statusEl = `<span class="badge badge-error" title="${escapeHtml(error)}">Error</span>`;
    else if (loading) statusEl = `<span class="badge badge-loading">Loading…</span>`;
    else              statusEl = envStatusBadge(e.envIdSource, e.latestRevision);

    const ready = !loading && !error;
    const revNum = (ready && e.revisionNumber != null) ? `v${e.revisionNumber}` : "";
    const buildLabel = e.envIdSource === "missing" ? "Build" : "Rebuild";

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

    // Detail rows depend on v4 data — placeholder while loading, retry on error.
    let detailRows;
    if (error) {
        detailRows = `
            <div class="env-meta-row env-detail-error">
                <span class="muted">Couldn't load details — ${escapeHtml(error)}</span>
                <button class="btn btn-secondary btn-small env-retry-btn" onclick="refreshEnvCard('${escapeHtml(e.name)}')">Retry</button>
            </div>`;
    } else if (loading) {
        detailRows = `<div class="env-meta-row env-detail-loading"><span class="env-shimmer"></span></div>`;
    } else {
        const baseImageTag = e.imageDisplay
            ? (e.imageDisplay.includes(":") ? e.imageDisplay.split(":")[1] : e.imageDisplay)
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
        detailRows = baseImageRow + sizeRow + ownerRow + updatedRow;
    }

    return `
        <div class="env-card-header">
            <div class="env-card-identity">
                ${icon}
                <div>
                    <div class="env-card-label">${escapeHtml(e.label)}</div>
                    <code class="env-card-name">${escapeHtml(e.expectedEnvName)}</code>
                </div>
            </div>
            <div class="env-card-status">
                ${statusEl}
                ${revNum ? `<span class="env-rev-chip" id="env-rev-${escapeHtml(e.name)}">${escapeHtml(revNum)}</span>` : ""}
                ${(ready && e.envUrl) ? `<a class="btn btn-secondary btn-small env-open-btn" href="${escapeHtml(e.envUrl)}" target="_blank" rel="noopener">Open Environment</a>` : ""}
            </div>
        </div>
        <div class="env-card-meta">
            ${detailRows}
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
        </div>`;
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

    stopEnvPolling(engine);  // don't let a status poll re-render the body mid-build
    logEl.classList.remove("hidden");
    logEl.textContent = "";
    btn.disabled = true;

    const card = document.getElementById(`env-card-${engine}`);
    if (card) { card.classList.remove("build-success", "build-failed"); card.classList.add("building"); }

    // Append a log line via DOM nodes (textContent) rather than innerHTML += so
    // each line is O(1) and we never re-parse the whole growing log.
    const append = (cls, marker, msg, extra) => {
        const line = document.createElement("span");
        if (cls) line.className = cls;
        line.textContent = `${marker} ${msg}`;
        logEl.appendChild(line);
        if (extra) {
            const ex = document.createElement("span");
            ex.className = "muted";
            ex.textContent = ` (${extra})`;
            logEl.appendChild(ex);
        }
        logEl.appendChild(document.createTextNode("\n"));
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

    if (card) {
        card.classList.remove("building");
        const succeeded = terminal && terminal.kind === "result" &&
            (terminal.data?.status || "").toLowerCase() === "succeeded";
        card.classList.add(succeeded ? "build-success" : "build-failed");
        setTimeout(() => card.classList.remove("build-success", "build-failed"), 2200);
    }

    if (terminal && terminal.kind === "result") {
        // Refresh only this card — its sibling <pre> log stays visible, and
        // every other card (and its open log / dockerfile view) is untouched.
        const prev = state.envCards[engine];
        const oldRev = prev ? prev.revisionNumber : null;
        await refreshEnvCard(engine);
        const newRev = state.envCards[engine] ? state.envCards[engine].revisionNumber : null;
        if (newRev != null && newRev !== oldRev) {
            const chip = document.getElementById(`env-rev-${engine}`);
            if (chip) {
                chip.classList.remove("rev-pop");
                void chip.offsetWidth;  // force reflow to restart the animation
                chip.classList.add("rev-pop");
                chip.addEventListener("animationend", () => chip.classList.remove("rev-pop"), { once: true });
            }
        }
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
            if (tab === "environments") {
                if (!state.envsLoaded) loadEnvironments();
                else rearmEnvPolling();   // resume polling any in-progress builds
            } else {
                stopAllEnvPolling();       // don't poll while the tab is hidden
            }
            if (tab === "databases") rearmDbPolling();  // resume spinning-up cards
            else stopAllDbPolling();                    // pause while hidden
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
    document.getElementById("filter-engine").onchange = renderDbCards;
    document.getElementById("filter-status").onchange = renderDbCards;
    document.getElementById("filter-search").oninput  = renderDbCards;

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
