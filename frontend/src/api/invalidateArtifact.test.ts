import { QueryClient } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import fs from "node:fs";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import { queryWrapper as wrapper } from "../test-utils";
import * as clientMod from "./client";
import { useSaveSection, useSaveSpecSection } from "./editor";
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

// Review 2026-10-26 (A8): the ratchet above is a REGEX, and it is blind to a
// trivial respelling. Rewriting `useSaveSection`'s onSuccess as
// `qc.invalidateQueries({ queryKey: ["draft", `${id}`] })` -- which drops the
// version-list and branch refresh the helper exists for -- left the ratchet and
// editor.test.tsx at 11/11 green, because BY_HAND only matches a variable
// literally named `id`.
//
// So the version list gets a BEHAVIOURAL guard that does not care how the
// invalidation is spelled: seed the real ["versions", id, artifact] key, save a
// section, and require it to go from fresh to stale. Any onSuccess that forgets
// the version list fails here however it is written.
describe("every section save marks the version list stale", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  const SAVED = {
    commit_sha: "b".repeat(40),
    head: "b".repeat(40),
    section_sha: "s1",
    issues: [],
    job_enqueued: false,
    changed: true,
  };
  const VARS = {
    index: 0,
    content: "## A\nx\n",
    base_commit_sha: "a".repeat(40),
    base_section_sha: "s",
    base_branch: "main",
  };
  const stale = (qc: QueryClient, key: unknown[]) => qc.getQueryState(key)?.isInvalidated;

  function primed() {
    const qc = new QueryClient();
    // The REAL key api/versions.ts registers, three segments long: the helper
    // invalidates the two-segment PREFIX, so both artifacts' lists refresh.
    qc.setQueryData(["versions", "ws-1", "draft"], { versions: [] });
    qc.setQueryData(["versions", "ws-1", "spec"], { versions: [] });
    qc.setQueryData(["branches", "ws-1"], { branches: [] });
    return qc;
  }

  it("a draft section save", async () => {
    vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: SAVED, error: undefined } as never);
    const qc = primed();
    expect(stale(qc, ["versions", "ws-1", "draft"])).toBe(false);
    expect(stale(qc, ["branches", "ws-1"])).toBe(false);
    const { result } = renderHook(() => useSaveSection("ws-1"), { wrapper: wrapper(qc) });
    await result.current.mutateAsync(VARS);
    await waitFor(() => expect(stale(qc, ["versions", "ws-1", "draft"])).toBe(true));
    // The prefix, not the full key: an edit that touched both artifacts must not
    // leave the other list behind.
    expect(stale(qc, ["versions", "ws-1", "spec"])).toBe(true);
    // A commit also advances the branch tip; without this the pill sits on the
    // previous commit and the graph looks as though the save landed elsewhere.
    expect(stale(qc, ["branches", "ws-1"])).toBe(true);
  });

  it("a spec section save", async () => {
    vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: SAVED, error: undefined } as never);
    const qc = primed();
    expect(stale(qc, ["versions", "ws-1", "spec"])).toBe(false);
    const { result } = renderHook(() => useSaveSpecSection("ws-1"), { wrapper: wrapper(qc) });
    await result.current.mutateAsync(VARS);
    await waitFor(() => expect(stale(qc, ["versions", "ws-1", "spec"])).toBe(true));
    expect(stale(qc, ["branches", "ws-1"])).toBe(true);
  });
});
