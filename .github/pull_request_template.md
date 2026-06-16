<!-- Briefly describe the change and why. -->

## Dependency review (only if this PR moves `uv.lock` or `pnpm-lock.yaml`)

Any PR that adds or bumps a dependency must document, per the supply-chain
baseline (spec §3.2):

- [ ] **Why** the dependency is needed (or why the version bump).
- [ ] **Sanity check** of the package: rough age, download/usage volume, and
      that it has a real maintainer/history (not a fresh typosquat).
- [ ] **Build backend?** Whether it ships an sdist build backend / install
      script. We install wheels-only (`[tool.uv] no-build-package`); if it is
      legitimately source-only, say so and exclude it from that allowlist with
      justification.
- [ ] **Cooldown:** the dependency predates `[tool.uv] exclude-newer`, or that
      timestamp was bumped deliberately for this update.
- [ ] Ran `uv run python scripts/gen_no_build.py` so the wheels-only allowlist
      matches `uv.lock` (CI also checks this).
