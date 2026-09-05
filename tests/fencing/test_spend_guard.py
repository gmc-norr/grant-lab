import textwrap

import pytest

from tests.fencing.spend_guard import (
    PINNED,
    llm_spend_writers,
    offenders,
    stale_pins,
)

# Every fixture that CALLS record_spend must also IMPORT it, exactly as real code
# does. The guard resolves writer names through imports (a bare name match let a
# module define its own `def record_spend` and bless every stamp in it), so a
# fixture without this header is not a faithful stand-in for the code under gate.
_WRITER_IMPORT = "from grant_lab.proposal_assist.spend import record_spend\n"


def _tree(tmp_path, monkeypatch, name, source, writer_import=True):
    import tests.fencing.spend_guard as g

    fake = tmp_path / "grant_lab" / name
    fake.parent.mkdir(parents=True, exist_ok=True)
    body = textwrap.dedent(source)
    if writer_import and "record_spend" in body and "import record_spend" not in body:
        body = _WRITER_IMPORT + body
    fake.write_text(body)
    monkeypatch.setattr(g, "SRC", tmp_path)
    monkeypatch.setattr(g, "PINNED", {})
    return g


def test_no_function_stamps_tokens_without_recording_spend():
    assert offenders() == []


def test_no_pin_is_stale():
    # Same discipline as sink_guard: the list can only shrink.
    assert stale_pins() == []


def test_every_pin_carries_a_reason():
    assert all(reason.strip() for reason in PINNED.values())


def test_stale_pin_detection_can_actually_fail(monkeypatch):
    # The real PINNED entries all genuinely stamp, so the two tests above cannot
    # go red on their own. This is the one that proves stale_pins() works, by
    # pinning a function that stamps nothing. `sum_spend_today` READS the ledger
    # and writes no token field — pinning it is exactly the stale state the
    # shrink-only rule exists to catch. (It previously pinned `record_spend`,
    # which stopped working the moment the guard learned to see ORM constructors:
    # record_spend builds the LlmSpend row, so it stamps.)
    import tests.fencing.spend_guard as g

    monkeypatch.setattr(
        g, "PINNED", {("grant_lab/proposal_assist/spend.py", "sum_spend_today"): "x"}
    )
    assert g.stale_pins() == ["grant_lab/proposal_assist/spend.py::sum_spend_today"]


def test_the_guard_can_actually_fail(tmp_path, monkeypatch):
    # A gate nobody has seen fail is not a gate. Point it at a function that
    # commits the exact defect and confirm it is reported.
    g = _tree(
        tmp_path,
        monkeypatch,
        "bad.py",
        """
        def f(job, done):
            job.input_tokens = done.tokens_in or 0
        """,
    )
    assert g.offenders() == ["grant_lab/bad.py::f"]


def test_the_guard_accepts_a_function_that_records(tmp_path, monkeypatch):
    # And the other direction, so it is not simply reporting everything.
    g = _tree(
        tmp_path,
        monkeypatch,
        "good.py",
        """
        def f(s, job, done):
            job.input_tokens = done.tokens_in or 0
            record_spend(s, workspace_id=job.workspace_id, kind='evaluator', status='ok')
        """,
    )
    assert g.offenders() == []


@pytest.mark.parametrize(
    ("label", "body"),
    [
        (
            "tuple_unpack",
            "(job.input_tokens, job.output_tokens) = (done.tokens_in, done.tokens_out)",
        ),
        ("annotated", "job.input_tokens: int = done.tokens_in"),
        ("augmented", "job.input_tokens += done.tokens_in"),
        ("chained", "job.input_tokens = other.input_tokens = done.tokens_in"),
    ],
)
def test_every_assignment_form_that_stamps_is_reported(tmp_path, monkeypatch, label, body):
    """An outside review found three of these live in the shipped guard.

    `_stamp_lines` matched only a bare `ast.Attribute` target, so tuple-unpack,
    annotated and augmented assignment were all invisible: a function could write
    `job.input_tokens += done.tokens_in`, record nothing, and the gate stayed
    green. Measured against the real guard before the fix, not theorised. `+=` is
    the one that matters — accumulating a token total is a natural way to write
    exactly the code this gate exists to catch.
    """
    g = _tree(
        tmp_path,
        monkeypatch,
        f"{label}.py",
        f"""
        def f(job, other, done):
            {body}
        """,
    )
    assert g.offenders() == [f"grant_lab/{label}.py::f"]


