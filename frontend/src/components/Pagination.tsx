import { Button, Group, Text } from "@mantine/core";

type Props = {
  page: number;
  hasPrev: boolean;
  hasNext: boolean;
  onPage: (page: number) => void;
};

export function Pagination({ page, hasPrev, hasNext, onPage }: Props) {
  return (
    <Group component="nav" aria-label="Pagination" gap="sm" align="center">
      <Button variant="default" size="xs" disabled={!hasPrev} onClick={() => onPage(page - 1)}>
        Previous
      </Button>
      <Text span size="sm">Page {page}</Text>
      <Button variant="default" size="xs" disabled={!hasNext} onClick={() => onPage(page + 1)}>
        Next
      </Button>
    </Group>
  );
}
