import { MantineProvider } from "@mantine/core";
import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { theme } from "../theme/theme";
import { StructuredFields, type StructuredValues } from "./StructuredFields";

const EMPTY: StructuredValues = {
  areas: "", career_stage: "", applicant_types: "", role: "", years_since_phd: "",
  org_type: "", capabilities: "", patient_populations: "", scale: "",
  funding_instruments: "", funder_exclusions: "", country: "", host_institution: "",
};

function renderSF(kind: string) {
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <StructuredFields kind={kind} values={EMPTY} onChange={vi.fn()} />
    </MantineProvider>,
  );
}

it("shows org fields for an organizational_unit, hides individual fields", () => {
  renderSF("organizational_unit");
  expect(screen.getByLabelText(/organization type/i)).toBeInTheDocument();
  expect(screen.queryByLabelText(/career stage/i)).toBeNull();
});

it("shows individual fields for an individual, hides org fields", () => {
  renderSF("individual");
  expect(screen.getByLabelText(/career stage/i)).toBeInTheDocument();
  expect(screen.queryByLabelText(/organization type/i)).toBeNull();
});
