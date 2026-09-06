"""Shared helpers for the matching-freshness tests."""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime

import httpx
from sqlalchemy import select

from grant_lab.db.models import (
    Call,
    Profile,
    ProfileKind,
    RankingJob,
    RankingJobStatus,
    Source,
    User,
)
from grant_lab.embeddings.client import EmbedResult
from grant_lab.ranking.ollama_client import LLMError, OllamaClient

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def make_user(session, *, email: str | None = None) -> User:
    u = User(email=email or f"freshness-{_next()}@umu.se", status="active")
    session.add(u)
    session.flush()
    return u


def make_profile(session, *, description: str = "Liver disease", **kw) -> Profile:
    """A Profile owned by a fresh User.

    `kind` must be one of the two ck_profiles_kind allows -- an earlier
    revision of this plan used "research_group", which the CHECK rejects.
    """
    u = make_user(session)
    p = Profile(
        name=kw.pop("name", f"Group {_next()}"),
        kind=kw.pop("kind", ProfileKind.ORGANIZATIONAL_UNIT),
        description=description,
        structured=kw.pop("structured", {}),
        created_by=u.id,
        **kw,
    )
    session.add(p)
    session.flush()
    return p


def make_call(session, *, source_slug: str = "vinnova", **kw) -> Call:
    src = session.execute(select(Source).where(Source.slug == source_slug)).scalar_one()
    n = _next()
    c = Call(
        source_id=src.id,
        external_id=kw.pop("external_id", f"FRESH-{n}"),
        url=f"https://example.org/calls/fresh-{n}",
        title=kw.pop("title", "Precision medicine programme"),
        summary="Funding for AI-based diagnostics.",
        full_text="Body about eligibility, scope, budget.",
        # reconcile_missing_rankings filters on published_at >= now-90d and
        # excludes NULL published_at entirely.
        published_at=kw.pop("published_at", datetime.now(UTC)),
        **kw,
    )
    session.add(c)
    session.flush()
    return c


class FakeEmbedder:
    """Counts embed calls AND supplies session().

    _refresh_missing_embeddings_batch does `with embed_client.session() as http:`
    twice per batch. A fake without it raises AttributeError in Task 4.
    """

    def __init__(self, *, fail: bool = False, model: str = "bge-m3"):
        self.model = model
        self.fail = fail
        self.calls = 0

    def embed(self, text, _client=None):
        self.calls += 1
        if self.fail:
            raise LLMError("ollama down")
        return EmbedResult(vector=[0.1] * 1024, embedded_chars=len(text))

    @contextlib.contextmanager
    def session(self):
        yield None


def enqueue_job(session, *, call_id: int, profile_id: int) -> int:
    job = RankingJob(call_id=call_id, profile_id=profile_id, status=RankingJobStatus.PENDING)
    session.add(job)
    session.flush()
    return job.id


def ollama_clean() -> OllamaClient:
    """A real OllamaClient over an httpx.MockTransport returning a clean Gemma
    envelope -- the pattern tests/test_ranking_worker.py:63-113 uses."""
    body = {
        "model": "gemma4:e4b-it-q4_K_M",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "topic_fit": {
                                "rating": 4,
                                "rationale": "AI match",
                                "quoted_evidence": "AI",
                            },
                            "eligibility": {
                                "rating": "pass",
                                "rationale": "ok",
                                "quoted_evidence": None,
                            },
                            "exclusions": {
                                "rating": "pass",
                                "rationale": "ok",
                                "quoted_evidence": None,
                            },
                        }
                    ),
                },
                "logprobs": {
                    "content": [
                        {
                            "token": "4",
                            "logprob": -0.1,
                            "top_logprobs": [
                                {"token": "4", "logprob": -0.1},
                                {"token": "3", "logprob": -2.0},
                            ],
                        },
                    ]
                },
            }
        ],
    }

    def handler(request):
        return httpx.Response(200, json=body)

    return OllamaClient(
        base_url="http://x",
        model="gemma4:e4b-it-q4_K_M",
        transport=httpx.MockTransport(handler),
    )
