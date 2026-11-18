import { readFileSync } from "node:fs";

import { QueryClient } from "@tanstack/react-query";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { appQueryClient } from "./App";
import * as clientMod from "./api/client";
import { ApiError } from "./api/client";
import { ROUTER_PROVIDER_PROPS } from "./routerProviderProps";
import { renderWithProviders } from "./test-utils";
import { TriageFeed } from "./routes/TriageFeed";

afterEach(() => {
  vi.restoreAllMocks();
});

// WHY THE ERROR SHAPE MATTERS MORE THAN THE PREDICATE. openapi-fetch hands back
// `{data, error, response}`; api/client.ts `unwrap` collapses that into an
// `ApiError` built from the envelope's CODE. Until this change it carried no
// status at all, so any `is4xx` written against it would have matched nothing
// and every 4xx would have gone on retrying — with a "zero retries" test still
// green if it only ever counted requests on a 2xx path. So the counts below are
// asserted on a REAL 422 and a REAL 500 answered through the real `unwrap`.

const QUERY_DEFAULTS = () => appQueryClient.getDefaultOptions().queries;

function clientFromAppDefaults() {
  // The app's OWN defaults, read off the client App.tsx builds — not a copy of
  // the predicate. Inverting the predicate in App.tsx reddens these tests.
  // `retryDelay` is the one override: the default backoff is 1 s then 2 s, which
  // is wall-clock cost and not the guard under test.
  return new QueryClient({ defaultOptions: { queries: { ...QUERY_DEFAULTS(), retryDelay: 0 } } });
}

function stubFeed(status: number, code: string, message: string) {
  return vi.spyOn(clientMod.api, "GET").mockImplementation((async () => ({
    data: undefined,
    error: { error: { code, message } },
    response: new Response(null, { status }),
  })) as never);
}

function listGets(get: ReturnType<typeof stubFeed>): number {
  return get.mock.calls.filter((c) => c[0] === "/api/calls").length;
}

