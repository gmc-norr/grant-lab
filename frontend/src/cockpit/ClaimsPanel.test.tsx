import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ClaimsPanel } from "./ClaimsPanel";

const h = vi.hoisted(() => ({
  ann: { isLoading: false, isError: false, data: undefined as unknown },
  insert: { mutate: vi.fn(), isPending: false },
  dismiss: { mutate: vi.fn(), isPending: false },
  reanalyze: { mutate: vi.fn(), isPending: false },
}));

vi.mock("../api/annotations", () => ({
  useAnnotations: () => h.ann,
  useInsertAnnotation: () => h.insert,
  useDismissAnnotation: () => h.dismiss,
  useReanalyzeSection: () => h.reanalyze,
}));

const SECTION = (over = {}) => ({
  index: 1,
  heading: "Body",
  section_sha: "a".repeat(64),
  occurrence: 0,
  job: { status: null, error: null },
  findings: [
    {
      id: 7,
      kind: "uncited_supported",
      claim_text: "This is a factual claim",
      evidence_quote: "verified words",
      ref_label: "Smith 2020",
      can_insert: true,
      sentence_ordinal: 0,
    },
  ],
  ...over,
});

afterEach(() => {
  h.ann = { isLoading: false, isError: false, data: undefined };
  h.insert = { mutate: vi.fn(), isPending: false };
  h.dismiss = { mutate: vi.fn(), isPending: false };
  h.reanalyze = { mutate: vi.fn(), isPending: false };
});

describe("ClaimsPanel", () => {
  it("renders a finding with an Insert button for the owner", () => {
    h.ann.data = { is_owner: true, sections: [SECTION()] };
    render(<ClaimsPanel id="ws1" />);
    expect(screen.getByText("This is a factual claim")).toBeInTheDocument();
    expect(screen.getByText("Smith 2020")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /insert/i }));
    expect(h.insert.mutate).toHaveBeenCalledWith(7);
  });

  it("hides actions for a viewer", () => {
    h.ann.data = { is_owner: false, sections: [SECTION()] };
    render(<ClaimsPanel id="ws1" />);
    expect(screen.getByText("This is a factual claim")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /insert/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /dismiss/i })).toBeNull();
  });

  it("dismiss requires a reason then submits", () => {
    h.ann.data = {
      is_owner: true,
      sections: [
        SECTION({
          findings: [
            {
              id: 9,
              kind: "uncited_no_match",
              claim_text: "no match claim",
              evidence_quote: null,
              ref_label: "reference",
              can_insert: false,
              sentence_ordinal: 0,
            },
          ],
        }),
      ],
    };
    render(<ClaimsPanel id="ws1" />);
    fireEvent.click(screen.getByRole("button", { name: /dismiss/i }));
    // Confirm is disabled until a reason is typed.
    const confirm = screen.getByRole("button", { name: /confirm dismiss/i });
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByPlaceholderText(/reason/i), {
      target: { value: "cited elsewhere" },
    });
    expect(confirm).not.toBeDisabled();
    fireEvent.click(confirm);
    expect(h.dismiss.mutate).toHaveBeenCalledWith({ annotationId: 9, reason: "cited elsewhere" });
  });

  it("shows analyzing… for a pending job and wires Reanalyze", () => {
    h.ann.data = {
      is_owner: true,
      sections: [SECTION({ findings: [], job: { status: "pending", error: null } })],
    };
    render(<ClaimsPanel id="ws1" />);
    expect(screen.getByText(/analyzing/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /reanalyze/i }));
    expect(h.reanalyze.mutate).toHaveBeenCalledWith(1);
  });

  it("renders the empty state when there are no sections", () => {
    h.ann.data = { is_owner: true, sections: [] };
    render(<ClaimsPanel id="ws1" />);
    expect(screen.getByText(/no draft yet/i)).toBeInTheDocument();
  });
});
