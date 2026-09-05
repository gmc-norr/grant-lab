import { QueryClient } from "@tanstack/react-query";
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import type { CallItem } from "../api/dashboard";
import { renderWithProviders } from "../test-utils";
import { TriageFeed } from "./TriageFeed";

afterEach(() => {
  vi.restoreAllMocks();
});

function call(over: Partial<CallItem> & { id: number; title: string }): CallItem {
  return {
    deadline: null,
    deadline_local_date: null,
    published_at: null,
    source_name: "Vinnova",
    source_slug: "vinnova",
    aggregate_score: null,
    programme: null,
    budget_min: null,
    budget_max: null,
    currency: null,
    url: "https://example.org/calls/x",
    summary: null,
    keywords: [],
    criteria: [],
    applicant_note_count: 0,
    escalated: false,
    ...over,
  };
}

const THREE = [
  call({ id: 1, title: "Call one" }),
  call({ id: 2, title: "Call two" }),
  call({ id: 3, title: "Call three" }),
];

type Counts = { inbox: number; saved: number; hidden: number };

// TWO endpoints, dispatched BY PATH: the feed reads GET /api/calls and the rail
// reads GET /api/calls/decisions/counts. A single mockResolvedValue would feed
// the dashboard body to both and make gate 7 meaningless.
function stub({
  items = THREE,
  // DELIBERATELY DIFFERENT FROM items.length AND FROM EACH OTHER, and that is
  // the whole of gate 7: with counts.inbox === items.length the rail is right
  // for the wrong reason and B25 (render items.length instead) stays green —
  // and a stub of 99/3/3 would half-coincide on saved and hidden (T7-7).
  counts = { inbox: 99, saved: 4, hidden: 12 } as Counts,
  profileId = 7 as number | null,
  profiles = [{ id: 7, name: "Molecular oncology" }],
  hasNext = false,
  // When set, every list fetch AFTER the first one hangs until this resolves.
  // That is how D20's "keys are ignored while the list is fetching" gets a
  // deterministic window instead of a race.
  hold = null as Promise<void> | null,
} = {}) {
  let listFetches = 0;
  const get = vi.spyOn(clientMod.api, "GET").mockImplementation((async (path: string) => {
    if (path === "/api/calls/decisions/counts") {
      return { data: counts, error: undefined, response: new Response() };
    }
    listFetches += 1;
    if (hold !== null && listFetches >= 2) await hold;
    return {
      data: {
        items,
        summary: { open_count: 0, high_score: 0, soon_deadline: 0 },
        pagination: {
          page: 1,
          limit: 50,
          has_prev: false,
          has_next: hasNext,
          total: items.length,
        },
        ranking: { pending: 0, failed: 0 },
        effective: { profile_id: profileId, source: null, sort: "newest", show_closed: false },
        facets: { sources: [], profiles },
      },
      error: undefined,
      response: new Response(),
    };
  }) as never);
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: { state: "saved" },
    error: undefined,
    response: new Response(),
  } as never);
  const del = vi.spyOn(clientMod.api, "DELETE").mockResolvedValue({
    data: { state: null },
    error: undefined,
    response: new Response(),
  } as never);
  return { get, post, del };
}

// `qc` is injectable and RETURNED, because the unmount gate below spies on
// invalidateQueries and therefore has to hold the same QueryClient the feed
// uses (the pattern CallDetail.test.tsx:31-34 already needed).
function renderFeed(route = "/", extra: ReactNode = null, qc = new QueryClient()) {
  return {
    qc,
    ...renderWithProviders(
      <>
        <TriageFeed />
        {extra}
      </>,
      { route, queryClient: qc },
    ),
  };
}

// Structural, not `ReturnType<typeof vi.spyOn>`: openapi-fetch's ClientMethod is
// generic over every path in the schema, so vi.spyOn's inferred instance type is
// not assignable to any hand-written spy alias. vitest does not typecheck — only
// `pnpm build` does — so getting this wrong is 34 green tests over a tsc error
// (T7-11).
type SpyCalls = { mock: { calls: unknown[][] } };

// The query the component sent to one endpoint. Asserted to EXIST first: a
// `.find(...)?.[1]` that silently short-circuits turns "the request was never
// made" into a passing test.
function queryOf(get: SpyCalls, path: string): Record<string, unknown> {
  const found = get.mock.calls.find((c) => c[0] === path);
  expect(found, `no request to ${path} was made`).toBeDefined();
  return (found as unknown as [string, { params: { query: Record<string, unknown> } }])[1].params
    .query;
}

function gets(get: SpyCalls, path: string): unknown[][] {
  return get.mock.calls.filter((c) => c[0] === path);
}

