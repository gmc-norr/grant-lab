export type ExportKind = "raw" | "composed";

export type ExportResult =
  | { ok: true }
  | { ok: false; kind: "blocked"; blocked: { citekey: string; reason: string }[] }
  | { ok: false; kind: "ledger_invalid" }
  | { ok: false; kind: "no_draft" }
  | { ok: false; kind: "error" };

function filenameFrom(res: Response, fallback: string): string {
  const cd = res.headers.get("content-disposition") ?? "";
  const m = /filename="?([^"]+?)"?(?:;|$)/.exec(cd);
  return m?.[1] ?? fallback;
}

/** Fetch an /api export and trigger a native download. openapi-fetch is awkward
 *  for binary, so this uses raw fetch (the session cookie is same-origin). */
export async function downloadExport(workspaceId: string, kind: ExportKind): Promise<ExportResult> {
  const path = kind === "raw" ? "export.md" : "export-composed.md";
  const fallback = kind === "raw" ? "proposal.md" : "proposal-composed.md";
  const res = await fetch(`/api/workspaces/${workspaceId}/${path}`, {
    credentials: "same-origin",
  });
  if (res.ok) {
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filenameFrom(res, fallback);
    a.click();
    URL.revokeObjectURL(url);
    return { ok: true };
  }
  if (res.status === 404) return { ok: false, kind: "no_draft" };
  if (res.status === 409) {
    const body = (await res.json().catch(() => null)) as {
      error?: { code?: string; detail?: { blocked?: { citekey: string; reason: string }[] } };
    } | null;
    const code = body?.error?.code;
    if (code === "export_blocked")
      return { ok: false, kind: "blocked", blocked: body?.error?.detail?.blocked ?? [] };
    if (code === "citation_ledger_invalid") return { ok: false, kind: "ledger_invalid" };
  }
  return { ok: false, kind: "error" };
}
