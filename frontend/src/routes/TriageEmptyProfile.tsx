// frontend/src/routes/TriageEmptyProfile.tsx
import { Link } from "react-router-dom";

import styles from "./TriageFeed.module.css";

// THE FIRST SCREEN of the match edition for a user with no profile resolved.
// Release 1's answer to "no profile" is an invitation, not the archive: the
// server's unranked fallback still returns every open call in that state
// (dashboard.py is deliberately untouched, and the full edition keeps it), so
// rendering the list here would be fifty calls ranked against nothing — the
// firehose release 1 exists to replace.
//
// NOT inbox zero. Inbox zero (TriageFeed's EmptyView) says *you are done*; this
// says *you have not started*. Shipping one for the other is the likely bug, so
// the two copies share no string and TriageFeed.test.tsx asserts each one's
// headline is ABSENT in the other's state — cross-absence, because `A !== B`
// passes for a component that renders neither.
//
// It renders its own <main>: SimpleShell wraps the outlet in a plain div
// precisely so each release-1 page owns its landmark, and this screen REPLACES
// the feed rather than sitting inside it.
//
// NO CSS MODULE OF ITS OWN — it borrows TriageFeed's. The match bundle's module
// count is a pinned gate (scripts/check_match_bundle.mjs), and a second stylesheet
// for one screen would move it for nothing.

type ProfileOption = { id: number; name: string };

export function TriageEmptyProfile({ profiles }: { profiles: ProfileOption[] }) {
  const hasProfiles = profiles.length > 0;
  return (
    <main className={styles.onboarding} data-screen="no-profile">
      <h1 className={styles.onboardingHeadline}>
        {hasProfiles ? "Pick a research profile." : "No research profile yet."}
      </h1>
      <p className={styles.onboardingBody} data-field="why">
        A research profile describes what your group works on, and every call is matched against
        it.{" "}
        {hasProfiles
          ? "Choose the one you are triaging for."
          : "Nothing is ranked for you until there is one."}
      </p>

      {hasProfiles && (
        // The user HAS profiles and simply has none selected. Sending them to
        // /profiles/new here would ask them to create a duplicate of a profile
        // they already own.
        <ul className={styles.picker} data-field="profile-picker" aria-label="Profiles">
          {profiles.map((p) => (
            <li key={p.id}>
              {/* The feed reads ?profile= from the URL (dashboardParamsFrom), so
                  the pick is a plain link: back and reload both work, and the
                  choice is NOT persisted as the user's default — a deep link
                  must not silently rewrite stored state (recorded decision). */}
              <Link
                className={`${styles.pickerLink} ${styles.onboardingPickerLink}`}
                data-profile-pick={p.id}
                to={`/?profile=${p.id}`}
              >
                {p.name}
              </Link>
            </li>
          ))}
        </ul>
      )}

      <p className={styles.onboardingBody}>
        <Link className={styles.createLink} to="/profiles/new">
          {hasProfiles ? "Create another profile" : "Create a research profile"}
        </Link>
      </p>
    </main>
  );
}
