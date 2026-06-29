import { Anchor, Text, Title } from "@mantine/core";
import { Link } from "react-router-dom";

export function AccountPending() {
  return (
    <>
      <Title order={1} size="h4" ta="center">
        Account awaiting approval
      </Title>
      <Text ta="center" c="dimmed" size="sm">
        Your sign-in worked. An administrator needs to approve your account before
        you can use Grant Lab. You'll be able to sign in once approved.
      </Text>
      <Text ta="center" size="sm">
        <Anchor component={Link} to="/login">
          Back to sign in
        </Anchor>
      </Text>
    </>
  );
}
