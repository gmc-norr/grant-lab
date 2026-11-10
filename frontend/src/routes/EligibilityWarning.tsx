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
 *  the model reads. */
export type EligibilityFacts = {
  /** Already split out of the form's newline-joined textarea / MultiSelect. */
  applicantTypes: readonly string[];
  orgType: string | null | undefined;
  country: string | null | undefined;
};

const blank = (value: string | null | undefined) => (value ?? "").trim() === "";

/** True when none of the three facts carries a value.
 *
 *  With nothing but "(none specified)" in the PROFILE block the model answers
 *  eligibility "unclear" for every call. Until 2026-11-10 `aggregate`
 *  (ranking/aggregate.py) capped such a call at 50, which emptied "top matches",
 *  the digest's top bucket and the boards; that cap is gone (spec
 *  2026-11-10-score-rule-topic-only-design.md), and eligibility no longer moves
 *  the score. What an unfilled profile still costs is the eligibility chip:
 *  every call shows "?" instead of a judgement. */
export function eligibilityFactsAreEmpty({
  applicantTypes,
  orgType,
  country,
}: EligibilityFacts): boolean {
  return applicantTypes.every(blank) && blank(orgType) && blank(country);
}

const WARNING =
  "Eligibility is not filled in, so calls can't be checked for eligibility. Fill in organisation type, country and applicant types.";

/** The warning itself: nothing at all unless all three facts are empty.
 *
 *  Rendered by both the editor (ProfileEdit, off live form state so it clears as
 *  soon as a fact is typed) and the detail page (ProfileDetail, off the server
 *  payload). It must stay OUTSIDE `ProfileOptions`, which collapses its children
 *  into a `<details>` -- a warning folded into a closed
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
 *  a board. Without it the missing facts were explained on the profile page alone
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
