import { useInitSpec, useLockSpec, useSaveSpecSection, useSpec, useUnlockSpec } from "../api/editor";
import { ArtifactSectionEditor } from "../editor/ArtifactSectionEditor";

export function SpecPanel({ id }: { id: string }) {
  const spec = useSpec(id);
  const save = useSaveSpecSection(id);
  const init = useInitSpec(id);
  const lock = useLockSpec(id);
  const unlock = useUnlockSpec(id);

  if (spec.isLoading) return <main aria-busy="true">Loading spec…</main>;
  if (spec.isError || !spec.data) {
    return (
      <main role="alert">
        <h1>Could not load the spec</h1>
      </main>
    );
  }
  const { head, can_edit, sections, locked, locked_at, locked_by, can_manage_lock } = spec.data;
  // Explicit skeleton CTA — no silent commit-on-open. Same whitespace-only
  // predicate the init endpoint uses, so the CTA and the server seed agree.
  const isEmpty = sections.every((s) => s.text.trim() === "");
  if (isEmpty) {
    return (
      <main className="spec-empty">
        <h1>Spec</h1>
        <p>This proposal has no spec yet.</p>
        <button type="button" disabled={!can_edit || init.isPending} onClick={() => init.mutate()}>
          Start spec from skeleton
        </button>
      </main>
    );
  }
  const lockedOn = locked_at ? new Date(locked_at).toLocaleDateString() : "";
  return (
    <div className="spec-shell">
      {locked && (
        <p className="spec-lock-banner" role="status">
          🔒 Spec locked{locked_by ? ` by ${locked_by}` : ""}
          {lockedOn ? ` on ${lockedOn}` : ""}
        </p>
      )}
      {can_manage_lock && (
        <div className="spec-lock-bar">
          {locked ? (
            <button type="button" disabled={unlock.isPending} onClick={() => unlock.mutate()}>
              Unlock spec
            </button>
          ) : (
            <button type="button" disabled={lock.isPending} onClick={() => lock.mutate()}>
              Lock spec
            </button>
          )}
        </div>
      )}
      <ArtifactSectionEditor
        workspaceId={id}
        queryKey={["spec", id]}
        head={head}
        canEdit={can_edit}
        sections={sections}
        save={save}
        refetch={spec.refetch}
        citations={false}
        fullViewLabel="Full spec"
      />
    </div>
  );
}
