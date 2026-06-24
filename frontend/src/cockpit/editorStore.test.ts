import { afterEach, expect, it, vi } from "vitest";

import {
  getSnapshot,
  resetEditorStore,
  setCursor,
  setDirty,
  setSave,
  subscribe,
} from "./editorStore";

afterEach(() => resetEditorStore());

it("starts empty", () => {
  expect(getSnapshot()).toEqual({ dirty: [], cursor: null, save: "idle" });
});

it("setDirty sorts, dedupes, and emits only on change", () => {
  const cb = vi.fn();
  const unsub = subscribe(cb);
  setDirty([3, 1]);
  expect(getSnapshot().dirty).toEqual([1, 3]);
  expect(cb).toHaveBeenCalledTimes(1);
  setDirty([1, 3]); // same contents -> no emit
  expect(cb).toHaveBeenCalledTimes(1);
  unsub();
});

it("setCursor / setSave update and emit", () => {
  const cb = vi.fn();
  subscribe(cb);
  setCursor({ line: 2, col: 4 });
  setSave("saving");
  expect(getSnapshot().cursor).toEqual({ line: 2, col: 4 });
  expect(getSnapshot().save).toBe("saving");
  expect(cb).toHaveBeenCalledTimes(2);
});

it("getSnapshot is referentially stable until a change (no tearing)", () => {
  const a = getSnapshot();
  setSave("idle"); // already idle -> no change
  expect(getSnapshot()).toBe(a);
});

it("reset returns to empty and notifies", () => {
  const cb = vi.fn();
  subscribe(cb);
  setSave("error");
  resetEditorStore();
  expect(getSnapshot()).toEqual({ dirty: [], cursor: null, save: "idle" });
  expect(cb).toHaveBeenCalled();
});