// The path + call_id of every mutation fired through one spy, in order.
function decisions(spy: SpyCalls): string[] {
  return spy.mock.calls.map((c) => {
    const [path, opts] = c as unknown as [string, { params: { path: { call_id: number } } }];
    return path.replace("{call_id}", String(opts.params.path.call_id));
  });
}

function profileIdOf(spy: SpyCalls, i: number): unknown {
  const [, opts] = spy.mock.calls[i] as unknown as [
    string,
    { params: { query: { profile_id: number } } },
  ];
  return opts.params.query.profile_id;
}

// "No mutation fired" — and it MUST await first. TanStack's mutate() defers the
// mutationFn to a microtask, so a synchronous `expect(post).not.toHaveBeenCalled()`
// immediately after fireEvent is evaluated BEFORE the request would have been
// made and passes no matter what the component did. MEASURED on the dry tree:
// with that synchronous form, mutation B23 (guard deleted) left gate 4 GREEN on
// the negative assertion; only the positive control caught it (T7-1).
async function expectNoDecision(post: SpyCalls, del: SpyCalls) {
  // act(), because the flush also lets a settled react-query update land, and an
  // unwrapped one prints "not wrapped in act(...)" to stderr on a GREEN run.
  await act(async () => {
    await new Promise((r) => setTimeout(r, 0));
  });
  expect(post.mock.calls).toEqual([]);
  expect(del.mock.calls).toEqual([]);
}

describe("gate 1 — the feed renders", () => {
  it("renders a card per call, showing title, funder and deadline", async () => {
    stub({
      items: [
        call({
          id: 1,
          title: "Call one",
          source_name: "Cancerfonden",
          deadline: "2027-03-01T12:00:00Z",
          deadline_local_date: "2027-03-01",
        }),
      ],
    });
    const { container } = renderFeed();
    await screen.findByText("Call one");
    expect(container.querySelector('[data-field="funder"]')).toHaveTextContent("Cancerfonden");
    expect(container.querySelector('[data-field="deadline"]')).toHaveTextContent("2027-03-01");
    expect(screen.getByRole("main")).toBeInTheDocument();
  });

  it("expands only the focused card", async () => {
    stub();
    const { container } = renderFeed();
    await screen.findByText("Call one");
    const expanded = [...container.querySelectorAll("[data-expanded]")].map(
      (el) => (el as HTMLElement).dataset.expanded,
    );
    expect(expanded).toEqual(["true", "false", "false"]);
  });
});

describe("gate 3 — j/k move the cursor, s and h decide", () => {
  it("s alone decides the FIRST card", async () => {
    const { post } = stub();
    renderFeed();
    await screen.findByText("Call one");
    fireEvent.keyDown(document.body, { key: "s" });
    await waitFor(() => expect(decisions(post)).toEqual(["/api/calls/1/save"]));
    expect(profileIdOf(post, 0)).toBe(7);
  });

  it("j then s saves the SECOND card, not the first", async () => {
    // B24 killer. THREE cards, because pressing s with one card passes whether
    // or not j does anything at all.
    const { post } = stub();
    renderFeed();
    await screen.findByText("Call one");
    fireEvent.keyDown(document.body, { key: "j" });
    fireEvent.keyDown(document.body, { key: "s" });
    await waitFor(() => expect(decisions(post)).toEqual(["/api/calls/2/save"]));
  });

  it("k walks back up: j j k s lands on the second card", async () => {
    const { post } = stub();
    renderFeed();
    await screen.findByText("Call one");
    fireEvent.keyDown(document.body, { key: "j" });
    fireEvent.keyDown(document.body, { key: "j" });
    fireEvent.keyDown(document.body, { key: "k" });
    fireEvent.keyDown(document.body, { key: "s" });
    await waitFor(() => expect(decisions(post)).toEqual(["/api/calls/2/save"]));
  });

  it("does not walk off either end of the list", async () => {
    // "j moves by 2 and clamps" passes the two-case version of this gate; six
    // presses in each direction is what tells the two apart (T7-6).
    const { post } = stub();
    renderFeed();
    await screen.findByText("Call one");
    for (let i = 0; i < 6; i++) fireEvent.keyDown(document.body, { key: "j" });
    fireEvent.keyDown(document.body, { key: "s" });
    for (let i = 0; i < 6; i++) fireEvent.keyDown(document.body, { key: "k" });
    fireEvent.keyDown(document.body, { key: "s" });
    await waitFor(() =>
      expect(decisions(post)).toEqual(["/api/calls/3/save", "/api/calls/1/save"]),
    );
  });

  it("h hides the focused call, on the resolved profile", async () => {
    const { post } = stub();
    renderFeed();
    await screen.findByText("Call one");
    fireEvent.keyDown(document.body, { key: "j" });
    fireEvent.keyDown(document.body, { key: "h" });
    await waitFor(() => expect(decisions(post)).toEqual(["/api/calls/2/hide"]));
    expect(profileIdOf(post, 0)).toBe(7);
  });

  it("leaves a ⌘/Ctrl chord to the browser", async () => {
    // ⌘S is Save Page. A single-letter listener that ignores modifiers steals it.
    const { post, del } = stub();
    renderFeed();
    await screen.findByText("Call one");
    fireEvent.keyDown(document.body, { key: "s", metaKey: true });
    fireEvent.keyDown(document.body, { key: "s", ctrlKey: true });
    fireEvent.keyDown(document.body, { key: "s", altKey: true });
    await expectNoDecision(post, del);
  });
});

