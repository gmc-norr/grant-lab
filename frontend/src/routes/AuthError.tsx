import { Anchor, Text, Title } from "@mantine/core";
import { Link } from "react-router-dom";

export function AuthError() {
  return (
    <>
      <Title order={1} size="h4" ta="center">
        Sign-in could not be completed
      </Title>
      <Text ta="center" c="dimmed" size="sm">
        We couldn't sign you in. Please try again.
      </Text>
      <Text ta="center" c="dimmed" size="sm">
        If you already have an account with this email, sign in your usual way, then
        connect Google from your account settings.
      </Text>
      <Text ta="center" size="sm">
        <Anchor component={Link} to="/login">
          Back to sign in
        </Anchor>
      </Text>
    </>
  );
}
