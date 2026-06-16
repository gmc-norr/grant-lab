# Supply-Chain Security for the React Transition — Design

_Spec · 2026-06-16 · Status: draft for review_

Cross-cutting companion to the **React SPA Foundation** effort. It hardens the dependency supply chain (npm/JS **and** Python/PyPI) introduced by moving the frontend to React. Read alongside:

- [react-spa-foundation-design](2026-06-15-react-spa-foundation-design.md) (the SPA design)
- [react-spa-foundation Plan 2 — serving/packaging](../plans/2026-06-15-react-spa-foundation-2-serving.md)
- [react-spa-foundation Plan 3 — the React app](../plans/2026-06-15-react-spa-foundation-3-app.md)

This spec is intended to be handed to a separate implementation session. It is self-contained: every control names a concrete mechanism and, where it overrides an existing decision, says so explicitly (see **§8 Reconciliation**).

---

## 1. Threat model & scope

### Why this spec exists

call-manager is an internal hospital tool. Its deploy host holds **confidential proposal drafts** (`var/`, see [README §Production deployment](../../../README.md)), the **Postgres** database of rankings, the **Ollama** model endpoint, and a route onto the **internal hospital network**. Moving the frontend to React introduces a large, fast-moving npm dependency graph maintained by thousands of third parties — a supply chain that saw active, worm-style compromise throughout 2025 (self-replicating token-stealers, maintainer-account phishing of high-download packages, typosquats).

### The two attack surfaces (do not conflate them)

| # | Surface | Where malicious code runs | Blast radius | Primary defense |
|---|---|---|---|---|
| **A** | **Build-time code execution** — a dependency's install/lifecycle script (`postinstall`, sdist build backend) runs during `pnpm install` / `uv sync` | On the **build machine**, with that machine's privileges: env vars, tokens, filesystem, network | **Severe** — on this project the build runs on the on-prem host (see §4), so a malicious script could read `var/`, DB creds, or reach the internal network | **Prevention + containment** (§3, §4) |
| **B** | **Malicious code in the shipped bundle** — bad code survives into the built JS and runs in the **user's browser** | In the browser, sandboxed, constrained by CSP | **Contained** — `script-src 'self'` + same-origin `connect-src` cap exfiltration | **Runtime containment** (§6) |

**Surface A is the primary risk and the center of gravity of this spec.** Surface B is real but already well-contained by the foundation's CSP decisions; we reaffirm and hold them.

The guiding principle is two-pronged: **keep bad packages out** (§2, §3), and **assume one eventually gets in and contain what it can do** (§4, §6).

### In scope

- npm/JS supply chain for the React SPA (`frontend/`).
- Python/PyPI supply chain for the FastAPI backend (`uv` / `uv.lock`).
- The CI and on-host build paths that install those dependencies.

### Out of scope / deferred

- Private registry proxy (Verdaccio/Artifactory) — documented as optional future hardening (§10).
- Vendoring `node_modules` / full offline mirror (§10).
- SLSA provenance / reproducible-build attestation (§10).
- Moving the build off the host into CI-only artifact delivery — **strictly stronger** than what we specify, kept as a recommended future step (§4, §10).
- Application-level authz/PHI handling, model/prompt security — covered by other specs.

---

## 2. Layer 1 — Minimize & govern the dependency surface

The single biggest lever is **fewer dependencies**: every package is another maintainer account that can be phished and another lockfile entry that can be poisoned.

1. **Lean framework (already decided, reaffirmed).** The foundation picked **Vite + React + React Router (Data Mode)**, not Next.js. Hold this: Vite pulls a fraction of Next's transitive graph and ships **no Node server in prod** (no second runtime attack surface). Keep deferring Modal/Drawer/Toast until needed, on Radix primitives, per the foundation.
2. **Dependency additions are reviewed like code.** Adding a package (direct or via a PR that moves the lockfile) requires, in the PR description: why it's needed, weekly-download/age/maintainer sanity, whether it ships install scripts, and a typosquat check on the name. A one-line dep add that pulls 40 transitive packages gets the scrutiny of 40 files of new code.
3. **Lockfiles are the source of truth and are committed.** `pnpm-lock.yaml` (frontend) and `uv.lock` (backend, already present) are committed and are the *only* sanctioned way dependencies enter. Installs are always frozen (§3).

