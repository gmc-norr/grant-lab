import { QueryClient } from "@tanstack/react-query";
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
    decision_note: null,
    ranking_failed: false,
    ranking_failed_at: null,
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
  // The same trick for the NOTE write (PUT /calls/{id}/note): with it the write
  // is still in flight while the next call is decided, which is the window
  // gate 11 is about.
  holdPut = null as Promise<void> | null,
  // ...and for the DECISION write itself. M-fe-1's second half is about the
  // window "a decision of mine has not landed yet", so that window has to be a
  // fact of the stub rather than a race against a microtask.
  holdPost = null as Promise<void> | null,
  // A LIST THAT CHANGES BETWEEN FETCHES, which the static `items` cannot
  // express — and M-fe-1 is entirely about what a refetch does to the cursor.
  // Entry n answers list fetch n+1; the LAST entry is sticky, so every later
  // refetch keeps it.
  lists = null as CallItem[][] | null,
} = {}) {
  let listFetches = 0;
  const get = vi.spyOn(clientMod.api, "GET").mockImplementation((async (path: string) => {
    if (path === "/api/calls/decisions/counts") {
      return { data: counts, error: undefined, response: new Response() };
    }
    listFetches += 1;
    if (hold !== null && listFetches >= 2) await hold;
    const rows = lists === null ? items : lists[Math.min(listFetches - 1, lists.length - 1)];
    return {
      data: {
        items: rows,
        summary: { open_count: 0, high_score: 0, soon_deadline: 0 },
        pagination: {
          page: 1,
          limit: 50,
          has_prev: false,
          has_next: hasNext,
          total: rows.length,
        },
        ranking: { pending: 0, failed: 0 },
        effective: { profile_id: profileId, source: null, sort: "newest", show_closed: false },
        facets: { sources: [], profiles },
      },
      error: undefined,
      response: new Response(),
    };
  }) as never);
  const post = vi.spyOn(clientMod.api, "POST").mockImplementation((async () => {
    if (holdPost !== null) await holdPost;
    return { data: { state: "saved" }, error: undefined, response: new Response() };
  }) as never);
  const del = vi.spyOn(clientMod.api, "DELETE").mockResolvedValue({
    data: { state: null },
    error: undefined,
    response: new Response(),
  } as never);
  // STUBBED IN EVERY TEST, not only the note ones: the strip commits on blur,
  // so any test that focuses and leaves it would otherwise reach the real
  // openapi-fetch client.
  const put = vi.spyOn(clientMod.api, "PUT").mockImplementation((async () => {
    if (holdPut !== null) await holdPut;
    return { data: { decision_note: null }, error: undefined, response: new Response() };
  }) as never);
  return { get, post, del, put };
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

it("reports failed decisions without removing the call", async () => {
  const { post } = stub();
  post.mockResolvedValue({ error: { error: { code: "internal_error", message: "Service unavailable" } } } as never);
  renderFeed();
  await screen.findByText("Call one");
  await userEvent.click(screen.getAllByRole("button", { name: /^Save$/ })[0]);
  expect(await screen.findByRole("alert")).toHaveTextContent("Service unavailable");
  expect(screen.getByText("Call one")).toBeInTheDocument();
});

it("search and deadline controls update the API query and reset pagination", async () => {
  const { get } = stub();
  renderFeed("/?profile=7&page=3");
  await screen.findByText("Call one");
  fireEvent.change(screen.getByRole("searchbox", { name: "Search calls" }), { target: { value: "oncology" } });
  await userEvent.click(screen.getByRole("button", { name: /^Search$/ }));
  await waitFor(() => expect(get).toHaveBeenCalledWith("/api/calls", { params: { query: expect.objectContaining({ q: "oncology", page: 1, profile: "7" }) } }));
  fireEvent.change(screen.getByRole("combobox", { name: "Order" }), { target: { value: "deadline" } });
  await waitFor(() => expect(get).toHaveBeenCalledWith("/api/calls", { params: { query: expect.objectContaining({ q: "oncology", sort: "deadline", page: 1 }) } }));
});

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

// Every note write, WITH THE IDS IT CARRIED. Gate 11 is entirely about which
// call_id ends up in the path, so the id is read from the request rather than
// from anything the component says about itself.
function notes(spy: SpyCalls): { callId: number; profileId: unknown; body: unknown }[] {
  return spy.mock.calls.map((c) => {
    const [, opts] = c as unknown as [
      string,
      { params: { path: { call_id: number }; query: { profile_id: number } }; body: unknown },
    ];
    return {
      callId: opts.params.path.call_id,
      profileId: opts.params.query.profile_id,
      body: opts.body,
    };
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

  it("focusing a card with a click or Tab moves the cursor there too", async () => {
    // REPRODUCED by an outside review: focus the second call, press `s`, and
    // the FIRST call was saved. `focusIndex` moved only on j/k, so the app's
    // idea of "current" and the browser's real focus were two different things,
    // and the keystroke used the stale one. Nothing about this is visible --
    // the wrong call is simply decided.
    const { post } = stub();
    renderFeed();
    await screen.findByText("Call one");
    const second = document.querySelector('[data-call-id="2"]') as HTMLElement;
    // `act` because .focus() is a raw DOM call, not a fireEvent: without it the
    // resulting setState has not flushed when the keydown below runs, and the
    // test fails against a fix that works.
    act(() => second.focus());
    fireEvent.keyDown(document.body, { key: "s" });
    await waitFor(() => expect(decisions(post)).toEqual(["/api/calls/2/save"]));
  });

  it("the Save button decides ITS OWN card, whatever the cursor believes", async () => {
    // A click carries which call it belongs to, so it cannot decide a different
    // one -- that is the whole reason visible controls fix this and a better
    // keyboard cursor only narrows it. The cursor is deliberately left on card
    // one here: a button wired to `focused` instead of to its own `call` would
    // save call 1 and pass a weaker test.
    const { post } = stub();
    renderFeed();
    await screen.findByText("Call three");
    const third = document.querySelector('[data-call-id="3"]') as HTMLElement;
    const save = within(third).getByRole("button", { name: /^save$/i });
    fireEvent.click(save);
    await waitFor(() => expect(decisions(post)).toEqual(["/api/calls/3/save"]));
  });

  it("the Hide button does the same for the other half of the decision", async () => {
    const { post } = stub();
    renderFeed();
    await screen.findByText("Call three");
    const third = document.querySelector('[data-call-id="3"]') as HTMLElement;
    fireEvent.click(within(third).getByRole("button", { name: /^hide$/i }));
    await waitFor(() => expect(decisions(post)).toEqual(["/api/calls/3/hide"]));
  });

  it("EVERY inbox card carries the controls, not just the focused one", async () => {
    // Guards the two gates above. Rendered only on the expanded card, the
    // buttons would still require moving the cursor first to reach any other
    // call -- which is the interaction that was broken. Then a test that
    // clicked "the Save button" would silently be testing the focused card
    // again.
    stub();
    renderFeed();
    await screen.findByText("Call one");
    expect(screen.getAllByRole("button", { name: /^save$/i })).toHaveLength(3);
    expect(screen.getAllByRole("button", { name: /^hide$/i })).toHaveLength(3);
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
    // AWAITED, and the second `s` RETRIED, since M-fe-1: a decision still in
    // flight now refuses the next one, so two `s` presses in the same tick are
    // one POST by design (see "a second s while the decision is still in flight
    // does not post twice"). The claim this gate makes is unchanged — six j's
    // land on card 3 and six k's land back on card 1, both POSTs asserted by
    // path — only the waiting is.
    await waitFor(() => expect(decisions(post)).toEqual(["/api/calls/3/save"]));
    for (let i = 0; i < 6; i++) fireEvent.keyDown(document.body, { key: "k" });
    await waitFor(() => {
      fireEvent.keyDown(document.body, { key: "s" });
      expect(decisions(post)).toEqual(["/api/calls/3/save", "/api/calls/1/save"]);
    });
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
  it("an empty PAGE TWO is not inbox zero", async () => {
    // REPRODUCED by an outside review: deciding the last card on page two
    // showed "you have triaged every call" while the rail beside it still said
    // 50 were left. The rail was right -- the claim is about the whole list and
    // was being made from the slice on screen. The counts here are deliberately
    // NOT zero, which is the only thing that separates the two states.
    stub({ items: [], counts: { inbox: 50, saved: 0, hidden: 0 } });
    renderFeed("/?page=2");
    await screen.findByText(/nothing left on this page/i);
    expect(screen.queryByText(/inbox zero/i)).toBeNull();
    expect(await screen.findByText(/50 calls are still waiting/i)).toBeInTheDocument();
    // Page two of a self-healing inbox is a dead end without a way back: page
    // one has refilled and nothing else on this screen points at it.
    expect(screen.getByRole("link", { name: /previous page/i })).toBeInTheDocument();
  });

  it("but a genuinely empty inbox still says inbox zero", async () => {
    // Guards the gate above. "Never claim inbox zero" would pass it and delete
    // the screen the product is actually for.
    stub({ items: [], counts: { inbox: 0, saved: 0, hidden: 0 } });
    renderFeed("/?page=2");
    await screen.findByText(/inbox zero/i);
    expect(screen.queryByText(/nothing left on this page/i)).toBeNull();
  });

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
  it("ignores s until the decision before it has settled, list refetch and all", async () => {
    // RENAMED, not weakened, by M-fe-1 (review 2026-10-26). It used to say
    // "while the list query is still fetching", because `isFetching` was the
    // guard; the guard is now "a decision of mine is in flight", and this window
    // is still exactly that one. Both hooks return invalidateDecisions(qc)'s
    // promise from their onSuccess and TanStack v5 awaits it before settling the
    // mutation, so holding the refetch below holds the mutation pending — which
    // is why the same held stub still closes the same window, and why dropping
    // `decisionPending` reddens this test (MEASURED: mutation C).
    //
    // What it is about has not moved: keepPreviousData keeps the OLD rows on
    // screen while the refetch a decision triggered is in flight, and a second
    // `s` in that window decides a card the server has already moved out of the
    // view — and writes twice besides.
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

describe("reviewer Minor 1 — decide()/undecide() carry the guard themselves, not just the key listener", () => {
  it('the "Return to inbox" button issues no request while the decision before it is in flight, and issues it once released', async () => {
    // RENAMED by M-fe-1: the guard inside decide()/undecide() was `isFetching`
    // and is now `refuseDecision`. The window this drives is unchanged and the
    // assertions are untouched — dropping `decisionPending` reddens this test
    // (MEASURED: mutation C), so it still pins a guard that is really there.
    //
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
    // THE CARD is gone, asserted on the card and not on the title text: since
    // Task 6 the note strip names the call it was opened for, so "Call two" is
    // still on screen — above the list, in the strip — after the row it belongs
    // to has left. queryByText here would now be asserting the strip's absence.
    await waitFor(() => expect(container.querySelector('[data-call-id="2"]')).toBeNull());
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
    const { container } = renderFeed("/?view=saved&profile=7&layout=expanded");
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

describe("Task 6 — the decision-note strip (D10, gates 10 and 11)", () => {
  // Waits for the strip and hands it back, so every test below asserts on the
  // real element rather than on `getByRole("textbox")` — the feed has exactly
  // one text field today, and a gate that would silently start testing a
  // different one when a search box lands is a gate with a shelf life.
  async function stripFor(container: HTMLElement, callId: number): Promise<HTMLElement> {
    return await waitFor(() => {
      const el = container.querySelector(`[data-note-strip="${callId}"]`);
      expect(el, `no note strip for call ${callId}`).not.toBeNull();
      return el as HTMLElement;
    });
  }

  it("the strip does not appear when the decision request fails", async () => {
    // D10. Optimistic display would solicit a note for a decision that did not
    // happen -- and the note endpoint would then 404 on a row that never existed.
    const { post } = stub();
    post.mockResolvedValue({
      data: undefined,
      error: { error: { code: "internal_error", message: "boom" } },
      response: new Response(null, { status: 500 }),
    } as never);
    const { container } = renderFeed();
    await screen.findByText("Call one");

    fireEvent.keyDown(document.body, { key: "h" });
    await waitFor(() => expect(decisions(post)).toEqual(["/api/calls/1/hide"]));
    // Past TanStack's microtask deferral AND past the rejection, so this is
    // "the strip never appeared", not "the strip has not appeared yet".
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(container.querySelector("[data-note-strip]")).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();

    // POSITIVE CONTROL, same render. Without it this gate is green for a feed
    // that never renders a strip at all — which is both the likeliest way to
    // "pass" it and the exact failure it cannot distinguish. One character
    // changes: the POST now succeeds.
    post.mockResolvedValue({
      data: { hidden: true },
      error: undefined,
      response: new Response(),
    } as never);
    await waitFor(() => {
      fireEvent.keyDown(document.body, { key: "h" });
      expect(container.querySelector("[data-note-strip]")).not.toBeNull();
    });
  });

  it("a keystroke inside the strip does not decide the focused call", async () => {
    // G10 / spec gate 10. VACUITY, and it is the whole reason this test is
    // written the way it is: a keydown fired at document.body carries body as
    // its target whatever has focus, so a version of this test that never
    // focuses the input passes whether or not isEditableTarget guards the
    // listener. Focus it FIRST and fire AT IT.
    const { post, del, put } = stub();
    const { container } = renderFeed();
    await screen.findByText("Call one");

    fireEvent.keyDown(document.body, { key: "h" });
    await waitFor(() => expect(decisions(post)).toEqual(["/api/calls/1/hide"]));
    const strip = await stripFor(container, 1);
    const box = within(strip).getByRole("textbox");
    act(() => box.focus());
    expect(document.activeElement).toBe(box);

    // Every triage key, typed as a letter of a reason would be.
    fireEvent.keyDown(box, { key: "h" });
    fireEvent.keyDown(box, { key: "s" });
    fireEvent.keyDown(box, { key: "j" });
    fireEvent.keyDown(box, { key: "k" });
    fireEvent.keyDown(box, { key: "u" });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    // The one hide that OPENED the strip, and nothing since.
    expect(decisions(post)).toEqual(["/api/calls/1/hide"]);
    expect(del.mock.calls).toEqual([]);

    // POSITIVE CONTROL, same render: blurred, the identical keystroke decides.
    // It also proves `j` was SUPPRESSED rather than merely silent — the cursor
    // never moved, so `s` saves call ONE. A feed whose keyboard is simply
    // broken fails here, which is the failure a guard mutation is most easily
    // confused with.
    act(() => box.blur());
    await waitFor(() => {
      fireEvent.keyDown(document.body, { key: "s" });
      expect(decisions(post)).toContain("/api/calls/1/save");
    });
    expect(decisions(post)).not.toContain("/api/calls/2/save");
    // And leaving an untouched box wrote nothing: blur commits (D10), but only
    // what changed.
    expect(notes(put)).toEqual([]);
  });

  it("a late note write lands on its own call, not the next one", async () => {
    // G11 / spec gate 11. TWO ways the write could reach the wrong call, and
    // this drives both: the CURSOR moves off call one before the note is
    // committed, and the next call is DECIDED — replacing the strip — before
    // the PUT resolves. The request must still carry call one's id.
    let releasePut = () => {};
    const putHeld = new Promise<void>((r) => {
      releasePut = r;
    });
    const { post, put } = stub({ holdPut: putHeld });
    const { container } = renderFeed();
    await screen.findByText("Call one");

    fireEvent.keyDown(document.body, { key: "h" });
    await waitFor(() => expect(decisions(post)).toEqual(["/api/calls/1/hide"]));
    const strip = await stripFor(container, 1);

    // THE CURSOR MOVES OFF THE CALL THE STRIP IS ABOUT, before a single
    // character is typed. Without this the adverse mutation is invisible:
    // focus is still on call one at commit time, so "the strip's bound target"
    // and "the currently focused call" give the same id and the gate is
    // hollow.
    fireEvent.keyDown(document.body, { key: "j" });
    await waitFor(() =>
      expect(container.querySelector('[data-expanded="true"]')).toHaveAttribute(
        "data-call-id",
        "2",
      ),
    );

    await userEvent.type(within(strip).getByRole("textbox"), "wrong field{Enter}");
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));

    // The PUT is hanging. Decide the NEXT call: the strip is replaced, the
    // in-flight request is NOT cancelled (D10). Fired inside waitFor because a
    // decision's refetch may still be in flight, and D20 stands `h` down for
    // exactly that window.
    await waitFor(() => {
      fireEvent.keyDown(document.body, { key: "h" });
      expect(decisions(post)).toContain("/api/calls/2/hide");
    });
    await stripFor(container, 2);
    releasePut();

    // ONE write, carrying call one's ids and call one's state — asserted off
    // the request, not off the component.
    await waitFor(() =>
      expect(notes(put)).toEqual([
        {
          callId: 1,
          profileId: 7,
          body: { note: "wrong field", expected_state: "hidden" },
        },
      ]),
    );
  });

  it("opens over the note the row already carries, so a re-decision cannot replace it unseen", async () => {
    // The feed's half of the same blind overwrite CallDetail had. `h` on an
    // ALREADY-HIDDEN card re-runs the upsert, whose CASE preserves the note
    // through `hidden -> hidden` -- so an unseeded box sits over a stored
    // reason, and the first Enter destroys it. Less harmful than on the call
    // page only because the note is rendered on the card two lines away; it is
    // the same destruction.
    const { post } = stub({
      items: [call({ id: 1, title: "Call one", decision_note: "wrong field" })],
    });
    const { container } = renderFeed("/?view=hidden");
    await screen.findByText("Call one");

    fireEvent.keyDown(document.body, { key: "h" });
    await waitFor(() => expect(decisions(post)).toEqual(["/api/calls/1/hide"]));
    const strip = await stripFor(container, 1);
    expect(within(strip).getByRole("textbox")).toHaveValue("wrong field");
  });

  it("opens EMPTY on a flip, because the flip has just cleared that note (D9)", async () => {
    // THE OTHER HALF, and the reason the seed is conditional rather than
    // `call.decision_note ?? null`. `s` in Hidden writes `hidden -> saved`, and
    // the save upsert's CASE writes NULL over the note. Seeding here would show
    // text the server discarded a moment ago and re-save it on the next Enter --
    // resurrecting, on a SAVE, a reason written about a HIDE.
    const { post } = stub({
      items: [call({ id: 1, title: "Call one", decision_note: "wrong field" })],
    });
    const { container } = renderFeed("/?view=hidden");
    await screen.findByText("Call one");

    fireEvent.keyDown(document.body, { key: "s" });
    await waitFor(() => expect(decisions(post)).toEqual(["/api/calls/1/save"]));
    const strip = await stripFor(container, 1);
    expect(within(strip).getByRole("textbox")).toHaveValue("");
  });

  it("un-deciding the call the strip is about closes it", async () => {
    // D4: a note belongs to its decision and dies with it. `u` deletes the
    // decision row, so a strip left open over it can only ever 404.
    const { post, del } = stub();
    const { container } = renderFeed("/?view=hidden");
    await screen.findByText("Call one");

    // `s` in Hidden is a flip — still a decision, so it opens the strip.
    fireEvent.keyDown(document.body, { key: "s" });
    await waitFor(() => expect(decisions(post)).toEqual(["/api/calls/1/save"]));
    await stripFor(container, 1);

    await waitFor(() => {
      fireEvent.keyDown(document.body, { key: "u" });
      expect(decisions(del)).toContain("/api/calls/1/hide");
    });
    await waitFor(() => expect(container.querySelector("[data-note-strip]")).toBeNull());
  });
});

// THE COMPOSITION, and it has to be tested here because neither half can see
// it alone: TriageCard.test.tsx renders a card with no feed, so there is no
// keyboard loop for a stolen caret to kill, and the feed's OWN strip above has
// no autoFocus, so its remount steals nothing. The bug lives only where a
// row's autofocused editor sits inside the feed that owns `j/k/s/h/u`.
describe("a row's note editor, inside the feed (Important 1 — the focus steal)", () => {
  // The PUT that changes the note on the server, so the refetch it triggers
  // changes the card's `decision_note`, changes the strip's key, and REMOUNTS
  // the strip. That remount is the whole event under test: without it the
  // focus effect never re-fires and every assertion below passes vacuously.
  function putThatEdits(put: SpyCalls, rows: CallItem[], index: number, after: string) {
    (put as unknown as { mockImplementation: (fn: unknown) => void }).mockImplementation(
      (async () => {
        rows[index] = call({ ...rows[index], decision_note: after });
        return { data: { decision_note: after }, error: undefined, response: new Response() };
      }) as never,
    );
  }

  it("does not pull the caret back after a BLUR commits the note, so j/k/s/h/u stay alive (D2)", async () => {
    const rows = [call({ id: 1, title: "Call one", decision_note: "before" })];
    const { put, del } = stub({ items: rows });
    putThatEdits(put, rows, 0, "before and after");

    renderFeed("/?view=hidden");
    await screen.findByText("Call one");

    await userEvent.click(screen.getByRole("button", { name: "before" }));
    const box = await screen.findByRole("textbox");
    await waitFor(() => expect(document.activeElement).toBe(box));
    await userEvent.type(box, " and after");

    // CLICKING AWAY, onto a part of the page that takes no focus. That leaves
    // document.activeElement on <body> -- which is precisely the state the
    // feed's window listener is live in, and the state the strip must not
    // undo.
    await userEvent.click(document.body);
    await waitFor(() => expect(put).toHaveBeenCalled());
    // The refetch has landed and the strip has been remounted with the new
    // seed. Asserting on the VALUE, not on a timer: this is what proves the
    // remount actually happened.
    await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue("before and after"));

    expect(document.activeElement).not.toBe(screen.getByRole("textbox"));

    // AND THE CONSEQUENCE, which is the half that matters. Fired AT the active
    // element and never at document.body: a keydown dispatched on body carries
    // body as its target whatever has focus, so `isEditableTarget` would stand
    // down for nothing and this gate would be green on the broken build too.
    fireEvent.keyDown(document.activeElement ?? document.body, { key: "u" });
    await waitFor(() => expect(decisions(del)).toEqual(["/api/calls/1/hide"]));
  });

  it("leaves the caret in the SECOND row's box when the first row's blur-commit lands (D10)", async () => {
    // The mis-filing D10 and D11 exist to prevent, arriving through focus
    // instead of through ids: row one's round-trip yanks the caret out of row
    // two's box, and row two's reason is then typed into row one's note.
    const rows = [
      call({ id: 1, title: "Call one", decision_note: "one" }),
      call({ id: 2, title: "Call two", decision_note: "two" }),
    ];
    const { put } = stub({ items: rows });
    putThatEdits(put, rows, 0, "one edited");

    const { container } = renderFeed("/?view=hidden");
    await screen.findByText("Call one");

    await userEvent.click(screen.getByRole("button", { name: "one" }));
    await userEvent.type(screen.getByRole("textbox"), " edited");
    // Straight into the SECOND row's editor. This one click does both things:
    // it blurs the first box (which commits it) and it opens and focuses the
    // second.
    await userEvent.click(screen.getByRole("button", { name: "two" }));
    const secondBox = container.querySelector('[data-note-strip="2"] input');
    expect(secondBox, "no editor on the second row").not.toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(secondBox));

    await waitFor(() => expect(put).toHaveBeenCalled());
    // Row one's strip has been remounted with the server's value -- the event
    // that re-fires the focus effect.
    await waitFor(() =>
      expect(container.querySelector('[data-note-strip="1"] input')).toHaveValue("one edited"),
    );
    expect(document.activeElement).toBe(secondBox);
  });

  it("KEEPS the caret when the note was committed with Enter", async () => {
    // The other side of the same fix, and the reason it is not "never focus on
    // a remount". Enter leaves the user in the box; dropping focus there puts
    // an open-but-unfocused editor over a live `u`, which deletes the decision
    // the note hangs off (D4). TriageCard.test.tsx pins this without a feed;
    // this pins that the feed composition did not lose it.
    const rows = [call({ id: 1, title: "Call one", decision_note: "before" })];
    const { put } = stub({ items: rows });
    putThatEdits(put, rows, 0, "before and after");

    renderFeed("/?view=hidden");
    await screen.findByText("Call one");

    await userEvent.click(screen.getByRole("button", { name: "before" }));
    await userEvent.type(await screen.findByRole("textbox"), " and after{Enter}");
    await waitFor(() => expect(put).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue("before and after"));
    expect(document.activeElement).toBe(screen.getByRole("textbox"));
  });
});

// ---------------------------------------------------------------------------
// M-fe-1 (review 2026-10-26, appendix A8). TWO defects in one mechanism.
//
// (1) THE CURSOR WAS AN ARRAY INDEX. `useDashboard` polls every 5 s while
//     `ranking.pending > 0` and the server re-sorts by score, so a poll that
//     inserted a higher-scoring call ABOVE the one being read moved a different
//     call under the cursor — and `s`/`h` then decided that one, silently. A8's
//     probe: `expanded before poll="Call A (being read)" after
//     poll="Call C (just scored)"; pressed s -> POST /api/calls/3/save`. In a
//     hospital tool that is a decision recorded against a call nobody read.
//
// (2) THE SAME POLL DISABLED TRIAGE. `busy={isFetching}` and `if (isFetching)
//     return` stood the buttons and the keys down for the whole of every
//     background refetch — A8 measured the Save button disabled in 21 of 191
//     samples over 11 s, and `s` in that window produced `save POSTs=0` with no
//     alert. The window a decision must be refused in is "a decision of mine has
//     not landed yet", not "the list is fetching".
//
// Both halves are driven here, and so is the thing the index cursor got RIGHT
// and an id cursor could easily lose: when the focused call leaves the list the
// cursor falls back to the slot it occupied, clamped.
describe("M-fe-1 — a poll must not retarget s/h, and must not swallow them", () => {
  // Read off the DOM, not off anything the component says about itself: "which
  // card is expanded" is the only thing the reader can actually see, and A8's
  // probe is phrased in exactly those terms.
  function expandedCallId(): string | null {
    const el = document.querySelector('[data-expanded="true"]');
    return el === null ? null : el.getAttribute("data-call-id");
  }
  function order(): (string | null)[] {
    return [...document.querySelectorAll("article")].map((el) => el.getAttribute("data-call-id"));
  }

  const A = call({ id: 1, title: "Call A (being read)", aggregate_score: 80 });
  const B = call({ id: 2, title: "Call B", aggregate_score: 70 });
  const C = call({ id: 3, title: "Call C (just scored)", aggregate_score: 90 });

  it("s saves the call being read, not the one a poll inserted above it", async () => {
    const qc = new QueryClient();
    const { get, post } = stub({ lists: [[A, B], [C, A, B]] });
    renderFeed("/", null, qc);
    await screen.findByText("Call A (being read)");
    expect(expandedCallId()).toBe("1");

    // THE POLL, triggered through the cache rather than through a 5 s timer, so
    // the window is a fact instead of a race. refetchInterval would reach the
    // same invalidation.
    act(() => {
      void qc.invalidateQueries({ queryKey: ["calls"] });
    });
    // THE REORDER REALLY HAPPENED. Without this assertion the retarget this
    // gate is about cannot occur and every assertion below passes vacuously —
    // A8's own output is "C is above A now", so that is what is pinned.
    await waitFor(() => expect(order()).toEqual(["3", "1", "2"]));
    expect(gets(get, "/api/calls")).toHaveLength(2);

    // The cursor is the CALL, so the card under it is still A at its new index.
    await waitFor(() => expect(expandedCallId()).toBe("1"));

    fireEvent.keyDown(document.body, { key: "s" });
    await waitFor(() => expect(decisions(post)).toEqual(["/api/calls/1/save"]));
  });

  it("h hides the call being read, not the one a poll inserted above it", async () => {
    // The other half of A8's sentence "with `h`, the unread call silently goes
    // to Hidden" — and `h` is the worse one, because the reader never sees the
    // call again to notice.
    const qc = new QueryClient();
    const { post } = stub({ lists: [[A, B], [C, A, B]] });
    renderFeed("/", null, qc);
    await screen.findByText("Call A (being read)");

    act(() => {
      void qc.invalidateQueries({ queryKey: ["calls"] });
    });
    await waitFor(() => expect(order()).toEqual(["3", "1", "2"]));

    fireEvent.keyDown(document.body, { key: "h" });
    await waitFor(() => expect(decisions(post)).toEqual(["/api/calls/1/hide"]));
  });

  it("s during a background refetch still saves, exactly once", async () => {
    let release = () => {};
    const held = new Promise<void>((r) => {
      release = r;
    });
    const qc = new QueryClient();
    const { get, post } = stub({ hold: held });
    renderFeed("/", null, qc);
    await screen.findByText("Call one");
    expect(gets(get, "/api/calls")).toHaveLength(1);

    // A POLL, not a decision's refetch: nothing has been mutated yet, so the
    // only thing in flight is the background fetch. The stub hangs every list
    // fetch after the first, so "a second request was made" and "a refetch is
    // open" are the same fact — asserted, because a window that never opened
    // would make the keystroke below pass for the wrong reason.
    act(() => {
      void qc.invalidateQueries({ queryKey: ["calls"] });
    });
    await waitFor(() => expect(gets(get, "/api/calls")).toHaveLength(2));

    fireEvent.keyDown(document.body, { key: "s" });
    await waitFor(() => expect(decisions(post)).toEqual(["/api/calls/1/save"]));
    release();
  });

  it("a background refetch leaves the Save and Hide controls live", async () => {
    let release = () => {};
    const held = new Promise<void>((r) => {
      release = r;
    });
    const qc = new QueryClient();
    const { get } = stub({ hold: held });
    renderFeed("/", null, qc);
    await screen.findByText("Call one");

    act(() => {
      void qc.invalidateQueries({ queryKey: ["calls"] });
    });
    await waitFor(() => expect(gets(get, "/api/calls")).toHaveLength(2));

    // A8's 21-of-191 window. The refetch is held open for the whole of this
    // assertion, so a `busy={isFetching}` feed can only fail it.
    await waitFor(() => {
      expect(screen.getAllByRole("button", { name: /^Save$/ })[0]).toBeEnabled();
      expect(screen.getAllByRole("button", { name: /^Hide$/ })[0]).toBeEnabled();
    });
    release();
  });

  it("a decision in flight DOES disable the controls", async () => {
    // The other side of the same prop, and the reason the gate is not simply
    // deleted: a second click must not post twice. No list refetch is held
    // here, so `isFetching` is false throughout and only a mutation-aware
    // `busy` can make this pass.
    let releasePost = () => {};
    const held = new Promise<void>((r) => {
      releasePost = r;
    });
    const { post } = stub({ holdPost: held });
    renderFeed();
    await screen.findByText("Call one");
    expect(screen.getAllByRole("button", { name: /^Save$/ })[0]).toBeEnabled();

    fireEvent.click(screen.getAllByRole("button", { name: /^Save$/ })[0]);
    await waitFor(() => expect(decisions(post)).toEqual(["/api/calls/1/save"]));
    await waitFor(() => {
      expect(screen.getAllByRole("button", { name: /^Save$/ })[0]).toBeDisabled();
      expect(screen.getAllByRole("button", { name: /^Hide$/ })[0]).toBeDisabled();
    });

    // POSITIVE CONTROL: the controls come back, so this is not a feed whose
    // buttons are simply dead.
    releasePost();
    await waitFor(() =>
      expect(screen.getAllByRole("button", { name: /^Save$/ })[0]).toBeEnabled(),
    );
  });

  it("a second s while the decision is still in flight does not post twice", async () => {
    let releasePost = () => {};
    const held = new Promise<void>((r) => {
      releasePost = r;
    });
    const { post } = stub({ holdPost: held });
    renderFeed();
    await screen.findByText("Call one");

    fireEvent.keyDown(document.body, { key: "s" });
    await waitFor(() => expect(decisions(post)).toEqual(["/api/calls/1/save"]));
    // The write is hanging. Two more presses, as an impatient reader makes them.
    fireEvent.keyDown(document.body, { key: "s" });
    fireEvent.keyDown(document.body, { key: "s" });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(decisions(post)).toEqual(["/api/calls/1/save"]);

    // POSITIVE CONTROL, same render: once the write lands the identical
    // keystroke decides again, so the keys are not simply broken.
    releasePost();
    await waitFor(() => {
      fireEvent.keyDown(document.body, { key: "s" });
      expect(decisions(post)).toHaveLength(2);
    });
  });

  it("still refuses s while the rows on screen belong to the list being LEFT (D20)", async () => {
    // D20 is NOT what was dropped, and this is the test that says so. A VIEW
    // SWITCH is a new query key, so `placeholderData: keepPreviousData` holds the
    // old view's cards up while the new view's fetch is in flight — and a
    // keystroke in that window decides a card the server has already moved out
    // of the view. The narrow fact for that window is `isPlaceholderData`, which
    // a refetch of the list we are already on does NOT set; that is exactly why
    // the broad `isFetching` could be narrowed without losing this.
    let release = () => {};
    const held = new Promise<void>((r) => {
      release = r;
    });
    const { get, post, del } = stub({ hold: held });
    renderFeed();
    await screen.findByText("Call one");

    fireEvent.click(screen.getByRole("link", { name: /Saved/ }));
    await waitFor(() => expect(gets(get, "/api/calls")).toHaveLength(2));
    // The LEFT list's rows are still the ones on screen — the window itself,
    // asserted, so this cannot pass against a feed that rendered nothing.
    expect(order()).toEqual(["1", "2", "3"]);

    fireEvent.keyDown(document.body, { key: "s" });
    await expectNoDecision(post, del);

    // POSITIVE CONTROL: once the new list lands, the identical keystroke decides.
    release();
    await waitFor(() => {
      fireEvent.keyDown(document.body, { key: "s" });
      expect(decisions(post)).toHaveLength(1);
    });
  });

  it("falls back to the same index, clamped, when the focused call leaves the list", async () => {
    // THE PROPERTY THE INDEX CURSOR GOT RIGHT. An id cursor that simply loses
    // its target would focus nothing, or jump back to the top; neither is what
    // a reader whose row was withdrawn under them expects.
    const qc = new QueryClient();
    const [one, two, three] = THREE;
    const { post } = stub({ lists: [[one, two, three], [one, three], [one]] });
    renderFeed("/", null, qc);
    await screen.findByText("Call one");

    fireEvent.keyDown(document.body, { key: "j" });
    await waitFor(() => expect(expandedCallId()).toBe("2"));

    // "Call two" is withdrawn by an ingest, NOT by a decision made here — so
    // nothing in this component knows the cursor's call is going away.
    act(() => {
      void qc.invalidateQueries({ queryKey: ["calls"] });
    });
    await waitFor(() => expect(order()).toEqual(["1", "3"]));
    // Index 1 of the new list.
    await waitFor(() => expect(expandedCallId()).toBe("3"));

    // CLAMPED: the list shrinks to one row, so index 1 no longer exists.
    act(() => {
      void qc.invalidateQueries({ queryKey: ["calls"] });
    });
    await waitFor(() => expect(order()).toEqual(["1"]));
    await waitFor(() => expect(expandedCallId()).toBe("1"));

    fireEvent.keyDown(document.body, { key: "s" });
    await waitFor(() => expect(decisions(post)).toEqual(["/api/calls/1/save"]));
  });

  it("the fallback index is the slot the call ACTUALLY sat in, not the one it was picked at", async () => {
    // The two differ as soon as a poll reorders the list: A is picked at index
    // 0, the poll puts C above it, and A is at index 1 when it is withdrawn. A
    // cursor that remembered only "index 0" would land on C — the unread call
    // again, by a second route.
    const qc = new QueryClient();
    const { post } = stub({ lists: [[A, B], [C, A, B], [C, B]] });
    renderFeed("/", null, qc);
    await screen.findByText("Call A (being read)");
    expect(expandedCallId()).toBe("1");

    act(() => {
      void qc.invalidateQueries({ queryKey: ["calls"] });
    });
    await waitFor(() => expect(order()).toEqual(["3", "1", "2"]));
    await waitFor(() => expect(expandedCallId()).toBe("1"));

    // A is withdrawn. It was sitting at index 1; index 1 of [C, B] is B.
    act(() => {
      void qc.invalidateQueries({ queryKey: ["calls"] });
    });
    await waitFor(() => expect(order()).toEqual(["3", "2"]));
    await waitFor(() => expect(expandedCallId()).toBe("2"));

    fireEvent.keyDown(document.body, { key: "s" });
    await waitFor(() => expect(decisions(post)).toEqual(["/api/calls/2/save"]));
  });
});

describe("gate 13 — a malformed deep link degrades, it does not dead-end", () => {
  it("?sort=foo&page=abc asks for the default sort on page 1, in ONE request", async () => {
    // Re-verified by the 2026-10-26 review: `?sort=foo` sent 4 requests and
    // errored after 7.5 s, and `?page=abc` went on the wire as `page=NaN`.
    // Asserting what reaches the WIRE is the only place it matters — the server
    // validates `sort` against a Literal and `page` against ge=1, so an
    // unsanitised deep link is a 422 the feed cannot render around.
    const { get } = stub();
    renderFeed("/?sort=foo&page=abc");
    await screen.findByText("Call one");
    const q = queryOf(get, "/api/calls");
    expect(q.sort).toBeUndefined(); // omitted -> the server's own "newest"
    expect(q.page).toBe(1);
    // ONE request, not four: nothing was refused, so nothing was retried.
    expect(gets(get, "/api/calls")).toHaveLength(1);
  });

  it("a sort the server DOES accept still reaches it", async () => {
    // The other direction. A sanitiser that dropped everything would pass the
    // test above and silently remove the deadline ordering.
    const { get } = stub();
    renderFeed("/?sort=deadline&page=3");
    await screen.findByText("Call one");
    const q = queryOf(get, "/api/calls");
    expect(q.sort).toBe("deadline");
    expect(q.page).toBe(3);
  });
});
