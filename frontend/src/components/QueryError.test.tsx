import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import { renderWithProviders } from "../test-utils";
import { QueryError } from "./QueryError";

describe("QueryError", () => {
  it("names the failure, offers Retry, and calls refetch when pressed", async () => {
    const onRetry = vi.fn();
    renderWithProviders(<QueryError message="Couldn't load calls." onRetry={onRetry} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't load calls.");
    // The review's complaint was that the only way out of a feed error was a
    // browser reload: 0 Retry buttons on 9 error screens.
    expect(onRetry).toHaveBeenCalledTimes(0);
    await userEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("renders exactly one main landmark", () => {
    // The feed's old error state was a bare <p>: "no rail, no toolbar, no
    // <main>". The e2e walk counts exactly one landmark per route.
    renderWithProviders(<QueryError message="Couldn't load calls." onRetry={() => {}} />);
    expect(document.querySelectorAll("main")).toHaveLength(1);
  });

  it("shows the server's own sentence alongside the generic one", () => {
    renderWithProviders(
      <QueryError
        message="Couldn't load calls."
        error={new ApiError("internal_error", "the ranking service is down", 500)}
        onRetry={() => {}}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("the ranking service is down");
  });

  it("does not quote a non-API error — only the generic sentence", () => {
    renderWithProviders(
      <QueryError
        message="Couldn't load calls."
        error={new TypeError("Failed to fetch")}
        onRetry={() => {}}
      />,
    );
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Couldn't load calls.");
    expect(alert).not.toHaveTextContent("Failed to fetch");
  });

  it("disables Retry while a retry is in flight", () => {
    renderWithProviders(
      <QueryError message="Couldn't load calls." onRetry={() => {}} retrying />,
    );
    expect(screen.getByRole("button", { name: /retry/i })).toBeDisabled();
  });
});
