// frontend/src/routes/TriageEmptyProfile.tsx
import { Link, useNavigate } from "react-router-dom";

import { useSetDefaultProfile } from "../api/dashboard";

import styles from "./TriageFeed.module.css";

// THE FIRST SCREEN for a user with no profile resolved.
// Release 1's answer to "no profile" is an invitation, not the archive: the
// server's unranked fallback still returns every open call in that state
// (dashboard.py is deliberately untouched), so
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
// NO CSS MODULE OF ITS OWN — it borrows TriageFeed's.

type ProfileOption = { id: number; name: string };

export function TriageEmptyProfile({ profiles }: { profiles: ProfileOption[] }) {
  const hasProfiles = profiles.length > 0;
  const navigate = useNavigate();
  const setDefault = useSetDefaultProfile();
  // A pick HERE is the user answering "which profile do you triage for?", so it
  // is stored as their default; without that, everyone who did not create the
  // profile they use landed on this screen on every visit (UX pass 2026-10-31).
  // The recorded rule is about DEEP LINKS -- `?profile=` in a URL must never
  // rewrite stored state -- and it still holds: the href below is unchanged, so
  // opening a pick in a new tab, or following any ?profile= link, writes nothing.
  // A failed write still navigates: the pick works for this visit either way.
  const pick = async (id: number) => {
    try {
      await setDefault.mutateAsync(id);
    } catch {
      // Not worth blocking the feed over; the next visit simply asks again.
    }
    navigate(`/?profile=${id}`);
  };
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
              {/* Still a real link to `/?profile=`, so a new tab or a copied
                  address works and writes nothing. A plain click is the user's
                  explicit choice and is saved as their default first (above). */}
              <Link
                className={`${styles.pickerLink} ${styles.onboardingPickerLink}`}
                data-profile-pick={p.id}
                to={`/?profile=${p.id}`}
                onClick={(event) => {
                  if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
                  event.preventDefault();
                  void pick(p.id);
                }}
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
