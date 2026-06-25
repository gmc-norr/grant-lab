import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import { useCitationSource } from "../api/citations";
import { ApiError } from "../api/client";
import { useCockpitNav } from "../cockpit/cockpitNav";
import { CitationComposer } from "../cockpit/CitationComposer";
import { resetEditorStore, setCursor, setDirty, setSave } from "../cockpit/editorStore";
import { SourceViewer } from "../cockpit/SourceViewer";
import { CodeMirrorEditor } from "./CodeMirrorEditor";

type Section = { index: number; heading: string; text: string; sha: string };

type SaveVars = {
  index: number;
  content: string;
  base_commit_sha: string;
  base_section_sha: string;
};
type SaveResultLike = { head: string; section_sha: string };

export type ArtifactSectionEditorProps = {
  workspaceId: string;
  /** TanStack cache key for this artifact, e.g. ["draft", id] | ["spec", id]. */
  queryKey: [string, string];
  head: string;
  canEdit: boolean;
  sections: Section[];
  save: { mutateAsync: (v: SaveVars) => Promise<SaveResultLike>; isPending: boolean };
  refetch: () => void | Promise<unknown>;
  /** Draft passes true (pills + select-to-cite). Spec passes false. */
  citations?: boolean;
  /** Label for the whole-artifact read-only View tab. */
  fullViewLabel?: string;
  /** When set, drives the peek panel to show a specific citation source. The `n`
   *  nonce lets the parent re-open the same evidence_id a second time. */
  openPeek?: { ev: string; n: number } | null;
};

// ApiError.message is "code: detail"; strip the code prefix, show the human detail.
function saveErrorMessage(e: unknown): string {
  if (e instanceof ApiError) {
    const prefix = `${e.code}: `;
    return e.message.startsWith(prefix) ? e.message.slice(prefix.length) : e.message;
  }
  return "Save failed — please try again.";
}

