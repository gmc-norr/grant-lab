import pytest

from call_manager.proposal_assist.citations.core import compute_citation_insert
from call_manager.proposal_assist.citations.ledger import empty_ledger

# Reuse the REAL reference fixture — Reference has required NOT-NULL columns
# (kind/source_type/filename/storage_path/mime_type/metadata_status); a hand-rolled
# minimal insert fails at flush. _seed_reference(session, wid, *, chunk_text) → ref_id.
from tests.proposal_assist.collab.test_collab_kernel_citation import _seed_reference
from tests.proposal_assist.conftest import make_user, make_workspace


def test_exact_offsets_disambiguate_repeated_quote(pg_session_factory):
    # "sky" occurs twice → offset-less path is ambiguous; exact offsets pick one.
    with pg_session_factory() as s:
        o = make_user(s, email="cci@umu.se")
        ws = make_workspace(s, owner=o)
        s.commit()
        wid = ws.id
        rid = _seed_reference(s, wid, chunk_text="blue sky and grey sky").id
        prop = "preamble\n\n## Methods\nThe claim here.\n"
        # offset-less → ambiguous error
        amb = compute_citation_insert(
            s, workspace_id=wid, proposal_text=prop, ledger=empty_ledger(),
            ref_uuid=rid, anchor="The claim here.", chunk_index=0,
            evidence_quote="sky", stance="supports", confidence=None)
        assert "error" in amb and "ambiguous" in amb["error"]
        # exact offsets of the SECOND "sky" (index 18..21) → success
        ok = compute_citation_insert(
            s, workspace_id=wid, proposal_text=prop, ledger=empty_ledger(),
            ref_uuid=rid, anchor="The claim here.", chunk_index=0,
            evidence_quote="sky", quote_start=18, quote_end=21,
            stance="supports", confidence=None)
        assert "error" not in ok
        assert ok["ev_id"] and ok["citekey"]


@pytest.mark.parametrize("qs,qe,frag", [
    (0, 99, "out of range"),          # quote_end > len(chunk.text)
    (5, 3, "out of range"),           # start >= end
    (0, 1, "not found"),              # slice "b" != evidence_quote "blue"
])
def test_exact_offsets_bounds_rejected(pg_session_factory, qs, qe, frag):
    with pg_session_factory() as s:
        o = make_user(s, email="cci2@umu.se")
        ws = make_workspace(s, owner=o)
        s.commit()
        wid = ws.id
        rid = _seed_reference(s, wid, chunk_text="blue sky").id
        res = compute_citation_insert(
            s, workspace_id=wid, proposal_text="p\n\n## H\nThe claim here.\n",
            ledger=empty_ledger(), ref_uuid=rid, anchor="The claim here.",
            chunk_index=0, evidence_quote="blue", quote_start=qs, quote_end=qe,
            stance="supports", confidence=None)
        assert "error" in res and frag in res["error"]


def test_error_returns_carry_codes(pg_session_factory):
    with pg_session_factory() as s:
        o = make_user(s, email="cci3@umu.se")
        ws = make_workspace(s, owner=o)
        s.commit()
        wid = ws.id
        rid = _seed_reference(s, wid, chunk_text="blue sky").id
        # anchor absent from the doc → anchor_not_found (maps to 409-stale on the direct path)
        r = compute_citation_insert(
            s, workspace_id=wid, proposal_text="p\n\n## H\nbody.\n", ledger=empty_ledger(),
            ref_uuid=rid, anchor="NOPE", chunk_index=0, evidence_quote="blue",
            quote_start=0, quote_end=4, stance="supports", confidence=None)
        assert r["error_code"] == "anchor_not_found"
