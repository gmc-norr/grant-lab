import {
  Anchor, Badge, Box, Button, Card, Group, NativeSelect, SegmentedControl, Stack, Text,
  TextInput, Title,
} from "@mantine/core";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { apiErrorMessage } from "../api/client";
import { useBoardsList, useCreateBoard, type Board } from "../api/boards";
import { useProfilesList } from "../api/profiles";

/** The board list: link per row, plus the one form that gets a board onto
 *  this list in the first place (there is no separate "/boards/new" route in
 *  this plan -- creating a board is a tiny, single-screen act, so it lives
 *  inline here rather than behind its own navigation).
 *
 *  Any user who can EDIT a profile may create a board on it (§4.3); the
 *  profile picker below lists every profile the caller can READ, which is
 *  wider. Picking one the caller cannot edit is refused server-side (the
 *  same `_profile_for_edit` rule every board route uses) and surfaces here as
 *  the ordinary create-error message -- this list has no `can_edit` flag to
 *  pre-filter with (`ProfileListItemOut` does not carry one).
 *
 *  GET /api/boards is READ-gated, not edit-gated -- a row here can still 403
 *  when opened (BoardDetail handles that); this list does not claim
 *  otherwise.
 */
export function BoardsList() {
  const boards = useBoardsList();
  const profiles = useProfilesList();
  const navigate = useNavigate();
  const create = useCreateBoard();

  const [title, setTitle] = useState("");
  const [profileId, setProfileId] = useState("");
  const [orientation, setOrientation] = useState<Board["orientation"]>("landscape");

  const profileName = (id: number) => profiles.data?.profiles.find((p) => p.id === id)?.name;

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!title.trim() || !profileId) return;
    create.mutate(
      { title: title.trim(), profile_id: Number(profileId), orientation },
      { onSuccess: (board) => navigate(`/boards/${board.id}`) },
    );
  }

  return (
    <Box component="main" maw="48rem" mx="auto" px="xs" pt="md" pb="xl">
      <Title order={1} mb="md">
        Corridor boards
      </Title>

      {boards.isLoading && <Text size="sm">Loading…</Text>}
      {boards.isError && (
        <Text role="alert" c="red" size="sm">
          Couldn't load boards.
        </Text>
      )}
      {boards.data && boards.data.boards.length === 0 && (
        <Text size="sm" c="dimmed" mb="md">
          No boards yet.
        </Text>
      )}
      {boards.data && boards.data.boards.length > 0 && (
        <Stack gap="xs" mb="lg">
          {boards.data.boards.map((b) => (
            <Card key={b.id} withBorder radius="md" p="sm" bg="var(--app-sidebar)">
              <Group justify="space-between" wrap="nowrap">
                <div>
                  <Anchor component={Link} to={`/boards/${b.id}`} fw={600}>
                    {b.title}
                  </Anchor>
                  <Text size="xs" c="dimmed">
                    {profileName(b.profile_id) ?? `Profile #${b.profile_id}`} · {b.orientation} ·{" "}
                    {b.approved_count} approved
                  </Text>
                </div>
                <Group gap="xs" wrap="nowrap">
                  {b.owner_undeliverable && (
                    <Badge color="red" variant="light">
                      Owner unreachable
                    </Badge>
                  )}
                  <Badge color={b.is_published ? "teal" : "gray"} variant="light">
                    {b.is_published ? "Published" : "Unpublished"}
                  </Badge>
                </Group>
              </Group>
            </Card>
          ))}
        </Stack>
      )}

      <Card withBorder radius="md" p="md" bg="var(--app-sidebar)">
        <Title order={2} size="h5" mb="sm">
          New board
        </Title>
        <form onSubmit={submit}>
          <Stack gap="sm">
            <TextInput
              label="Title"
              required
              value={title}
              onChange={(e) => setTitle(e.currentTarget.value)}
              description="Shown on the public wall once published — free text, so it should not name anything the profile's owner would not put on a corridor wall."
            />
            <NativeSelect
              label="Profile"
              required
              value={profileId}
              onChange={(e) => setProfileId(e.currentTarget.value)}
              data={[
                { value: "", label: "— choose a profile —" },
                ...(profiles.data?.profiles.map((p) => ({ value: String(p.id), label: p.name })) ?? []),
              ]}
            />
            <SegmentedControl
              aria-label="Orientation"
              value={orientation}
              onChange={(v) => setOrientation(v as Board["orientation"])}
              data={[
                { label: "Landscape", value: "landscape" },
                { label: "Portrait", value: "portrait" },
              ]}
            />
            <Group gap="sm" align="center">
              <Button type="submit" disabled={create.isPending}>
                Create board
              </Button>
              {create.isError && (
                <Text role="alert" c="red" size="sm">
                  {apiErrorMessage(create.error, "Couldn't create the board.")}
                </Text>
              )}
            </Group>
          </Stack>
        </form>
      </Card>
    </Box>
  );
}