export function ArtifactSectionEditor({
  workspaceId,
  queryKey,
  head,
  canEdit,
  sections,
  save,
  refetch,
  citations = false,
  fullViewLabel = "Full draft",
  openPeek,
}: ArtifactSectionEditorProps) {
  const qc = useQueryClient();
  const { sec, setSec } = useCockpitNav();
  // The active SECTION index lives in the URL (?sec); the Section<->Full VIEW is a
  // local, editor-only toggle (it never crosses the sidebar boundary).
  const [view, setView] = useState<"section" | "full">("section");
  // PER-SECTION dirty buffers: edits to one section survive switching sections or
  // the Full tab and back. A section is dirty iff its buffer differs from the fetch.
  const [buffers, setBuffers] = useState<Record<number, string>>({});
  const [staleSection, setStaleSection] = useState<number | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [peekEv, setPeekEv] = useState<string | null>(null);
  useEffect(() => {
    if (openPeek) setPeekEv(openPeek.ev);
  }, [openPeek?.ev, openPeek?.n]);
  const [pinnedEv, setPinnedEv] = useState<string | null>(null);
  const [selection, setSelection] = useState<{ from: number; to: number; text: string } | null>(
    null,
  );
  const [composerOpen, setComposerOpen] = useState(false);
  const peek = useCitationSource(workspaceId, peekEv);
  const pinned = useCitationSource(workspaceId, pinnedEv);

  const byIndex = useMemo(() => new Map(sections.map((s) => [s.index, s])), [sections]);
  // Clamp the URL section to one that exists in THIS artifact. An out-of-range ?sec
  // (artifact switch / deep-link before load) renders section 0 now and history-
  // replaces the URL once sections resolve — no spurious back-history entry.
  const validSec = byIndex.has(sec) ? sec : (sections[0]?.index ?? 0);
  useEffect(() => {
    if (sections.length > 0 && !byIndex.has(sec)) setSec(validSec, { replace: true });
  }, [sec, validSec, sections.length, byIndex, setSec]);

  // Side-effects of moving to a new section (mirrors the old selectSection): drop a
  // stale conflict/save banner, the claim selection, and the composer; force Section
  // view. Runs whenever ?sec changes. Does NOT touch buffers (edits must survive).
  useEffect(() => {
    setStaleSection(null);
    setSaveError(null);
    setSelection(null);
    setComposerOpen(false);
    setView("section");
  }, [sec]);

  const activeIndex = view === "full" ? -1 : validSec;
  const activeSection = activeIndex >= 0 ? byIndex.get(activeIndex) : undefined;
  const fullText = sections.map((s) => s.text).join("");
  const editorValue =
    view === "full" ? fullText : (buffers[activeIndex] ?? activeSection?.text ?? "");

  const isDirty = (idx: number) =>
    buffers[idx] !== undefined && buffers[idx] !== byIndex.get(idx)?.text;

  // Publish the dirty section set (for the sidebar Outline • markers). Recompute when
  // buffers or the fetched sections change.
  useEffect(() => {
    setDirty(
      sections.filter((s) => buffers[s.index] !== undefined && buffers[s.index] !== s.text).map((s) => s.index),
    );
  }, [buffers, sections]);

  // Publish save-state for the status bar.
  useEffect(() => {
    if (save.isPending) setSave("saving");
    else if (saveError) setSave("error");
    else if (activeIndex >= 0 && isDirty(activeIndex)) setSave("dirty");
    else setSave("saved");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [save.isPending, saveError, buffers, activeIndex]);

  // Clear the store on unmount so a stale cursor/save/dirty never leaks to the status
  // bar of a non-cockpit view or the next artifact.
  useEffect(() => () => resetEditorStore(), []);

  function onEditorChange(next: string) {
    if (activeIndex < 0) return;
    setSaveError(null);
    setBuffers((b) => ({ ...b, [activeIndex]: next }));
  }

  function reloadSection(index: number) {
    setBuffers((b) => {
      const next = { ...b };
      delete next[index];
      return next;
    });
    setStaleSection(null);
    void refetch();
  }

  async function onSave() {
    if (!activeSection) return;
    const index = activeSection.index;
    const content = buffers[index] ?? activeSection.text;
    setSaveError(null);
    try {
      const result = await save.mutateAsync({
        index,
        content,
        base_commit_sha: head,
        base_section_sha: activeSection.sha,
      });
      qc.setQueryData(queryKey, (prev: { head: string; sections: Section[] } | undefined) => {
        if (!prev) return prev;
        return {
          ...prev,
          head: result.head,
          sections: prev.sections.map((s) =>
            s.index === index ? { ...s, text: content, sha: result.section_sha } : s,
          ),
        };
      });
      setBuffers((b) => {
        const next = { ...b };
        delete next[index];
        return next;
      });
      setStaleSection(null);
    } catch (e) {
      if (e instanceof ApiError && e.code === "conflict") {
        setStaleSection(index);
        return;
      }
      setStaleSection(null);
      setSaveError(saveErrorMessage(e));
    }
  }

  const onPillClick = (_citekey: string, evId: string) => setPeekEv(evId);

  return (
    <main className="cockpit">
      <section className="cockpit-main">
        <div role="tablist" aria-label="View">
          <button
            type="button"
            role="tab"
            aria-selected={view === "section"}
            onClick={() => setView("section")}
          >
            Section
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={view === "full"}
            onClick={() => setView("full")}
          >
            {fullViewLabel}
          </button>
        </div>

        {staleSection !== null && (
          <p role="alert">
            This section changed underneath you. Your edits are kept.{" "}
            <button type="button" onClick={() => reloadSection(staleSection)}>
              Reload (discard my edits)
            </button>{" "}
            <button type="button" onClick={() => setStaleSection(null)}>
              Keep editing
            </button>
          </p>
        )}

        {saveError !== null && (
          <p role="alert">
            {saveError}{" "}
            <button type="button" onClick={() => setSaveError(null)}>
              Dismiss
            </button>
          </p>
        )}

        <CodeMirrorEditor
          value={editorValue}
          onChange={view === "full" ? () => {} : onEditorChange}
          requireHeading={view === "section" && activeIndex > 0}
          readOnly={view === "full" || !canEdit}
          citations={citations}
          onPillClick={citations ? onPillClick : undefined}
          onSelectionChange={citations && view === "section" ? setSelection : undefined}
          onCursorChange={setCursor}
        />

        {canEdit && view === "section" && (
          <div className="cockpit-section-actions">
            <button
              type="button"
              onClick={onSave}
              disabled={save.isPending || !isDirty(activeIndex)}
            >
              Save section
            </button>
            {citations && (
              <button
                type="button"
                className="cite-selection-btn"
                disabled={!selection || isDirty(activeIndex)}
                title={isDirty(activeIndex) ? "Save this section first" : undefined}
                onClick={() => setComposerOpen(true)}
              >
                Cite selection
              </button>
            )}
          </div>
        )}
      </section>

      {citations && peekEv !== null && (
        <section className="cockpit-peek" aria-label="Citation source peek">
          {peek.isLoading && <p aria-busy="true">Loading source…</p>}
          {peek.isError && <p role="alert">Could not load the cited source.</p>}
          {peek.data && (
            <SourceViewer
              source={peek.data}
              pinned={false}
              onClose={() => setPeekEv(null)}
              onPin={() => {
                setPinnedEv(peekEv);
                setPeekEv(null);
              }}
              onNavigate={(evId) => setPeekEv(evId)}
            />
          )}
        </section>
      )}

      {citations && pinnedEv !== null && (
        <aside className="cockpit-pinned" aria-label="Pinned source">
          {pinned.isLoading && <p aria-busy="true">Loading source…</p>}
          {pinned.isError && <p role="alert">Could not load the cited source.</p>}
          {pinned.data && (
            <SourceViewer
              source={pinned.data}
              pinned
              onClose={() => setPinnedEv(null)}
              onPin={() => {}}
              onNavigate={(evId) => setPinnedEv(evId)}
            />
          )}
        </aside>
      )}

      {citations && composerOpen && selection && activeSection && (
        <section className="cockpit-composer">
          <CitationComposer
            workspaceId={workspaceId}
            claim={{
              section_index: activeSection.index,
              base_commit_sha: head,
              base_section_sha: activeSection.sha,
              anchor: selection.text,
            }}
            onDone={() => {
              setComposerOpen(false);
              setSelection(null);
              void refetch();
            }}
            onCancel={() => setComposerOpen(false)}
          />
        </section>
      )}
    </main>
  );
}