describe("the app's query retry policy", () => {
  it("is a predicate, not TanStack's default retry: 3", () => {
    expect(typeof QUERY_DEFAULTS()?.retry).toBe("function");
  });

  it("refuses to retry a 4xx and allows exactly ONE retry otherwise", () => {
    // `n` is query-core's `failureCount`, which is 0 on the FIRST failure and is
    // incremented only AFTER this predicate has answered (retryer.ts: `let
    // failureCount = 0` at :80, `retry(failureCount, error)` at :178-179, `+= 1`
    // at :187). So `n < 1` is ONE retry — two requests in all, which is what the
    // acceptance criterion asks for. `n < 2` would be three.
    const retry = QUERY_DEFAULTS()?.retry as (n: number, e: unknown) => boolean;
    const four = new ApiError("validation_error", "bad sort", 422);
    const five = new ApiError("internal_error", "boom", 500);
    expect(retry(0, four)).toBe(false);
    expect(retry(0, five)).toBe(true);
    expect(retry(1, five)).toBe(false);
    expect(retry(2, five)).toBe(false);
  });

  it("App.tsx hands THAT client to the provider, and builds no other", () => {
    // Closes the hole where the exported client is not the one in the tree: a
    // second `new QueryClient(` in this file would carry its own defaults.
    // `import.meta.url` is not a file: URL under vitest ("The URL must be of
    // scheme file" — measured), so this reads from the vitest root, which is
    // frontend/. The first assertion is the positive control: a wrong path would
    // make the two below vacuous.
    const src = readFileSync("src/App.tsx", "utf-8");
    expect(src).toContain("export function App()");
    expect(src).toContain("client={appQueryClient}");
    expect((src.match(/new QueryClient\(/g) ?? []).length).toBe(1);
  });

  it("a 4xx on the feed costs exactly ONE request", async () => {
    const get = stubFeed(422, "validation_error", "sort: unexpected value");
    renderWithProviders(<TriageFeed />, { route: "/", queryClient: clientFromAppDefaults() });
    // The error screen renders only once the retryer has GIVEN UP, so it is the
    // right place to count from: TanStack keeps the query pending while retries
    // are in flight.
    await screen.findByRole("alert");
    expect(listGets(get)).toBe(1);
  });

  it("a 500 on the feed is retried ONCE and no more — 2 requests, down from 4", async () => {
    // The acceptance criterion is "within 2 requests" (task 18, red bullet 3).
    // The task ALSO prescribed `n < 2`, which is three requests — the two
    // contradicted, and the review 2026-10-28 settled it in favour of the
    // outcome: 2 requests. This counts the requests, so it is the half of the
    // pair that cannot be satisfied by restating the predicate.
    const get = stubFeed(500, "internal_error", "the ranking service is down");
    renderWithProviders(<TriageFeed />, { route: "/", queryClient: clientFromAppDefaults() });
    await screen.findByRole("alert");
    expect(listGets(get)).toBe(2);
  });

  it("a 500 on the feed shows QueryError, and Retry refetches", async () => {
    const get = stubFeed(500, "internal_error", "the ranking service is down");
    renderWithProviders(<TriageFeed />, { route: "/", queryClient: clientFromAppDefaults() });
    const alert = await screen.findByRole("alert");
    await waitFor(() => expect(alert).toHaveTextContent(/couldn.t load calls/i));
    const before = listGets(get);
    await userEvent.click(screen.getByRole("button", { name: /retry/i }));
    await waitFor(() => expect(listGets(get)).toBeGreaterThan(before));
  });
});

describe("how the app's router hands React a navigation", () => {
  // The behaviour is tested in routes/TriageFeed.test.tsx, under the test
  // harness's own data router. These two close the gap between that harness and
  // the app: both must be given the SAME object, and the object must say what
  // routerProviderProps.ts explains.
  it("does not wrap navigations in a transition", () => {
    expect(ROUTER_PROVIDER_PROPS).toEqual({ useTransitions: false });
  });

  it("App.tsx and the test harness both spread THAT object into their RouterProvider, last", () => {
    // Read from the vitest root (frontend/), as the client test above does.
    //
    // A TEXT scan, with the limits of one: a `<RouterProvider` inside a comment
    // or a string counts as an element, and an aliased import is not seen at
    // all. Both make this FAIL, never pass — rewrite the scan rather than the
    // file if that is what you hit.
    for (const file of ["src/App.tsx", "src/test-utils.tsx"]) {
      const src = readFileSync(file, "utf-8");
      // Up to the first `/>`, across lines and across any `=>` in a prop.
      const providers = src.match(/<RouterProvider\b[\s\S]*?\/>/g) ?? [];
      // The positive control: a wrong path, or a file that no longer mounts a
      // RouterProvider, must not pass the two below vacuously.
      expect(providers, `${file} mounts exactly one RouterProvider`).toHaveLength(1);
      // LAST, so nothing after it can override it — on the same line or the
      // next (a formatter breaks a long element into one attribute per line).
      expect(providers[0], file).toMatch(/\{\.\.\.ROUTER_PROVIDER_PROPS\}\s*\/>$/);
      // ...and nothing before it sets the flag either: props are for the object.
      expect(providers[0], file).not.toMatch(/\buseTransitions\b/);
    }
  });
});

describe("is4xx", () => {
  it("reads the status unwrap now carries", () => {
    expect(clientMod.is4xx(new ApiError("validation_error", "bad", 422))).toBe(true);
    expect(clientMod.is4xx(new ApiError("forbidden", "no", 403))).toBe(true);
    expect(clientMod.is4xx(new ApiError("too_many_requests", "slow down", 429))).toBe(true);
    expect(clientMod.is4xx(new ApiError("internal_error", "boom", 500))).toBe(false);
    expect(clientMod.is4xx(new ApiError("network_error", "HTTP 502", 502))).toBe(false);
  });

  it("falls back to the envelope code when no status reached the error", () => {
    // api/files.ts and api/references.ts build their own ApiError off a raw
    // fetch, and a hand-made one in a test has no status either.
    expect(clientMod.is4xx(new ApiError("not_found", "gone"))).toBe(true);
    expect(clientMod.is4xx(new ApiError("conflict", "clash"))).toBe(true);
    expect(clientMod.is4xx(new ApiError("internal_error", "boom"))).toBe(false);
    expect(clientMod.is4xx(new ApiError("error", "unknown"))).toBe(false);
  });

  it("a transport failure is NOT a 4xx — those are worth retrying", () => {
    expect(clientMod.is4xx(new TypeError("Failed to fetch"))).toBe(false);
    expect(clientMod.is4xx(undefined)).toBe(false);
    expect(clientMod.is4xx(null)).toBe(false);
  });
});

describe("unwrap carries the HTTP status onto the ApiError", () => {
  it("off the envelope path", () => {
    try {
      clientMod.unwrap({
        data: undefined,
        error: { error: { code: "validation_error", message: "bad sort" } },
        response: new Response(null, { status: 422 }),
      });
      expect.unreachable("unwrap should have thrown");
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError);
      expect((e as ApiError).status).toBe(422);
    }
  });

  it("off the non-JSON path", () => {
    try {
      clientMod.unwrap({
        data: undefined,
        error: undefined,
        response: new Response("Bad Gateway", { status: 502 }),
      });
      expect.unreachable("unwrap should have thrown");
    } catch (e) {
      expect((e as ApiError).status).toBe(502);
    }
  });
});