describe("gate 3b — j/k move real DOM focus, not just a highlight", () => {
  it("j moves DOM focus to the next card, k moves it back", async () => {
    // B28 killer. j/k move a REACT STATE index; the effect that turns it into
    // real DOM focus (spec §5: a screen-reader user pressing j must hear the
    // next card, not just see a highlight move) needs its own gate, because
    // `data-expanded` proves only that the index moved (T7-13).
    stub();
    const { container } = renderFeed();
    await screen.findByText("Call one");
    fireEvent.keyDown(document.body, { key: "j" });
    await waitFor(() =>
      expect(document.activeElement).toBe(container.querySelectorAll("article")[1]),
    );
    fireEvent.keyDown(document.body, { key: "k" });
    await waitFor(() =>
      expect(document.activeElement).toBe(container.querySelectorAll("article")[0]),
    );
  });
});

describe("gate 4 — the key handler stands down inside an editable element", () => {
  // Spec §5: otherwise typing "she" into a note field saves, hides and moves the
  // cursor. PARAMETERISED OVER ALL THREE targets — one target is not a guard, it
  // is one third of a guard, and B23 is applied one branch at a time.
  const EDITABLES: { name: string; node: ReactNode; target: string }[] = [
    { name: "input", node: <input data-testid="editable" />, target: "editable" },
    { name: "textarea", node: <textarea data-testid="editable" />, target: "editable" },
    {
      name: "nested contenteditable",
      // The keydown target inside a rich editor is the inline element the caret
      // sits in, NOT the editable host — so the guard has to walk up.
      node: (
        <div contentEditable suppressContentEditableWarning tabIndex={0} data-testid="editable">
          <span data-testid="editable-inner">note</span>
        </div>
      ),
      target: "editable-inner",
    },
  ];

  it.each(EDITABLES)(
    "s typed inside a focused $name fires no mutation",
    async ({ node, target }) => {
      const { post, del } = stub();
      renderFeed("/", node);
      await screen.findByText("Call one");
      const host = screen.getByTestId("editable");
      host.focus();
      expect(document.activeElement).toBe(host);

      fireEvent.keyDown(screen.getByTestId(target), { key: "s" });
      fireEvent.keyDown(screen.getByTestId(target), { key: "h" });
      fireEvent.keyDown(screen.getByTestId(target), { key: "j" });
      await expectNoDecision(post, del);

      // POSITIVE CONTROL, in the same test and on the same render. Without it
      // this gate passes for a component whose key handler does not work AT ALL
      // — the failure mode a guard mutation is most easily confused with. It
      // also proves j was SUPPRESSED rather than merely silent: the cursor is
      // still on card one.
      host.blur();
      fireEvent.keyDown(document.body, { key: "s" });
      await waitFor(() => expect(decisions(post)).toEqual(["/api/calls/1/save"]));
    },
  );
});

describe("gate 5 — inbox zero is a designed screen", () => {
  it("renders the inbox-zero copy, not an empty div", async () => {
    // B26 killer, and it asserts the ACTUAL COPY: a `toBeEmptyDOMElement()`-style
    // check would pass over a bare <div/>.
    stub({ items: [], counts: { inbox: 0, saved: 4, hidden: 12 } });
    const { container } = renderFeed();
    expect(await screen.findByText("Inbox zero.")).toBeInTheDocument();
    expect(container.querySelector('[data-empty="inbox"]')).toHaveTextContent(
      "You have triaged every call matched against Molecular oncology.",
    );
    expect(container.querySelector('[data-empty="inbox"]')).toHaveTextContent(
      "New calls appear here as sources publish them.",
    );
  });

  it("says something DIFFERENT for an empty Saved list, and again for Hidden", async () => {
    // "Inbox zero" over an empty Saved tab would be a lie, so the empty branch
    // cannot be one shared string.
    stub({ items: [], counts: { inbox: 99, saved: 0, hidden: 12 } });
    const saved = renderFeed("/?view=saved");
    expect(await screen.findByText("Nothing saved yet.")).toBeInTheDocument();
    expect(saved.container.querySelector('[data-empty="saved"]')).not.toBeNull();
    expect(saved.container.querySelector('[data-empty="inbox"]')).toBeNull();
    saved.unmount();

    stub({ items: [], counts: { inbox: 99, saved: 4, hidden: 0 } });
    const hidden = renderFeed("/?view=hidden");
    expect(await screen.findByText("Nothing hidden.")).toBeInTheDocument();
    expect(hidden.container.querySelector('[data-empty="hidden"]')).not.toBeNull();
    expect(hidden.container.textContent).not.toContain("Inbox zero.");
    expect(hidden.container.textContent).not.toContain("Nothing saved yet.");
  });
});

