import { Select, Stack, Text } from "@mantine/core";
import { useSearchParams } from "react-router-dom";

const STATUS_OPTIONS = [
  { value: "", label: "All statuses" },
  { value: "drafting", label: "Drafting" },
  { value: "submitted", label: "Submitted" },
  { value: "awarded", label: "Awarded" },
  { value: "rejected", label: "Rejected" },
  { value: "abandoned", label: "Abandoned" },
];

// Sidebar filter declared on the /cockpit route via `handle.Sidebar`. Binds the
// status filter to the URL (?status); the list reads the same URL, so the two
// stay in sync with no props across the shell boundary.
export function WorkspacesFilters() {
  const [sp, setSp] = useSearchParams();
  const status = sp.get("status") ?? "";
  return (
    <Stack gap="sm" p="sm">
      <Text component="h2" tt="uppercase" fz="0.7rem" fw={600} c="dimmed">Filters</Text>
      <Select
        label="Status"
        data={STATUS_OPTIONS}
        value={status}
        allowDeselect={false}
        onChange={(v) =>
          setSp((prev) => {
            const next = new URLSearchParams(prev);
            if (v) next.set("status", v);
            else next.delete("status");
            return next;
          })
        }
      />
    </Stack>
  );
}
