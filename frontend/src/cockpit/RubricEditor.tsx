import { NumberInput, Stack, Table, Text, Textarea, TextInput } from "@mantine/core";

import type { RubricCriterion } from "../api/rubric";

export function RubricEditor({
  criteria,
  funderNotes,
  onChange,
}: {
  criteria: RubricCriterion[];
  funderNotes: string;
  onChange: (next: { criteria: RubricCriterion[]; funderNotes: string }) => void;
}) {
  const total = criteria.reduce((s, c) => s + (Number(c.weight) || 0), 0);
  const patch = (i: number, field: keyof RubricCriterion, value: unknown) =>
    onChange({
      // spread preserves passthrough fields (subcriteria, etc.)
      criteria: criteria.map((c, idx) => (idx === i ? { ...c, [field]: value } : c)),
      funderNotes,
    });

  return (
    <Stack gap="md">
      <Table withTableBorder withColumnBorders horizontalSpacing="md" verticalSpacing="sm" layout="fixed">
        <Table.Thead>
          <Table.Tr>
            <Table.Th w="22%">Criterion</Table.Th>
            <Table.Th>Description</Table.Th>
            <Table.Th w={130}>Weight</Table.Th>
            <Table.Th w={64}>%</Table.Th>
            <Table.Th w={120}>Scale max</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {criteria.map((c, i) => (
            <Table.Tr key={i}>
              <Table.Td>
                <TextInput
                  aria-label={`Name for ${c.name}`}
                  value={c.name}
                  onChange={(e) => patch(i, "name", e.currentTarget.value)}
                />
              </Table.Td>
              <Table.Td>
                <Textarea
                  aria-label={`Description for ${c.name}`}
                  value={c.description}
                  autosize
                  minRows={2}
                  maxRows={6}
                  onChange={(e) => patch(i, "description", e.currentTarget.value)}
                />
              </Table.Td>
              <Table.Td>
                <NumberInput
                  aria-label={`Weight for ${c.name}`}
                  value={c.weight}
                  min={0}
                  step={0.01}
                  decimalScale={2}
                  onChange={(v) => patch(i, "weight", Number(v) || 0)}
                />
              </Table.Td>
              <Table.Td>
                <Text size="sm" c="dimmed">
                  {total > 0 ? Math.round((Number(c.weight) / total) * 100) : 0}%
                </Text>
              </Table.Td>
              <Table.Td>
                <NumberInput
                  aria-label={`Scale max for ${c.name}`}
                  value={c.scale_max}
                  min={1}
                  max={10}
                  onChange={(v) => patch(i, "scale_max", Number(v) || 1)}
                />
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      <Textarea
        label="Funder notes"
        value={funderNotes}
        autosize
        minRows={3}
        maxRows={10}
        onChange={(e) => onChange({ criteria, funderNotes: e.currentTarget.value })}
      />
    </Stack>
  );
}
