import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "react-router-dom";

import { useCitationSource } from "../api/citations";
import { ApiError } from "../api/client";
import { useDraft, useSaveSection } from "../api/editor";
import { CitationComposer } from "../cockpit/CitationComposer";
import { SourceViewer } from "../cockpit/SourceViewer";
import { CodeMirrorEditor } from "../editor/CodeMirrorEditor";

type Tab = { kind: "section"; index: number } | { kind: "full" };

// The banner copy for a failed save. ApiError.message is formatted "code: detail"
// (api/client.ts), so strip the machine-code prefix and show just the backend's
// human detail; a non-ApiError falls back to a generic line.
function saveErrorMessage(e: unknown): string {
  if (e instanceof ApiError) {
    const prefix = `${e.code}: `;
    return e.message.startsWith(prefix) ? e.message.slice(prefix.length) : e.message;
  }
  return "Save failed — please try again.";
}

export function Cockpit() {
  const { id = "" } = useParams();
  const qc = useQueryClient();
  const draft = useDraft(id);
  const save = useSaveSection(id);
  // PER-SECTION dirty buffers: edits to one section survive switching to another
  // section or to the Full-draft tab and back. A section is dirty iff it has an
  // entry here that differs from the fetched text (see isDirty).
  const [buffers, setBuffers] = useState<Record<number, string>>({});
  // Remember the last section we were on so returning from Full draft restores it
  // (spec line 223: preserve the selection across swaps) instead of resetting to 0.
  const [lastSectionIndex, setLastSectionIndex] = useState(0);
  const [tab, setTab] = useState<Tab>({ kind: "section", index: 0 });
  // The section index that 409'd; its banner offers Reload (discard) / Keep editing.
  const [staleSection, setStaleSection] = useState<number | null>(null);
  // A non-conflict save failure (400 structure / 429 busy / 500 / network): surfaced
  // as a dismissible banner so a save NEVER fails silently. The buffer is preserved
  // (edits kept); the user can fix and re-Save. Cleared on a new edit, a section
  // switch, or the start of the next save.
  const [saveError, setSaveError] = useState<string | null>(null);
  // SP2a-4a citation peek: clicking a pill sets peekEv (center split); ⇱ Pin
  // promotes it to pinnedEv (right pane). Both drive a useCitationSource query.
  const [peekEv, setPeekEv] = useState<string | null>(null);
  const [pinnedEv, setPinnedEv] = useState<string | null>(null);
  // SP2a-4b select-to-cite: the editor's current selection (claim span) + whether
  // the composer is open. The Cite control is gated on a SAVED (non-dirty) section
  // because the anchor must exist in the on-disk proposal.md.
  const [selection, setSelection] = useState<{ from: number; to: number; text: string } | null>(null);
  const [composerOpen, setComposerOpen] = useState(false);
  // These hooks MUST run before the early returns below so hook order is stable.
  const peek = useCitationSource(id, peekEv);
  const pinned = useCitationSource(id, pinnedEv);

  if (draft.isLoading) return <main aria-busy="true">Loading cockpit…</main>;
  if (draft.isError || !draft.data) {
    return (
      <main role="alert">
        <h1>Could not load this workspace</h1>
      </main>
    );
  }

  const { head, can_edit, sections } = draft.data;
  const fullText = sections.map((s) => s.text).join("");

  // The active section + its editor value. For the current section we prefer its
  // in-flight buffer over the fetched text; once the buffer is cleared (after a
  // successful save or a Reload) we fall back to the fetched/served text — which is
  // what makes the conflict "Reload" affordance actually reload (Codex #3).
  const activeIndex = tab.kind === "section" ? tab.index : -1;
  const activeSection = activeIndex >= 0 ? sections[activeIndex] : undefined;
  const editorValue =
    tab.kind === "full"
      ? fullText
      : (buffers[activeIndex] ?? activeSection?.text ?? "");

  const isDirty = (idx: number) =>
    buffers[idx] !== undefined && buffers[idx] !== sections[idx]?.text;

  // Switch the CURRENT section only. MUST NOT clear other sections' buffers — their
  // unsaved edits survive (Codex #2). The conflict banner is per-section, so clear it.
  function selectSection(index: number) {
    setTab({ kind: "section", index });
    setLastSectionIndex(index);
    setStaleSection(null);
    setSaveError(null);
    // Drop any stale claim span / open composer: the selection belonged to the
    // section we just left and its anchor won't exist in the new section.
    setSelection(null);
    setComposerOpen(false);
  }

  function onEditorChange(next: string) {
    if (activeIndex < 0) return;
    setSaveError(null);
    setBuffers((b) => ({ ...b, [activeIndex]: next }));
  }

  // Discard this section's edits and show the freshly-fetched/served text. Clearing
  // the buffer makes editorValue fall back to draft text (so the editor visibly
  // reloads — fixing the inert reload), and the refetch pulls the latest server bytes.
  function reloadSection(index: number) {
    setBuffers((b) => {
      const next = { ...b };
      delete next[index];
      return next;
    });
    setStaleSection(null);
    void draft.refetch();
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
      // Optimistically reconcile the cache for THIS section from the save response
      // (head + section_sha; Task 2 dependency) BEFORE clearing the buffer, so the
      // editor never flashes the pre-save draft.data while a refetch is in flight,
      // and the next edit's anti-clobber base is correct (Codex #3).
      qc.setQueryData(["draft", id], (prev: typeof draft.data | undefined) => {
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
        // Spec line 194: a stale save must NEVER drop the user's buffer. Keep it and
        // prompt; "Reload" discards, "Keep editing" dismisses (re-save re-conflicts
        // until reloaded).
        setStaleSection(index);
        return;
      }
      // Any other failure (400 structure, 429 busy, 500, network) must SURFACE, not
      // fail silently as an unhandled async rejection. A non-conflict failure also
      // supersedes a prior stale-conflict prompt, so clear it (never stack two
      // banners). The buffer is preserved — the user can fix and re-Save.
      setStaleSection(null);
      setSaveError(saveErrorMessage(e));
    }
  }

  // Clicking a citation pill opens the source peek (SP2a-4a). select-to-cite
  // (writing a NEW citation) is SP2a-4b.
  const onPillClick = (_citekey: string, evId: string) => setPeekEv(evId);

  return (
    <main className="cockpit">
      <nav aria-label="Outline">
        <ul>
          {sections.map((s) => (
            <li key={s.index}>
              <button
                type="button"
                aria-current={tab.kind === "section" && tab.index === s.index}
                onClick={() => selectSection(s.index)}
              >
                {(s.heading || "preamble") + (isDirty(s.index) ? " •" : "")}
              </button>
            </li>
          ))}
        </ul>
      </nav>

      <section className="cockpit-main">
        <div role="tablist" aria-label="View">
          <button
            type="button"
            role="tab"
            aria-selected={tab.kind === "section"}
            onClick={() => selectSection(lastSectionIndex)}
          >
            Section
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab.kind === "full"}
            onClick={() => setTab({ kind: "full" })}
          >
            Full draft
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

        {/* ONE persistent EditorView across tab swaps — no `key` remount. The
            controlled `value` effect swaps section bytes and the readOnly
            Compartment (Task 6) flips edit/read-only, so the EditorView instance
            (and its scroll container) survives Section<->Full-draft swaps; the
            restored lastSectionIndex preserves which section you return to
            (spec line 223). */}
        <CodeMirrorEditor
          value={editorValue}
          onChange={tab.kind === "full" ? () => {} : onEditorChange}
          requireHeading={tab.kind === "section" && activeIndex > 0}
          readOnly={tab.kind === "full" || !can_edit}
          onPillClick={onPillClick}
          onSelectionChange={tab.kind === "section" ? setSelection : undefined}
        />

        {can_edit && tab.kind === "section" && (
          <div className="cockpit-section-actions">
            <button
              type="button"
              onClick={onSave}
              disabled={save.isPending || !isDirty(activeIndex)}
            >
              Save section
            </button>
            {/* The anchor must already exist on disk, so cite is only offered on a
                SAVED (non-dirty) section; dirty -> disabled with a hint to save. */}
            <button
              type="button"
              className="cite-selection-btn"
              disabled={!selection || isDirty(activeIndex)}
              title={isDirty(activeIndex) ? "Save this section first" : undefined}
              onClick={() => setComposerOpen(true)}
            >
              Cite selection
            </button>
          </div>
        )}
      </section>

      {peekEv !== null && (
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

      {pinnedEv !== null && (
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

      {composerOpen && selection && activeSection && (
        <section className="cockpit-composer">
          <CitationComposer
            workspaceId={id}
            claim={{
              section_index: activeSection.index,
              base_commit_sha: head,
              base_section_sha: activeSection.sha,
              anchor: selection.text,
            }}
            onDone={() => {
              setComposerOpen(false);
              setSelection(null);
              void draft.refetch();
            }}
            onCancel={() => setComposerOpen(false)}
          />
        </section>
      )}
    </main>
  );
}
