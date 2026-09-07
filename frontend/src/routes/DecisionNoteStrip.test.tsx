// D10's contract, one test per bullet. The strip is a separate component
// precisely so these can be written without standing up the whole feed.
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { describe, expect, it, vi } from "vitest";

import { DecisionNoteStrip } from "./DecisionNoteStrip";

const target = { callId: 7, profileId: 3, title: "ERC Synergy", state: "hidden" as const };

function setup(save = vi.fn().mockResolvedValue({})) {
  render(<DecisionNoteStrip target={target} onSave={save} onDismiss={vi.fn()} />);
  return save;
}

describe("D10 — the strip", () => {
  it("names the call it is about", () => {
    setup();
    expect(screen.getByText(/ERC Synergy/)).toBeInTheDocument();
  });

  it("discloses that an administrator can read it", () => {
    // D3. A privacy property the user cannot see is not a privacy property.
    setup();
    expect(screen.getByText(/administrator can read/i)).toBeInTheDocument();
  });

  it("commits on Enter, with the ids it was created for", async () => {
    const save = setup();
    const box = screen.getByRole("textbox");
    await userEvent.type(box, "wrong field{Enter}");
    await waitFor(() =>
      expect(save).toHaveBeenCalledWith({ id: 7, profileId: 3, note: "wrong field" }),
    );
  });

  it("commits on blur", async () => {
    const save = setup();
    await userEvent.type(screen.getByRole("textbox"), "too small");
    await userEvent.tab();
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
  });

  it("Escape reverts and does not save", async () => {
    const save = setup();
    await userEvent.type(screen.getByRole("textbox"), "oops{Escape}");
    expect(save).not.toHaveBeenCalled();
  });

  it("does not save on every keystroke", async () => {
    const save = setup();
    await userEvent.type(screen.getByRole("textbox"), "abcdef");
    expect(save).not.toHaveBeenCalled();
  });

  it("keeps the typed text and shows the error when the save fails", async () => {
    // Inside <StrictMode>, which is what main.tsx wraps the real app in and
    // what renderWithProviders does NOT. StrictMode dev-double-invokes effects
    // (mount -> cleanup -> mount), so a mounted-ref cleared only in cleanup is
    // false for the component's whole life -- and commit()'s `if
    // (!mounted.current) return` then swallows setError. Measured 2026-09-07:
    // without StrictMode here the test passes against that bug, and every
    // failed save in `pnpm dev` was silent while the user believed it saved.
    // Production builds do not double-invoke, which is why only this wrapper
    // can catch it. Precedent: collab/CollabEditor.test.tsx.
    const save = vi.fn().mockRejectedValue(new Error("boom"));
    render(
      <StrictMode>
        <DecisionNoteStrip target={target} onSave={save} onDismiss={vi.fn()} />
      </StrictMode>,
    );
    await userEvent.type(screen.getByRole("textbox"), "kept{Enter}");
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(screen.getByRole("textbox")).toHaveValue("kept");
  });

  // The three below are not in the brief. The first exists because `initialNote`
  // is in the contract for TASK 7's sake and every test above renders without
  // it -- a prop nothing exercises is a prop that is wrong when it is first
  // needed. The other two make `state` and `onDismiss` load-bearing for the
  // same reason.
  it("seeds the input from initialNote, and does not re-send what it was seeded with", async () => {
    const save = vi.fn().mockResolvedValue({});
    render(
      <DecisionNoteStrip
        target={target}
        initialNote="wrong panel"
        onSave={save}
        onDismiss={vi.fn()}
      />,
    );
    expect(screen.getByRole("textbox")).toHaveValue("wrong panel");
    // Focus and leave without typing. Blur commits (D10), so without the
    // "unchanged since the server saw it" check this fires a request that
    // writes back exactly what is already stored -- on every strip anyone opens
    // and ignores.
    await userEvent.click(screen.getByRole("textbox"));
    await userEvent.tab();
    expect(save).not.toHaveBeenCalled();
  });

  it("asks about the decision it was created for, in that decision's words", () => {
    setup();
    expect(screen.getByText(/Why did you hide/)).toBeInTheDocument();
    expect(screen.queryByText(/Why did you save/)).toBeNull();
    // The other branch, on a second render: a strip that always says "hide"
    // passes the assertion above.
    render(
      <DecisionNoteStrip
        target={{ ...target, state: "saved" }}
        onSave={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );
    expect(screen.getByText(/Why did you save/)).toBeInTheDocument();
  });

  it("Escape closes the strip, not just the text in it", async () => {
    const dismiss = vi.fn();
    render(<DecisionNoteStrip target={target} onSave={vi.fn()} onDismiss={dismiss} />);
    await userEvent.type(screen.getByRole("textbox"), "oops{Escape}");
    expect(dismiss).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("textbox")).toHaveValue("");
  });
});