def test_a_widened_form_still_passes_when_it_records(tmp_path, monkeypatch):
    # The other direction: widening the target walk must not report correct code.
    g = _tree(
        tmp_path,
        monkeypatch,
        "good_aug.py",
        """
        def f(s, job, done):
            job.input_tokens += done.tokens_in
            record_spend(s, workspace_id=job.workspace_id, kind='evaluator', status='ok')
        """,
    )
    assert g.offenders() == []


def test_a_sibling_that_records_does_not_bless_this_function(tmp_path, monkeypatch):
    # THE bypass a module-level guard has. This is the real shape of
    # evaluator/pass_runner.py after Task 4: a success path that records and a
    # _fail that must record too.
    g = _tree(
        tmp_path,
        monkeypatch,
        "two.py",
        """
        def _fail(session, job, done):
            job.input_tokens = done.tokens_in or 0

        def success(session, job, done):
            job.input_tokens = done.tokens_in or 0
            record_spend(session, workspace_id=job.workspace_id, kind='evaluator', status='ok')
        """,
    )
    assert g.offenders() == ["grant_lab/two.py::_fail"]


def test_an_unrelated_record_usage_does_not_bless_a_stamping_function(tmp_path, monkeypatch):
    # record_usage is the CITATION ledger's writer (citations/usage.py), a
    # different ledger from the one this plan creates. Name-matching it must not
    # excuse a function that stamps and never records.
    g = _tree(
        tmp_path,
        monkeypatch,
        "unrelated.py",
        """
        def stamps(job, done):
            job.output_tokens = done.tokens_out or 0

        def telemetry(session):
            record_usage(session)
        """,
    )
    assert g.offenders() == ["grant_lab/unrelated.py::stamps"]


def test_an_aliased_import_writer_is_not_reported(tmp_path, monkeypatch):
    # Correct code, aliased. Without _spend_writer_names this is a false
    # positive — reproduced before the helper was added.
    g = _tree(
        tmp_path,
        monkeypatch,
        "aliased.py",
        """
        from grant_lab.proposal_assist.spend import record_spend as rs

        def f(session, job, done):
            job.input_tokens = done.tokens_in or 0
            rs(session, workspace_id=job.workspace_id, kind='evaluator', status='ok')
        """,
    )
    assert g.offenders() == []


def test_a_dead_record_spend_still_satisfies_the_guard_KNOWN_LIMIT(tmp_path, monkeypatch):
    # Measured, not aspirational: an AST scan cannot do reachability, so a
    # record_spend under `if False:` passes. What catches that is the RUNTIME
    # tests in Tasks 3-6, which drive the real path and assert a row exists.
    # Written down so nobody mistakes this gate for more than it is. If someone
    # later teaches the guard reachability, this test goes red — update it then.
    g = _tree(
        tmp_path,
        monkeypatch,
        "dead.py",
        """
        def never_records(session, job, done):
            job.input_tokens = done.tokens_in or 0
            if False:
                record_spend(session)
        """,
    )
    assert g.offenders() == []


def test_a_locally_defined_record_spend_does_not_bless_the_module(tmp_path, monkeypatch):
    """Round 2 of the outside review found this against the shipped guard.

    The writer set was seeded with the bare strings, so ANY call named
    record_spend counted — including one the module defined itself. A module
    could shadow the name with a no-op and every token stamp in it went green.
    """
    g = _tree(
        tmp_path,
        monkeypatch,
        "shadow.py",
        """
        def record_spend(*a, **k):
            pass

        def f(s, job, done):
            job.input_tokens = done.tokens_in
            record_spend(s)
        """,
        writer_import=False,
    )
    assert g.offenders() == ["grant_lab/shadow.py::f"]


def test_an_unrelated_attribute_call_does_not_bless_the_module(tmp_path, monkeypatch):
    # Same family: `whatever.record_spend(...)` matched on the attribute name
    # alone. Only an alias bound by an import from a writer module counts now.
    g = _tree(
        tmp_path,
        monkeypatch,
        "attr.py",
        """
        import unrelated

        def f(s, job, done):
            job.input_tokens = done.tokens_in
            unrelated.record_spend(s)
        """,
        writer_import=False,
    )
    assert g.offenders() == ["grant_lab/attr.py::f"]


