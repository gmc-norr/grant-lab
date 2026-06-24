import { Select } from "@mantine/core";
import { useSearchParams } from "react-router-dom";

import styles from "./WorkspacesFilters.module.css";

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
    <div className={styles.filters}>
      <h2 className={styles.heading}>Filters</h2>
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
    </div>
  );
}
