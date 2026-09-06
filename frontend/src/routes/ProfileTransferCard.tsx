import { Button, Card, Checkbox, Group, Stack, Text, TextInput, Title } from "@mantine/core";
import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { useTransferProfileOwner } from "../api/profiles";

/** Move a profile to another user. Owner-or-admin gated server-side
 *  (`_profile_for_edit`), so mounted the same way `ProfileVisibilityCard` is:
 *  only when the profile-detail payload says `can_edit`.
 *
 *  `outgoingLostRead` is a PROP, deliberately never a second fetch this card
 *  owns. D17: an earlier draft of this feature computed the warning only from
 *  the PUT response, which does not exist until AFTER the transfer -- too
 *  late for a warning the note requires to land BEFORE the act. It is read
 *  instead off `GET /api/profiles/{id}`'s own `outgoing_lost_read` field,
 *  which the caller (ProfileDetail) is already holding.
 *
 *  That field already encodes the whole answer -- false for an admin owner
 *  (`transfer_read_effect` short-circuits on `readable_profile_ids`'s admin
 *  arm) and false for anyone who is not the owner at all. So this component
 *  must render it AS GIVEN and must not re-derive "is the caller the owner"
 *  from anything else (e.g. `can_edit`, which is also true for a non-owner
 *  admin) -- that reconstruction is exactly the bug the note records once.
 */
export function ProfileTransferCard({
  profileId,
  outgoingLostRead,
}: {
  profileId: number;
  outgoingLostRead: boolean;
}) {
  const transfer = useTransferProfileOwner(profileId);
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [keepReadAccess, setKeepReadAccess] = useState(false);

  return (
    <Card withBorder radius="md" p="md" bg="var(--app-sidebar)" mb="md">
      <Title order={2} size="h5" mb="sm">
        Transfer ownership
      </Title>
      {outgoingLostRead && (
        <Text role="alert" size="sm" c="red" mb="sm">
          You will lose access to this profile once it is transferred, unless you keep read
          access below.
        </Text>
      )}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          transfer.mutate(
            { email: email.trim(), keep_read_access: keepReadAccess },
            {
              onSuccess: (result) => {
                setEmail("");
                // The REALIZED outcome, off the PUT response -- distinct from
                // the prop above, which is the pre-act hypothetical.
                //
                // BOTH conjuncts are load-bearing, and the second one is the
                // fix for a real bug. `outgoing_lost_read` on the PUT response
                // describes the PREVIOUS OWNER, not the caller: the server
                // computes it for `session.get(User, p.created_by)` read
                // before the ownership write (web/api/profiles.py). So an
                // ADMIN transferring somebody else's private profile gets
                // `true` -- the previous owner lost read, the admin lost
                // nothing and can still read it through the admin arm -- and
                // navigating on that alone yanked them off a page they can
                // perfectly well see.
                //
                // `outgoingLostRead` (the GET prop) is `false` for a non-owner
                // caller, because transfer_read_effect returns False when
                // `profile.created_by != outgoing.id`. Conjoining the two is
                // therefore exactly "the caller IS the outgoing owner AND they
                // really lost it": owner -> navigate; owner+keep_read_access
                // -> stay; admin -> stay.
                if (outgoingLostRead && result.outgoing_lost_read) navigate("/profiles");
              },
            },
          );
        }}
      >
        <Stack gap="sm">
          <TextInput
            label="New owner's email"
            placeholder="colleague@umu.se"
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.currentTarget.value)}
          />
          <Checkbox
            // NOT "my own": the server writes the ProfileViewer row for
            // `outgoing` -- the CURRENT owner (web/api/profiles.py). When an
            // admin transfers somebody else's profile this grants access to
            // that third party and does nothing for the admin, who already
            // reads everything. The label must describe who actually gets the
            // row.
            label="Keep the current owner's read access to this profile"
            checked={keepReadAccess}
            onChange={(e) => setKeepReadAccess(e.currentTarget.checked)}
          />
          <Group gap="sm" align="center">
            <Button type="submit" disabled={transfer.isPending || !email.trim()}>
              Transfer
            </Button>
            {transfer.isError && (
              // ONE generic message for an unknown address and an inactive one alike
              // (Open-8) -- mirrors ProfileVisibilityCard's add-viewer refusal. The
              // server already collapses both into the same 400, so this is not
              // re-deriving anything; it is refusing to invent a MORE specific
              // message than the one the server chose to send.
              <Text role="alert" c="red" size="sm">
                Couldn't transfer this profile. Check the email address and try again.
              </Text>
            )}
          </Group>
          {transfer.isSuccess && (
            <Text size="sm" c="dimmed">
              Transferred to {transfer.data.owner.email}.
            </Text>
          )}
        </Stack>
      </form>
    </Card>
  );
}
