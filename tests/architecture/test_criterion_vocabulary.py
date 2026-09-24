"""The chip row must know every criterion AND every rating the backend can write.

`frontend/src/components/CriterionChips.tsx` carries two literal blocks: a
`CRITERIA` list (the reading order of the chip row) and a `TONE` map (rating ->
the colour and symbol the chip gets). Together they are the only place in the SPA
that enumerates this vocabulary, and nothing in the type system connects either of
them to the backend: `call_ranking_criteria.criterion` and `.rating` are bare
`String` columns with no CHECK and no enum, and the OpenAPI schema types both
`string`. So a new criterion is a two-line change in `ranking/worker.py`, and a
fifth rating is one line in `ranking/criteria.py`, and both silently ship a wrong
chip.

WHY THIS FILE EXISTS, precisely: the failure it catches already happened once, on
paper. The design spec (§4.2) enumerates SIX criteria and FOUR ratings. The source
wrote SEVEN and FIVE shapes -- `ranking/worker.py` writes an `embedding` criterion
the spec never mentions, and `ranking/criteria.py`'s `GateStatus` has a third
state, `unknown`, for every gate that could not judge. A component built to the
spec is wrong on arrival, and no test in either language would have said so.
(Since the ranking-freshness slice the worker writes SIX criteria -- no `timeline`
row, ranking spec D8 -- and they are still not the design spec's six: `embedding`
is among them.)

TWO SOURCES PER HALF, AND THE SECOND IS THE POINT.

Criteria. A `criterion=` scan of worker.py sees THREE of the six:

    criterion=name,             <- a VARIABLE, in the gate loop
    criterion="topic_fit",
    criterion="eligibility",
    criterion="exclusions",

The three gate criteria are string literals several lines above, in the
`gate_results` tuple list that loop iterates. An earlier draft of this gate
scanned only `criterion=` and would have pinned three of the then seven while
reporting success. So the scan collects the first element of every tuple in the
`gate_results` assignment AS WELL, and unions the two -- and
`test_neither_criterion_scan_alone_is_the_whole_vocabulary` below is what stops the
tuple half from being quietly dropped again.

Ratings. `worker.py` writes `gr.status.value` for the three gates, so the gate
vocabulary is `GateStatus`'s members; the three LLM criteria's ratings are whatever
`ranking/prompt.py`'s parser lets through, which is exactly its
`<name>_rating not in ("pass", "fail", ...)` validator tuples. Neither source alone
is the vocabulary: `GateStatus` has `unknown` and no `unclear`, the validators have
`unclear` and no `unknown`. (`topic_fit` is the deliberate exception -- it is an
int 1..5, rendered `n/5` by its own branch in the component, and is not a TONE key.)

WHAT WOULD TRIVIALLY PASS, and is therefore not what is asserted:

  * asserting the frontend is a SUBSET of the backend. That passes when the
    frontend list is empty. The assertion is COVERAGE -- every criterion and every
    rating the backend writes appears in the frontend.
  * a hand-maintained copy of the vocabulary in this file. Then the expected
    output is whatever the failure prints, and adding a criterion to worker.py
    changes nothing. Everything here is parsed from the real sources; the only
    literal sets are in the positive controls, whose job is to fail loudly when a
    scanner silently starts returning nothing.
  * a scanner that returns the empty set. Coverage of nothing is free. Hence the
    controls, and hence the three `..._actually_rejects_...` tests, which run the
    real rules against doctored sources and require a RED.

KNOWN BLIND SPOTS, written down rather than implied:

  * the criterion union sees a name only as a string literal in the first element
    of a `gate_results` tuple or as a `criterion="..."` keyword. Indirection --
    `crit = "bar"; criterion=crit` -- is invisible. It fails CLOSED on a renamed
    `gate_results` (`_assigned_value` raises), which is the direction that matters.
  * the rating scan reads `not in (...)` validators whose left operand is a NAME
    containing "rating" (`el_rating`, `ex_rating`). A future validator spelled
    `verdict not in ("yes", "no")` would be missed. The alternative -- every
    string tuple in prompt.py -- is red on correct code the first time someone
    writes an unrelated membership test in that file, so the narrow scan is
    deliberate and is pinned by BOTH a set control and a count control below.

A legitimate new criterion reddens FOUR exact-count controls at once. That is
deliberate: equality, not a floor. Adding a criterion is a four-file change and
this file is one of them.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FILTERS = REPO / "src" / "grant_lab" / "web" / "filters.py"
WORKER = REPO / "src" / "grant_lab" / "ranking" / "worker.py"
CRITERIA_PY = REPO / "src" / "grant_lab" / "ranking" / "criteria.py"
PROMPT = REPO / "src" / "grant_lab" / "ranking" / "prompt.py"
CHIPS = REPO / "frontend" / "src" / "components" / "CriterionChips.tsx"

# The name of the list `worker.py` iterates to write the three gate criteria.
GATE_LIST = "gate_results"
# The enum whose `.value` `worker.py` writes as a gate criterion's rating.
GATE_ENUM = "GateStatus"


# ---------------------------------------------------------------------------
# Scanners. Each takes SOURCE TEXT, not a path, so the controls below can run
# the identical code over a doctored source instead of over a paraphrase of it.
# ---------------------------------------------------------------------------


def _assigned_value(src: str, name: str) -> ast.expr:
    """The value assigned to a module-level `name`, annotated or not."""
    for node in ast.walk(ast.parse(src)):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        hit = any(isinstance(t, ast.Name) and t.id == name for t in targets)
        if hit and node.value is not None:
            return node.value
    raise AssertionError(f"no assignment to {name!r} -- it was renamed or removed")


def label_keys(src: str) -> set[str]:
    """`web/filters.py::_CRITERION_LABELS`' keys: the label vocabulary."""
    value = _assigned_value(src, "_CRITERION_LABELS")
    assert isinstance(value, ast.Dict), "_CRITERION_LABELS is no longer a dict literal"
    return {k.value for k in value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def gate_tuple_names(src: str) -> set[str]:
    """The first element of every tuple in `gate_results` -- the three gates."""
    value = _assigned_value(src, GATE_LIST)
    assert isinstance(value, ast.List), f"{GATE_LIST} is no longer a list literal"
    names: set[str] = set()
    for elt in value.elts:
        assert isinstance(elt, ast.Tuple) and elt.elts, (
            f"{GATE_LIST} holds a non-tuple: {ast.dump(elt)}"
        )
        first = elt.elts[0]
        assert isinstance(first, ast.Constant) and isinstance(first.value, str), (
            f"{GATE_LIST} tuple does not start with a criterion name: {ast.dump(elt)}"
        )
        names.add(first.value)
    return names


def criterion_kwarg_literals(src: str) -> set[str]:
    """Every `criterion="..."` keyword argument, anywhere, as a literal."""
    return {
        kw.value.value
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "criterion"
        and isinstance(kw.value, ast.Constant)
        and isinstance(kw.value.value, str)
    }


def backend_criteria(worker_src: str) -> set[str]:
    """Every criterion name `worker.py` can write into the table."""
    return gate_tuple_names(worker_src) | criterion_kwarg_literals(worker_src)


def gate_status_values(src: str) -> set[str]:
    """`ranking/criteria.py::GateStatus`' string members -- the GATE ratings.

    `worker.py` writes `rating=gr.status.value`, so this enum IS the rating
    vocabulary for budget / trl / embedding.
    """
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ClassDef) and node.name == GATE_ENUM:
            values = {
                stmt.value.value
                for stmt in node.body
                if isinstance(stmt, ast.Assign)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            }
            assert values, f"{GATE_ENUM} has no string members -- it stopped being a StrEnum"
            return values
    raise AssertionError(
        f"no `class {GATE_ENUM}` in ranking/criteria.py -- it was renamed or removed"
    )


