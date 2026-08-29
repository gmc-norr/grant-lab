import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { MemoryRouter, useSearchParams } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useCockpitNav } from "./cockpitNav";
import { resetEditorStore, setDirty } from "./editorStore";
import { nextSelection, useHistoryNav } from "./historyNav";

function wrapperFor(initial: string) {
  return ({ children }: { children: ReactNode }) =>
    createElement(MemoryRouter, { initialEntries: [initial] }, children);
}

describe("useHistoryNav", () => {
  it("defaults to the draft with nothing selected", () => {
    const { result } = renderHook(() => useHistoryNav(), {
      wrapper: wrapperFor("/cockpit/ws1?tab=history"),
    });
    expect(result.current.artifact).toBe("draft");
    expect(result.current.a).toBeNull();
    expect(result.current.b).toBeNull();
  });

  it("reads both shas from the URL", () => {
    const { result } = renderHook(() => useHistoryNav(), {
      wrapper: wrapperFor("/cockpit/ws1?tab=history&artifact=spec&a=abc1234&b=def5678"),
    });
    expect(result.current.artifact).toBe("spec");
    expect(result.current.a).toBe("abc1234");
    expect(result.current.b).toBe("def5678");
  });

  it("keeps an abbreviated sha — the panel, not the regex, reports it unknown", () => {
    // {7,64} matches the backend deliberately. An abbreviation is valid to git
    // and resolves server-side; it simply matches no row, which is the general
    // "not in this list" state HistoryPanel names.
    const { result } = renderHook(() => useHistoryNav(), {
      wrapper: wrapperFor("/cockpit/ws1?tab=history&a=abc1234"),
    });
    expect(result.current.a).toBe("abc1234");
  });

  it("drops a sha that is not sha-shaped rather than fetching it", () => {
    const { result } = renderHook(() => useHistoryNav(), {
      wrapper: wrapperFor("/cockpit/ws1?tab=history&a=--output=x&b=def5678"),
    });
    expect(result.current.a).toBeNull();
    expect(result.current.b).toBe("def5678");
  });

  it("ignores an unknown artifact and falls back to draft", () => {
    const { result } = renderHook(() => useHistoryNav(), {
      wrapper: wrapperFor("/cockpit/ws1?tab=history&artifact=bogus"),
    });
    expect(result.current.artifact).toBe("draft");
  });

  it("toggle fills a then b, and toggling a selected sha removes it", () => {
    const { result } = renderHook(() => useHistoryNav(), {
      wrapper: wrapperFor("/cockpit/ws1?tab=history"),
    });
    act(() => result.current.toggle("aaaaaaa"));
    expect(result.current.a).toBe("aaaaaaa");
    act(() => result.current.toggle("bbbbbbb"));
    expect(result.current.b).toBe("bbbbbbb");
    act(() => result.current.toggle("aaaaaaa"));
    expect(result.current.a).toBe("bbbbbbb");
    expect(result.current.b).toBeNull();
  });

  it("a third selection replaces the older of the two", () => {
    const { result } = renderHook(() => useHistoryNav(), {
      wrapper: wrapperFor("/cockpit/ws1?tab=history&a=aaaaaaa&b=bbbbbbb"),
    });
    act(() => result.current.toggle("ccccccc"));
    expect(result.current.a).toBe("bbbbbbb");
    expect(result.current.b).toBe("ccccccc");
  });

  it("changing artifact clears the selection", () => {
    const { result } = renderHook(() => useHistoryNav(), {
      wrapper: wrapperFor("/cockpit/ws1?tab=history&a=aaaaaaa&b=bbbbbbb"),
    });
    act(() => result.current.setArtifact("spec"));
    expect(result.current.artifact).toBe("spec");
    expect(result.current.a).toBeNull();
    expect(result.current.b).toBeNull();
  });

  it("defaults the layout to split when the parameter is absent", () => {
    const { result } = renderHook(() => useHistoryNav(), {
      wrapper: wrapperFor("/cockpit/ws1?tab=history"),
    });
    expect(result.current.layout).toBe("split");
  });

  it("reads layout=unified from the URL", () => {
    const { result } = renderHook(() => useHistoryNav(), {
      wrapper: wrapperFor("/cockpit/ws1?tab=history&layout=unified"),
    });
    expect(result.current.layout).toBe("unified");
  });

  it("normalises an unrecognised layout to split rather than rejecting it", () => {
    const { result } = renderHook(() => useHistoryNav(), {
      wrapper: wrapperFor("/cockpit/ws1?tab=history&layout=sideways"),
    });
    expect(result.current.layout).toBe("split");
  });

  it("writes the layout to the URL", () => {
    const { result } = renderHook(() => useHistoryNav(), {
      wrapper: wrapperFor("/cockpit/ws1?tab=history"),
    });
    act(() => result.current.setLayout("unified"));
    expect(result.current.layout).toBe("unified");
  });

  it("keeps the layout when the artifact changes", () => {
    // The layout is how the user reads, not a property of what is selected.
    // Clearing it here would silently undo their choice mid-session.
    const { result } = renderHook(() => useHistoryNav(), {
      wrapper: wrapperFor("/cockpit/ws1?tab=history&layout=unified"),
    });
    act(() => result.current.setArtifact("spec"));
    expect(result.current.artifact).toBe("spec");
    expect(result.current.layout).toBe("unified");
  });

  it("keeps the layout when the selection changes", () => {
    const { result } = renderHook(() => useHistoryNav(), {
      wrapper: wrapperFor("/cockpit/ws1?tab=history&layout=unified"),
    });
    act(() => result.current.toggle("abc1234"));
    expect(result.current.a).toBe("abc1234");
    expect(result.current.layout).toBe("unified");
  });

  it("selectAndOpen fills the first slot AND opens the History tab in one write", () => {
    const { result } = renderHook(() => useHistoryNav(), {
      wrapper: wrapperFor("/cockpit/ws1?tab=draft"),
    });
    act(() => result.current.selectAndOpen("abc1234"));
    expect(result.current.a).toBe("abc1234");
  });

  it("selectAndOpen follows the same rule as toggle for the second slot", () => {
    const { result } = renderHook(() => useHistoryNav(), {
      wrapper: wrapperFor("/cockpit/ws1?tab=history&a=abc1234"),
    });
    act(() => result.current.selectAndOpen("def5678"));
    expect(result.current.a).toBe("abc1234");
    expect(result.current.b).toBe("def5678");
  });

  it("selectAndOpen deselects a sha that is already selected", () => {
    const { result } = renderHook(() => useHistoryNav(), {
      wrapper: wrapperFor("/cockpit/ws1?tab=history&a=abc1234&b=def5678"),
    });
    act(() => result.current.selectAndOpen("def5678"));
    expect(result.current.a).toBe("abc1234");
    expect(result.current.b).toBeNull();
  });

  it("selectAndOpen slides the window along when both slots are full", () => {
    const { result } = renderHook(() => useHistoryNav(), {
      wrapper: wrapperFor("/cockpit/ws1?tab=history&a=aaaaaaa&b=bbbbbbb"),
    });
    act(() => result.current.selectAndOpen("ccccccc"));
    expect(result.current.a).toBe("bbbbbbb");
    expect(result.current.b).toBe("ccccccc");
  });

  it("selectAndOpen leaves the artifact alone", () => {
    const { result } = renderHook(() => useHistoryNav(), {
      wrapper: wrapperFor("/cockpit/ws1?tab=draft&artifact=spec"),
    });
    act(() => result.current.selectAndOpen("abc1234"));
    expect(result.current.artifact).toBe("spec");
    expect(result.current.a).toBe("abc1234");
  });
});

