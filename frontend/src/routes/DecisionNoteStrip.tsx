// frontend/src/routes/DecisionNoteStrip.tsx
import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";

import { apiErrorMessage } from "../api/client";
import styles from "./DecisionNoteStrip.module.css";

// WHAT THE STRIP IS ABOUT, held as a prop rather than looked up (spec D10).
// This is the whole defence against a late write landing on the next call: the
// component never reads "the currently focused call", so a note typed about
// call 7 carries call 7's ids even when the cursor has moved on and the strip
// itself has already been replaced. `CallDetail` keeps {id, profileId} for its
// Undo for the same reason.
//
// `state` is here for two jobs and not for a third: it words the prompt ("Why
// did you hide…" vs "…save…"), and Task 7 needs it to edit an existing note.
// It is deliberately NOT part of the onSave payload -- see NoteSaveArgs.
export type NoteTarget = {
  callId: number;
  profileId: number;
  title: string;
  state: "saved" | "hidden";
};

// The three fields the strip knows about. `expectedState` (D11) is the PARENT's
// to add: the parent holds the target it created the strip with, and the strip
// having two ways to say the same thing is how they drift apart.
export type NoteSaveArgs = { id: number; profileId: number; note: string | null };

type Props = {
  target: NoteTarget;
  // OPTIONAL, defaulting to empty, because Task 6 only ever creates. Task 7
  // reuses this component to EDIT, and a component with no way to receive the
  // current value can only ever create.
  initialNote?: string | null;
  // OPT-IN, defaulting OFF, and it must stay that way. The feed's strip appears
  // on its own after a decision the user made with the keyboard; grabbing the
  // caret there would turn `h h h` into typed text in the first box and break
  // D2's whole promise that the strip is ignorable.
  //
  // A strip the user opened by CLICKING a button is the opposite case, and
  // leaving it unfocused is a data-loss path rather than a nicety: the click
  // unmounts the button, so `document.activeElement` falls back to <body>
  // while a text box is visibly open, and the feed's window listener is
  // therefore live. Measured 2026-09-07 in the real feed: `u` on that row then
  // sent DELETE /api/calls/{id}/hide, and by D4 the decision and its note die
  // together -- silent destruction of the reason just written. `s` is worse
  // still: it flips hidden -> saved, which by D9 clears the note server-side.
  // The same gap reopens after a successful save, because the note-covering
  // `key` remounts the strip while the editor stays visibly open.
  autoFocus?: boolean;
  // HOW the user last left the box, told to the caller the moment a commit
  // starts. It exists for one caller shape: a surface that REMOUNTS this
  // component on a successful save (a row editor keyed on the note, so the
  // seed cannot go stale) re-runs the focus effect above -- and after a
  // BLUR-commit that pulls the caret back into a box the user has already
  // left. The strip cannot remember this for itself: the remount is a
  // different instance and its refs went with the old one, so the bit has to
  // be held by the caller that survives.
  //
  // Fired for EVERY commit, including one that turns out to be a no-op. It
  // reports where the caret belongs, not whether a write happened.
  onCommit?: (via: "enter" | "blur") => void;
  onSave: (args: NoteSaveArgs) => void | Promise<unknown>;
  onDismiss: () => void;
  // UNDO (UX review 2026-10-31). The decision just made, reversed in one click:
  // before this, a mis-click removed the card with nothing on screen to bring it
  // back except the `u` key in another view. OPTIONAL and the caller's call:
  // the feed passes it only when "back" is unambiguous -- a decision made from
  // the Inbox, where back means undecided. A flip made in Saved or Hidden has a
  // different "before", and those cards carry their own "Return to inbox".
  onUndo?: () => void;
  undoBusy?: boolean;
};

