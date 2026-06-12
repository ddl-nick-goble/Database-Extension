# Databases (and Arbitrary Servers) as Domino Apps

### An "Open-TCP-over-the-App-Channel" Architecture and Implementation Plan

**Status:** Design proposal — grounded in verified Domino behavior, with an explicit register of assumptions still to confirm against the codebase.
**Author/owner:** Nick Goble (Domino)
**Primary driver:** USAA skunk-works opportunity (contact: Avakian) — "can Domino spin up a server to host a full-stack application?"
**Date:** 2026-06-12

---

## 0. How to read this document

This is deliberately verbose. It is meant to be a *robust* handoff artifact — something the platform/engineering team can pick up and build from, and something whose security posture can be defended in front of a regulated bank's security organization. It is split into three layers:

1. **The "why" and the "what"** — motivation, the core design idea, and the architecture (Sections 1–6).
2. **The "what if it's more than databases"** — generalization to arbitrary TCP services, which is what the USAA ask is actually about (Section 7).
3. **The "how" and the "what if we're wrong"** — security framing, the register of unverified assumptions, the phased implementation plan, and — critically — explicit *fallback plans* for every place we might get stuck (Sections 8–12).

Throughout, claims that are **verified** against Domino's documented behavior are marked **[VERIFIED]**. Claims that are **assumed** (Nick believes them true but we have not confirmed against the codebase) are marked **[ASSUMED]**. This honesty is intentional: the difference between "works today" and "needs one platform change" lives entirely in that distinction.

---

## 1. Executive summary

Domino already knows how to do one thing extremely well: take a container, run it as an authenticated web **App**, route traffic to it through its proxy regardless of which cluster/data-plane it landed in, and enforce who is allowed to reach it. That machinery is HTTP/L7 only.

Databases — and full-stack application backends generally — do not speak HTTP. They speak raw TCP wire protocols (Postgres on 5432, etc.). The naive instinct is to teach Domino to "open TCP ports," which means new ingress, new network paths, new security surface, and a fight with the platform's networking and the security org of any regulated customer.

**This design refuses that fight.** Instead of building new typed, explicit, port-opening infrastructure, we *overload the channel Domino already has*. We wrap the database's TCP stream inside a WebSocket and serve it from a perfectly ordinary Domino App. A small client on the consumer side unwraps it back into a local TCP socket. To Domino, a "database" is just another web app on port 8888. To the user, it's `localhost:5432`.

