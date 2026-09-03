# Matching method — second look (2026-09-03)

**Question:** do the findings in
[reviews/2026-09-03-matching-method-and-release-split-review.md](../reviews/2026-09-03-matching-method-and-release-split-review.md)
hold up against the code and the live database, and should the ranking method change?

**Short answer:** the method is sound and stays. The review's code citations hold. Two of
its factual claims are wrong (history-file entries, "nearly free" reranker) and one of its
method arguments is for a different task (pairwise judging). Every question about *how
good* the ranker is remains unanswerable because the golden set is empty — and that is
the only thing that would justify a change.

**Decision (user, 2026-09-03):** keep Gemma + G-Eval + selective Sonnet. No redesign,
no reranker, no model swap, and **no tuning now** — the interest is in principles. In
future the *user* should be able to build the yardstick from inside the product.

Every number below was produced by a command run in this session on
`579c5b94` against the live dev database. Nothing is predicted. Items marked
**not verified** were not checked.

---

## 1. The review's claims, checked

| Claim (review §) | Verdict | Evidence |
| --- | --- | --- |
| F1 golden set is empty | **True** | `golden_set_triples`: **0 rows**. `data/golden_set_last.json` = `{"n_fixture": 0, "n_compared": 0, "tau": null}`, mtime **Mon 2026-08-31 06:00** — the weekly job's real output, not a test artefact. |
| F1 history file has "four entries" (review lines 21–22) | **Wrong** | `data/golden_set_history.jsonl` had **1052 rows** over **28 distinct days** (first 2026-06-11, last 2026-09-03; 228 rows on 2026-08-16, the day the suite was parallelised). `max n_fixture = 4`. The test suite writes to the live file — see §3. |
| F2 cosine < 0.20 kills a pair before any LLM | True | `config.py:148` pregate 0.30, `config.py:158` hard-fail 0.20. |
| F2 escalation rescues only eligibility-fail + topic ≥ 3 | True | `rank_pair.py:167-169`. |
| F3 `CALL_TEXT_LIMIT` is 16000 characters | True | `prompt.py:11` (in `ranking/prompt.py`, not `config.py`). |
| F3 daily cap 100, min score 45 | True | `config.py:700`, `config.py:693`. |
| F3 non-Anthropic prices | **Not verified** | Outside the repo. |
| F4 `claude-sonnet-4-6` → `claude-sonnet-5` is a free win | **True and safe** | `config.py:691` default. Prices from the `claude-api` skill table (cached 2026-06-24): Sonnet 4.6 **$3 / $15**, Sonnet 5 **$2 / $10** per MTok. `anthropic_client.py:101-105` sends only `model`, `max_tokens`, `system`, `messages` — nothing Sonnet 5 rejects (no `temperature`, no `budget_tokens`, no prefill). |
| F5 columns exist | True | `models.py:387-388` (`escalated`, `escalated_from_score`); `models.py:368` (`embed_similarity`). |
| F5 query result | **Ran it** — see §2 | |
| F6 Gemma runs on every pair that clears the gates | True | `rank_pair.py`; local always runs first. |
| F7 jina-reranker-v3 BEIR numbers | **Not verified** | Vendor benchmark, English web retrieval. Our data is Swedish + English funding calls vs research profiles. |
| F7 "0.6B is nearly free on the 2080 Ti" | **Wrong** | See §2: **1036 MiB free** with Gemma loaded; Ollama **0.21.0** has no rerank endpoint (`POST /api/rerank → 404`). |
| F7 "pairwise beats absolute scoring" | Applies to a different task | Pairwise + order alternation is the norm for judging *generated text*. This task is *relevance* with a cutoff (digest, board). A comparison yields an order and no cutoff. |
| Release split: `router.simple.tsx` | Planned, not present | File does not exist; the spec (§5) lists it as release-1 work. `router.tsx` has **9** `lazy(` routes. |
| Peer-university survey | Not verified | Outside the repo. |

## 2. Live measurements

**F5 escalation-shift query** (`call_rankings WHERE escalated AND escalated_from_score IS NOT NULL`):

| escalations | avg_shift | big_flips (≥15) |
| --- | --- | --- |
| 11 | 20.5 | 5 |

Shift distribution: 6 unchanged (0); −20 ×1; −40 ×1; −56…−60 ×2; +50 ×1. Sonnet mostly
says "less relevant" — it trims false positives and rescued one call. **n = 11 is too
small to act on.**

**Rows by producing model** (58 rankings, 1 profile `Digital Pathology & Imaging AI`,
58 calls from 2 sources):

| model | aggregate_reason | rows | score range |
| --- | --- | --- | --- |
| gemma4:e4b-it-q4_K_M | — | 16 | 0–100 |
| '' (deterministic gate) | timeline | 15 | 0 |
| gemma4:e4b-it-q4_K_M | eligibility | 14 | 0 |
| claude-sonnet-4-6 | — | 6 | 20–60 |
| claude-sonnet-4-6 | eligibility | 4 | 0 |
| claude-sonnet-4-6 | exclusion | 1 | 4 |
| '' | budget / trl | 1 + 1 | 0 |

`embed_similarity` is non-NULL on **58/58**; 14 rows score > 0; 8 rows ≥ 45. So the
three orderings needed to judge the ranker (cosine only, Gemma only, final) are already
stored per row — the comparison needs labels, not code.

**GPU / serving path** (`nvidia-smi`, `ollama ps`, both models loaded the way the app
loads them):