describe("gate 6 / gate 11 — the view lives in the URL, and a bad one degrades", () => {
  it("asks the server for the INBOX view by default", async () => {
    const { get } = stub();
    renderFeed();
    await screen.findByText("Call one");
    expect(queryOf(get, "/api/calls").view).toBe("inbox");
  });

  it("?view=saved asks for the saved list and marks the rail item current", async () => {
    const { get } = stub({ items: [call({ id: 9, title: "A saved call" })] });
    const { container } = renderFeed("/?view=saved");
    await screen.findByText("A saved call");
    expect(queryOf(get, "/api/calls").view).toBe("saved");

    const current = [...container.querySelectorAll('[aria-current="page"]')].map(
      (el) => el.textContent,
    );
    expect(current).toHaveLength(1);
    expect(current[0]).toContain("Saved");
  });

  it("?view=inbo is a plain inbox, not a 422 that dead-ends the index", async () => {
    // B33 killer (T7-8). FastAPI validates `view` against a Literal, so an
    // unsanitised deep link is a 422 the feed cannot render around — the whole
    // screen dead-ends. `dashboardParamsFrom` drops the unknown value; this
    // asserts what reaches the WIRE, which is the only place it matters.
    const { get } = stub();
    renderFeed("/?view=inbo");
    await screen.findByText("Call one");
    expect(queryOf(get, "/api/calls").view).toBe("inbox");
  });

  it("the rail links carry the view, and reset the page", async () => {
    stub();
    const { container } = renderFeed("/?view=saved&page=4");
    await screen.findByText("Call one");
    const href = screen.getByRole("link", { name: /hidden/i }).getAttribute("href") ?? "";
    expect(href).toContain("view=hidden");
    // A view change is a new list: page 1 of it, not page 4 of the old one.
    expect(href).not.toContain("page=");
    expect(container.querySelector('[data-count="hidden"]')).not.toBeNull();
  });
});

describe("gate 7 — the rail counts come from the API", () => {
  it("renders the counts response, not items.length", async () => {
    // B25 killer, and it only works because the stub makes the two DISAGREE:
    // 99 inbox over 3 items, and 4 / 12 that match neither the list nor 99.
    stub({ items: THREE, counts: { inbox: 99, saved: 4, hidden: 12 } });
    const { container } = renderFeed();
    await screen.findByText("Call one");
    await waitFor(() =>
      expect(container.querySelector('[data-count="inbox"]')).toHaveTextContent("99"),
    );
    expect(container.querySelector('[data-count="saved"]')).toHaveTextContent("4");
    expect(container.querySelector('[data-count="hidden"]')).toHaveTextContent("12");
    // The list really does have three cards, so 99 cannot have come from it.
    expect(container.querySelectorAll("[data-call-id]")).toHaveLength(3);
  });

  it("asks for the counts on the resolved profile, with the list's own filters", async () => {
    const { get } = stub();
    renderFeed("/?source=vinnova&q=genomics");
    await screen.findByText("Call one");
    expect(queryOf(get, "/api/calls/decisions/counts")).toEqual({
      // NOTE THE NAMING SPLIT (D14): the list takes ?profile=, the decision
      // routes take ?profile_id=. Both are carried verbatim.
      profile_id: 7,
      source: "vinnova",
      q: "genomics",
      show_closed: false,
    });
  });

  it("names the profile the feed is matched against", async () => {
    stub();
    const { container } = renderFeed();
    await screen.findByText("Call one");
    expect(container.textContent).toContain("Matching against");
    expect(container.textContent).toContain("Molecular oncology");
  });
});

describe("gate 13 — the rail carries a profile picker (D11)", () => {
  it("lists every readable profile as a link that keeps the current view", async () => {
    // B31b killer. The match index has no other profile switcher at all — it
    // left with DashboardHeader — so without this the DoD smoke "switching
    // profile changes which calls are hidden" is unperformable.
    stub({
      profiles: [
        { id: 7, name: "Molecular oncology" },
        { id: 8, name: "Other group" },
      ],
    });
    renderFeed("/?view=saved&profile=7");
    await screen.findByText("Call one");
    expect(screen.getByRole("link", { name: "Other group" })).toHaveAttribute(
      "href",
      "/?profile=8&view=saved",
    );
    const current = screen.getByRole("link", { name: "Molecular oncology" });
    expect(current).toHaveAttribute("href", "/?profile=7&view=saved");
    expect(current).toHaveAttribute("aria-current", "true");
  });
});

