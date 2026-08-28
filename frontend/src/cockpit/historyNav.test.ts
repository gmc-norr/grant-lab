import { renderHook, act } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { useHistoryNav } from "./historyNav";

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
});
