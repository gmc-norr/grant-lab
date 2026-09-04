# frontend/Dockerfile — ephemeral, non-root, secretless SPA build.
# One-shot: takes source + lockfiles, emits ONLY the static bundle to /build/dist.
# Base pinned BY DIGEST (node:22-bookworm-slim bundles corepack, which activates
# the pnpm pinned by package.json "packageManager"). Bump digest deliberately.
FROM node:22-bookworm-slim@sha256:e21fc383b50d5347dc7a9f1cae45b8f4e2f0d39f7ade28e4eef7d2934522b752

ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
# Enable corepack shims as root (writes to /usr/local/bin), create a non-root
# build user, and give it an owned workdir. The build then runs as `builder`
# with no Docker socket, no app secrets, and no var/ mount.
RUN corepack enable \
    && useradd --create-home --uid 10001 builder \
    && mkdir -p /build && chown builder:builder /build
USER builder
WORKDIR /build

# Copy ONLY lockfile + manifest + pnpm config first (layer-cache friendly), then
# install frozen. Scripts are off (pnpm v10 default + empty onlyBuiltDependencies
# allowlist); the install is integrity-checked against the committed lockfile.
COPY --chown=builder:builder frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml frontend/.npmrc ./
RUN pnpm install --frozen-lockfile

# Now the source; the build emits to /build/dist (writable, inside the context).
# .dockerignore keeps frontend/node_modules + frontend/dist out so this COPY
# cannot clobber the freshly-installed deps or drag host cruft into the image.
COPY --chown=builder:builder frontend/ ./

# The edition is a BUILD ARG, not a hard-coded `full`. It defaults to full, which
# is what ships today, so this is behaviour-preserving; a match image is
# `docker build --build-arg VITE_APP_EDITION=match -f frontend/Dockerfile .`.
# vite.config.ts resolves the router alias through loadEnv(), which merges
# process.env, so a RUN-scoped export is enough.
#
# The BUNDLE GATE deliberately does not run in here: scripts/check_match_bundle.mjs
# lives at the repo root and the build context copies only frontend/, so it is not
# present. It runs OUTSIDE, in scripts/package_spa.sh, over the extracted
# dist/module-manifest.json — the stronger place for it, because that reads the
# artefact instead of trusting this ARG.
ARG VITE_APP_EDITION=full
RUN VITE_APP_EDITION="$VITE_APP_EDITION" pnpm run build   # outDir = /build/dist (vite.config.ts -> resolve(__dirname, "dist"))