def _rating_validator_tuples(src: str) -> list[tuple[str, ...]]:
    """Every `<something>rating not in (<strings>)` tuple in the parser."""
    out: list[tuple[str, ...]] = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        if not isinstance(node.ops[0], ast.NotIn):
            continue
        left = node.left
        if not isinstance(left, ast.Name) or "rating" not in left.id.lower():
            continue
        right = node.comparators[0]
        if not isinstance(right, ast.Tuple):
            continue
        if not all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in right.elts):
            continue
        out.append(tuple(e.value for e in right.elts))  # type: ignore[attr-defined]
    return out


def validator_ratings(src: str) -> set[str]:
    """Every rating `ranking/prompt.py`'s parser lets through for an LLM criterion."""
    return {r for tup in _rating_validator_tuples(src) for r in tup}


def backend_ratings(criteria_src: str, prompt_src: str) -> set[str]:
    """Every non-numeric rating string that can reach `call_ranking_criteria.rating`.

    `topic_fit` is excluded on purpose: it is an int 1..5 (prompt.py validates
    `0 <= rating <= 5`) and the component renders it `n/5` in its own branch,
    never through TONE.
    """
    return gate_status_values(criteria_src) | validator_ratings(prompt_src)


_TS_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_CRITERIA_BLOCK = re.compile(r"const CRITERIA\s*=\s*\[(.*?)\]\s*as const", re.S)
_TONE_BLOCK = re.compile(r"const TONE\s*:[^=]*=\s*\{(.*?)\}", re.S)


