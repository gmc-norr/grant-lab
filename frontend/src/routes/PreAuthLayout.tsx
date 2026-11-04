import { Center, Paper, Stack } from "@mantine/core";
import { Outlet } from "react-router-dom";

import { BrandMark } from "../shell/BrandMark";

// SECOND top-level pathless layout (sibling to SimpleShell). Centered card on the
// app background, NO SimpleShell chrome. INVARIANT: this layout
// and its pages make NO authenticated /api call on mount — a useSession here
// would 401 -> unwrap -> redirectToLogin -> infinite loop. The public
// /api/auth/config call lives in the pages (Login/Register), not here.
export function PreAuthLayout() {
  return (
    <Center mih="100vh" bg="var(--app-editor)" p="md">
      <Paper
        component="main"
        w="100%"
        maw="22rem"
        p="xl"
        radius="md"
        shadow="sm"
        withBorder
        bg="var(--app-sidebar)"
      >
        <Stack gap="sm">
          <Center>
            <BrandMark size={36} />
          </Center>
          <Outlet />
        </Stack>
      </Paper>
    </Center>
  );
}
