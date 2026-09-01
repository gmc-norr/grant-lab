import { QueryClient } from "@tanstack/react-query";
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it, vi } from "vitest";

import { invalidateArtifact } from "./invalidateArtifact";

describe("invalidateArtifact", () => {
  it("refreshes the artifact, version list, and branch tips", () => {
    // The defect: `useSaveVersion` invalidated ["draft", id] while the version
    // list lives under ["versions", id, artifact] — a key that appeared in NO
    // invalidateQueries call anywhere in the frontend. The version the user had
    // just created was absent from the pane and could not be compared.
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    void invalidateArtifact(qc, "ws-1", "draft");
    const keys = spy.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
    expect(keys).toContain(JSON.stringify(["draft", "ws-1"]));
    expect(keys).toContain(JSON.stringify(["versions", "ws-1"]));
    expect(keys).toContain(JSON.stringify(["branches", "ws-1"]));
  });

  it("uses the versions PREFIX so both artifacts' lists refresh", () => {
    // ["versions", id] is a prefix of ["versions", id, artifact]; TanStack
    // matches by prefix. A full key would refresh one artifact's list and leave
    // the other stale after an edit that touched both.
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    void invalidateArtifact(qc, "ws-1", "spec");
    const versionsCall = spy.mock.calls.find(
      (c) => (c[0]?.queryKey as unknown[])?.[0] === "versions",
    );
    expect(versionsCall?.[0]?.queryKey).toEqual(["versions", "ws-1"]);
  });
});

describe("no mutation invalidates an artifact by hand", () => {
  // The ratchet. Six call sites each had to remember two keys and one forgot
  // for every one that remembered; a helper only helps if the by-hand form
  // cannot come back. Adding a mutation that invalidates ["draft", id] without
  // the version list fails HERE.
  const SRC = path.resolve(__dirname, "..");
  const BY_HAND =
    /invalidateQueries\(\{\s*queryKey:\s*\[\s*(?:"draft"|"spec"|artifact)\s*,\s*id\s*\]/;

  // api/branches.ts invalidates draft, spec AND versions together in one
  // Promise.all for a branch switch — the only caller that already did this,
  // and the shape invalidateArtifact was modelled on. It is not a mutation of
  // one artifact, so it does not fit the helper's signature.
  const EXEMPT = new Set(["api/branches.ts", "api/invalidateArtifact.ts"]);

  function walk(dir: string): string[] {
    return fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
      const full = path.join(dir, e.name);
      if (e.isDirectory()) return walk(full);
      return /\.tsx?$/.test(e.name) && !/\.test\.tsx?$/.test(e.name) ? [full] : [];
    });
  }

  it("every artifact invalidation goes through the helper", () => {
    const offenders = walk(SRC)
      .filter((f) => !EXEMPT.has(path.relative(SRC, f)))
      .filter((f) => BY_HAND.test(fs.readFileSync(f, "utf8")))
      .map((f) => path.relative(SRC, f));
    expect(offenders).toEqual([]);
  });
});