The design metaphor (Nick's) is exact: rather than defining a narrow function with an explicit, typed signature for "a database service," we use the equivalent of `eval()` — a single generic, overloaded channel that can carry *any* TCP payload. Databases are simply the first thing we pass through it. The same mechanism hosts a message queue, a gRPC service, or the backend of a full-stack app tomorrow, with zero new platform primitives.

**Consequences of this choice, all favorable:**

- **No central server.** Each database is self-contained. There is no frp-style hub to operate, scale, secure, or make highly available.
- **No new ingress.** Nothing opens a raw port. Traffic rides the existing app proxy.
- **Cross-plane for free.** The app proxy already routes to wherever the app runs, across data planes. We inherit that.
- **Auth for free.** Domino already authenticates and authorizes app access and injects identity headers. We inherit the entire access-control model — the single strongest thing we can bring to a USAA security review.
- **Discovery for free.** The list of running databases *is* the list of running apps on a designated "database environment," queryable through the Domino API. Domino's own metadata is our service registry.

**Honest sizing.** A demo that wins Avakian over is a **days-level** build. A productized, hardened, security-signed-off feature is **multiple engineering-weeks**, and its long pole is *platform-team buy-in and the customer's security review*, not code difficulty. Do not conflate the two when setting expectations.

---

## 2. Background and motivation

### 2.1 The opportunity

Nick met with a leader of a skunk-works division at USAA (Avakian) who has internal pull. He explicitly asked whether Domino can help spin up a server to host a full-stack application, was enthusiastic that Domino is built on Docker, and is frustrated that USAA already pays for separate app-hosting infrastructure. He indicated he would push internally if he likes what he sees. This is a wedge: a credible demo plus a defensible security story could turn into real adoption of Domino as an application-hosting platform, not just a model-training one.

### 2.2 What Domino Apps are today **[VERIFIED]**

A Domino App is a containerized workload published behind Domino's authenticating HTTP reverse proxy. Key documented facts:

- Apps must bind `0.0.0.0` and listen on **port 8888**.
- Apps are served under a path prefix of the form `/apps/<app-uuid>/` (example observed: `https://fsi-demo.domino-eval.com/apps/c81f1982-0f17-473e-97e6-ac043dcffafb/`).
- Domino **does not strip** that prefix before forwarding to the container. The app must be aware of its own base path. This is why every framework integration requires base-path configuration (Dash's `requests_pathname_prefix`, Streamlit's route prefix, etc.).
- Domino injects the prefix into the container at runtime as the environment variable **`DOMINO_RUN_HOST_PATH`**.
- Domino injects **identity headers** into proxied requests, which is why Flask apps "just work" with Domino identity out of the box.

### 2.3 How app-to-app communication works today

There is no first-class "link app A to app B" primitive in Domino. In practice, if one workload needs to reach an app, it finds the app's URL and speaks that app's HTTP API. This is a constraint we explicitly **embrace rather than replace**: our design makes a database reachable through the same "find the URL, speak to it" model that already exists. We are adding a *protocol* (TCP-over-WebSocket) on top of the existing channel, not a new linking primitive.

### 2.4 Why this is hard (the core mismatch)

| Property | Domino App channel | Database |
|---|---|---|
| Protocol | HTTP/L7, WebSocket-capable | Raw TCP wire protocol |
| Port | 8888, behind proxy | Native (e.g. 5432), wants direct socket |
| Auth | Proxy-enforced, identity headers | DB-native auth only |
| Routing | Path-prefixed, cross-plane | Expects a host:port |
| Lifecycle | Deployment-style, restartable | Stateful, wants stable storage/identity |

The architecture exists to bridge the top four rows by overloading the channel. The fifth row (state/lifecycle) is the genuinely hard production problem and is treated honestly in Sections 9 and 10.

### 2.5 Deployment-topology constraint **[VERIFIED + decision]**

Domino Nexus deployments consist of a **control plane** (one Kubernetes cluster running platform services) and one or more **data planes** (separate clusters/namespaces that execute user workloads), connected over private links (PrivateLink-style), with **Istio** managing service-to-service traffic inside each plane as a mesh.

**Decision:** We assume a database and its consumer **cannot be guaranteed to live in the same data plane.** This single decision eliminates "just use a Kubernetes ClusterIP" as a baseline (that only works intra-plane and still must satisfy Istio mesh policy). It is also what makes riding the app proxy so attractive: the proxy is *already* the sanctioned cross-plane path, and it is HTTP — exactly what our overloaded channel speaks.

---

## 3. Design philosophy: overload the channel, don't widen the platform

Three principles drive every downstream decision.

**Principle 1 — Overload, don't extend (the `eval` principle).**
We do not add database-shaped features to Domino. We pass arbitrary TCP through the one generic, authenticated, cross-plane channel Domino already has. A database is not a new kind of object in Domino's world; it is an App that happens to carry a Postgres stream. This keeps the platform surface area flat and makes the mechanism reusable for anything that speaks TCP.

**Principle 2 — Decentralize; no shared fate.**
There is no central tunnel server. Every database carries its own tunnel endpoint inside its own app container. If one database dies, nothing else is affected. There is no hub to scale or make HA. This is the explicit rejection of the frp model (see Appendix A for why).

**Principle 3 — Inherit, don't reinvent.**
Auth, authorization, cross-plane routing, identity, and discovery already exist in Domino. We deliberately route everything through those existing mechanisms so that (a) we write less code and (b) the security story is "we use Domino's existing controls," which is vastly easier to defend than "we built a new access path."

**Non-goals (explicitly out of scope for v1):**
- Replacing managed database services for production systems of record.
- A first-class app-linking UI primitive (we reuse URL+discovery instead).
- Exposing databases to the public internet (design is internal-only by intent).
- Automated database backups/HA as a platform feature (addressed as a risk, not a v1 deliverable).

---

## 4. Architecture overview

### 4.1 The pattern in one paragraph

A **database** is published as a normal Domino App. Its container image is the database environment plus an embedded **wstunnel server**. On startup the container runs the database bound to `localhost` and runs wstunnel bound to `0.0.0.0:8888`, configured to accept WebSocket upgrades on the path Domino assigned it (`$DOMINO_RUN_HOST_PATH`). A **consumer** (notebook, job, another app, or a human with a desktop client) runs a small **wstunnel client** pointed at that app's URL; the client opens a local TCP socket (e.g. `localhost:5432`) and tunnels it through the WebSocket to the database. Consumers find which databases exist by querying the **Domino API** for running apps on the designated database environment.

### 4.2 Component diagram

```
                          DATA PLANE A                         DATA PLANE B
                 ┌──────────────────────────────┐    ┌──────────────────────────────┐
                 │  DB App pod (Domino App)      │    │  Consumer workload pod        │
                 │                               │    │  (notebook / job / app)       │
                 │  ┌────────────┐  localhost    │    │                               │
                 │  │ PostgreSQL │◄────5432────┐  │    │  psql / DBeaver / SQLAlchemy  │
                 │  └────────────┘             │  │    │          │ localhost:5432     │
                 │  ┌──────────────────────┐   │  │    │  ┌───────▼─────────────┐     │
                 │  │ wstunnel SERVER       │───┘  │    │  │ wstunnel CLIENT     │     │
                 │  │ 0.0.0.0:8888          │      │    │  │ + discovery helper  │     │
                 │  │ path=$DOMINO_RUN_HOST │      │    │  └───────┬─────────────┘     │
                 │  └──────────▲────────────┘      │    └──────────┼───────────────────┘
                 └─────────────┼───────────────────┘               │
                               │  WebSocket (TCP-in-WS) over HTTPS  │
                               │                                    │
                       ┌───────┴────────────────────────────────────┴───────┐
                       │     DOMINO APP PROXY  (control plane / front door)   │
                       │  • authenticates caller, injects identity headers    │
                       │  • routes /apps/<uuid>/ to the right pod, any plane   │
                       │  • already the sanctioned cross-plane path            │
                       └──────────────────────────────────────────────────────┘
                               ▲
                               │ Domino API: "list running apps on the
                               │ DB environment" → URLs  (= discovery)
```

### 4.3 The four moving parts

1. **DB environment image** — base database + wstunnel server binary + an entrypoint that wires `DOMINO_RUN_HOST_PATH` into the tunnel's upgrade path. Published as a Domino Environment so any user can launch "a database" as an App.
2. **wstunnel server (embedded)** — terminates the WebSocket, forwards the unwrapped TCP stream to `localhost:<db-port>` inside the same pod.
3. **Consumer client** — a thin CLI/library wrapping the wstunnel client; given an app URL and a Domino credential, opens a local TCP listener and tunnels to the DB app.
4. **Discovery helper** — a thin wrapper over the Domino API that lists running apps filtered to the database environment and returns `(name, owner, project, url)` tuples the consumer client can connect to.

### 4.4 The end-to-end connection flow

1. User (or automation) calls the discovery helper → Domino API returns the databases they are *authorized* to see, with URLs.
2. User picks one. The consumer client starts: it authenticates to Domino (browser session or API token), opens a WebSocket to `https://<host>/apps/<uuid>/<wstunnel-path>`, and begins listening on `localhost:5432`.
3. The app proxy authenticates the request, injects identity headers, and routes the WebSocket to the DB app pod in whatever plane it lives.
4. The embedded wstunnel server accepts the upgrade and bridges the stream to `localhost:5432` (Postgres) inside the pod.
5. The user points `psql`/DBeaver/SQLAlchemy at `localhost:5432`. From their perspective it is an ordinary local Postgres.

---

## 5. Detailed component specifications

> The code sketches below are **illustrative**, to make the design concrete for the implementing team. Exact flags, base images, and Domino Environment mechanics must be confirmed during the Phase-0 spike.

### 5.1 DB environment image

A Domino Environment (Dockerfile) roughly of the form:

```dockerfile
FROM <domino-standard-base>

# 1. Install the database engine (Postgres shown; parameterize for others)
RUN apt-get update && apt-get install -y postgresql && rm -rf /var/lib/apt/lists/*

# 2. Install the tunnel server (single static binary — see Appendix A)
ARG WSTUNNEL_VERSION=<pinned>
RUN curl -fsSL <wstunnel-release-url> -o /usr/local/bin/wstunnel \
 && chmod +x /usr/local/bin/wstunnel

# 3. Entrypoint that wires DOMINO_RUN_HOST_PATH into the tunnel path
COPY app.sh /opt/db-app/app.sh
RUN chmod +x /opt/db-app/app.sh
```

`app.sh` (the Domino app launch script):

```bash
#!/usr/bin/env bash
set -euo pipefail

# Start the database, bound to loopback only — never directly exposed.
pg_ctl -D "$PGDATA" -o "-c listen_addresses=localhost -p 5432" -w start

# Domino tells us our public path prefix at runtime.
UPGRADE_PATH="${DOMINO_RUN_HOST_PATH:-/}"

# Tunnel server: accept WS upgrades on our assigned path, bridge to local DB.
exec wstunnel server \
  --restrict-http-upgrade-path-prefix "${UPGRADE_PATH}" \
  --bind 0.0.0.0:8888
# (consumers request the local-to-remote forward 5432->localhost:5432 at connect time)
```

Design notes:
- The database **only ever binds to loopback inside the pod.** The sole path in is the authenticated WebSocket. There is no DB port exposed to any network.
- `8888` and `0.0.0.0` satisfy Domino's app contract. **[VERIFIED]**
- The wstunnel upgrade path is set from `DOMINO_RUN_HOST_PATH` so it lines up with whatever UUID-bearing prefix Domino assigns this launch. **[VERIFIED that the env var carries the prefix; ASSUMED that wstunnel's path-restriction flag composes cleanly with it — confirm in spike.]**

### 5.2 wstunnel server (embedded)

Responsibilities: accept the WebSocket upgrade on the correct path; forward the bridged TCP to `localhost:<db-port>`; emit logs/metrics; exit cleanly when the app stops. wstunnel is chosen over chisel/frp specifically because its explicit `--http-upgrade-path-prefix` maps one-to-one onto Domino's `DOMINO_RUN_HOST_PATH` model (Appendix A).

### 5.3 Consumer client

A small wrapper (CLI + importable library) over the wstunnel client:

```
db-connect <database-name-or-url> [--local-port 5432]
```

Behavior:
- Resolves a friendly name to an app URL via the discovery helper (5.4), or accepts a URL directly.
- Acquires a Domino credential: a desktop user rides their browser session; a notebook/job uses a Domino API token. **[ASSUMED: apps are reachable with an API token, not only a browser session — confirm in spike.]**
- Opens the WebSocket to `<app-url>/<wstunnel-path>` and binds `localhost:<local-port>`.
- Prints the local DSN (`postgresql://localhost:5432/...`) for the user to paste into any tool.

Packaging it both as a CLI and a Python library matters: humans want the CLI; notebooks/jobs want `with db_connect("sales-db") as dsn: ...`.

### 5.4 Discovery helper

A thin function over the Domino API:

- Calls the "list running apps" endpoint.
- Filters to apps whose Environment is the designated **database environment** (a specific Domino Environment we tag/reserve for this purpose).
- Returns `(name, owner, project, url, status)` for each.
- Returns only what the caller is **authorized** to see — because the Domino API already enforces visibility/permissions, access control is inherited, not reimplemented.

**[ASSUMED:** the apps API returns both the environment identifier *and* the reachable URL in a single response. Almost certainly true but unconfirmed — a 5-minute check in the spike.**]**

---

## 6. Authentication, authorization, and network model

- **Authentication:** handled by the Domino app proxy on every request to the DB app. No anonymous path exists; the WebSocket upgrade itself is an authenticated request.
- **Authorization (who can reach which DB):** governed by Domino app sharing/permissions and reflected in what the discovery API returns. To grant team X access to database Y, you share app Y with team X — using the exact mechanism admins already understand.
- **Identity:** Domino injects identity headers into the proxied request. The DB app can, optionally, use these to enforce per-user database roles (a hardening item, not v1).
- **Transport security:** the consumer→proxy hop is HTTPS/WSS. The proxy→pod hop rides Domino's existing in-cluster transport (Istio mesh, frequently mTLS). End to end, no plaintext DB traffic crosses a network boundary.
- **Network exposure:** **zero new ingress.** The data plane opens no inbound ports. The DB binds loopback only. This is the property that makes the security story defensible.

---

## 7. Generalization: this was never really about databases

The `eval`/overloading principle pays off here. Nothing in Sections 4–6 is Postgres-specific. The mechanism carries an arbitrary TCP stream. Therefore the same pattern hosts:

- **Other databases** — MySQL, Mongo, Redis, etc. (swap the engine in the image; change the loopback port).
- **Message brokers / caches** — anything with a TCP protocol.
- **gRPC / custom binary services.**
- **The backend of a full-stack application** — which is *exactly what Avakian asked for*: "spin up a server to host a full-stack application." A web frontend can be a normal Domino App already; its backend services (DB, API server on a non-HTTP port, websocket service) ride this same overloaded channel.

This is the strategic punchline for the USAA conversation. We are not pitching "Domino can host Postgres." We are pitching **"Domino can host arbitrary servers — full application stacks — using infrastructure USAA already trusts, with no new ingress and Domino's existing auth,"** and databases are simply the first, most legible proof of it. The recommendation is to **build the database case first** (concrete, demoable, obviously useful) but **write and present the design as the general case**, so the platform team builds a reusable primitive and Avakian sees the bigger picture.

A v2 productization could expose a generic "TCP service" app type with a declared internal port, of which "database" is a preset — but that is an ergonomic wrapper over the identical mechanism, not new plumbing.

---

## 8. Security architecture (framed for a regulated bank)

This section is written to be liftable, nearly verbatim, into a one-pager for USAA's security organization.

**Posture summary.** Databases hosted on Domino are reachable only through Domino's existing authenticated application proxy. No new network ingress is created. The database process binds only to pod loopback. All access is authenticated and authorized by Domino's existing controls, and all transport is encrypted.

**Threat-model highlights:**

- *Unauthorized network access:* No raw port is exposed anywhere; the only route to the database is an authenticated WebSocket through the proxy. An attacker on the network sees no database listener.
- *Unauthorized user access:* A caller must authenticate to Domino and must have been granted access to that specific app. Discovery returns nothing the caller is not authorized to see.
- *Lateral movement between tenants:* Each database is an isolated app/pod; there is no shared tunnel server to compromise. Istio mesh policy governs intra-plane traffic.
- *Data in transit:* HTTPS/WSS consumer→proxy; Istio (frequently mTLS) proxy→pod. No plaintext DB protocol crosses a boundary.
- *Data residency / plane locality:* Because routing rides the app proxy and the database runs in a data plane, data can be kept in a chosen region/plane; the control plane brokers connections without necessarily proxying bulk data through a foreign region (confirm specifics per deployment).
- *Auditability:* App access is logged by the proxy under the authenticated identity, inheriting Domino's existing audit trail.

**The honest caveats we will put in front of security ourselves** (volunteering these is what earns trust):
- Database-native authn/authz still applies *inside* the tunnel and must be configured; the tunnel controls reachability, not in-database privileges.
- Long-lived connections interact with proxy timeouts (Section 9) — to be validated, not assumed.
- Durable storage and backup/restore for stateful databases is a deployment responsibility (Section 9/10), not something the transport solves.

---

## 9. Register of assumptions to verify (the make-or-break list)

These are ordered by how badly a wrong answer hurts. Phase 0 exists to burn this list down.

1. **WebSocket longevity through the proxy. [ASSUMED]**
   Does the Domino app proxy pass WebSocket upgrades *and keep long-lived, low-traffic connections alive* without buffering or idle-timeout kills? Dashboards use WebSockets, so upgrades almost certainly pass; database sessions stay open far longer and idler, so the *timeout* behavior is the real question.
   *Impact if false:* connections drop mid-session. Mitigation in Section 10.

2. **Programmatic (token) access to apps. [ASSUMED]**
   Can a notebook/job authenticate to a published app with a Domino API token rather than an interactive browser session?
   *Impact if false:* only humans-in-browsers can connect; automation can't. Mitigation in Section 10.

3. **Discovery API completeness. [ASSUMED]**
   Does the apps API return environment ID *and* reachable URL together, and can we reliably tag/identify "the database environment"?
   *Impact if false:* discovery needs a second call or a different filter. Low risk.

4. **Stateful storage & lifecycle. [OPEN]**
   Domino Apps are deployment-style and generally treat container filesystem as ephemeral across restarts. A database needs durable storage that survives restart/reschedule, plus a backup/restore story. What persistent volume / Domino Dataset semantics are available to an App, and with what durability and concurrency guarantees?
   *Impact:* this is the biggest gap between "great demo" and "production system of record." Mitigation in Section 10.

5. **Istio mesh policy on the proxy→pod hop. [OPEN]**
   Does mesh `PeerAuthentication`/`AuthorizationPolicy` permit the proxied WebSocket to reach the app pod as expected? (Normal apps work, so this is likely fine, but our long-lived TCP-in-WS is an atypical traffic shape.)

6. **Path composition. [PARTIALLY VERIFIED]**
   We know `DOMINO_RUN_HOST_PATH` carries the prefix and the prefix is not stripped. We have *not* confirmed wstunnel's path-restriction flag composes with it exactly. Cheap to verify.

---

## 10. Risks and fallback plans ("what we do if we're stuck")

For each major risk: the **primary** approach, then ordered **fallbacks**. This is the "plan to beg if we're stuck" Nick asked for — every dead end has a marked exit.

### 10.1 Risk: proxy kills long-lived WebSockets (Assumption 1 false)
- **Primary:** rely on the proxy passing WS as-is.
- **Fallback A:** enable wstunnel/application-level **keepalive/heartbeat** pings to keep the connection under any idle threshold; make the consumer client auto-reconnect transparently and pool connections so a drop is invisible to `psql`.
- **Fallback B:** tune the proxy's idle timeout for the database environment specifically (a platform config change, not a redesign).
- **Fallback C:** switch transport from raw WS to **HTTP/2 streaming** (wstunnel supports it) if the proxy treats it more favorably.
- **Fallback D (last resort, scoped):** for *same-plane* consumers only, bypass the proxy with an Istio-meshed ClusterIP path; keep the proxy path for cross-plane. This reintroduces the intra-plane case as an optimization, not the baseline.

### 10.2 Risk: no programmatic token access to apps (Assumption 2 false)
- **Primary:** consumer client sends a Domino API token; proxy honors it.
- **Fallback A:** mint a short-lived, **scoped service-account token** for automation and inject it the way other Domino jobs authenticate to platform services.
- **Fallback B:** a tiny auth-broker sidecar in the consumer workload that performs the Domino login flow and attaches the resulting session to outbound tunnel requests.
- **Fallback C:** restrict v1 automation to *same-plane* consumers using in-cluster identity, and ship cross-plane automation in v2 once the token path is built.

### 10.3 Risk: stateful storage / durability (Assumption 4 — the hard one)
- **Primary (v1 demo):** ephemeral or project-scratch storage — perfect for demos, dev/test databases, and "scratch a dataset" use cases. Be explicit that this is not a system of record.
- **Fallback / productization A:** back the database's data directory with a **Domino Dataset** or a dedicated **PersistentVolume**, and run the DB app with StatefulSet-like guarantees (stable storage, single-writer). Requires confirming what volume semantics an App can mount.
- **Fallback B:** **separate compute from state** — the Domino "database app" is a *gateway/connection broker* in front of an externally managed datastore (RDS/CloudSQL/etc. inside the customer's account). The overloaded-channel transport is unchanged; only the backing store moves. This is likely the right answer for production systems of record at a bank and is worth pitching as the "serious" tier.
- **Fallback C:** position hosted-in-Domino databases explicitly as **dev/test/ephemeral** and document managed-DB integration as the production path. Honest scoping beats overpromising to a bank.

### 10.4 Risk: Istio mesh blocks the traffic (Assumption 5)
- **Primary:** normal app mesh policy permits it.
- **Fallback A:** add a targeted `AuthorizationPolicy` permitting the proxy→DB-app WebSocket.
- **Fallback B:** run the DB app with the appropriate mesh sidecar configuration / or as a mesh-excluded workload reached only via the proxy.

### 10.5 Risk: "this is a hack" perception (internal Domino + USAA security)
- **Primary:** lead with the framing in Sections 3 and 8 — overloading a *trusted, authenticated* channel is an architectural choice, not a workaround; wstunnel/frp-class tools are mature and widely used in production.
- **Fallback:** offer the v2 "first-class generic TCP service app type" as the productized face, with the overloaded channel as its sanctioned internal mechanism.

### 10.6 Risk: performance / throughput
- **Primary:** acceptable for interactive analytics and dev/test (the target use cases).
- **Fallback:** document throughput/latency from the Phase-0 benchmark; for high-throughput needs, point to the managed-store gateway model (10.3 Fallback B) which can place the heavy data path closer to the consumer.

---

## 11. Phased implementation plan

Effort figures are deliberately rough and assume one engineer who knows Domino internals plus access to a test deployment. They size *engineering*; they do **not** include the USAA security-review calendar time, which is the true long pole and is owned by the customer.

### Phase 0 — Verification spike (burn down Section 9) — ~2–4 days
**Goal:** convert every **[ASSUMED]/[OPEN]** into **[VERIFIED]** or a chosen fallback.
- Stand up one Postgres+wstunnel app by hand; connect a wstunnel client; run `psql`.
- Hold a connection open and idle for 5/15/30/60 min to probe proxy timeouts (Assumption 1).
- Connect with an API token from a headless context (Assumption 2).
- Inspect the apps API response shape for env ID + URL (Assumption 3).
- Confirm volume options an App can mount (Assumption 4).
- Confirm path composition with `DOMINO_RUN_HOST_PATH` (Assumption 6).
**Exit criteria:** a written go/no-go with each assumption resolved and fallbacks selected where needed.

### Phase 1 — Demoable POC (for Avakian) — ~3–5 days (can overlap Phase 0)
**Goal:** an unmistakable "it just works" demo.
- A reusable **database Environment image** (Postgres + wstunnel + `app.sh`).
- A minimal **consumer CLI** (`db-connect <url>` → `localhost:5432`).
- A scripted demo: launch the DB app from the Domino UI, run `db-connect`, open DBeaver, query. Emphasize "it's just Docker; no ports opened; Domino auth."
**Exit criteria:** end-to-end demo runs reliably from a clean state in front of a stranger.

### Phase 2 — Discovery + ergonomics — ~3–5 days
- The **discovery helper** (Domino API → list of authorized databases).
- Friendly-name resolution in the CLI; Python library form for notebooks/jobs.
- Multi-engine images (MySQL/Mongo/Redis) to demonstrate generality (Section 7).
**Exit criteria:** a user can discover and connect to any authorized database by name, from both a terminal and a notebook.

### Phase 3 — Hardening — ~1–3 weeks
- Implement the selected fallbacks from Phase 0 (keepalive/reconnect, token auth, etc.).
- Durable storage path (10.3) for at least the dev/test tier.
- TLS/identity polish, structured logging/metrics, resource limits, graceful shutdown.
- Failure-mode testing: app restart, reschedule, network blip, proxy timeout, concurrent consumers.
**Exit criteria:** survives a chaos pass; connections recover transparently; data persists across restart in the supported tier.

### Phase 4 — Generalization to arbitrary TCP services — ~1–2 weeks
- Parameterize the image so the internal port/engine is configurable → a generic "TCP service" app.
- Demonstrate a non-database service (e.g., a full-stack app backend) over the same channel — the explicit USAA proof point.
**Exit criteria:** the same mechanism hosts at least one non-database TCP service end-to-end.

### Phase 5 — Productization & handoff — ~2–4 weeks + security review (customer-owned)
- Decide v2 surface (keep it as Environments/recipes vs. a first-class "TCP service" app type).
- Admin controls: which environments are permitted, quotas, audit hooks.
- Documentation: user guide, admin guide, and the security one-pager (Section 8) for USAA.
- Managed-store gateway model (10.3 Fallback B) documented as the production-system-of-record path.
**Exit criteria:** platform team owns it; security review package delivered.

### Critical path / dependencies
Phase 0 gates everything (it can de-risk or redesign). Phase 1 can start in parallel using optimistic assumptions and absorb Phase 0's findings. Phases 2–4 are largely sequential. Phase 5's security review should be initiated *early* (right after Phase 1's demo lands the interest) because its calendar time dwarfs the engineering.

---

## 12. Handoff checklist for the platform/engineering team

- [ ] Phase-0 go/no-go document with all Section-9 assumptions resolved.
- [ ] Database Environment image(s), versioned, with pinned wstunnel and `app.sh`.
- [ ] Consumer client published as CLI + Python library, with auth handling.
- [ ] Discovery helper with permission-aware listing.
- [ ] Selected fallbacks from Section 10 implemented and tested.
- [ ] Durable-storage tier defined and tested; production-system-of-record path documented (managed-store gateway).
- [ ] Failure-mode/chaos test results recorded.
- [ ] Admin controls (allowed environments, quotas) and audit hooks.
- [ ] User guide, admin guide, and USAA security one-pager.
- [ ] Decision recorded: keep as Environments/recipes vs. promote to a first-class TCP-service app type.

---

## Appendix A — Why wstunnel (and why not frp/chisel/NodePort)

- **NodePort / raw LoadBalancer:** opens a port; fails the "no new ingress" and security goals; not even viable through the HTTP-only app proxy. Demo-grade at best.
- **frp:** excellent, production-grade, ~tens-of-thousands of stars — but it is a *centralized* model (a server clients register with) whose consumer side needs a raw TCP port the Domino HTTP proxy won't route. It reintroduces shared fate and an operated hub. Rejected on Principle 2.
- **chisel:** TCP/UDP over HTTP, secured by SSH, single binary — works, but its path handling is fussier to align with `DOMINO_RUN_HOST_PATH`, and its sweet spot is point-to-point tunnels with known endpoints.
- **wstunnel:** TCP-over-WebSocket/HTTP2 with an explicit `--http-upgrade-path-prefix` that maps one-to-one onto Domino's `DOMINO_RUN_HOST_PATH` prefix model, and a clean fit for "embed the server in each app." Chosen for exactly this path-alignment property and the decentralized topology.

## Appendix B — Glossary

- **Control plane / data plane (Domino Nexus):** the platform-services cluster vs. the workload-execution cluster(s); connected privately, with Istio meshes inside each.
- **`DOMINO_RUN_HOST_PATH`:** runtime env var giving an app its URL path prefix; used to align the tunnel's WebSocket upgrade path.
- **Overloaded channel:** the design's core idea — carrying arbitrary TCP through the single generic authenticated HTTP app channel rather than building typed per-service infrastructure.
- **Discovery helper:** Domino-API wrapper that lists authorized running databases; Domino metadata serves as the service registry.
- **wstunnel server/client:** the embedded (in-app) and consumer-side halves of the TCP-over-WebSocket bridge.

---

*Verification posture reminder: items marked **[ASSUMED]**/**[OPEN]** are not yet confirmed against Domino's source/proxy config. Phase 0 exists to resolve them. Treat this document as a robust design to build from, not a statement that the feature works today.*
