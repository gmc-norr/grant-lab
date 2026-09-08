// frontend/src/routes/CallDetail.tsx
import { Anchor, Button, Group, Stack, Text } from "@mantine/core";
import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { useCallDetail, useHideCall, useSetDecisionNote, useUnhideCall } from "../api/calls";
import { CallDetailBody } from "./CallDetailBody";
import { DecisionNoteStrip } from "./DecisionNoteStrip";

export function CallDetail() {
  const { id = "" } = useParams();
  const callId = Number(id);
  const [sp] = useSearchParams();
  const profileParam = sp.get("profile");
  const profile = profileParam != null && profileParam !== "" ? Number(profileParam) : null;
  const navigate = useNavigate();
  // THE URL IS THE STATE (D11). The reader arrived from a triage view on some
  // profile, and `to="/"` would land them in the Inbox of no profile in
  // particular — not where they were. `sp` is what they arrived with, verbatim.
  const backTo = sp.toString() ? `/?${sp.toString()}` : "/";
  const hide = useHideCall();
  const unhide = useUnhideCall();
  const [confirmHide, setConfirmHide] = useState(false);
  // THREE states, because the server's answer is a SEED and not the truth for
  // the rest of the session. `null` defers to `call.decision_state`; an object
  // is "hidden by this session" and remembers the profile the hide was made ON
  // so Undo deletes the same row even if the URL changes underneath us;
  // "undecided" is "un-hidden by this session" and must WIN over the seed --
  // this page's query key is ["call", id, profile], which
  // invalidateQueries(["calls"]) does not prefix-match, so nothing refetches
  // after the DELETE and a plain `??` would put the Undo straight back.
  const [override, setOverride] = useState<
    { id: number; profileId: number } | "undecided" | null
  >(null);
  const setNote = useSetDecisionNote();
  // WHETHER THE BOX IS UP, held apart from `hidden` so Escape can dismiss it
  // (D2 -- the prompt is ignorable) without also dismissing the Undo it sits
  // beside. Both are cleared together by the Undo itself.
  const [noteOpen, setNoteOpen] = useState(false);
  // What the note IS, after this session's edits. `undefined` means "nothing
  // committed here yet, use the server's value". Needed for the same reason as
  // `override`: the note mutation invalidates ["calls"], which does not
  // prefix-match this page's ["call", id, profile] key, so `call.decision_note`
  // stays at its page-load value for the whole session. Without this, closing
  // the editor would show the text the note USED to be.
  const [noteOverride, setNoteOverride] = useState<string | null | undefined>(undefined);
  const { data, isLoading, isError, error } = useCallDetail(callId, profile);

  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data) {
    const notFound = (error as { code?: string } | null)?.code === "not_found";
    return <Text role="alert" p="md">{notFound ? "Call not found." : "Couldn't load this call."}</Text>;
  }

  const { call } = data;
  // PD-31b. Before this, the page learned its decision state ONLY from the
  // click that produced it -- so a refresh, a bookmark or a Hidden card's title
  // landed on "Hide this call" over an already-hidden call, with no Undo and
  // nowhere to render the `decision_note` the server was already sending.
  const hidden =
    override === "undecided"
      ? null
      : (override ??
        (call.decision_state === "hidden" && profile !== null
          ? { id: call.id, profileId: profile }
          : null));
  const shownNote = noteOverride === undefined ? call.decision_note : noteOverride;

  return (
    <Stack component="main" maw="52rem" mx="auto" px="xs" pt="md" pb="xl" gap="lg">
      <Anchor component={Link} to={backTo} size="sm">← All calls</Anchor>

      <CallDetailBody {...data} />

      <Group component="section" aria-label="Hide this call" align="center" gap="sm">
        {hidden !== null ? (
          <>
            <Text span c="dimmed">Call hidden.</Text>
            <Button variant="subtle" size="xs" disabled={unhide.isPending}
                    onClick={async () => {
                      // No per-call onSuccess: useUnhideCall (api/calls.ts)
                      // invalidates ["calls"] AND ["decision-counts"] itself, so
                      // the feed's rail is current whether or not this page is
                      // still mounted when the DELETE lands. In THIS edition
                      // there is no rail mounted at all and the counts half is a
                      // no-op — which is exactly why it must not be this page's
                      // job to remember it.
                      await unhide.mutateAsync(hidden);
                      setOverride("undecided");
                      // A note belongs to its decision and dies with it (D4),
                      // so the box goes with the row it was about -- leaving it
                      // up would solicit a reason whose write can only 404.
                      setNoteOpen(false);
                    }}>
              Undo
            </Button>
            <Button variant="subtle" size="xs" onClick={() => navigate(backTo)}>
              Back to all calls
            </Button>
            {/* WHY, asked where the decision was made (D2). The target carries
                the {id, profileId} this page already remembers for the Undo,
                for exactly the same reason: ?profile= can change under a late
                write, and the note must land on the decision it was about, not
                on whatever the URL says now.

                SEEDED with the note the server already holds. A hide made a
                second ago has none -- but arriving here from the Hidden view
                and pressing "Hide this call" re-hides `hidden -> hidden`, which
                the upsert's CASE deliberately PRESERVES the note through. An
                unseeded box over an existing reason is a blind overwrite: one
                Enter replaces the text the golden set exists to read, and the
                writer never saw what they destroyed.

                `call.decision_note` is the value from page load and stays that
                way while the box is open: this page's query key is
                `["call", id, profile]`, which `invalidateQueries(["calls"])`
                does not prefix-match, so neither the hide nor the note write
                refetches underneath the seed. */}
            {noteOpen ? (
              <DecisionNoteStrip
                target={{
                  callId: hidden.id,
                  profileId: hidden.profileId,
                  title: call.title,
                  state: "hidden",
                }}
                initialNote={shownNote}
                onSave={async (args) => {
                  const r = await setNote.mutateAsync({ ...args, expectedState: "hidden" });
                  // Only after the write LANDS. An optimistic update here would
                  // show a note the server rejected -- and this endpoint really
                  // does reject: a 409 when the decision moved under the box.
                  setNoteOverride(args.note);
                  return r;
                }}
                onDismiss={() => setNoteOpen(false)}
              />
            ) : (
              /* D12. The editor has to be reachable when there is NO note yet,
                 or "add a note later" is impossible on this page -- and since
                 the strip is deliberately ignorable, most hidden calls arrive
                 here with none. Mirrors TriageCard: the text itself is the
                 control, `title` gives the whole of it to a hover. */
              <Button
                variant="subtle"
                size="xs"
                data-field="decision-note"
                title={shownNote ?? undefined}
                onClick={() => setNoteOpen(true)}
              >
                {shownNote ?? "Add note"}
              </Button>
            )}
          </>
        ) : confirmHide ? (
          <>
            <Text span c="dimmed">Hide this call from your feed?</Text>
            <Button variant="default" size="xs" disabled={hide.isPending || profile === null}
                    onClick={async () => {
                      if (profile === null) return;
                      await hide.mutateAsync({ id: call.id, profileId: profile });
                      setConfirmHide(false);
                      setOverride({ id: call.id, profileId: profile });
                      // ON SUCCESS, NEVER OPTIMISTICALLY (D10). A failed hide
                      // that still asked for a reason would solicit a note for
                      // a decision that did not happen.
                      setNoteOpen(true);
                    }}>
              Yes, hide it
            </Button>
            <Button variant="subtle" size="xs" onClick={() => setConfirmHide(false)}>
              Cancel
            </Button>
          </>
        ) : (
          <>
            {/* A decision belongs to a profile: call_decisions.profile_id is NOT
                NULL, so with no ?profile= in scope there is nowhere to record the
                hide. The button is disabled rather than firing a 422 the user
                cannot act on. */}
            <Button variant="default" size="xs" disabled={profile === null}
                    onClick={() => setConfirmHide(true)}>
              Hide this call
            </Button>
            <Text c="dimmed" fz="0.8rem">
              {profile === null
                ? "Pick a profile to hide calls."
                : "Removes it from this profile's feed. Other users are unaffected."}
            </Text>
          </>
        )}
      </Group>
    </Stack>
  );
}
