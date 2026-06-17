import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "react-router-dom";

import { ApiError } from "../api/client";
import { useDraft, useSaveSection } from "../api/editor";
import { CodeMirrorEditor } from "../editor/CodeMirrorEditor";

type Tab = { kind: "section"; index: number } | { kind: "full" };

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
  }

  function onEditorChange(next: string) {
    if (activeIndex < 0) return;
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
      } else {
        throw e;
      }
    }
  }

  // select-to-cite is SP2a-4: wire the callback but no-op it here.
  const onPillClick = (_citekey: string, _evId: string) => {
    /* SP2a-4 */
  };

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
        />

        {can_edit && tab.kind === "section" && (
          <button
            type="button"
            onClick={onSave}
            disabled={save.isPending || !isDirty(activeIndex)}
          >
            Save section
          </button>
        )}
      </section>
    </main>
  );
}