describe("one URL write, not two — spec section 6.1", () => {
  // react-router 7.18.2's setSearchParams is NOT a React state setter: the
  // functional form receives params memoised from location.search AT RENDER
  // TIME, captured in a useCallback closure. Two calls in one handler both
  // build from the pre-click URL and the second discards the first.
  function Probe({ mode }: { mode: "two-writers" | "selectAndOpen" }) {
    const hist = useHistoryNav();
    const cock = useCockpitNav();
    const [sp] = useSearchParams();
    return (
      <>
        <button
          onClick={() => {
            if (mode === "two-writers") {
              hist.toggle("abc1234");
              cock.setTab("diff");
            } else {
              hist.selectAndOpen("abc1234");
            }
          }}
        >
          click
        </button>
        <output data-testid="url">{sp.toString()}</output>
      </>
    );
  }

  function urlAfterClick(mode: "two-writers" | "selectAndOpen") {
    render(
      <MemoryRouter initialEntries={["/cockpit/ws1?tab=draft"]}>
        <Probe mode={mode} />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("click"));
    return screen.getByTestId("url").textContent ?? "";
  }

  it("selectAndOpen leaves BOTH the selection and the tab in the URL", () => {
    const url = urlAfterClick("selectAndOpen");
    expect(url).toContain("tab=diff");
    expect(url).toContain("a=abc1234");
  });

  it("PINNED: toggle-then-setTab loses the selection — this is why selectAndOpen exists", () => {
    // Measured, not assumed. Deleting selectAndOpen and "simplifying" back to
    // two calls must break something; this is that something.
    const url = urlAfterClick("two-writers");
    expect(url).toContain("tab=diff");
    expect(url).not.toContain("a=abc1234");
  });
});

