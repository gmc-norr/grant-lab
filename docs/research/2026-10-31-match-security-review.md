**Grant Lab match edition — security review, 31 October 2026**

Reviewed revision: `5bd6b112`. This review covers the beta-facing match application and its shared backend, deployment and build boundaries. Application code and configuration were left unchanged.

The first fix should be the password-reset race in S1: a completed administrator reset can be overwritten by an already-running request using the old credentials. The public example signing key remains a separate high-impact deployment trap. Other confirmed gaps concern domain enforcement, ownership transfer and build-time environment exclusion. A browser security policy also breaks the optional Google login flow.

The existing protections are substantial: 582 selected security/access tests passed, and additional JWT probes rejected seven adverse tokens. Those passing checks do not cover the concurrency failures reproduced below. I would close S1 and S2, resolve the medium findings relevant to the beta configuration, and verify the actual HTTPS deployment before admitting staff accounts.

Priorities describe remediation order, not a formal CVSS score. P1 means high-impact account/session protection; P2 means a material policy, authorization, build or release issue. Conditions and demonstrated impact are stated separately for each finding.

**S1 · P1 · An in-flight old-password change can defeat a completed administrator reset.**

Locations: [me.py:208](../../src/grant_lab/web/api/me.py#L208), [me.py:220](../../src/grant_lab/web/api/me.py#L220), [admin_users.py:289](../../src/grant_lab/web/api/admin_users.py#L289).

Both credential-changing routes load the user without a row lock and increment `session_epoch` from their own loaded value. A self-service change can verify the old password, pause during hashing, and subsequently overwrite an administrator's completed reset. Both operations can write epoch 1, and the older request then mints a session under that still-current epoch.

Executed reproduction against disposable PostgreSQL:

1. Started `/api/me/password` with the existing session and old password, pausing after verification and before replacement hashing completed.
2. Completed the administrator reset. Its response was 200; the reset credential was stored; the old cookie correctly received 401.
3. Resumed the first request. It returned 200, replaced the reset credential, and issued a cookie accepted by `/api/me` with 200.
4. Verified that the first request's chosen password worked and the administrator's reset password no longer did.

An attacker needs the old credentials/session and a request already in progress during the reset; this is not an unauthenticated password-change endpoint. The security consequence is failure of the account-recovery boundary precisely when an old credential may be compromised.

Change: serialize credential changes on the user row and verify current authorization/password state inside that transaction. Make epoch updates monotonic across password changes, administrator resets, disable and logout. An atomic epoch increment alone does not prevent the stale password write. Add an ordered two-request regression that checks the final credential and both cookies after the reset completes. Include rehash-on-login in the review of credential writers.

**S2 · P1, conditional on deployment · Production validation accepts the README's public signing key.**

Locations: [README.md:87](../../README.md#L87), [config.py:12](../../src/grant_lab/config.py#L12), [config.py:931](../../src/grant_lab/config.py#L931).

The documented `local-development-secret-change-me` is 34 characters long and missing from the known-placeholder denylist. An HTTPS configuration therefore accepts it. Anyone knowing that example can sign cookies for existing user IDs and epochs in a deployment retaining it.

Re-ran the earlier beta-review probe: the match application accepted an independently signed cookie using the public example and returned 200 from the administrator user-list endpoint. No password authentication was performed. The local `.env` uses a different key and meets the production length floor; this is not evidence that the deployed service has been compromised.

Change: remove the copyable key from the quickstart in favor of a generation step, reject that exact example in production, and test all documented nonempty example keys. The existing `.env.example` already leaves the key blank, which is the safer pattern. Deployments actually using the public key need a new random key and session invalidation.

This revalidates beta-review F1.

**S3 · P2 · Password login ignores the configured email-domain sign-in policy.**

Locations: [auth.py:95](../../src/grant_lab/web/api/auth.py#L95), [config.py:74](../../src/grant_lab/config.py#L74), [oauth_google.py:156](../../src/grant_lab/web/routes/oauth_google.py#L156).

`ALLOWED_EMAIL_DOMAINS` is documented as restricting sign-in and access requests. Registration and Google sign-in check it. Password login checks the password and account status but never the domain.

Executed reproduction: seeded a previously admitted, active password account at a domain absent from the current allowlist. Password sign-in returned 200, issued a session, and `/api/me` returned 200. A realistic trigger is removal of a formerly allowed partner domain. This does not let a new outsider activate an account: administrator approval is still required.

Change: enforce the intended policy consistently across authentication methods. If domain removal is intended to revoke access immediately, existing sessions also need a defined revocation policy. If this is an admission-only setting, document and name it accordingly and resolve the difference with Google login. Do not rely on the current setting as an offboarding control.

**S4 · P2 · Ownership transfer does not serialize with ordinary profile edits.**

Locations: [profiles.py:508](../../src/grant_lab/web/api/profiles.py#L508), [profiles.py:523](../../src/grant_lab/web/api/profiles.py#L523).

Transfer and sharing mutations lock the profile row; the ordinary update route authorizes against an unlocked read. An outgoing owner can therefore finish an edit after a transfer has committed and revoked their private-profile access.

Re-ran the earlier two-request probe: paused the edit after authorization, transferred the profile without retaining read access, and resumed the edit. Both writes returned 200. The outgoing owner's next GET returned 404, but their in-flight edit had overwritten the new owner's profile content.

Change: acquire the profile row lock before authorization and hold it through the update commit. Keep the existing distinction between unreadable profiles (404) and readable profiles the caller cannot edit (403). Embedding/backfill work can remain outside the transaction. This revalidates beta-review F6; no new cross-user private-profile read bypass was found in the reviewed ordinary request paths.

**S5 · P2, conditional on build inputs · Nested frontend environment files enter the supposedly secretless image.**

Locations: [.dockerignore:31](../../.dockerignore#L31), [frontend/Dockerfile:26](../../frontend/Dockerfile#L26).

The Docker ignore patterns exclude root environment files but do not exclude nested frontend ones. The Dockerfile's `COPY frontend/ ./` consequently admits these files into builder image layers. Extracting only the static bundle afterward does not remove those layers from the builder image/cache.

Executed canary test: built a scratch image in a fresh temporary directory using the repository's exact `.dockerignore`, with public dummy values in root and nested environment files. Root `.env` and `.env.bak-canary` were excluded. `frontend/.env`, `frontend/.env.production` and `frontend/.env.security-review-canary` all survived in the image. The image and container were removed afterward.

Only `frontend/.env.example` exists in the current frontend tree. This test demonstrates an exclusion failure, not an observed leak of a real key. The consequence becomes credential exposure if an operator builds with sensitive local values in a nested frontend environment file, even when those variable names lack Vite's public `VITE_` prefix.

Change: use recursive environment-file exclusions and an explicit exception for safe examples if required. Add a canary-based build-context test covering root and nested dotenv files. Review Git ignore coverage for newly introduced dotenv variants too.

**S6 · P2 release finding · The Python audit fails on locked `joserfc==1.7.4`.**

Locations: [uv.lock:940](../../uv.lock#L940), [oidc.py:108](../../src/grant_lab/auth/oidc.py#L108), [.github/workflows/ci.yml](../../.github/workflows/ci.yml).

The CI-equivalent audit command exited 1 with `CVE-2026-119283` and `CVE-2026-119284`. The maintainer documents [unbounded JWE recipient decryption](https://github.com/authlib/joserfc/security/advisories/GHSA-jpxq-7h8p-2q76) and [compact-JWS allocation before size rejection](https://github.com/authlib/joserfc/security/advisories/GHSA-9pg5-8c7x-5hfx).

The application does not call the affected JWE API. Its JWS decoding receives Google's ID token from the token endpoint, rather than accepting arbitrary browser-supplied tokens. I did not demonstrate a directly exploitable application path for either advisory. This finding establishes a vulnerable dependency version and a failing release gate, not a reproduced remote authentication bypass.

Change: deliberately resolve and lock a patch version clearing both advisories, regenerate derived dependency metadata, and rerun the OIDC controls and audit. The audit feed currently lists fixes at 1.7.5 and 1.7.6 respectively, while the maintainer pages label 1.7.5; use the resulting audit verdict when resolving that discrepancy.

The production JavaScript audit reports zero known vulnerabilities. The complete JavaScript dependency graph reports **48 advisory entries across 14 modules: 4 critical, 18 high, 22 moderate and 4 low**. These additional findings are in the development/build/test dependency graph, including Vite and Vitest. They warrant toolchain maintenance and restrictions on exposed development servers; they are not evidence that the statically served match bundle has those same reachable vulnerabilities.

**S7 · P2 compatibility finding · CSP blocks the native Google login redirect in Chromium.**

Locations: [middleware.py:67](../../src/grant_lab/web/middleware.py#L67), [Login.tsx](../../frontend/src/routes/Login.tsx), [oauth_google.py:74](../../src/grant_lab/web/routes/oauth_google.py#L74).

The login button submits a native form to `/auth/google/start`, which redirects to a different origin. The production document policy permits only `form-action 'self'`. Chromium applies that restriction to the redirected navigation and blocks the flow. Browser behavior around form redirects varies, as described in the [MDN `form-action` reference](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/form-action).

Executed browser reproduction against the real match login page and Google-start handler, with a fake identity provider on a second loopback origin. With the actual policy, Chromium stayed on login, reported a form-action violation, and never reached the canary. Changing only the document's form-action directive to additionally permit that exact canary origin made the same flow reach the identity page. No real Google authentication was performed.

Change: allow the trusted authorization endpoint's origin in this directive when OIDC is enabled, or revise the start flow while preserving CSRF, state and PKCE protections. Add a browser test covering the real redirect chain. Do not broadly relax script or form destinations. Google OIDC is disabled in the local `.env`, so this is not a blocker for a deliberately password-only beta. It is an authentication-availability failure caused by a security control, not an account-takeover finding.

**Deployment and private-data flow observations.**

The local `.env` selects match/prod with drafting disabled, but its browser base URL uses HTTP. That configuration disables Secure cookies and HSTS. The actual reverse proxy, certificate and network path were not inspected. Before real accounts are admitted, establish and verify the external HTTPS origin, redirect behavior, cookie attributes and trusted-proxy topology. An HTTP setting is supported for development and does not prove that an externally exposed service currently sends credentials in plaintext.

The same file contains an Anthropic key. **Disabling drafting does not disable hosted rank escalation.** The scheduler constructs the remote ranking client when the key is present, and the ranking prompt includes the profile description and structured preferences. If the beta process uses this file, qualifying matches can send private profile text to the hosted provider. Profile visibility governs other application users; it is not a cloud-egress control. Confirm that this data flow is intended for the beta, and remove the key from its configuration if ranking must remain entirely on premises. No model requests were made during this review.

Profile bootstrap also sends selected researcher identifiers/names to PubMed and mission/grant text to the configured local model. Email digests, if enabled, send profile-specific selections through the configured transport. These are data-flow boundaries to document, not demonstrated unauthorized disclosures. No patient data or private research content was used in probes.

Sensitive match JSON endpoints generally lack an explicit `Cache-Control: private, no-store` policy, unlike the board preview and full-edition retention detail. Add an explicit policy for authenticated personal/research responses and verify proxy caching rules before shared-workstation use. No cross-user cache disclosure was reproduced, so this is defense in depth rather than a confirmed leak.

**Protections checked and retained.**

An inventory of the match API found **59 method/path declarations**: 37 use the active-user dependency, 17 require an administrator, and 5 deliberately use their own public/session-aware behavior (auth configuration, login, registration, logout and public boards). The optional OAuth endpoints and health/static/UI routes were reviewed separately. The match route-exclusion tests passed for the future workspace surface.

The selected tests and source inspection support these protections:

- Passwords use Argon2, unknown accounts take a dummy verification path, login failures use a generic response, and login attempts are limited by email and client IP. Pending/disabled users do not receive ordinary access.
- Signed sessions have server-side expiry and a checked per-user epoch. HTTPS configuration enables HttpOnly/Secure/SameSite=Lax attributes. Ordinary logout and disable revocation work; S1 identifies the concurrent credential-write gap.
- OIDC binds state, nonce and S256 PKCE through an encrypted, expiring transaction cookie. JWT decoding pins RS256 and checks issuer, audience, authorized party, expiry and verified email. Additional probes rejected `alg=none`, HS256, future `iat`, future `nbf`, array audience without `azp`, expired tokens and a wrong nonce, with a valid signed control accepted.
- Private profiles start private, profile reads use the shared access predicate, unreadable profiles return 404, and digest assembly rechecks readability. Public boards omit profile identities, rankings and private decision notes; unpublished and nonexistent boards have the same 404 response.
- Cross-origin and null-Origin state-changing requests are rejected; headerless requests are intentionally allowed for non-browser clients. No modern-browser CSRF bypass was reproduced. Production scripts are restricted to self, framing is restricted, and response content types receive nosniff protection.
- Reviewed SQL construction uses bound values. Funding URLs are restricted to HTTP(S), React renders text/attributes without raw HTML insertion on the match pages, digest HTML is autoescaped, and source fetches use the shared private-address checks and pinned transport. No new SQL injection, stored script execution or SSRF path was found in this review.
- Current local `.env`/backup files, runtime workspace data and named key files are ignored by Git. A limited current-tree scan found no selected credential markers. Docker/action pins and frozen lockfiles are present. S5 identifies the separate nested build-context gap.

These are bounded review results, not a claim that every possible attack has been excluded.

**Validation and limits.**

| Executed check | Result |
|---|---|
| Selected auth, OIDC, session, CSRF, profile-access, public-board, header, SSRF and edition tests: 48 files | **496 passed**, 62.64 s |
| Additional board CRUD/items and administrator operations/golden-set tests: 4 files | **86 passed**, 52.68 s |
| New disposable-database probes | Password-reset race and removed-domain login reproduced |
| Re-run earlier disposable-database probes | Public signing-key acceptance and ownership-transfer race reproduced |
| Additional JWT controls | Seven adverse tokens rejected; valid signed control accepted |
| Isolated Docker context/image canaries | Root environment files excluded; all three nested frontend variants included |
| Chromium CSP redirect reproduction and control | Actual policy blocked identity navigation; exact-origin allowance reached the local canary |
| Python audit using the CI waiver command | Failed: two advisories in `joserfc` |
| Production JavaScript audit | Zero known vulnerabilities |
| All JavaScript dependency audit | 48 entries as detailed in S6 |
| Selected current-tree credential-marker scan | No matches; credential values were not printed |

All database probes used disposable testcontainers PostgreSQL. Model calls were disabled/mocked, identity-provider canaries were synthetic, and the Docker experiment contained dummy values only. Temporary test files and review servers were removed. The only new lasting repository artifact from this turn is this report; the earlier beta-readiness document was preserved.

This was a source-assisted security review with local reproductions. It did not test the live deployment, real Google login, production data, actual hosted-model traffic, outbound email, TLS certificates, external firewall/proxy configuration, backup access, remote CI permissions or sustained denial-of-service load. The credential scan was not a full Git-history or high-entropy secret audit. The future full-edition proposal/collaboration implementation was outside the beta security assessment.

Recommended order: fix the credential-reset transaction race; eliminate the public signing-key template trap; resolve domain-policy and ownership-transfer consistency; correct nested environment exclusions and the Python audit; fix and browser-test OIDC's CSP path if it is included in beta. Verify HTTPS and explicitly choose the hosted-ranking data flow before inviting users.