// Whitespace is not a note (D8). Normalised HERE as well as on the server, so
// "has a note" has one answer on both sides and a stray space does not count as
// an edit worth a request.
function normalize(value: string): string | null {
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

// ONE WRITE AT A TIME PER (profile, call), in the order the user committed them.
// Enter and blur both commit, so two saves for one call can be outstanding at once,
// and neither the mutation nor the endpoint orders them: an older request that
// landed last overwrote the newer text (outside review 2026-10-29, F3: "First",
// Enter, " Second", Enter, completed in reverse -> "First"). Held at module level,
// not per instance, because the feed and the call page remount the strip while a
// write may still be in flight. A failed write does not stop the queue: the next
// one carries newer text and is the one that counts.
const saveQueues = new Map<string, Promise<unknown>>();

function enqueueSave(key: string, run: () => void | Promise<unknown>): Promise<unknown> {
  const previous = saveQueues.get(key) ?? Promise.resolve();
  const next = previous.catch(() => undefined).then(run);
  saveQueues.set(key, next);
  next
    .finally(() => {
      if (saveQueues.get(key) === next) saveQueues.delete(key);
    })
    .catch(() => undefined);
  return next;
}

export function DecisionNoteStrip({
  target,
  initialNote = null,
  autoFocus = false,
  onCommit,
  onSave,
  onDismiss,
  onUndo,
  undoBusy = false,
}: Props) {
  const inputId = useId();
  const input = useRef<HTMLInputElement | null>(null);
  // Read on MOUNT as the seed, then RE-ADOPTED by the effect below whenever the
  // server's value changes AND the box is clean. Not a controlled value: while
  // the user is typing, what they typed wins.
  const [value, setValue] = useState(initialNote ?? "");
  const [error, setError] = useState<string | null>(null);
  // The NEWEST note handed to the save queue (or seeded from the server). Enter
  // AND blur both commit (D10), so without this a note committed with Enter is
  // sent a second time by the blur that follows it, and a blur with nothing
  // typed sends a no-op request on every strip anyone ever ignores.
  const latestSent = useRef<string | null>(normalize(initialNote ?? ""));
  // What the server has ACKNOWLEDGED, kept apart from latestSent so a failed save
  // can fall back to the truth rather than to whatever was sent before it.
  const acked = useRef<string | null>(normalize(initialNote ?? ""));
  // Which commit is the newest (only ITS failure is shown), and how many of this
  // strip's writes are still queued or in flight.
  const seq = useRef(0);
  const inFlight = useRef(0);
  // ADOPT A NEW SERVER VALUE ONLY WHEN THE BOX IS CLEAN.
  //
  // The note really can change underneath an open editor: the strip trims
  // before sending (D8), so "  padded  " is stored as "padded" and the box must
  // not keep the padding for the rest of the session. Task 7 first solved that
  // by KEYING the strip on the note, which made every change a remount -- and a
  // remount re-seeds from `initialNote`.
  //
  // The everyday way the note changes is this editor's OWN acknowledgement,
  // followed by the list refetch that re-renders the row. So the remount landed
  // mid-sentence: type "First", press Enter, keep typing, and the moment the
  // PUT came back the box snapped to "First" and everything after it was gone.
  // The user watched their own words disappear, with no error and no undo.
  //
  // Clean means the visible text still normalises to what the server is
  // believed to hold. Compared NORMALISED, or "  padded  " reads as dirty
  // against "padded" and the trim never lands.
  useEffect(() => {
    // NOT while a write of ours is queued or in flight: this value may be OLDER
    // than what is about to be stored -- the refetch after the first of two
    // saves. Adopting it would put stale text in a box that reads as clean, and
    // the next blur would write that stale text back over the newer note.
    if (inFlight.current > 0) return;
    const next = initialNote ?? "";
    if (normalize(value) === latestSent.current) setValue(next);
    // Either way the server now holds `next`, so a later blur compares against
    // the right baseline instead of re-sending a note that is already stored.
    acked.current = latestSent.current = normalize(next);
    // `value` is deliberately NOT a dependency: this must fire when the SERVER
    // value changes, never on a keystroke -- which is the whole point.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialNote]);

  // The strip is REPLACED by the next decision while a write may still be in
  // flight (D10: the request is not cancelled). Its failure still belongs to
  // the mutation, but there is no longer a strip to show it on.
  const mounted = useRef(true);
  // Re-ARMED in the effect body, not only cleared in its cleanup. <StrictMode>
  // (main.tsx) dev-double-invokes effects: mount -> cleanup -> mount. A
  // cleanup-only ref goes false on that first cleanup and never comes back, so
  // in `pnpm dev` every failed save returned early at the guard below and the
  // strip sat there unchanged -- text kept, no message -- and the user believed
  // the note had saved. Production builds do not double-invoke, which is
  // exactly why nothing would ever have caught it. The failure test below runs
  // inside <StrictMode> for that reason.
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // Runs on every MOUNT, which is what closes the post-save half of the gap:
  // the caller keys this component on the note, so a successful save remounts
  // it, and a focus set only on first open would be lost exactly then.
  //
  // AND IT IS THE CALLER WHO DECIDES, per mount, via `autoFocus` -- because
  // "the save remounted us" is not on its own a reason to take the caret. A
  // save committed with ENTER leaves the user in the box, and dropping focus
  // there reopens the very window this effect exists to close. A save
  // committed by BLUR means they are already somewhere else, and re-focusing
  // then is a steal: it silently kills the feed's keyboard loop
  // (`isEditableTarget` correctly stands the window listener down for a
  // focused input), or lands the next keystrokes in the wrong call's box. The
  // caller tells the two apart with `onCommit` and answers here.
  useEffect(() => {
    if (autoFocus) input.current?.focus();
  }, [autoFocus]);

  async function commit(via: "enter" | "blur") {
    // BEFORE the early return: this reports where the caret belongs, which is
    // just as true of a box committed unchanged as of one that wrote.
    onCommit?.(via);
    const note = normalize(value);
    if (note === latestSent.current) return;
    latestSent.current = note;
    const mine = ++seq.current;
    inFlight.current += 1;
    setError(null);
    const args = { id: target.callId, profileId: target.profileId, note };
    try {
      await enqueueSave(`${target.profileId}:${target.callId}`, () => onSave(args));
      acked.current = note;
      if (mine === seq.current && mounted.current) setError(null);
    } catch (err) {
      // A newer save is queued behind this one and carries newer text, so ITS
      // outcome is the one the user needs to see.
      if (mine !== seq.current) return;
      // Not saved after all, so the same text may be retried -- BY THE USER.
      // The strip does not retry on its own (D10).
      latestSent.current = acked.current;
      if (!mounted.current) return;
      setError(
        `Couldn't save your note — ${apiErrorMessage(err, "the request failed")}. Your text is still here.`,
      );
    } finally {
      inFlight.current -= 1;
    }
  }

  // NO stopPropagation, deliberately. The feed's window listener stands down
  // for keystrokes inside a text field via isEditableTarget, and that guard is
  // what gate 10 tests; swallowing the event here would make the gate green
  // whether or not the guard exists.
  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") {
      e.preventDefault();
      void commit("enter");
    } else if (e.key === "Escape") {
      e.preventDefault();
      setValue(latestSent.current ?? "");
      setError(null);
      onDismiss();
    }
  }

  return (
    <section
      className={styles.noteStrip}
      data-note-strip={target.callId}
      aria-label="Add a reason for this decision"
    >
      {onUndo && (
        <p className={styles.noteDone} data-field="decision-done">
          {target.state === "hidden" ? "Hidden." : "Saved to your shortlist."}{" "}
          <button
            type="button"
            className={styles.noteUndo}
            disabled={undoBusy}
            // Keep the caret where it is: a blur here would commit a half-typed
            // note for the decision this click is about to delete.
            onMouseDown={(e) => e.preventDefault()}
            onClick={onUndo}
          >
            Undo
          </button>
        </p>
      )}
      <label className={styles.noteLabel} htmlFor={inputId}>
        Why did you {target.state === "hidden" ? "hide" : "save"}{" "}
        <strong className={styles.noteTitle}>{target.title}</strong>?
      </label>
      {/* No autofocus BY DEFAULT, and that is the point of the whole screen: the
          feed is a keyboard loop, and a strip that grabbed the caret would turn
          `h h h` into typed text in the first box. The strip is ignorable (D2).
          A caller that opens the box from an explicit click passes autoFocus --
          see the prop for why that case is the reverse. The focus is set in an
          effect rather than via the React `autoFocus` attribute so it re-runs
          on the keyed remount a save causes. */}
      <input
        ref={input}
        id={inputId}
        type="text"
        className={styles.noteInput}
        // Matches max_length=1000 on DecisionNoteIn, so the limit is felt while
        // typing rather than discovered as a 422 after pressing Enter.
        maxLength={1000}
        value={value}
        placeholder="Optional — Enter saves, Esc dismisses"
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={onKeyDown}
        // Blur commits as well as Enter (D10). No autosave and no debounce: a
        // timer that outlives the strip is the same bug as an unbound write.
        onBlur={() => void commit("blur")}
      />
      {/* D3, and worded so it stays true whichever surfaces are mounted: an
          administrator has database access whether or not a page exists, so a
          disclosure phrased around the page would be misleading in exactly the
          deployment where it matters. No aria-live here -- the feed already
          announces list changes and a second live region talks over it. */}
      <p className={styles.noteDisclosure}>
        An administrator can read this to improve matching. Your name is not attached.
      </p>
      {error !== null && (
        <p role="alert" className={styles.noteError}>
          {error}
        </p>
      )}
    </section>
  );
}