@pytest.mark.parametrize(
    ("label", "source"),
    [
        (
            "module_alias",
            """
            from grant_lab.proposal_assist import spend

            def f(s, job, done):
                job.input_tokens = done.tokens_in
                spend.record_spend(s)
            """,
        ),
        (
            # Inside the citations package on purpose: record_usage writes the
            # CITATION ledger and counts as a writer only there. Outside it, a
            # function could stamp an evaluator job while recording unrelated
            # citation spend — a bypass an outside review found and the next
            # test pins.
            "proposal_assist/citations/usage_writer",
            """
            from grant_lab.proposal_assist.citations.usage import record_usage

            def f(s, job, done):
                job.tokens_in = done.tokens_in
                record_usage(s)
            """,
        ),
    ],
)
def test_real_import_shapes_are_still_accepted(tmp_path, monkeypatch, label, source):
    # The other direction: tightening resolution must not report correct code.
    g = _tree(tmp_path, monkeypatch, f"{label}.py", source, writer_import=False)
    assert g.offenders() == []


@pytest.mark.parametrize(
    ("label", "source"),
    [
        (
            "qualified",
            """
            from grant_lab.db import models

            def mk(**k):
                return models.LlmSpend(**k)
            """,
        ),
        (
            "aliased",
            """
            from grant_lab.db.models import LlmSpend as SpendRow

            def mk(**k):
                return SpendRow(**k)
            """,
        ),
        (
            "plain",
            """
            from grant_lab.db.models import LlmSpend

            def mk(**k):
                return LlmSpend(**k)
            """,
        ),
        (
            "core_insert",
            """
            from sqlalchemy import insert

            from grant_lab.db.models import LlmSpend

            def mk(s, **k):
                return s.execute(insert(LlmSpend).values(**k))
            """,
        ),
        (
            "bulk_mappings",
            """
            from grant_lab.db.models import LlmSpend

            def mk(s, rows):
                return s.bulk_insert_mappings(LlmSpend, rows)
            """,
        ),
    ],
)
def test_every_write_shape_is_seen(tmp_path, monkeypatch, label, source):
    """The "exactly one writer" claim only holds if the check can SEE a second one.

    It matched ast.Name("LlmSpend") only, so `models.LlmSpend(...)` and
    `import LlmSpend as SpendRow` were both invisible — a second writer could
    have existed and this assertion would still have passed.
    """
    g = _tree(tmp_path, monkeypatch, f"{label}.py", source, writer_import=False)
    assert g.llm_spend_writers() == [f"grant_lab/{label}.py"]


def test_record_usage_does_not_bless_a_non_citation_module(tmp_path, monkeypatch):
    """record_usage writes the CITATION ledger, not this one.

    Found by round 3 of the outside review: the writer set was the same
    everywhere, so a function could stamp `job.input_tokens` on an evaluator job
    and satisfy the gate with an unrelated `record_usage(...)` call for citation
    spend. It now counts only for modules under proposal_assist/citations/.
    """
    g = _tree(
        tmp_path,
        monkeypatch,
        "cross_writer.py",
        """
        from grant_lab.proposal_assist.citations.usage import record_usage

        def f(s, job, done):
            job.input_tokens = done.tokens_in
            record_usage(s, op="metadata")
        """,
        writer_import=False,
    )
    assert g.offenders() == ["grant_lab/cross_writer.py::f"]


def test_a_fully_qualified_writer_import_is_accepted(tmp_path, monkeypatch):
    # The false positive in the same round: `import a.b.c` binds `a`, and the
    # real call is the whole dotted chain. Legitimate code, must not be reported.
    g = _tree(
        tmp_path,
        monkeypatch,
        "fq.py",
        """
        import grant_lab.proposal_assist.spend

        def f(s, job, done):
            job.input_tokens = done.tokens_in
            grant_lab.proposal_assist.spend.record_spend(s)
        """,
        writer_import=False,
    )
    assert g.offenders() == []


