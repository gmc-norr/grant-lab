import { afterEach, describe, expect, it, vi } from "vitest";

import { downloadExport } from "./export";

function mockFetch(res: Partial<Response> & { json?: () => Promise<unknown> }) {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(res));
}

afterEach(() => { vi.unstubAllGlobals(); });

describe("downloadExport", () => {
  it("saves a blob on 200", async () => {
    const click = vi.fn();
    vi.spyOn(document, "createElement").mockReturnValue({
      click,
      remove: vi.fn(),
      href: "",
      download: "",
    } as unknown as HTMLAnchorElement);
    vi.stubGlobal("URL", { createObjectURL: () => "blob:x", revokeObjectURL: vi.fn() });
    mockFetch({
      ok: true,
      status: 200,
      blob: async () => new Blob(["x"]),
      headers: new Headers({ "content-disposition": 'attachment; filename="W.md"' }),
    } as unknown as Response);
    const r = await downloadExport("ws1", "raw");
    expect(r).toEqual({ ok: true });
    expect(click).toHaveBeenCalled();
  });

  it("returns the blocked list on 409 export_blocked", async () => {
    mockFetch({
      ok: false,
      status: 409,
      json: async () => ({
        error: { code: "export_blocked", message: "x", detail: { blocked: [{ citekey: "a", reason: "metadata_unapproved" }] } },
      }),
    } as unknown as Response);
    const r = await downloadExport("ws1", "composed");
    expect(r).toEqual({ ok: false, kind: "blocked", blocked: [{ citekey: "a", reason: "metadata_unapproved" }] });
  });

  it("flags a corrupt ledger on 409 citation_ledger_invalid", async () => {
    mockFetch({
      ok: false,
      status: 409,
      json: async () => ({ error: { code: "citation_ledger_invalid", message: "x" } }),
    } as unknown as Response);
    expect(await downloadExport("ws1", "composed")).toEqual({ ok: false, kind: "ledger_invalid" });
  });

  it("flags no-draft on 404", async () => {
    mockFetch({ ok: false, status: 404, json: async () => ({}) } as unknown as Response);
    expect(await downloadExport("ws1", "raw")).toEqual({ ok: false, kind: "no_draft" });
  });
});