def frontend_criteria(src: str) -> list[str]:
    """The `CRITERIA` array in CriterionChips.tsx, comments stripped.

    Comments are stripped for the same reason `test_match_gates_are_wired.py`
    parses the CI YAML instead of grepping it: a commented-out `// "embedding",`
    inside the array would otherwise satisfy this gate while the running code
    knows nothing about the criterion.
    """
    m = _CRITERIA_BLOCK.search(src)
    if m is None:
        raise AssertionError(
            "no `const CRITERIA = [...] as const` in CriterionChips.tsx -- if it moved, "
            "move this gate with it; do not delete the gate"
        )
    return re.findall(r'"([^"]+)"', _TS_COMMENT.sub("", m.group(1)))


def frontend_tone_keys(src: str) -> list[str]:
    """The keys of the `TONE` map in CriterionChips.tsx, comments stripped."""
    m = _TONE_BLOCK.search(src)
    if m is None:
        raise AssertionError(
            "no `const TONE: Record<...> = {...}` in CriterionChips.tsx -- if it moved, "
            "move this gate with it; do not delete the gate"
        )
    return re.findall(r'["\']?([A-Za-z_]\w*)["\']?\s*:', _TS_COMMENT.sub("", m.group(1)))


# ---------------------------------------------------------------------------
# Positive controls. A scanner that quietly returns nothing passes every
# coverage assertion below, so each one is pinned to what it really finds.
# ---------------------------------------------------------------------------


def test_the_gate_tuple_scan_finds_the_three_gates():
    assert gate_tuple_names(WORKER.read_text()) == {"budget", "trl", "embedding"}


def test_the_criterion_kwarg_scan_sees_only_three_of_six():
    """The measurement this whole file is built on. `criterion=name` in the gate
    loop is a VARIABLE, so a `criterion=` scan -- the obvious one -- misses the
    three gates of the six."""
    assert criterion_kwarg_literals(WORKER.read_text()) == {
        "topic_fit",
        "eligibility",
        "exclusions",
    }


def test_neither_criterion_scan_alone_is_the_whole_vocabulary():
    """Deleting either half of the union must not be survivable. If the two sets
    ever overlap or one grows to cover the other, this file's central claim has
    changed and the docstring above is stale."""
    src = WORKER.read_text()
    tuples, kwargs = gate_tuple_names(src), criterion_kwarg_literals(src)
    assert tuples.isdisjoint(kwargs), tuples & kwargs
    assert len(tuples | kwargs) == len(tuples) + len(kwargs) == 6


def test_the_label_map_scan_finds_the_seven_labels():
    """Seven, one more than the worker writes: `timeline` keeps its label as the
    reason code of an expired call's aggregate_reason (ranking spec D8)."""
    assert len(label_keys(FILTERS.read_text())) == 7


def test_the_frontend_criteria_scan_finds_a_real_list():
    found = frontend_criteria(CHIPS.read_text())
    assert len(found) == len(set(found)) == 6, found
    assert "topic_fit" in found


def test_the_frontend_criteria_scan_ignores_a_commented_out_entry():
    doctored = (
        'export const CRITERIA = [\n  "topic_fit",\n'
        '  // "embedding",\n  /* "trl", */\n] as const;\n'
    )
    assert frontend_criteria(doctored) == ["topic_fit"]


def test_the_frontend_criteria_scan_refuses_a_renamed_list():
    with pytest.raises(AssertionError, match="do not delete the gate"):
        frontend_criteria('export const CHIP_ORDER = ["topic_fit"] as const;\n')


def test_the_gate_status_scan_finds_the_three_gate_ratings():
    assert gate_status_values(CRITERIA_PY.read_text()) == {"pass", "fail", "unknown"}


def test_the_validator_scan_finds_the_three_llm_ratings():
    src = PROMPT.read_text()
    assert validator_ratings(src) == {"pass", "fail", "unclear"}
    # A COUNT control as well as a set control: `eligibility` and `exclusions`
    # have one validator each, and their union hides the loss of either
    # (`("pass", "fail")` is a subset of `("pass", "fail", "unclear")`).
    assert len(_rating_validator_tuples(src)) == 2, _rating_validator_tuples(src)


def test_neither_rating_scan_alone_is_the_whole_vocabulary():
    """`unknown` is the gates' and only the gates'; `unclear` is the LLM's and
    only the LLM's. A component pinned to either source alone is wrong."""
    gates = gate_status_values(CRITERIA_PY.read_text())
    llm = validator_ratings(PROMPT.read_text())
    assert "unknown" in gates - llm
    assert "unclear" in llm - gates


def test_the_frontend_tone_scan_finds_a_real_map():
    found = frontend_tone_keys(CHIPS.read_text())
    assert len(found) == len(set(found)) == 4, found
    assert "unknown" in found


