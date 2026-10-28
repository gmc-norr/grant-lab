import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Outlet, RouterProvider, createMemoryRouter } from "react-router-dom";

import { CHUNK_RELOAD_FLAG, RouteError } from "./RouteError";

// THE THREE PATHS OF THE AUTOMATIC RELOAD, and the third is the reason the
// sessionStorage access is wrapped at all: in a privacy mode that refuses
// storage, `getItem` THROWS, and a reload that cannot read its own guard flag is
// a reload that can run forever. So a throwing store must reload ZERO times and
// hand the member the button instead.
//
// `reload` is COUNTED, never asserted truthy: "reloaded once" and "reloaded on
// every render" both make a `toHaveBeenCalled()` green, and the second is the
// defect this guard exists to prevent.

const CHUNK_MESSAGE =
  "Failed to fetch dynamically imported module: https://app.invalid/assets/AdminUsers-9f3a1c.js";

function stubStorage(store: Map<string, string>, mode: "ok" | "throws" = "ok") {
  const fake = {
    getItem: (k: string) => {
      if (mode === "throws") throw new Error("The operation is insecure.");
      return store.has(k) ? (store.get(k) as string) : null;
    },
    setItem: (k: string, v: string) => {
      if (mode === "throws") throw new Error("The operation is insecure.");
      store.set(k, v);
    },
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
    key: () => null,
    length: 0,
  };
  vi.stubGlobal("sessionStorage", fake as unknown as Storage);
}

function Boom({ error }: { error: unknown }): never {
  throw error;
}

function renderThrowing(error: unknown) {
  // The REAL react-router error path: a route element that throws, with
  // RouteError as the parent route's errorElement. Rendering <RouteError/>
  // directly would need a mocked useRouteError and would prove nothing about
  // the error actually reaching it.
  const router = createMemoryRouter(
    [
      {
        // The Outlet is load-bearing: a layout element that does not render
        // one never renders the child at all, so nothing can throw and the test
        // passes over the whole mechanism.
        element: (
          <div>
            shell
            <Outlet />
          </div>
        ),
        errorElement: <RouteError />,
        children: [{ index: true, element: <Boom error={error} /> }],
      },
    ],
    { initialEntries: ["/"] },
  );
  return render(<RouterProvider router={router} />);
}

const realLocation = window.location;

describe("RouteError", () => {
  let reload: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    reload = vi.fn();
    // jsdom refuses a plain assignment to window.location, so redefine it —
    // the same pattern SimpleShell.test.tsx uses for `assign`.
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...realLocation, reload },
    });
    // react-router logs the caught error; the three expected ones are noise.
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    Object.defineProperty(window, "location", { configurable: true, value: realLocation });
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("reloads ONCE on the first chunk-load error and marks the session flag", () => {
    const store = new Map<string, string>();
    stubStorage(store);
    renderThrowing(new Error(CHUNK_MESSAGE));
    expect(reload).toHaveBeenCalledTimes(1);
    expect(store.get(CHUNK_RELOAD_FLAG)).toBe("1");
  });

  it("does NOT reload a second time — the flag already stands", () => {
    const store = new Map<string, string>([[CHUNK_RELOAD_FLAG, "1"]]);
    stubStorage(store);
    renderThrowing(new Error(CHUNK_MESSAGE));
    expect(reload).toHaveBeenCalledTimes(0);
    expect(screen.getByRole("button", { name: /reload/i })).toBeInTheDocument();
  });

  it("a sessionStorage that THROWS renders the Reload button and reloads zero times", () => {
    stubStorage(new Map(), "throws");
    renderThrowing(new Error(CHUNK_MESSAGE));
    expect(reload).toHaveBeenCalledTimes(0);
    expect(screen.getByRole("button", { name: /reload/i })).toBeInTheDocument();
  });

  it("does not auto-reload a plain render error, and still offers Reload", () => {
    const store = new Map<string, string>();
    stubStorage(store);
    renderThrowing(new Error("Cannot read properties of undefined (reading 'map')"));
    expect(reload).toHaveBeenCalledTimes(0);
    expect(store.has(CHUNK_RELOAD_FLAG)).toBe(false);
    expect(screen.getByRole("button", { name: /reload/i })).toBeInTheDocument();
  });

  it("the Reload button reloads when pressed", async () => {
    stubStorage(new Map(), "throws");
    renderThrowing(new Error(CHUNK_MESSAGE));
    expect(reload).toHaveBeenCalledTimes(0);
    await userEvent.click(screen.getByRole("button", { name: /reload/i }));
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("renders one main landmark, an alert and a way back out", () => {
    stubStorage(new Map(), "throws");
    renderThrowing(new Error(CHUNK_MESSAGE));
    // The landmark is what the e2e walk's proof-of-life counts, and the review's
    // complaint about react-router's screen was "mainNav=0": no way out.
    expect(document.querySelectorAll("main")).toHaveLength(1);
    expect(screen.getByRole("alert")).toHaveTextContent(/went wrong|couldn.t load/i);
    expect(screen.getByRole("link", { name: /calls|home/i })).toHaveAttribute("href", "/");
  });
});