def test_unpacked_values_kwargs_are_seen(tmp_path, monkeypatch):
    """`.values(**{"input_tokens": n})` — the form the comment claimed and the
    code excluded. `kw.arg` is None for an unpacked mapping, so the `if kw.arg`
    filter dropped it. A comment promising a guarantee the code does not deliver
    is worse than not claiming it."""
    g = _tree(
        tmp_path,
        monkeypatch,
        "kwargs_values.py",
        """
        from sqlalchemy import update

        def f(s, n):
            s.execute(update(Job).values(**{"input_tokens": n}))
        """,
        writer_import=False,
    )
    assert g.offenders() == ["grant_lab/kwargs_values.py::f"]


def test_unpacked_values_on_an_unrelated_field_is_not_reported(tmp_path, monkeypatch):
    g = _tree(
        tmp_path,
        monkeypatch,
        "kwargs_unrelated.py",
        """
        from sqlalchemy import update

        def f(s, n):
            s.execute(update(Job).values(**{"status": n}))
        """,
        writer_import=False,
    )
    assert g.offenders() == []


def test_a_shadowed_writer_import_does_not_count(tmp_path, monkeypatch):
    """An import is not a promise the name still means the writer.

    `from ...spend import record_spend` followed by a local `def record_spend`
    passed the gate while calling a no-op. A name rebound anywhere in the module
    is dropped from the writer set.
    """
    g = _tree(
        tmp_path,
        monkeypatch,
        "shadowed_import.py",
        """
        from grant_lab.proposal_assist.spend import record_spend

        def record_spend(*a, **k):
            pass

        def f(s, job, done):
            job.input_tokens = done.tokens_in
            record_spend(s)
        """,
        writer_import=False,
    )
    assert g.offenders() == ["grant_lab/shadowed_import.py::f"]


def test_an_orm_constructor_with_token_fields_is_seen(tmp_path, monkeypatch):
    """`EvaluatorJob(input_tokens=n)` — building a row, the natural shape.

    Same family as `+=`: not an evasion, just a form nobody had written down.
    Restricted to classes imported from grant_lab.db.models on purpose — the
    first draft flagged ANY call with a `tokens_in=` keyword and reported twelve
    legitimate functions, `record_spend` itself among them.
    """
    g = _tree(
        tmp_path,
        monkeypatch,
        "ctor.py",
        """
        from grant_lab.db.models import EvaluatorJob

        def f(s, n):
            s.add(EvaluatorJob(input_tokens=n))
        """,
        writer_import=False,
    )
    assert g.offenders() == ["grant_lab/ctor.py::f"]


def test_a_non_model_call_with_a_token_keyword_is_not_reported(tmp_path, monkeypatch):
    # The control for the above. `TurnDoneEvent(tokens_in=...)` and record_spend's
    # own keywords are not row writes; flagging them was the twelve-offender bug.
    g = _tree(
        tmp_path,
        monkeypatch,
        "not_a_model.py",
        """
        def f(n):
            return SomeEvent(tokens_in=n)
        """,
        writer_import=False,
    )
    assert g.offenders() == []


@pytest.mark.parametrize(
    ("label", "source"),
    [
        (
            "root_shadow",
            """
            import grant_lab.proposal_assist.spend

            grant_lab = object()

            def f(s, job, done):
                job.input_tokens = done.tokens_in
                grant_lab.proposal_assist.spend.record_spend(s)
            """,
        ),
        (
            "param_shadow",
            """
            from grant_lab.proposal_assist.spend import record_spend

            def f(s, job, done, record_spend=None):
                job.input_tokens = done.tokens_in
                record_spend(s)
            """,
        ),
        (
            "for_shadow",
            """
            from grant_lab.proposal_assist.spend import record_spend

            def f(s, job, done, xs):
                for record_spend in xs:
                    pass
                job.input_tokens = done.tokens_in
                record_spend(s)
            """,
        ),
        (
            "local_import_as_shadow",
            """
            from grant_lab.proposal_assist.spend import record_spend

            def f(s, job, done):
                import unrelated as record_spend
                job.input_tokens = done.tokens_in
                record_spend(s)
            """,
        ),
        (
            "local_from_import_shadow",
            """
            from grant_lab.proposal_assist.spend import record_spend

            def f(s, job, done):
                from unrelated import record_spend
                job.input_tokens = done.tokens_in
                record_spend(s)
            """,
        ),
    ],
)
def test_every_shadowing_form_invalidates_the_writer(tmp_path, monkeypatch, label, source):
    """An import is not a promise the name still means the writer.

    Round 5 caught `def` and simple assignment. Round 5's own fix missed
    parameters, `for` targets, `with ... as`, except-handler names, tuple targets,
    and — the subtle one — a DOTTED alias whose ROOT name is rebound:
    `import a.b.spend` binds `a`, so `a = fake` kills the whole path while plain
    set subtraction kept it.
    """
    g = _tree(tmp_path, monkeypatch, f"{label}.py", source, writer_import=False)
    assert g.offenders() == [f"grant_lab/{label}.py::f"]