describe("nextSelection — the stale-pair trap", () => {
  it("cannot reach a single valid selection by clicking the only row", () => {
    // A one-version workspace reached by a stale link: `a` is a sha that is not
    // in the list, `b` is the only real row. Clicking that row deselects it and
    // LEAVES the stale sha; clicking again slides it back into the pair. The
    // state cycles with period two and never reaches "one valid sha". This is
    // why HistoryPanel offers an explicit way out instead of telling the user
    // to click the pane.
    const stale = "abc1234";
    const real = "aaaaaaa";
    let cur: { a: string | null; b: string | null } = { a: stale, b: real };
    const seen: string[] = [];
    for (let i = 0; i < 4; i++) {
      cur = nextSelection(cur, real);
      seen.push(`${cur.a ?? "-"}/${cur.b ?? "-"}`);
    }
    expect(seen).toEqual([
      `${stale}/-`,
      `${stale}/${real}`,
      `${stale}/-`,
      `${stale}/${real}`,
    ]);
    expect(seen.every((x) => x.includes(stale))).toBe(true);
  });
});

describe("clearSelection", () => {
  function ClearProbe() {
    const hist = useHistoryNav();
    const [sp] = useSearchParams();
    return (
      <>
        <button onClick={() => hist.clearSelection()}>clear</button>
        <output data-testid="url">{sp.toString()}</output>
      </>
    );
  }

  it("removes BOTH shas and keeps everything else in the URL", () => {
    render(
      <MemoryRouter
        initialEntries={["/cockpit/ws1?tab=history&artifact=spec&layout=unified&a=abc1234&b=aaaaaaa"]}
      >
        <ClearProbe />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("clear"));
    // Parsed, not substring-matched: "tab=history" contains the literal "b=",
    // so `not.toContain("b=")` fails on a URL that is in fact correct.
    const url = new URLSearchParams(screen.getByTestId("url").textContent ?? "");
    expect(url.get("a")).toBeNull();
    expect(url.get("b")).toBeNull();
    // The exit must not cost the user their tab, artifact or layout.
    expect(url.get("tab")).toBe("history");
    expect(url.get("artifact")).toBe("spec");
    expect(url.get("layout")).toBe("unified");
  });
});

describe("sha normalisation", () => {
  it("lowercases a sha from the URL so it can match a list row", () => {
    // SHA_RE accepts A-F, but git's %H is lowercase and every comparison in
    // HistoryPanel is string identity against it. An uppercase link was
    // therefore accepted and then reported as "not in the Versions list".
    const { result } = renderHook(() => useHistoryNav(), {
      wrapper: wrapperFor("/cockpit/ws1?tab=history&a=AAAAAAA&b=BbBbBbB"),
    });
    expect(result.current.a).toBe("aaaaaaa");
    expect(result.current.b).toBe("bbbbbbb");
  });
});