---

## 3. Layer 2 — Prevention controls (keep bad packages from installing)

### 3.1 JavaScript (pnpm)

**Switch the frontend package manager from npm to pnpm.** This overrides the foundation's npm decision (§8). Rationale: pnpm's defaults are the prevention controls we want, for free.

- **Lifecycle scripts OFF by default.** pnpm **v10+** does **not** run dependencies' install/lifecycle scripts unless they are explicitly allowlisted via **`onlyBuiltDependencies`** (`pnpm approve-builds` to manage the list). This neutralizes the #1 build-time malware vector (Surface A) for the whole transitive graph. Keep the allowlist as small as possible and review every entry.
- **Version cooldown — `minimumReleaseAge`.** Configure pnpm's release-age cooldown (in minutes) so it refuses to install any version published more recently than the window. **Set ~7–14 days** (e.g. `minimumReleaseAge: 10080` for 7 days). This is the **highest-ROI control in the spec**: nearly every 2025 worm was detected and yanked within hours-to-days, so a cooldown blocks them at install time *before any scanner knows they are bad*. It makes detection (§5) a backstop, not the primary defense.
  - _Verify your pnpm version supports `minimumReleaseAge`_ (recent pnpm 10.x). Pin that version via the `packageManager` field (§4). Allow targeted exceptions only for a genuine emergency security patch, documented in the PR.
