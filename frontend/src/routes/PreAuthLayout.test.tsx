import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MantineProvider } from "@mantine/core";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { theme } from "../theme/theme";
import { PreAuthLayout } from "./PreAuthLayout";

afterEach(() => { vi.restoreAllMocks(); });

it("renders children with NO shell chrome and fires NO /api/me on mount (anti-loop invariant)", () => {
  const get = vi.spyOn(clientMod.api, "GET");
  render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={["/x"]}>
          <Routes>
            <Route element={<PreAuthLayout />}>
              <Route path="/x" element={<p>child</p>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
  expect(screen.getByText("child")).toBeInTheDocument();
  expect(screen.queryByLabelText("Primary")).not.toBeInTheDocument(); // no ActivityBar
  // The layout itself makes no authenticated call (no /api/me).
  for (const call of get.mock.calls) expect(call[0]).not.toBe("/api/me");
});