describe("selectAndOpen guards unsaved edits", () => {
  function Probe() {
    const hist = useHistoryNav();
    const [sp] = useSearchParams();
    return (
      <>
        <button onClick={() => hist.selectAndOpen("aaaaaaa")}>pick</button>
        <output data-testid="url">{sp.toString()}</output>
      </>
    );
  }

  function clickPickAt(url: string) {
    render(
      <MemoryRouter initialEntries={[url]}>
        <Probe />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("pick"));
    return screen.getByTestId("url").textContent ?? "";
  }

  afterEach(() => {
    act(() => resetEditorStore());
    vi.restoreAllMocks();
  });

  it("asks before discarding unsaved edits, and does nothing if refused", () => {
    // Opening the comparison changes the middle destination, which unmounts the
    // artifact editor and throws away its per-section buffers — the same discard
    // Cockpit.onTab already confirms. selectAndOpen is a SECOND way to change
    // that destination, and it was silent.
    act(() => setDirty([1]));
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    const url = clickPickAt("/cockpit/ws1?tab=draft&sec=1");
    expect(confirmSpy).toHaveBeenCalledTimes(1);
    const parsed = new URLSearchParams(url);
    expect(parsed.get("tab")).toBe("draft");
    expect(parsed.get("a")).toBeNull();
  });

  it("proceeds when the discard is accepted", () => {
    act(() => setDirty([1]));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const parsed = new URLSearchParams(clickPickAt("/cockpit/ws1?tab=draft&sec=1"));
    expect(parsed.get("tab")).toBe("diff");
    expect(parsed.get("a")).toBe("aaaaaaa");
  });

  it("does NOT ask when the editor is clean", () => {
    const confirmSpy = vi.spyOn(window, "confirm");
    const parsed = new URLSearchParams(clickPickAt("/cockpit/ws1?tab=draft"));
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(parsed.get("tab")).toBe("diff");
  });

  it("does NOT ask when the comparison is ALREADY open", () => {
    // The input stays a LEGACY ?tab=history on purpose. The guard asks "am I
    // already on the comparison?"; comparing the raw ?tab to "diff" would say
    // no for a legacy link and prompt to discard edits that are not being
    // discarded. This test is what makes tabFromParams load-bearing there.
    // Picking a second version while on the comparison changes no destination,
    // so there is nothing to discard and nagging would be wrong.
    act(() => setDirty([1]));
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const parsed = new URLSearchParams(clickPickAt("/cockpit/ws1?tab=history&b=bbbbbbb"));
    expect(confirmSpy).not.toHaveBeenCalled();
    // `a` was empty, so the rule fills it and leaves `b` alone — checked
    // against nextSelection rather than guessed.
    expect(parsed.get("a")).toBe("aaaaaaa");
    expect(parsed.get("b")).toBe("bbbbbbb");
  });
});

describe("selectAndOpen after the rename", () => {
  function Probe() {
    const hist = useHistoryNav();
    const [sp] = useSearchParams();
    return (
      <>
        <button onClick={() => hist.selectAndOpen("aaaaaaa")}>pick</button>
        <output data-testid="url">{sp.toString()}</output>
      </>
    );
  }

  function clickPickAt(url: string) {
    render(
      <MemoryRouter initialEntries={[url]}>
        <Probe />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("pick"));
    return new URLSearchParams(screen.getByTestId("url").textContent ?? "");
  }

  afterEach(() => {
    act(() => resetEditorStore());
    vi.restoreAllMocks();
  });

  it("opens the comparison as tab=diff", () => {
    expect(clickPickAt("/cockpit/ws1?tab=draft").get("tab")).toBe("diff");
  });

  it("deselecting the last version neither opens the comparison nor prompts", () => {
    // The resulting selection must be computed BEFORE the guard runs. Guarding
    // first prompts "discard your unsaved edits?" and then discovers no
    // destination change was going to happen — a prompt for nothing, which is
    // how users learn to dismiss prompts without reading them.
    act(() => setDirty([1]));
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const url = clickPickAt("/cockpit/ws1?tab=draft&a=aaaaaaa");
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(url.get("tab")).toBe("draft");
    expect(url.get("a")).toBeNull();
  });
});
