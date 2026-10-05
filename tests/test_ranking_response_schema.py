"""The JSON schema the remote ranker is held to (structured outputs) must agree
with parse_response: every rating the schema allows parses, the schema refuses
what the parser refuses, and it obeys the API's rules for a schema (closed
objects, every property required).

Why it exists: Sonnet wrote its JSON free-hand and ~10% of escalations failed
to parse (ranking_remote_attempts, 2026-09-23..10-03). A 2026-10-05 probe
re-sent 8 of those requests; 3 failed again with `Expecting ',' delimiter`, each
on a stray `;` after the `summary` string.
"""

from __future__ import annotations

import json

import jsonschema
import pytest

from grant_lab.ranking.prompt import (
    _ANALYSIS_CATEGORIES,
    RANK_RESPONSE_SCHEMA,
    ParseError,
    parse_response,
    rank_response_format,
)

_CRITERIA = ("topic_fit", "eligibility", "exclusions")


def _reply(**over) -> dict:
    reply = {
        "topic_fit": {"rating": 4, "rationale": "r", "quoted_evidence": None},
        "eligibility": {"rating": "pass", "rationale": "r", "quoted_evidence": None},
        "exclusions": {"rating": "pass", "rationale": "r", "quoted_evidence": "q"},
        "summary": "s",
        "applicant_notes": [{"category": "deadline", "text": "t"}],
    }
    reply.update(over)
    return reply


def _criterion(rating) -> dict:
    return {"rating": rating, "rationale": "r", "quoted_evidence": None}


def _objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield node
        for value in node.values():
            yield from _objects(value)
    elif isinstance(node, list):
        for value in node:
            yield from _objects(value)


def test_every_object_is_closed_and_requires_all_its_properties():
    objects = list(_objects(RANK_RESPONSE_SCHEMA))
    assert len(objects) == 5  # root, three criteria, one applicant note
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert sorted(obj["required"]) == sorted(obj["properties"])


def test_note_categories_are_exactly_the_parsers():
    note = RANK_RESPONSE_SCHEMA["properties"]["applicant_notes"]["items"]
    assert sorted(note["properties"]["category"]["enum"]) == sorted(_ANALYSIS_CATEGORIES)


def test_every_rating_the_schema_allows_parses():
    props = RANK_RESPONSE_SCHEMA["properties"]
    for name in _CRITERIA:
        for rating in props[name]["properties"]["rating"]["enum"]:
            reply = _reply(**{name: _criterion(rating)})
            jsonschema.validate(reply, RANK_RESPONSE_SCHEMA)
            assert getattr(parse_response(json.dumps(reply)), f"{name}_rating") == rating


_CANDIDATES = (-1, 0, 1, 2, 3, 4, 5, 6, True, "4", "pass", "fail", "unclear", "maybe", None)


@pytest.mark.parametrize("name", _CRITERIA)
def test_the_schema_allows_a_rating_exactly_when_the_parser_does(name):
    """Both directions. A schema narrower than the parser is silent: dropping
    "unclear" would force Sonnet to call an ambiguous call pass or fail, and
    nothing would fail to parse (final review 2026-10-05, finding 2)."""
    for rating in _CANDIDATES:
        reply = _reply(**{name: _criterion(rating)})
        try:
            jsonschema.validate(reply, RANK_RESPONSE_SCHEMA)
            schema_ok = True
        except jsonschema.ValidationError:
            schema_ok = False
        try:
            parse_response(json.dumps(reply))
            parser_ok = True
        except ParseError:
            parser_ok = False
        assert schema_ok == parser_ok, (name, rating, schema_ok, parser_ok)


@pytest.mark.parametrize(
    "name,rating",
    [("topic_fit", 6), ("topic_fit", "4"), ("eligibility", "maybe"), ("exclusions", "unclear")],
)
def test_the_schema_refuses_what_the_parser_refuses(name, rating):
    reply = _reply(**{name: _criterion(rating)})
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(reply, RANK_RESPONSE_SCHEMA)
    with pytest.raises(ParseError):
        parse_response(json.dumps(reply))


def test_the_schema_is_the_same_on_every_call():
    """Deterministic, so the API's compiled-schema cache and the prompt cache
    keep hitting: a frozenset's iteration order changes between processes."""
    assert json.dumps(RANK_RESPONSE_SCHEMA) == json.dumps(
        rank_response_format()["json_schema"]["schema"]
    )
    note = RANK_RESPONSE_SCHEMA["properties"]["applicant_notes"]["items"]
    categories = note["properties"]["category"]["enum"]
    assert categories == sorted(categories)


def test_rank_response_format_is_the_openai_json_schema_shape():
    assert rank_response_format() == {
        "type": "json_schema",
        "json_schema": {"name": "call_ranking", "strict": True, "schema": RANK_RESPONSE_SCHEMA},
    }