| fact | measured |
| --- | --- |
| RTX 2080 Ti total | 11264 MiB |
| idle | 93 MiB used |
| Gemma Q4 loaded (`gemma4:e4b-it-q4_K_M`, 9.6 GB on disk) | **9794 MiB used, 1036 MiB free** |
| embedder (`mxbai-embed-large`) while Gemma is loaded | **evicted** — Ollama swaps them |
| Gemma `load_duration` | 8.04 s |
| Ollama version / `POST /api/rerank` | 0.21.0 / **HTTP 404** |

A 0.6B reranker (~1.2 GB weights in fp16 before activations) does not fit beside Gemma
and cannot be served by the current stack. It would be a third model in the swap on a
new serving path — an experiment with real cost, not a free one.

**Score composition.** The 0–100 aggregate is `round(topic × 20)` (`aggregate.py:76`),
where `topic` is the G-Eval expected value when logprobs exist, else the argmax
(`aggregate.py:69`; the remote path has no logprobs, `ev.py:28`). Eligibility /
exclusion only cap it. The topic scale in the prompt defines **only 5, 3, 1, 0**
(`prompt.py:69-71`); levels 2 and 4 have no anchor text.

## 3. Side finding: the test suite writes to the live data directory

`ranking/eval.py:31-34` resolves `_REPORT_PATH` and `_HISTORY_PATH` at import time from
`CALL_MANAGER_DATA_DIR` (default `data/`). `tests/test_ranking_eval.py:32` monkeypatches
**only** `_REPORT_PATH`; `tests/test_golden_set_eval_history.py:61` patches both;
`test_golden_set_eval_db_mode.py` and `test_api_admin_dashboard.py` patch neither.

Proof by running: `wc -l data/golden_set_history.jsonl` = **1052** → ran
`tests/test_ranking_eval.py tests/test_golden_set_eval_db_mode.py` (15 passed) →
**1058**. `golden_set_last.json` mtime unchanged (the autouse fixture protects it).

Consequence: the history file is worthless as evidence and the review's "four entries"
reading of it is wrong. Fix is small (patch `_HISTORY_PATH` in the same autouse fixture,
or set `CALL_MANAGER_DATA_DIR` to `tmp_path` in `conftest.py`). Not done — out of scope.
Same family as the 2026-08 `var/workspaces` pollution.

## 4. Principle-level verdict

**Sound, keep:**

- Cheap deterministic checks → near-free cosine → local rubric → paid second opinion.
  The cascade is the right shape.
- **Pointwise graded relevance with a rubric and a verbatim quote.** A triage inbox and a
  digest need "good enough to show", i.e. an absolute grade with a cutoff. Pairwise is
  the wrong principle for this product.
- Reasons are the product; `call_ranking_criteria` already carries rating + rationale +
  quoted evidence per criterion. The score is a sort key.
- G-Eval is a detail that breaks ties. Keep or drop; it does not touch the principle.

**Rubric, not Likert.** The review's "rubric beats Likert" point is right and applies to
exactly one field: `topic_fit` is the last bare 0–5. Every other criterion is already
rubric-shaped. The fix is an anchor sentence for every level (0–5) in `prompt.py:69-71`
plus a prompt-version bump. That is a principle fix, parked with everything else.

**Two additions that are principles, not tuning:**

1. **The system must measure itself.** The only yardstick today is a person with a
   spreadsheet. The product should let a user mark "good match" / "not for us" while
   working, and that becomes a `golden_set_triples` row (the 2026-06-11 product review
   asked for the same two buttons). This is also what "the user should be able to do it
   in future" means concretely.
2. **No filter without an audit sample.** Two stages kill a pair before anyone sees it
   (cosine < 0.20; a low local grade). A small random sample of the killed pairs should
   get a second look — not to raise scores, to make the miss rate visible (review F2).

**What would change the verdict:** labels showing Gemma misses relevant calls (recall at
the 45 line). Then the first mechanism to try is a cross-encoder *replacing* Gemma's
topic score (the parked v2 hybrid), with Gemma kept for the rationale — on a separate
serving path. Not before.

## 5. Recorded order (all parked)

1. `REMOTE_RANK_MODEL=claude-sonnet-5` — free, safe (§1 F4).
2. Six rubric anchors for `topic_fit`; bump prompt version.
3. Labels. Then compare cosine / Gemma / final against them.
4. Audit sample of the low band (F2).
5. Reranker trial only if step 3 shows misses.

## 6. Tooling left behind (gitignored, outside the repo history)

`data/golden_labelling/golden_labels.py` — three commands, all run this session:

```
uv run python data/golden_labelling/golden_labels.py sheet             # wrote 58 pairs; 17 gate rows pre-filled with 0
uv run python data/golden_labelling/golden_labels.py import --dry-run  # would insert 17, skipped 0
uv run python data/golden_labelling/golden_labels.py report            # tau for cosine / Gemma / final, misses at the 45 line
```

The sheet (`data/golden_labelling/labels.csv`) is **blind** — no model scores. The
report was proven on 7 fake labels inside a rolled-back transaction
(`golden_set_triples` count after: 0). Rows decided by a deterministic gate are excluded
from the three-way tau because no model or cosine judged them. The scale mirrors the
ranker's: 0 / 25 / 50 / 75 / 100.

Only **41** pairs need a human (58 minus 17 gate rows) — one or two hours, not the
review's "roughly a day". The review's 100–200 target needs a second profile: there is
exactly one.

## 7. Recorded, not re-measured

- Q4_K_M vs Q8_0: Spearman 0.965, 9.1 vs 23.6 min per 50 pairs — memory of a 2026-04-17
  live test, not re-run.
- Pricing for non-Anthropic models, jina/BEIR numbers, peer-university newsletter survey —
  **not verified** (outside the repo).
