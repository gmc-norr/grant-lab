import { Alert, Anchor } from "@mantine/core";
import { Link } from "react-router-dom";

import { useProfile } from "../api/profiles";

/** The three eligibility facts the RANKER actually reads, normalised.
 *
 *  The API surface is flat strings (`ProfileUpdateIn.org_type`, `.country`,
 *  `.applicant_types`) and the editor's form state matches it, but
 *  `parse_structured` (profiles/fields.py) files them into namespaces, and the
 *  prompt reads them from there: `_eligibility_block` (ranking/prompt.py) prints
 *  `structured["org"]["org_type"]`, `structured["geo"]["country"]` and
 *  `structured["eligibility"]["applicant_types"]`. Same three leaves, two
 *  spellings of the path -- so a warning keyed on these names is keyed on what
 *  decides the cap. */
export type EligibilityFacts = {
  /** Already split out of the form's newline-joined textarea / MultiSelect. */
  applicantTypes: readonly string[];
  orgType: string | null | undefined;
  country: string | null | undefined;
};

const blank = (value: string | null | undefined) => (value ?? "").trim() === "";

/** True when none of the three facts carries a value.
 *
 *  Why these decide whether anything ever reaches the board: `aggregate`
 *  (ranking/aggregate.py) does `if eligibility == "unclear": base = min(base, 50)`,
 *  and the model answers "unclear" for every call while the PROFILE block has
 *  nothing but "(none specified)" to go on. The dashboard's "top matches" is
 *  >=70, the digest's top bucket >=60 and a corridor board's `min_score` 60, so
 *  an unfilled profile empties all three at once -- silently, until this alert.
 *
 *  The cap rule itself is NOT changed (recorded human decision): the owner fills
 *  the facts in, and this is what tells them to. */
export function eligibilityFactsAreEmpty({
  applicantTypes,
  orgType,
  country,
}: EligibilityFacts): boolean {
  return applicantTypes.every(blank) && blank(orgType) && blank(country);
}

const WARNING =
  "Eligibility is not filled in. Calls can't be judged eligible, so no call can score above 50 and none will reach 'top matches' or the board. Fill in organisation type, country and applicant types.";

/** The warning itself: nothing at all unless all three facts are empty.
 *
 *  Rendered by both the editor (ProfileEdit, off live form state so it clears as
 *  soon as a fact is typed) and the detail page (ProfileDetail, off the server
 *  payload). It must stay OUTSIDE `ProfileOptions`, which collapses its children
 *  into a `<details>` in the match edition -- a warning folded into a closed
 *  disclosure is not a warning. */
export function EligibilityWarning({
  editHref,
  ownerOnly = false,
  ...facts
}: EligibilityFacts & {
  /** Where to fix it, when the reader may. Pages that ARE the editor omit it. */
  editHref?: string;
  /** The reader cannot edit this profile: say who can, not "fill in". */
  ownerOnly?: boolean;
}) {
  if (!eligibilityFactsAreEmpty(facts)) return null;
  return (
    <Alert role="alert" color="yellow" mb="md" data-field="eligibility-empty">
      {WARNING}
      {editHref && (
        <>
          {" "}
          <Anchor component={Link} to={editHref} data-field="eligibility-fix">
            Fill them in →
          </Anchor>
        </>
      )}
      {ownerOnly && " Only the profile's owner can change this."}
    </Alert>
  );
}

/** The same warning for pages that know only a profile ID -- the calls feed and
 *  a board. Without it the cap was explained on the profile page alone, while
 *  the places it bites showed "50 / 100" on every top card and an empty board
 *  (UX pass 2026-10-31). Same rule, same facts, read off the same endpoint the
 *  profile page uses, so the three pages cannot disagree about it. */
export function ProfileEligibilityWarning({ profileId }: { profileId: number | null }) {
  const { data } = useProfile(profileId ?? 0, { enabled: profileId != null });
  if (profileId == null || !data) return null;
  return (
    <EligibilityWarning
      applicantTypes={data.applicant_types}
      orgType={data.org_type}
      country={data.country}
      editHref={data.can_edit ? `/profiles/${data.id}/edit` : undefined}
      ownerOnly={!data.can_edit}
    />
  );
}
