import { MantineProvider } from "@mantine/core";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { theme } from "../theme/theme";
import { CitationsPanel } from "./CitationsPanel";

const h = vi.hoisted(() => ({
  audit: { isLoading: false, isError: false, data: undefined as unknown, refetch: vi.fn() },
  deep: { mutate: vi.fn(), isPending: false, reset: vi.fn(), data: undefined as unknown },
  meta: { data: { role: "owner" } as { role: string } | undefined },
}));

vi.mock("../api/audit", () => ({
  useCitationAudit: () => h.audit,
  useDeepCheck: () => h.deep,
}));
vi.mock("../api/workspace", () => ({ useCockpitMeta: () => h.meta }));

afterEach(() => {
  h.audit = { isLoading: false, isError: false, data: undefined, refetch: vi.fn() };
  h.deep = { mutate: vi.fn(), isPending: false, reset: vi.fn(), data: undefined };
  h.meta = { data: { role: "owner" } };
});

describe("CitationsPanel", () => {
  it("shows the no-draft empty state", () => {
    h.audit.data = { draft_present: false, issues: [], error_count: 0, warning_count: 0 };
    render(
      <MantineProvider theme={theme} defaultColorScheme="dark">
        <CitationsPanel id="ws1" onNavigate={vi.fn()} />
      </MantineProvider>,
    );
    expect(screen.getByText(/no draft yet/i)).toBeInTheDocument();
  });

  it("renders deterministic issues and routes View-source", () => {
    h.audit.data = {
      draft_present: true,
      error_count: 1,
      warning_count: 0,
      issues: [
        {
          type: "orphan_marker",
          severity: "error",
          citekey: "ghost",
          evidence_id: "ev-1",
          location: "§2",
          detail: "marker has no ledger row",
        },
      ],
    };
    const onNavigate = vi.fn();
    render(
      <MantineProvider theme={theme} defaultColorScheme="dark">
        <CitationsPanel id="ws1" onNavigate={onNavigate} />
      </MantineProvider>,
    );
    expect(screen.getByText("orphan_marker")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /view source/i }));
    expect(onNavigate).toHaveBeenCalledWith({ top_tab: "draft", evidence_id: "ev-1" });
  });

  it("renders errors before warnings (severity grouping)", () => {
    h.audit.data = {
      draft_present: true,
      error_count: 1,
      warning_count: 1,
      // backend order is warning-then-error; the panel must reorder error-first.
      issues: [
        { type: "metadata_unapproved", severity: "warning", citekey: "a", evidence_id: null, location: null, detail: "w" },
        { type: "orphan_marker", severity: "error", citekey: "b", evidence_id: null, location: null, detail: "e" },
      ],
    };
    render(
      <MantineProvider theme={theme} defaultColorScheme="dark">
        <CitationsPanel id="ws1" onNavigate={vi.fn()} />
      </MantineProvider>,
    );
    const items = screen.getAllByRole("listitem");
    expect(items[0]).toHaveTextContent("orphan_marker");
    expect(items[1]).toHaveTextContent("metadata_unapproved");
  });

  it("shows Deep check for the owner and runs it", () => {
    h.audit.data = { draft_present: true, issues: [], error_count: 0, warning_count: 0 };
    render(
      <MantineProvider theme={theme} defaultColorScheme="dark">
        <CitationsPanel id="ws1" onNavigate={vi.fn()} />
      </MantineProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: /deep check/i }));
    expect(h.deep.mutate).toHaveBeenCalled();
  });

  it("hides Deep check from a non-owner member", () => {
    h.meta = { data: { role: "member" } }; // the cockpit-meta API returns owner|member, never "viewer"
    h.audit.data = { draft_present: true, issues: [], error_count: 0, warning_count: 0 };
    render(
      <MantineProvider theme={theme} defaultColorScheme="dark">
        <CitationsPanel id="ws1" onNavigate={vi.fn()} />
      </MantineProvider>,
    );
    expect(screen.queryByRole("button", { name: /deep check/i })).not.toBeInTheDocument();
  });

  it("supersedes with deep-check results + summary + cap banner", () => {
    h.audit.data = { draft_present: true, issues: [], error_count: 0, warning_count: 0 };
    h.deep.data = {
      semantic_available: true,
      checked: 3,
      cap_exceeded: true,
      error_count: 1,
      warning_count: 0,
      issues: [
        {
          type: "semantic_contradicts",
          severity: "error",
          citekey: "smith2021",
          evidence_id: "ev-2",
          location: "§1",
          detail: "source contradicts the claim",
        },
      ],
    };
    render(
      <MantineProvider theme={theme} defaultColorScheme="dark">
        <CitationsPanel id="ws1" onNavigate={vi.fn()} />
      </MantineProvider>,
    );
    expect(screen.getByText("semantic_contradicts")).toBeInTheDocument();
    expect(screen.getByText(/checked 3/i)).toBeInTheDocument();
    expect(screen.getByText(/budget/i)).toBeInTheDocument();
  });

  it("resets deep-check data and refetches the baseline on Re-run", () => {
    h.audit.data = { draft_present: true, issues: [], error_count: 0, warning_count: 0 };
    h.deep.data = {
      semantic_available: true,
      checked: 1,
      cap_exceeded: false,
      error_count: 0,
      warning_count: 0,
      issues: [],
    };
    render(
      <MantineProvider theme={theme} defaultColorScheme="dark">
        <CitationsPanel id="ws1" onNavigate={vi.fn()} />
      </MantineProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: /re-run/i }));
    expect(h.deep.reset).toHaveBeenCalled();
    expect(h.audit.refetch).toHaveBeenCalled();
  });
});