- **Frozen, integrity-checked installs everywhere.** Use **`pnpm install --frozen-lockfile`** (the `npm ci` equivalent) in CI and in the build container — never a plain resolving install. The lockfile carries integrity hashes; a tampered tarball fails the hash check.
- **Exact version pins.** No `^`/`~` ranges in `package.json`; pin exact versions so a poisoned patch can't enter even before the lockfile is regenerated. (`pnpm config set save-exact true` / `save-prefix=''`.)
- **Config location.** Put these in a committed pnpm config (`frontend/.npmrc` and/or the `pnpm` key in `frontend/package.json` settings, per your pnpm version's supported location). The settings must be in the repo, not developer-local, so CI and every contributor get them.

### 3.2 Python (uv)

Mirror the same posture; `uv` already gives most of it.

- **Frozen, hashed installs.** `uv sync --frozen` (or `--locked`) in CI and deploy. `uv.lock` already records per-artifact hashes and uv verifies them — keep it committed and authoritative.
- **Cooldown — `exclude-newer`.** uv has no per-package age gate, but **`[tool.uv] exclude-newer = "<timestamp>"`** (or `--exclude-newer`) pins resolution to only consider artifacts published before a date. Set it to "now minus ~1–2 weeks" and bump it **deliberately** when intentionally updating dependencies — this gives the same cooldown effect for batch updates. (New `[tool.uv]` section in `pyproject.toml`; none exists today.)
- **Prefer wheels, avoid sdist build execution.** Installing an **sdist** runs its build backend (`setup.py`/PEP 517) — arbitrary code at install time (Surface A on the Python side). Prefer pre-built **wheels** via uv's wheels-only resolution (`--only-binary :all:` / the `[tool.uv]` no-build/only-binary setting — _confirm the exact key for your uv version_), with a small explicit allowlist for the few legitimately source-only dependencies. Keep uv's **build isolation on** (default).
- **Exact, locked versions** via `uv.lock`; dependency bumps go through the same reviewed-PR rule as JS (§2).

---

## 4. Layer 3 — Build-time blast-radius containment (the core control)

**Decision (user, 2026-06-16): the frontend build runs on the on-prem host**, matching today's manual `systemctl`-based deploy. That is the highest-blast-radius build location, so containment here is the spec's center of gravity.

The host today runs the app via uvicorn directly and only containerizes Postgres ([docker-compose.yml](../../../docker-compose.yml) — `db` service only). We add an **ephemeral, unprivileged build container** so that "install untrusted dependencies" never shares a process or environment with "hold production secrets."

### 4.1 The separation principle

> The **build step** installs untrusted deps and emits **only static files**. It has **no secrets, no `var/` mount, no DB credentials, and egress restricted to the package registries**. The **app process** — which *does* hold `SESSION_SECRET`, `DATABASE_URL`, `var/`, and internal-network reach — is a separate step that only ever **consumes** those static files. A compromised build script therefore finds an empty, sandboxed box.

### 4.2 Build-container requirements

- **Ephemeral & isolated:** a dedicated build `Dockerfile` (new — none exists today), run as a one-shot `docker run --rm` (or a multi-stage build). **Non-root** user; **read-only root filesystem** where feasible; **no Docker socket mount**.
- **No secrets in the build env:** the build receives *no* application env (`SESSION_SECRET`, `DATABASE_URL`, OAuth secrets, Resend/SMTP creds) and **no `var/` mount**. Only the source tree + lockfiles.
- **Restricted egress:** network access limited to the package registries needed to fetch dependencies (and nothing on the internal hospital network). Drop the network after the fetch step if your build flow allows (fetch → `--offline` build).
- **Pinned, reproducible toolchain:** base image pinned **by digest** (not a floating tag); **pnpm pinned** via the `packageManager` field + corepack (so the build can't be steered onto a different pnpm); `pnpm install --frozen-lockfile` with scripts off (§3.1).
- **Artifact-only output:** the container's sole output is the built bundle, written into the package tree the backend expects — **`src/call_manager/web/spa/`** per [Plan 2 packaging](../plans/2026-06-15-react-spa-foundation-2-serving.md) (hatch `artifacts`/`force-include`). Nothing else crosses the boundary.
- **Verify before serve:** after the build, assert the expected artifact exists (`src/call_manager/web/spa/index.html`) before the app is (re)started — reuse the wheel-contents check the foundation already specifies.

### 4.3 Residual risk & the stronger alternative

On-host containerized build removes secret/`var/`/network access from the build, but the container still runs on production hardware. The **strictly stronger** option — **build only in CI (GitHub Actions: no PHI, no prod secrets, no internal network) and ship the built bundle to the host as a verified artifact, so the host never runs a package manager at all** — is documented as recommended future hardening (§10). The containerized on-host build is the v1 baseline because it keeps the current deploy model.

---

## 5. Layer 4 — Detection (tiered; a backstop, not the primary defense)

Because the §3 cooldown blocks freshly-published malicious versions before any scanner flags them, detection is a **backstop**. It is layered so the project stays covered even if hospital infosec permits no third-party SaaS.

- **Tier 0 — structural, no scanner (already covered in §3):** pnpm scripts-off + `minimumReleaseAge`; uv `exclude-newer` + wheels-only; frozen, hashed installs; exact pins. These **prevent**, so they do not depend on a detector knowing a package is bad.
- **Tier 1 — GitHub-native, zero new vendor:** **Dependabot** (`.github/dependabot.yml`) for the `npm` and Python (`uv`/`pip`) ecosystems + the **Dependency Review** action on PRs (fails a PR that introduces a known-vulnerable or newly-flagged dependency). Already on GitHub; almost certainly already permitted. _Confirm the exact Dependabot ecosystem identifier for the uv lockfile when implementing._
- **Tier 2 — behavioral / zero-day:**
  - **Socket CLI in CI** (`@socketsecurity/cli`) — analyzes the lockfile and flags new install scripts, network/filesystem access, obfuscation, and typosquats at PR time. Prefer the **CLI in CI** over the Socket GitHub App initially: it's a lighter governance ask (no broad repo-scoped third-party grant; only public package names leave the building). Upgrade to the App later if infosec approves.
  - **OSV-Scanner** (offline-capable) — scans both `pnpm-lock.yaml` and `uv.lock` against the OSV advisory DB; a CVE/advisory cross-check that works even fully air-gapped.
- **Baseline audits:** `pnpm audit` (JS) and `pip-audit`/equivalent (Python) as cheap always-on CI steps.

**Governance note (hospital):** dependency scanning transmits only *public open-source package names and versions* — not PHI and not your source. That is low-sensitivity, but a SaaS GitHub App still needs infosec sign-off. The tiering guarantees that a "no SaaS" ruling still leaves Tier 0 + OSV-Scanner + audits, which is strong on its own.

---

## 6. Layer 5 — Runtime containment (Surface B)

Reaffirm and hold the foundation's runtime posture; a malicious dependency that survives into the bundle must not be able to phone home or load a second stage.

- **`script-src 'self'` (no `'unsafe-inline'`, no `'unsafe-eval'`).** Already the policy ([web/middleware.py:33](../../../src/call_manager/web/middleware.py#L33), `build_csp()` applied at [main.py:280](../../../src/call_manager/main.py#L280)). The foundation already requires Vite to emit **hashed, no-inline JS** and adds a CI assertion that `script-src` contains no `'unsafe-inline'`. Hold both — they are now also a supply-chain control, not just an XSS control.
- **Self-host every asset; no third-party CDN.** Serving JS/CSS from your own origin keeps the `script-src 'self'` guarantee meaningful and removes a runtime CDN-compromise vector (which a Subresource-Integrity hash would only partially mitigate). No `<script src="cdn…">`.
- **`connect-src` allowlist.** Keep `connect-src` to `'self'` (the foundation needs it to cover `/api` + `/events`). A tight `connect-src` is what stops a compromised in-bundle dependency from exfiltrating to an attacker origin from the user's browser.
- **`style-src 'unsafe-inline'` stays** during Pico coexistence ([web/middleware.py:34](../../../src/call_manager/web/middleware.py#L34)) — unchanged; the hard requirement is on `script-src`.

---

## 7. CI/CD changes (concrete)

Current CI ([.github/workflows/ci.yml](../../../.github/workflows/ci.yml)) runs uv sync + ruff + pytest, then Node 20 + `node --test tests/js/`. It does **not** install any npm packages today. Changes:

1. **Frontend install via pnpm, scripts off, frozen.** Enable corepack / pin pnpm via `packageManager`; `cd frontend && pnpm install --frozen-lockfile` with lifecycle scripts disabled (pnpm v10 default + `onlyBuiltDependencies` allowlist). The `minimumReleaseAge` cooldown applies here too.
2. **Backend install frozen.** Keep `uv sync` but make it **`--frozen`**; add the `[tool.uv]` `exclude-newer` + `only-binary` settings to `pyproject.toml`.
3. **Detection steps:** add OSV-Scanner (scans `pnpm-lock.yaml` + `uv.lock`), `pnpm audit`, `pip-audit`, the Socket CLI, the Dependency Review action, and a committed `.github/dependabot.yml`.
4. **Build job:** the containerized frontend build (§4) produces `src/call_manager/web/spa/`; reconcile ordering with [Plan 2](../plans/2026-06-15-react-spa-foundation-2-serving.md) (build before `hatch build`; wheel-contents assertion).
5. **Legacy `tests/js/`** keeps running on `node --test` during the overlap (per the foundation's Rev-4 decision); it has no `package.json` and is unaffected by the new `frontend/` pnpm setup. Reconcile to one runner at migration completion.

---

## 8. Reconciliation with the foundation specs (explicit overrides)

This spec changes decisions made in the React SPA Foundation docs. The implementing session must apply these:

| Foundation decision | This spec | Action for the implementer |
|---|---|---|
| **Package mgr = npm**, `package-lock.json`, `npm ci` ([foundation design, Consolidated decisions](2026-06-15-react-spa-foundation-design.md)) | **pnpm** (scripts-off, cooldown) | Replace npm with pnpm throughout. **[Plan 3, Task 1](../plans/2026-06-15-react-spa-foundation-3-app.md)** `npm install …` → `pnpm add …`; lockfile `package-lock.json` → `pnpm-lock.yaml`; CI `npm ci` → `pnpm install --frozen-lockfile`. |
| CI installs frontend with `npm` | pnpm via corepack/`packageManager`, frozen, scripts off | Update [ci.yml](../../../.github/workflows/ci.yml) per §7. |
| Build runs as part of release (mechanism unspecified) | **Containerized, secretless, egress-restricted on-host build** | Add a build `Dockerfile`; wire it into the deploy sequence in [README §Deploy](../../../README.md). |
| `uv sync` (CI) | `uv sync --frozen` + `[tool.uv]` `exclude-newer`/`only-binary` | Add `[tool.uv]` to `pyproject.toml` (none today). |
| CSP `script-src 'self'`, hashed no-inline bundle | **Unchanged — reaffirmed** as a supply-chain control | No change; keep the existing CI assertion. |

Nothing in this spec changes the foundation's serving model, auth/CSRF, API contract, SSE contract, or dashboard-port scope.

---

## 9. Incident response

You **do not publish** npm or PyPI packages, so there is no maintainer token to steal and no downstream to poison — incident handling is purely about *consuming* a compromised dependency.

- **A scanner fires (Socket/OSV/Dependabot):** treat as a release blocker. Inspect the flagged package/version; if confirmed or unclear, **pin away from it** — `pnpm.overrides` (JS) or a pinned constraint + relock (Python) — and regenerate the lockfile. The §3 cooldown means you usually have *not yet installed* the bad version; the pin just keeps it out.
- **A public compromise is disclosed for something you depend on:** check the lockfile for the exact resolved version. If you're inside the cooldown window you likely never pulled it. If you did, roll the lockfile back to the last-known-good version and rebuild.
- **Build behaves anomalously (unexpected network, files):** the build container has no secrets, no `var/`, and no internal-network reach, so exposure is contained by construction; rebuild from a clean container on a known-good lockfile and investigate the diff.
- **Kill switch:** because `script-src 'self'` + `connect-src 'self'` contain Surface B, even a tainted bundle can't exfiltrate from users' browsers; pulling the bad lockfile entry and redeploying is the remedy.

---

## 10. Deferred / future hardening

Recorded so the implementing session doesn't re-derive them, and so they're easy to pick up later:

- **CI-only artifact build (recommended next step).** Move the build entirely into GitHub Actions and ship a verified static bundle to the host, so the on-prem box never runs a package manager. Strictly stronger than §4; deferred only to preserve the current deploy model.
- **Private registry proxy** (Verdaccio / Artifactory / institutional mirror) as the single sanctioned source, with a vetted allowlist and the ability to freeze packages. Natural home for the cooldown and for surviving npmjs.org outages. Worth adopting if the hospital already runs Artifactory.
- **Vendoring / full offline mirror** of `node_modules` for fully air-gapped builds.
- **SLSA provenance / build attestation** (npm provenance, `pnpm`/sigstore verification) once the toolchain supports it cleanly.

---

## 11. Success criteria

1. Frontend uses **pnpm** with lifecycle **scripts off by default** (minimal `onlyBuiltDependencies` allowlist) and a **`minimumReleaseAge` cooldown** of ~7–14 days; installs are `--frozen-lockfile`; versions exact-pinned. (Overrides the foundation's npm decision; foundation docs updated per §8.)
2. Backend uses `uv sync --frozen` with an **`exclude-newer` cooldown** and **wheels-preferred** resolution; `uv.lock` hashes authoritative.
3. The frontend build runs in an **ephemeral, non-root, secretless, egress-restricted container** that emits only `src/call_manager/web/spa/`; no DB creds, no `var/`, no internal-network reach in the build env.
4. CI runs **Dependabot + Dependency Review (Tier 1)** and **Socket CLI + OSV-Scanner + audits (Tier 2)** over both lockfiles; the cooldown makes detection a backstop, not the primary defense.
5. Runtime CSP `script-src 'self'` / `connect-src 'self'`, hashed no-inline bundle, all assets self-hosted — reaffirmed and CI-asserted.
6. An incident-response path exists (lockfile pin-rollback; cooldown buys triage time; no publish surface).
7. The spec is self-contained and the foundation overrides (§8) are explicit enough to implement without re-litigating the package-manager or build-location decisions.
