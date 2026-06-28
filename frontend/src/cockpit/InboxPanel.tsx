import { Stack, Text } from "@mantine/core";

import { useDraft, useSpec } from "../api/editor";
import { ProposalReview } from "./ProposalReview";

type Section = { index: number; text: string };

// `count` is single-sourced from BottomPanel's useInboxCount (the badge), so the
// badge and this panel's empty-state are driven by the SAME number. The two
// <ProposalReview> below self-fetch the same ["proposed-changes", id, artifact]
// query keys the badge reads — React Query dedupes them to one network call per
// artifact, so the rows shown here cannot diverge from the badge count.
export function InboxPanel({ id, count }: { id: string; count: number }) {
  const draft = useDraft(id);
  const spec = useSpec(id);

  const draftSections: Section[] = (draft.data?.sections ?? []).map((s) => ({
    index: s.index,
    text: s.text,
  }));
  const specSections: Section[] = (spec.data?.sections ?? []).map((s) => ({
    index: s.index,
    text: s.text,
  }));
  const specLocked = Boolean(spec.data?.locked);

  if (count === 0) {
    return <Text c="dimmed" p="sm">Nothing pending.</Text>;
  }
  return (
    <Stack gap="md">
      <ProposalReview workspaceId={id} artifact="draft" currentSections={draftSections} />
      <ProposalReview
        workspaceId={id}
        artifact="spec"
        currentSections={specSections}
        locked={specLocked}
      />
    </Stack>
  );
}