describe("gate 14 — Saved and Hidden end with a way to the next page (D15)", () => {
  it("renders a Next page link carrying page, view and profile when has_next", async () => {
    // B32 killer. The inbox self-heals — decided cards leave and page 1 refills
    // — but Saved and Hidden do not, so without this the 51st saved call is
    // unreachable in release 1.
    stub({ hasNext: true });
    renderFeed("/?view=saved&profile=7");
    await screen.findByText("Call one");
    const href = screen.getByRole("link", { name: /next page/i }).getAttribute("href") ?? "";
    expect(href).toContain("page=2");
    expect(href).toContain("view=saved");
    expect(href).toContain("profile=7");
  });

  it("renders none when the server says there is no next page", async () => {
    stub({ hasNext: false });
    renderFeed("/?view=saved&profile=7");
    await screen.findByText("Call one");
    expect(screen.queryByRole("link", { name: /next page/i })).toBeNull();
  });
});

describe("gate 9 — u un-decides in Saved and Hidden, and does nothing in the Inbox (D2)", () => {
  it("u in Hidden issues DELETE /hide on the resolved profile", async () => {
    // B30a killer: a `u` that POSTed /hide would be a RE-hide — the opposite
    // action, and one that re-stamps decided_at so the call jumps to the top of
    // the very list the user was trying to empty.
    const { post, del } = stub();
    renderFeed("/?view=hidden");
    await screen.findByText("Call one");
    fireEvent.keyDown(document.body, { key: "u" });
    await waitFor(() => expect(decisions(del)).toEqual(["/api/calls/1/hide"]));
    expect(profileIdOf(del, 0)).toBe(7);
    expect(post.mock.calls).toEqual([]);
  });

  it("u in Saved issues DELETE /save, not /hide", async () => {
    // B30b killer.
    const { post, del } = stub();
    renderFeed("/?view=saved");
    await screen.findByText("Call one");
    fireEvent.keyDown(document.body, { key: "j" });
    fireEvent.keyDown(document.body, { key: "u" });
    await waitFor(() => expect(decisions(del)).toEqual(["/api/calls/2/save"]));
    expect(post.mock.calls).toEqual([]);
  });

  it("u in the Inbox issues nothing at all", async () => {
    // There is no decision row to delete, so there is nothing to undo. Awaited
    // flush, because TanStack defers mutate() to a microtask.
    const { post, del } = stub();
    renderFeed("/?view=inbox");
    await screen.findByText("Call one");
    fireEvent.keyDown(document.body, { key: "u" });
    await expectNoDecision(post, del);
  });

  it("the Return to inbox button issues exactly what u issues", async () => {
    const { del } = stub();
    renderFeed("/?view=hidden");
    await screen.findByText("Call one");
    fireEvent.click(screen.getByRole("button", { name: "Return to inbox" }));
    await waitFor(() => expect(decisions(del)).toEqual(["/api/calls/1/hide"]));
    expect(profileIdOf(del, 0)).toBe(7);
  });
});

describe("gate 10 — a decision refetches BOTH caches (T7-12)", () => {
  it("a save refetches the list and the rail counts", async () => {
    // B29a killer. useSaveCall's onSuccess -> invalidateDecisions is the only
    // thing that fires the second request to each endpoint; App.tsx's staleTime
    // is 20 s, so a forgotten invalidation is a rail that sits one triage behind
    // for the rest of the session.
    const { post, get } = stub();
    renderFeed();
    await screen.findByText("Call one");
    expect(gets(get, "/api/calls")).toHaveLength(1);
    expect(gets(get, "/api/calls/decisions/counts")).toHaveLength(1);
    fireEvent.keyDown(document.body, { key: "s" });
    await waitFor(() => expect(decisions(post)).toEqual(["/api/calls/1/save"]));
    await waitFor(() => expect(gets(get, "/api/calls")).toHaveLength(2));
    await waitFor(() => expect(gets(get, "/api/calls/decisions/counts")).toHaveLength(2));
  });

  it("a hide refetches the rail counts too, which useHideCall alone does not", async () => {
    // B29b killer. MEASURED, not assumed: useHideCall invalidates ["calls"] and
    // ["decision-counts"] in ONE onSuccess (invalidateDecisions), and dropping
    // the counts half leaves the list refetching and the rail frozen — which is
    // why the counts assertion, not the list one, is what has teeth here. Gate
    // 10b pins the other half of that contract: it survives an unmount.
    const { post, get } = stub();
    renderFeed();
    await screen.findByText("Call one");
    fireEvent.keyDown(document.body, { key: "h" });
    await waitFor(() => expect(decisions(post)).toEqual(["/api/calls/1/hide"]));
    await waitFor(() => expect(gets(get, "/api/calls/decisions/counts")).toHaveLength(2));
    expect(gets(get, "/api/calls").length).toBeGreaterThanOrEqual(2);
  });

  it("an un-decide refetches both too", async () => {
    const { del, get } = stub();
    renderFeed("/?view=saved");
    await screen.findByText("Call one");
    fireEvent.keyDown(document.body, { key: "u" });
    await waitFor(() => expect(decisions(del)).toEqual(["/api/calls/1/save"]));
    await waitFor(() => expect(gets(get, "/api/calls/decisions/counts")).toHaveLength(2));
  });
});

