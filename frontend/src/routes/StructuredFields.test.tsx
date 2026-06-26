import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { StructuredFields, type StructuredValues } from "./StructuredFields";

const EMPTY: StructuredValues = {
  areas: "", career_stage: "", applicant_types: "", role: "", years_since_phd: "",
  org_type: "", capabilities: "", patient_populations: "", scale: "",
  funding_instruments: "", funder_exclusions: "", country: "", host_institution: "",
};

it("shows org fields for an organizational_unit, hides individual fields", () => {
  render(<StructuredFields kind="organizational_unit" values={EMPTY} onChange={vi.fn()} />);
  expect(screen.getByLabelText(/organization type/i)).toBeInTheDocument();
  expect(screen.queryByLabelText(/career stage/i)).toBeNull();
});

it("shows individual fields for an individual, hides org fields", () => {
  render(<StructuredFields kind="individual" values={EMPTY} onChange={vi.fn()} />);
  expect(screen.getByLabelText(/career stage/i)).toBeInTheDocument();
  expect(screen.queryByLabelText(/organization type/i)).toBeNull();
});