def test_a_sibling_parameter_does_not_invalidate_this_functions_import(tmp_path, monkeypatch):
    """The false positive the shadowing rule created for itself.

    Round 6 pooled every function's parameters and locals into ONE module-wide
    set, so a helper with a parameter named `record_spend` invalidated the import
    for every sibling in the file — real, correct code reported as unrecorded.
    Shadowing is per-function now, which is the scope Python actually uses.
    """
    g = _tree(
        tmp_path,
        monkeypatch,
        "sibling.py",
        """
        from grant_lab.proposal_assist.spend import record_spend

        def helper(record_spend=None):
            return record_spend

        def f(s, job, done):
            job.input_tokens = done.tokens_in
            record_spend(s)
        """,
        writer_import=False,
    )
    assert g.offenders() == []


def test_an_import_rebinding_the_writer_name_shadows_it(tmp_path, monkeypatch):
    # `import json as record_spend` binds the name to something that is not the
    # writer. An import binds like an assignment and was the one binding form the
    # collector did not treat as one.
    g = _tree(
        tmp_path,
        monkeypatch,
        "import_shadow.py",
        """
        from grant_lab.proposal_assist.spend import record_spend
        import json as record_spend

        def f(s, job, done):
            job.input_tokens = done.tokens_in
            record_spend(s)
        """,
        writer_import=False,
    )
    assert g.offenders() == ["grant_lab/import_shadow.py::f"]


def test_the_ledger_has_exactly_one_writer():
    # The plan's central claim, asserted for the first time. The stamps/records
    # rule above would happily accept a second module that both constructed an
    # LlmSpend row and called record_spend.
    # Reference-level, not constructor-level: a second writer using
    # `session.execute(insert(LlmSpend)...)` or `bulk_insert_mappings` builds no
    # LlmSpend object, so a constructor check could never have seen it.
    # db/models/spend.py is the declaration (it was db/models.py until the
    # 2026-09-05 package split); proposal_assist/spend.py is the writer. Two
    # files named spend.py, doing opposite jobs -- read the directory, not the
    # basename. Anything else appearing here is a second module touching the
    # ledger class and needs a reason.
    assert llm_spend_writers() == [
        "grant_lab/db/models/spend.py",
        "grant_lab/proposal_assist/spend.py",
    ]


@pytest.mark.parametrize(
    ("label", "source"),
    [
        (
            "local_establishing_from",
            """
            def f(s, job, done):
                from grant_lab.proposal_assist.spend import record_spend
                job.input_tokens = done.tokens_in
                record_spend(s)
            """,
        ),
        (
            "local_establishing_alias",
            """
            def f(s, job, done):
                import grant_lab.proposal_assist.spend as spend
                job.input_tokens = done.tokens_in
                spend.record_spend(s)
            """,
        ),
    ],
)
def test_a_function_local_import_that_ESTABLISHES_the_writer_still_counts(
    tmp_path, monkeypatch, label, source
):
    """The control for the local-import shadow rule, and the trap it must not fall into.

    Both module-level import branches hit this once already: the first attempt at
    each shadowed the very name it establishes, because a writer-module import
    looks exactly like a rebinding one until you check where it comes from. A
    function-local import is a real pattern here — the processor imports its
    registry inside the function to break a cycle — so a rule that flagged these
    would fire on correct code, and a gate that fires on correct code gets
    disabled.
    """
    g = _tree(tmp_path, monkeypatch, f"{label}.py", source, writer_import=False)
    assert g.offenders() == []