describe("gate 10b — the counts invalidation outlives the feed (F1)", () => {
  it("a hide invalidates ['decision-counts'] even when the feed unmounts first", async () => {
    // TanStack v5 drops a mutate()-level callback the moment the observer has
    // no listeners left (query-core/build/modern/mutationObserver.js:77 —
    // `if (this.#mutateOptions && this.hasListeners())`). Triage is a keyboard
    // loop: `h` and then a click through to the call page inside the same few
    // hundred milliseconds is the ORDINARY path, and it lost the rail's
    // invalidation completely — the counts then read one triage behind for the
    // rest of App.tsx's 20 s staleTime. So ["decision-counts"] is invalidated by
    // useHideCall ITSELF (api/calls.ts), where the Mutation carries the callback
    // and no live observer is required.
    let land = () => {};
    const landed = new Promise<void>((r) => {
      land = r;
    });
    const { post } = stub();
    post.mockImplementation((() =>
      landed.then(() => ({
        data: { hidden: true },
        error: undefined,
        response: new Response(),
      }))) as never);

    const qc = new QueryClient();
    const invalidate = vi.spyOn(qc, "invalidateQueries");
    const { unmount } = renderFeed("/", null, qc);
    await screen.findByText("Call one");
    fireEvent.keyDown(document.body, { key: "h" });
    // The POST is genuinely in flight BEFORE the reader leaves — without this
    // the gate would pass on a hide that never happened at all.
    await waitFor(() => expect(decisions(post)).toEqual(["/api/calls/1/hide"]));

    unmount();
    // Everything below is measured AFTER the last listener is gone.
    invalidate.mockClear();
    await act(async () => {
      land();
      await new Promise((r) => setTimeout(r, 0));
    });

    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ["decision-counts"] }));
    // Additive, not instead-of: the list invalidation useHideCall always had.
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["calls"] });
  });
});

describe("D20 — a key cannot decide a card from the list it just left", () => {
  it("ignores s while the list query is still fetching", async () => {
    // keepPreviousData keeps the OLD rows on screen while the refetch a decision
    // triggered is in flight. Without this guard a second `s` in that window
    // decides a card the server has already moved out of the view.
    let release = () => {};
    const held = new Promise<void>((r) => {
      release = r;
    });
    const { post } = stub({ hold: held });
    renderFeed();
    await screen.findByText("Call one");
    fireEvent.keyDown(document.body, { key: "s" });
    await waitFor(() => expect(decisions(post)).toEqual(["/api/calls/1/save"]));

    // The refetch is now hanging: nothing more may be decided.
    fireEvent.keyDown(document.body, { key: "j" });
    fireEvent.keyDown(document.body, { key: "s" });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(decisions(post)).toEqual(["/api/calls/1/save"]);

    // POSITIVE CONTROL: once the list settles, the very same keystroke works —
    // so this gate cannot be satisfied by a feed whose keys are simply broken.
    release();
    await waitFor(() => expect(screen.getByText("Call one")).toBeInTheDocument());
    await waitFor(() => {
      fireEvent.keyDown(document.body, { key: "s" });
      expect(decisions(post)).toHaveLength(2);
    });
  });
});

