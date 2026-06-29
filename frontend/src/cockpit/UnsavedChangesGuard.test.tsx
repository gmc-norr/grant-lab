import { act, render, screen } from "@testing-library/react";
import { fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { RouterProvider, createMemoryRouter, useNavigate } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { resetEditorStore, setDirty } from "./editorStore";
import { UnsavedChangesGuard } from "./UnsavedChangesGuard";

afterEach(() => {
  vi.restoreAllMocks();
  // resetEditorStore() emits to a still-mounted guard (this afterEach runs before
  // Testing-Library's auto-cleanup), so flush that re-render inside act().
  act(() => resetEditorStore());
});

it("registers a beforeunload handler ONLY while a buffer is dirty", () => {
  const addSpy = vi.spyOn(window, "addEventListener");
  const removeSpy = vi.spyOn(window, "removeEventListener");
  // Clean: no beforeunload handler.
  const { rerender } = render(
    <MemoryRouter>
      <UnsavedChangesGuard />
    </MemoryRouter>,
  );
  expect(addSpy.mock.calls.some(([t]) => String(t) === "beforeunload")).toBe(false);

  // Dirty -> the handler is registered. setDirty emits to the mounted guard, so act().
  act(() => setDirty([1]));
  rerender(
    <MemoryRouter>
      <UnsavedChangesGuard />
    </MemoryRouter>,
  );
  expect(addSpy.mock.calls.some(([t]) => String(t) === "beforeunload")).toBe(true);

  // Clean again -> the handler is removed (cleanup ran). setDirty emits → act().
  act(() => setDirty([]));
  rerender(
    <MemoryRouter>
      <UnsavedChangesGuard />
    </MemoryRouter>,
  );
  expect(removeSpy.mock.calls.some(([t]) => String(t) === "beforeunload")).toBe(true);
});

it("the beforeunload handler prevents default (would prompt) while dirty", () => {
  setDirty([0]);
  render(
    <MemoryRouter>
      <UnsavedChangesGuard />
    </MemoryRouter>,
  );
  const e = new Event("beforeunload", { cancelable: true }) as BeforeUnloadEvent;
  const prevented = !window.dispatchEvent(e);
  expect(prevented).toBe(true); // preventDefault was called -> browser shows its prompt
});

function GuardedApp() {
  const navigate = useNavigate();
  return (
    <>
      <UnsavedChangesGuard />
      <button type="button" onClick={() => navigate("/other")}>
        leave
      </button>
      <p>HOME</p>
    </>
  );
}

function dataRouter(initial = "/cockpit/ws-1") {
  return createMemoryRouter(
    [
      { path: "/cockpit/ws-1", element: <GuardedApp /> },
      { path: "/other", element: <p>OTHER PAGE</p> },
    ],
    { initialEntries: [initial] },
  );
}

it("blocks an in-app navigation while dirty and STAYS when the user cancels the confirm", () => {
  setDirty([2]);
  vi.spyOn(window, "confirm").mockReturnValue(false); // user cancels
  render(<RouterProvider router={dataRouter()} />);
  fireEvent.click(screen.getByRole("button", { name: /leave/i }));
  // Navigation was blocked + reset: still on HOME, never reached OTHER PAGE.
  expect(screen.getByText("HOME")).toBeInTheDocument();
  expect(screen.queryByText("OTHER PAGE")).toBeNull();
  expect(window.confirm).toHaveBeenCalled();
});

it("proceeds with the navigation when the user confirms the discard", () => {
  setDirty([2]);
  vi.spyOn(window, "confirm").mockReturnValue(true); // user confirms discard
  render(<RouterProvider router={dataRouter()} />);
  fireEvent.click(screen.getByRole("button", { name: /leave/i }));
  expect(screen.getByText("OTHER PAGE")).toBeInTheDocument();
});

it("does NOT block navigation when there are no unsaved edits", () => {
  // clean store
  const confirmSpy = vi.spyOn(window, "confirm");
  render(<RouterProvider router={dataRouter()} />);
  fireEvent.click(screen.getByRole("button", { name: /leave/i }));
  expect(screen.getByText("OTHER PAGE")).toBeInTheDocument();
  expect(confirmSpy).not.toHaveBeenCalled();
});

it("does not crash under a non-data router (useBlocker is isolated behind the data-router check)", () => {
  // Production uses createBrowserRouter (a data router), but many Cockpit tests mount
  // under a plain MemoryRouter. The guard must render harmlessly there (no useBlocker).
  setDirty([1]);
  expect(() =>
    render(
      <MemoryRouter>
        <UnsavedChangesGuard />
      </MemoryRouter>,
    ),
  ).not.toThrow();
});
