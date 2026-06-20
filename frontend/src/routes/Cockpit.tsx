import { useParams } from "react-router-dom";

import { useDraft, useSaveSection } from "../api/editor";
import { ArtifactSectionEditor } from "../editor/ArtifactSectionEditor";

export function Cockpit() {
  const { id = "" } = useParams();
  const draft = useDraft(id);
  const save = useSaveSection(id);

  if (draft.isLoading) return <main aria-busy="true">Loading cockpit…</main>;
  if (draft.isError || !draft.data) {
    return (
      <main role="alert">
        <h1>Could not load this workspace</h1>
      </main>
    );
  }
  const { head, can_edit, sections } = draft.data;
  return (
    <ArtifactSectionEditor
      workspaceId={id}
      queryKey={["draft", id]}
      head={head}
      canEdit={can_edit}
      sections={sections}
      save={save}
      refetch={draft.refetch}
      citations
    />
  );
}
