import { Anchor, Text, Title } from "@mantine/core";
import { Link } from "react-router-dom";

export function AccountNoAccess() {
  return (
    <>
      <Title order={1} size="h4" ta="center">
        No access
      </Title>
      <Text ta="center" c="dimmed" size="sm">
        This account does not have access. If you think this is a mistake, contact
        an administrator.
      </Text>
      <Text ta="center" size="sm">
        <Anchor component={Link} to="/login">
          Back to sign in
        </Anchor>
      </Text>
    </>
  );
}