describe("reviewer Minor 1 — decide()/undecide() are gated on isFetching, not just the key listener", () => {
  it('the "Return to inbox" button issues no request while a refetch is in flight, and issues it once released', async () => {
    // The D20 comment on the key listener only covers keystrokes. TriageCard's
    // "Return to inbox" button calls onUndecide (= undecide()) directly and
    // never goes near the listener, and keepPreviousData renders that button
    // on the STALE view's rows for the whole window a refetch is in flight —
    // e.g. a Saved→Hidden switch shows the Hidden button over a row from the
    // list being left. Without the guard INSIDE undecide()/decide() themselves,
    // a click in that window issues the mutation anyway.
    let release = () => {};
    const held = new Promise<void>((r) => {
      release = r;
    });
    const { post, del } = stub({ hold: held });
    renderFeed("/?view=hidden");
    await screen.findByText("Call one");

    // Put a refetch in flight through a DIFFERENT trigger than the one under
    // test: `s` in Hidden is a flip (still a valid action, kind "save") whose
    // onSuccess invalidates ["calls"], and the held stub keeps that refetch
    // hanging — the same D20 window, reached a different way.
    fireEvent.keyDown(document.body, { key: "s" });
    await waitFor(() => expect(decisions(post)).toEqual(["/api/calls/1/save"]));

    const delCountBefore = del.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "Return to inbox" }));
    // Awaited flush, not a synchronous assertion — TanStack defers mutate() to
    // a microtask (T7-1).
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(del.mock.calls.length).toBe(delCountBefore);

    // POSITIVE CONTROL, same render: once the refetch settles, the identical
    // click does issue the DELETE — so a button whose handler is simply
    // broken cannot pass this gate.
    release();
    await waitFor(() => expect(screen.getByText("Call one")).toBeInTheDocument());
    // Click INSIDE waitFor and retried, like the D20 gate's own positive
    // control: isFetching does not necessarily read false the instant
    // release() resolves, so a single un-retried click can race the state
    // update it depends on.
    await waitFor(() => {
      fireEvent.click(screen.getByRole("button", { name: "Return to inbox" }));
      expect(del.mock.calls.length).toBe(delCountBefore + 1);
    });
  });
});

describe("reviewer Minor 2 — focus follows a decided card, not just the numeric index", () => {
  it("s on a non-final card refocuses the DOM article that slides into its slot", async () => {
    // Local mock, not the shared stub(): this gate needs the refetched list to
    // actually be SHORTER after a decision, which the shared stub's static
    // `items` cannot express — every other gate is content with a fixed list,
    // so that behaviour is not folded into stub() itself. THREE cards; the
    // decision removes the SECOND ("Call two") and "Call three" slides into
    // its slot — the numeric focus index (1) never moves, only the id sitting
    // at it does, so a focus effect keyed on the index alone would not re-run
    // and the newly-shifted card would sit expanded but un-focused.
    const decidedIds = new Set<number>();
    vi.spyOn(clientMod.api, "GET").mockImplementation((async (path: string) => {
      if (path === "/api/calls/decisions/counts") {
        return {
          data: { inbox: 99, saved: 4, hidden: 12 },
          error: undefined,
          response: new Response(),
        };
      }
      const visible = THREE.filter((c) => !decidedIds.has(c.id));
      return {
        data: {
          items: visible,
          summary: { open_count: 0, high_score: 0, soon_deadline: 0 },
          pagination: {
            page: 1,
            limit: 50,
            has_prev: false,
            has_next: false,
            total: visible.length,
          },
          ranking: { pending: 0, failed: 0 },
          effective: { profile_id: 7, source: null, sort: "newest", show_closed: false },
          facets: { sources: [], profiles: [{ id: 7, name: "Molecular oncology" }] },
        },
        error: undefined,
        response: new Response(),
      };
    }) as never);
    vi.spyOn(clientMod.api, "POST").mockImplementation((async (_path: string, opts: unknown) => {
      const { params } = opts as { params: { path: { call_id: number } } };
      decidedIds.add(params.path.call_id);
      return { data: { state: "saved" }, error: undefined, response: new Response() };
    }) as never);
    vi.spyOn(clientMod.api, "DELETE").mockResolvedValue({
      data: { state: null },
      error: undefined,
      response: new Response(),
    } as never);

    const { container } = renderFeed();
    await screen.findByText("Call one");

    fireEvent.keyDown(document.body, { key: "j" });
    await waitFor(() =>
      expect(document.activeElement).toBe(container.querySelectorAll("article")[1]),
    );

    fireEvent.keyDown(document.body, { key: "s" });
    await waitFor(() => expect(screen.queryByText("Call two")).toBeNull());
    await waitFor(() =>
      expect(container.querySelectorAll("article")[1]).toHaveTextContent("Call three"),
    );
    await waitFor(() =>
      expect(document.activeElement).toBe(container.querySelectorAll("article")[1]),
    );
  });
});

describe("reviewer Minor 3 — the cursor also resets when the PAGE changes, not just the view", () => {
  it("clicking Next page resets focus to the first card of the new page", async () => {
    // D15's Next-page link changes only `?page=`; `view` stays "saved" across
    // the click, so a reset effect keyed on `[view]` alone would carry a
    // page-1 cursor straight onto page 2.
    stub({ hasNext: true });
    const { container } = renderFeed("/?view=saved&profile=7");
    await screen.findByText("Call one");

    fireEvent.keyDown(document.body, { key: "j" });
    await waitFor(() =>
      expect(container.querySelectorAll("[data-expanded]")[1]).toHaveAttribute(
        "data-expanded",
        "true",
      ),
    );

    fireEvent.click(screen.getByRole("link", { name: /next page/i }));

    await waitFor(() =>
      expect(container.querySelectorAll("[data-expanded]")[0]).toHaveAttribute(
        "data-expanded",
        "true",
      ),
    );
    expect(container.querySelectorAll("[data-expanded]")[1]).toHaveAttribute(
      "data-expanded",
      "false",
    );
  });
});

