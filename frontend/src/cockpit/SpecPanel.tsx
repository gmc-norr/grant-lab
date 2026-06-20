import { useInitSpec, useSaveSpecSection, useSpec } from "../api/editor";
import { ArtifactSectionEditor } from "../editor/ArtifactSectionEditor";

export function SpecPanel({ id }: { id: string }) {
  const spec = useSpec(id);
  const save = useSaveSpecSection(id);
  const init = useInitSpec(id);

  if (spec.isLoading) return <main aria-busy="true">Loading spec…</main>;
  if (spec.isError || !spec.data) {
    return (
      <main role="alert">
        <h1>Could not load the spec</h1>
      </main>
    );
  }
  const { head, can_edit, sections } = spec.data;
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
  return (
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
  );
}