def test_the_frontend_tone_scan_ignores_a_commented_out_entry():
    doctored = (
        'const TONE: Record<string, Tone> = {\n  pass: "pass",\n  // unknown: "unsure",\n};\n'
    )
    assert frontend_tone_keys(doctored) == ["pass"]


def test_the_frontend_tone_scan_refuses_a_renamed_map():
    with pytest.raises(AssertionError, match="do not delete the gate"):
        frontend_tone_keys('const RATING_TONE: Record<string, Tone> = { pass: "pass" };\n')


# ---------------------------------------------------------------------------
# The gates.
# ---------------------------------------------------------------------------


def test_the_chip_row_covers_every_criterion_the_worker_writes():
    """COVERAGE, not subset: every criterion the backend can write must be in the
    frontend list. A new criterion added to `gate_results` the way the
    existing three were added goes red here."""
    backend = backend_criteria(WORKER.read_text())
    missing = backend - set(frontend_criteria(CHIPS.read_text()))
    assert missing == set(), (
        f"ranking/worker.py writes {sorted(missing)}, which CriterionChips.tsx's CRITERIA "
        "list does not know -- the chip renders, but at the end of the row in arbitrary order"
    )


def test_the_label_map_covers_every_criterion_the_worker_writes():
    """The other silent degradation: `criterion_label()` Title-cases an unknown
    key, so a missing label is a chip reading 'Co Funding' that nobody chose."""
    backend = backend_criteria(WORKER.read_text())
    missing = backend - label_keys(FILTERS.read_text())
    assert missing == set(), f"web/filters.py::_CRITERION_LABELS has no label for {sorted(missing)}"


def test_the_chip_row_invents_no_criterion_of_its_own():
    """The reverse direction, and it is not symmetry for its own sake: an entry
    the backend never writes is a permanent slot in the sort order that no data
    ever fills, and it makes the coverage assertion above pass for free."""
    extra = set(frontend_criteria(CHIPS.read_text())) - label_keys(FILTERS.read_text())
    assert extra == set(), (
        f"CriterionChips.tsx names {sorted(extra)}, which the backend never writes"
    )


def test_the_tone_map_covers_every_rating_the_backend_can_write():
    """The half revision 2 promised and never specified (F-5). A rating TONE does
    not know does not blank the chip -- it falls through to the `unsure` tone and
    prints the raw word where a symbol belongs, which reads as a different claim
    than the one the backend made."""
    backend = backend_ratings(CRITERIA_PY.read_text(), PROMPT.read_text())
    missing = backend - set(frontend_tone_keys(CHIPS.read_text()))
    assert missing == set(), (
        f"the backend can write rating {sorted(missing)}, which CriterionChips.tsx's TONE map "
        "does not know -- the chip falls through to the 'unsure' tone and prints the bare word "
        "where a symbol belongs"
    )


# ---------------------------------------------------------------------------
# Proof by breaking. The controls above prove the scanners see something; these
# prove the RULE rejects the thing it exists to reject -- without waiting for a
# real regression to demonstrate it.
# ---------------------------------------------------------------------------


def test_the_coverage_rule_actually_rejects_a_gap():
    backend = backend_criteria(WORKER.read_text())
    doctored = [c for c in frontend_criteria(CHIPS.read_text()) if c != "embedding"]
    assert backend - set(doctored) == {"embedding"}


def test_the_coverage_rule_sees_a_criterion_added_THE_WAY_THE_GATES_ARE():
    """B18b in the run record: the mutation that a `criterion=`-only scan
    survives. Doctored source, so this control holds even when nobody is
    mutating the tree."""
    src = WORKER.read_text().replace(
        '("embedding", r.gates.embedding),',
        '("embedding", r.gates.embedding),\n        ("co_funding", r.gates.co_funding),',
        1,
    )
    assert "co_funding" in backend_criteria(src)
    assert "co_funding" not in criterion_kwarg_literals(src), "the kwarg scan alone would MISS it"
    assert "co_funding" not in set(frontend_criteria(CHIPS.read_text()))


def test_the_tone_rule_actually_rejects_a_new_gate_status():
    """B20d in the run record, as a doctored source: a fifth `GateStatus` member
    is one line, and every gate row would start carrying it the same day."""
    src = CRITERIA_PY.read_text().replace(
        '    UNKNOWN = "unknown"', '    UNKNOWN = "unknown"\n    PARTIAL = "partial"', 1
    )
    assert "partial" in gate_status_values(src), (
        "the doctored source did not take -- the anchor moved"
    )
    backend = gate_status_values(src) | validator_ratings(PROMPT.read_text())
    assert backend - set(frontend_tone_keys(CHIPS.read_text())) == {"partial"}