describe("Task 8 — no profile is an onboarding screen, not a firehose", () => {
  // THE FIXTURE GIVES THREE ITEMS WITH profile_id null ON PURPOSE: that is what
  // the server really sends in this state (dashboard.py's unranked fallback is
  // deliberately untouched, and the full edition keeps it). An `items: []`
  // fixture would let a component that renders the onboarding screen ABOVE the
  // feed pass every absence assertion for free.
  it("gate 1 — renders the onboarding screen INSTEAD of the feed", async () => {
    const { get } = stub({ items: THREE, profileId: null, profiles: [] });
    const { container } = renderFeed();
    expect(
      await screen.findByRole("heading", { name: "No research profile yet." }),
    ).toBeInTheDocument();
    // THE FEED IS GONE — not empty, GONE. Asserting the onboarding copy alone
    // passes a component that renders both, so three separate structures: the
    // cards, the rail that frames them, and the inbox-zero panel.
    expect(container.querySelectorAll("[data-call-id]")).toHaveLength(0);
    expect(container.querySelector("[data-count]")).toBeNull();
    expect(container.querySelector("[data-empty]")).toBeNull();
    // The rail is gone with the feed, so the three view tabs are unreachable
    // until a profile is picked (recorded decision).
    expect(screen.queryByRole("navigation", { name: "Triage views" })).toBeNull();
    // And the counts query is never fired — profile_id is a REQUIRED int there,
    // so a request in this state is a 422 the user would never see.
    expect(gets(get, "/api/calls/decisions/counts")).toHaveLength(0);
  });

  // Gate 2, in BOTH directions and in one test: inbox zero says *you are done*,
  // this says *you have not started*, and shipping one for the other is exactly
  // the failure this catches. Cross-absence, not a string comparison — `A !== B`
  // passes for a component that renders neither.
  it("gate 2 — says something DIFFERENT from inbox zero, in both directions", async () => {
    stub({ items: THREE, profileId: null, profiles: [] });
    const noProfile = renderFeed();
    await screen.findByRole("heading", { name: "No research profile yet." });
    expect(noProfile.container.textContent).not.toContain("Inbox zero.");
    expect(noProfile.container.textContent).not.toContain("You have triaged every call");
    noProfile.unmount();

    stub({ items: [], counts: { inbox: 0, saved: 4, hidden: 12 } });
    const inboxZero = renderFeed();
    expect(await screen.findByText("Inbox zero.")).toBeInTheDocument();
    expect(inboxZero.container.textContent).not.toContain("No research profile yet.");
    expect(inboxZero.container.textContent).not.toContain("Pick a research profile.");
    expect(inboxZero.container.querySelector('[data-screen="no-profile"]')).toBeNull();
  });

  // Gate 3. A user who already has profiles is offered them; sending them to
  // /profiles/new ALONE asks for a duplicate of what they already own.
  it("gate 3 — offers the readable profiles when the user has some but none is selected", async () => {
    stub({
      items: THREE,
      profileId: null,
      profiles: [
        { id: 7, name: "Molecular oncology" },
        { id: 9, name: "Clinical genomics" },
      ],
    });
    const { container } = renderFeed();
    await screen.findByRole("heading", { name: "Pick a research profile." });
    const picks = [...container.querySelectorAll("[data-profile-pick]")];
    expect(picks.map((el) => el.getAttribute("href"))).toEqual(["/?profile=7", "/?profile=9"]);
    expect(screen.queryByRole("heading", { name: "No research profile yet." })).toBeNull();
    // The other branch of gate 3 lives in TriageEmptyProfile.test.tsx: with no
    // profiles there is /profiles/new and NO picker.
  });

  it("fires no decision when there is no profile to record it on", async () => {
    // NOT because the listener is gone — it is still bound. The keydown effect
    // runs on every render of TriageFeed, including the one that returns the
    // onboarding screen, so `s` and `h` over that screen still reach onKey with
    // three items in scope. What stands the decision down is the FIRST LINE of
    // decide() and undecide(): `if (profileId === null) return`. That is the
    // guard this test protects, and deleting it fires a POST with no profile —
    // a guaranteed 422. It awaits past TanStack's microtask deferral — see
    // expectNoDecision.
    const { post, del } = stub({ items: THREE, profileId: null, profiles: [] });
    renderFeed();
    await screen.findByRole("heading", { name: "No research profile yet." });
    fireEvent.keyDown(document.body, { key: "s" });
    fireEvent.keyDown(document.body, { key: "h" });
    await expectNoDecision(post, del);
  });
});
