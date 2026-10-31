// D10's contract, one test per bullet. The strip is a separate component
// precisely so these can be written without standing up the whole feed.
import { act, render, screen, waitFor } from "@testing-library/react";
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

  it("reports HOW each commit happened, including one that writes nothing", async () => {
    // The signal a caller that REMOUNTS this strip on a save needs to tell an
    // Enter-commit (the user is still in the box; keep the caret) from a
    // blur-commit (they left; do not take it back). Fired BEFORE the
    // no-op check on purpose: it says where the caret belongs, not whether a
    // write happened -- so the third entry below is an Enter over unchanged
    // text, which sends nothing and still means "the user is here".
    const save = vi.fn().mockResolvedValue({});
    const commits: string[] = [];
    render(
      <DecisionNoteStrip
        target={target}
        onSave={save}
        onCommit={(via) => commits.push(via)}
        onDismiss={vi.fn()}
      />,
    );
    const box = screen.getByRole("textbox");
    await userEvent.type(box, "wrong field{Enter}");
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    await userEvent.type(box, " indeed");
    await userEvent.tab();
    await waitFor(() => expect(save).toHaveBeenCalledTimes(2));
    await userEvent.click(box);
    await userEvent.type(box, "{Enter}");
    expect(commits).toEqual(["enter", "blur", "enter"]);
    // The third commit really was a no-op: same text, so nothing was sent.
    expect(save).toHaveBeenCalledTimes(2);
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

// Outside review 2026-10-29, F3. Enter and blur both commit, so two saves for the
// same call can be outstanding at once, and the endpoint has no ordering: an older
// request that lands last overwrites the newer text. The fake server below records
// what the LAST completed write stored, and `settleNewestFirst` always completes the
// newest outstanding request first -- the order that lost "Second" in the review.
type Pending = { note: string | null; settle: () => void };

function fakeServer(failCalls: number[] = []) {
  let stored: string | null = null;
  let calls = 0;
  const pending: Pending[] = [];
  const save = vi.fn(
    (args: { note: string | null }) =>
      new Promise<void>((resolve, reject) => {
        const n = ++calls;
        pending.push({
          note: args.note,
          settle: () => {
            if (failCalls.includes(n)) {
              reject(new Error(`save ${n} failed`));
              return;
            }
            stored = args.note;
            resolve();
          },
        });
      }),
  );
  async function settleNewestFirst() {
    for (;;) {
      await act(() => new Promise((r) => setTimeout(r, 0)));
      const next = pending.pop();
      if (!next) return;
      await act(async () => next.settle());
    }
  }
  async function settleOldest() {
    await act(() => new Promise((r) => setTimeout(r, 0)));
    const next = pending.shift();
    if (next) await act(async () => next.settle());
    await act(() => new Promise((r) => setTimeout(r, 0)));
  }
  return { save, pending, settleNewestFirst, settleOldest, stored: () => stored };
}

describe("F3 — overlapping saves for one call", () => {
  it("never lets an older save overwrite newer text", async () => {
    const server = fakeServer();
    render(<DecisionNoteStrip target={target} onSave={server.save} onDismiss={vi.fn()} />);
    const box = screen.getByRole("textbox");
    await userEvent.type(box, "First{Enter}");
    await userEvent.type(box, " Second{Enter}");
    await server.settleNewestFirst();
    expect(server.stored()).toBe("First Second");
    expect(box).toHaveValue("First Second");
  });

  it("does not report an older failure once newer text has saved", async () => {
    const server = fakeServer([1]);
    render(<DecisionNoteStrip target={target} onSave={server.save} onDismiss={vi.fn()} />);
    const box = screen.getByRole("textbox");
    await userEvent.type(box, "First{Enter}");
    await userEvent.type(box, " Second{Enter}");
    await server.settleNewestFirst();
    expect(server.stored()).toBe("First Second");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    // ...and the baseline is the saved text, so leaving the box sends nothing more.
    const sent = server.save.mock.calls.length;
    await userEvent.tab();
    await server.settleNewestFirst();
    expect(server.save).toHaveBeenCalledTimes(sent);
  });

  it("still reports a failure of the newest save, and lets the user retry it", async () => {
    const server = fakeServer([1]);
    render(<DecisionNoteStrip target={target} onSave={server.save} onDismiss={vi.fn()} />);
    const box = screen.getByRole("textbox");
    await userEvent.type(box, "Only{Enter}");
    await server.settleNewestFirst();
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(box).toHaveValue("Only");
    await userEvent.type(box, "{Enter}");
    await server.settleNewestFirst();
    expect(server.stored()).toBe("Only");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("does not adopt a refetched OLDER note while a newer save is still going", async () => {
    // The everyday shape: the first save's acknowledgement refetches the list, and
    // that refetch carries "First" while " Second" is still on its way. Adopting it
    // would show "First" in a box that reads as clean, and the user's next words
    // would be typed onto -- and saved over -- the stale text.
    const server = fakeServer();
    const props = { target, onSave: server.save, onDismiss: vi.fn() };
    const { rerender } = render(<DecisionNoteStrip {...props} initialNote={null} />);
    const box = screen.getByRole("textbox");
    await userEvent.type(box, "First{Enter}");
    await userEvent.type(box, " Second{Enter}");
    await server.settleOldest();
    rerender(<DecisionNoteStrip {...props} initialNote="First" />);
    expect(box).toHaveValue("First Second");
    await server.settleOldest();
    rerender(<DecisionNoteStrip {...props} initialNote="First Second" />);
    expect(box).toHaveValue("First Second");
    expect(server.stored()).toBe("First Second");
  });

  it("orders saves for the same call across a remounted strip", async () => {
    // The feed and the call page both remount the strip (keyed on the call and
    // decision), so ordering held per INSTANCE would miss this.
    const server = fakeServer();
    const first = render(
      <DecisionNoteStrip target={target} onSave={server.save} onDismiss={vi.fn()} />,
    );
    await userEvent.type(screen.getByRole("textbox"), "Old{Enter}");
    first.unmount();
    render(
      <DecisionNoteStrip
        target={target}
        initialNote="Old"
        onSave={server.save}
        onDismiss={vi.fn()}
      />,
    );
    await userEvent.type(screen.getByRole("textbox"), " and new{Enter}");
    await server.settleNewestFirst();
    expect(server.stored()).toBe("Old and new");
  });
});

describe("Undo (UX review 2026-10-31)", () => {
  it("says what happened and offers Undo when the caller can undo", async () => {
    const onUndo = vi.fn();
    render(
      <DecisionNoteStrip
        target={{ ...target, state: "saved" }}
        onSave={vi.fn()}
        onDismiss={vi.fn()}
        onUndo={onUndo}
      />,
    );
    expect(screen.getByText(/Saved to your shortlist\./)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(onUndo).toHaveBeenCalledTimes(1);
  });

  it("words a hide as a hide", () => {
    render(<DecisionNoteStrip target={target} onSave={vi.fn()} onDismiss={vi.fn()} onUndo={vi.fn()} />);
    expect(screen.getByText(/^Hidden\./)).toBeInTheDocument();
  });

  it("is disabled while a decision is still landing", () => {
    render(
      <DecisionNoteStrip target={target} onSave={vi.fn()} onDismiss={vi.fn()} onUndo={vi.fn()} undoBusy />,
    );
    expect(screen.getByRole("button", { name: "Undo" })).toBeDisabled();
  });

  it("offers nothing when the caller passes no Undo", () => {
    setup();
    expect(screen.queryByRole("button", { name: "Undo" })).toBeNull();
  });
});
