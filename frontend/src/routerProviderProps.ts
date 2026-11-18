// NAVIGATIONS HERE ARE NOT WRAPPED IN A REACT TRANSITION.
//
// Left to its default, react-router lands a navigation and then hands React the
// new location inside React.startTransition, which renders in a later task.
// Until it has, everything on screen — the controls' handlers, the links' hrefs
// — still belongs to the address from before, and whatever the reader does next
// is built on that old address. Seen on 2026-11-18 as a flaky match-e2e: a
// search, then a change of order 1 ms later, and the address went from
// `/?profile=1&q=genomics` to `/?profile=1&sort=deadline`. The order control had
// copied search params that did not have the search in them yet.
//
// With `useTransitions: false` the router sets its state directly. A navigation
// started by a click or a change is then an ordinary update made inside that
// event. React renders those in a microtask after the handler, which is before
// the next USER event — each one is its own task — for a filter change and for
// a followed <Link> alike. (Not covered, and not needed so far: a navigation
// started outside an event, such as one made after an awaited request.)
//
// This is what the app's RouterProvider gets (App.tsx, pinned in App.test.tsx)
// and what the test harness's data router gets (test-utils.tsx), from this one
// object, so the tests cannot drift from the app. Two other remedies were
// built and measured in a throttled browser before this one; the numbers are
// with the tests: "the page is current before the next event can arrive",
// routes/TriageFeed.test.tsx.
//
// TWO THINGS THIS COSTS OR ASSUMES:
// - No route may have a `loader`, an `action`, a route-level `lazy` or
//   middleware, and the router may not be created with `dataStrategy`,
//   `getContext` or `patchRoutesOnNavigation`. With any of them the router
//   finishes the navigation after an await — outside the event — and the page is
//   behind again until a later task. router.test.tsx fails if one appears;
//   measure again before deleting that test.
// - Between two admin pages that are both lazy, the <Suspense> fallback now shows
//   at once, where a transition kept the page already on screen up until the
//   chunk arrived. Entering the admin area from elsewhere shows the fallback
//   either way, because its boundary is new. (Probed in jsdom during review, not
//   in a browser.)
export const ROUTER_PROVIDER_PROPS = { useTransitions: false } as const;
