# Appendix A: mypy triage

Every error mypy reported at `49f63485` with the configuration in the spec (65 errors in 28 files),
its kind, and its fix. The fixes are the patch files beside this appendix. Each group's patch
was written and checked in its own throwaway worktree; all of them together were then checked
in one tree (mypy: no issues in 120 files; full suite 2302 passed with this plan's new tests).

Kinds, from the spec: **typing-only** (the code is right; the types were wrong or loose),
**real bug** (a test fails first), **library gap** (an ignore with a reason). No real bug was found.
Some rows cover several errors with one cause (for example, one `assert` clears six).

## web (`fix-web.patch`)

| Where | Code | Kind | Why | Fix |
|---|---|---|---|---|
| `src/grant_lab/web/spa_serving.py:23` | unused-ignore | typing-only | The override ignore is stale; the signature no longer conflicts. | Removed the ignore comment. |
| `src/grant_lab/web/dashboard.py:609` | call-overload | typing-only | select(Call, None) has no typed overload. SQLAlchemy coerces None to NULL, so SQL is unchanged. | Use select(Call, null()) and declare stmt as a union of Select[tuple[Call, CallRanking]] \| Select[tuple[Call, None]], because Select is invariant. |
| `src/grant_lab/web/dashboard.py:739` | arg-type | typing-only | scalars().all() returns Sequence[Source] but DashboardView.sources is list[Source]. | Build the value with list(session.execute(...).scalars()). |
| `src/grant_lab/web/routes/health.py:49` | return-value | typing-only | The 503 path returns a JSONResponse deliberately, but the return annotation was HealthOut. | Annotate -> HealthOut \| JSONResponse and set response_model=HealthOut on the route so the OpenAPI schema is unchanged. |
| `src/grant_lab/web/api/auth.py:106` | union-attr | typing-only | lock_user returns User \| None. After 'if not ok: raise' user cannot be None, because verify_password returns ok=True only for a non-None stored hash, which only an existing user has. | Add 'assert user is not None' with a reason comment after the not-ok denial. This one narrowing clears the six auth.py errors (106, 109, 119, 125, 126, 127). |
| `src/grant_lab/web/api/auth.py:109` | union-attr | typing-only | Same narrowing as line 106. | Covered by the assert at line 106. |
| `src/grant_lab/web/api/auth.py:119` | union-attr | typing-only | Same narrowing as line 106. | Covered by the assert at line 106. |
| `src/grant_lab/web/api/auth.py:125` | union-attr | typing-only | Same narrowing as line 106. | Covered by the assert at line 106. |
| `src/grant_lab/web/api/auth.py:126` | union-attr | typing-only | Same narrowing as line 106. | Covered by the assert at line 106. |
| `src/grant_lab/web/api/auth.py:127` | union-attr | typing-only | Same narrowing as line 106. | Covered by the assert at line 106. |
| `src/grant_lab/web/routes/oauth_google.py:65` | assignment | typing-only | The payload dict was inferred as dict[str, str], but the link branch stores ints in it. The cipher accepts any JSON-able dict. | Annotate payload as dict[str, str \| int \| None]. |
| `src/grant_lab/web/routes/oauth_google.py:66` | assignment | typing-only | Same inferred payload type as line 65. | Covered by the payload annotation. |
| `src/grant_lab/web/routes/oauth_google.py:164` | union-attr | typing-only | AccountResolution.user is None only for outcome email_collision, which returns earlier. The three union-attr errors on this line and the one at 166 share this cause. | Add 'assert res.user is not None' with a reason comment after the collision branch. |
| `src/grant_lab/web/routes/oauth_google.py:164` | union-attr | typing-only | Same narrowing as the first error on this line. | Covered by the assert. |
| `src/grant_lab/web/routes/oauth_google.py:164` | union-attr | typing-only | Same narrowing as the first error on this line. | Covered by the assert. |
| `src/grant_lab/web/routes/oauth_google.py:166` | union-attr | typing-only | Same narrowing as line 164. | Covered by the assert. |
| `src/grant_lab/web/api/calls.py:868` | arg-type | typing-only | ranking is set only inside the 'profile_obj is not None' branch, where profile_id is a parsed int. The checker cannot link the two variables. | Add 'assert profile_id is not None' before the call. |
| `src/grant_lab/web/api/boards.py:1046` | arg-type | typing-only | The request model validator enforces exactly one of call_ids / min_score. In the else branch where call_ids is None, min_score is therefore set. | Add 'assert body.min_score is not None' with a reason comment. |
| `src/grant_lab/web/api/boards.py:1093` | arg-type | typing-only | Same request-validator guarantee as line 1046. | Add 'assert body.min_score is not None' with a reason comment. |

## root (`fix-root.patch`)

| Where | Code | Kind | Why | Fix |
|---|---|---|---|---|
| `src/grant_lab/net_safety.py:144` | arg-type | typing-only | info[4][0] from getaddrinfo is typed str\|int (sockaddr union); for TCP families it is always the address str. | Build addrs with str(info[4][0]) and comment why; identical at runtime. |
| `src/grant_lab/net_safety.py:146` | return-value | typing-only | Same str\|int element flowed into the str return of resolve_public_ip. | Resolved by the same str() normalisation of addrs. |
| `src/grant_lab/net_safety.py:200` | assignment | typing-only | PinnedNetworkBackend duck-typed httpcore's backend (with __getattr__) but was not a NetworkBackend, so assigning it to pool._network_backend failed typing. | Subclass httpcore.NetworkBackend with explicit connect_tcp, connect_unix_socket and sleep delegating to the inner backend (the base class defines those, so __getattr__ would no longer reach them); drop the untyped __getattr__ and Any. |
| `src/grant_lab/scheduler.py:426` | arg-type | typing-only | build_scheduler accepts ConsecutiveFailureTracker \| _Alerter (tests pass duck-typed alerters) but run_ingestion's annotation names the concrete tracker; runtime only calls record_failure/record_success. | Not in this patch: replaced by `fix-alerter.patch` (see below). |
| `src/grant_lab/main.py:343` | assignment | typing-only | email_sender was inferred as TransportEmailSender from the try branch, then assigned Console/NoOp senders, all of which satisfy the EmailSender protocol. | Declare email_sender: EmailSender (imported from grant_lab.auth.email). |
| `src/grant_lab/main.py:600` | arg-type | typing-only | google_client_id is str\|None; Settings' model_validator already refuses google_oidc_enabled without both credentials, so None is impossible when enabled (only model_construct bypasses it). | Restructure the conditional expression into an if block that raises RuntimeError on a missing credential, narrowing both fields. |
| `src/grant_lab/main.py:601` | arg-type | typing-only | Same as client_id for google_client_secret. | Same narrowing. |
| `src/grant_lab/config.py:484` | prop-decorator | library gap | Existing ignore still needed: mypy rejects @computed_field stacked on @property even with the pydantic plugin. | Rewritten to # type: ignore[prop-decorator]  # pydantic computed_field wraps property (also line 503); mypy still reports both when removed. |

## ranking (`fix-ranking.patch`)

| Where | Code | Kind | Why | Fix |
|---|---|---|---|---|
| `src/grant_lab/ranking/criteria.py:136` | type-var/operator (3 errors) | typing-only | _trl guarded with any(v is None ...), which mypy cannot use to narrow the four names, so max/min saw Any \| None. | Replaced the any() guard with an explicit 'is None' chain on all four values (same UNKNOWN result). |
| `src/grant_lab/ranking/freshness.py:317` | attr-defined | typing-only | session.execute returns Result, which has no rowcount; an INSERT...SELECT really returns a CursorResult. | cast to CursorResult[Any] with a reason comment. |
| `src/grant_lab/ranking/queue.py:66` | attr-defined | typing-only | Same Result vs CursorResult typing gap for inserted_primary_key on an INSERT. | cast to CursorResult[Any] with a reason comment. |
| `src/grant_lab/ranking/queue.py:117` | return-value | typing-only | scalars().all() is a Sequence; function is annotated list. | return list(rows). |
| `src/grant_lab/ranking/queue.py:229` | attr-defined | typing-only | UPDATE results are CursorResult; typed as Result. | cast dead to CursorResult[Any]. |
| `src/grant_lab/ranking/queue.py:230` | attr-defined | typing-only | Same as above for requeued. | cast requeued to CursorResult[Any]. |
| `src/grant_lab/ranking/queue.py:260` | attr-defined | typing-only | Same Result vs CursorResult gap in revert_to_pending. | cast to CursorResult[Any] with a reason comment. |
| `src/grant_lab/ranking/rank_pair.py:163` | arg-type | typing-only | **dict[str,int] splatted into chat(logprobs: bool, top_logprobs: int\|None). | Branch on logprobs_enabled and pass logprobs=True, top_logprobs=10 explicitly, else omit them (the wire call and tests that pin absent kwargs are unchanged). |
| `src/grant_lab/ranking/anthropic_client.py:146` | call-overload | typing-only | System blocks, messages and output_config were loose dicts / a ** splat of dict, matching no SDK overload. | Typed system_blocks as list[TextBlockParam], messages as list[MessageParam] (_split_system, one cast with reason), request_config as OutputConfigParam, effort as the Literal that settings already enforces, and branch on request_config so the output_config kwarg is still absent when empty. |
| `src/grant_lab/ranking/anthropic_client.py:248` | attr-defined | typing-only | _billed sets exc.usage on LLMError, which declared no such attribute (rank_pair reads it with getattr). | Declared 'usage: dict \| None = None' on LLMError in ollama_client.py. |
| `src/grant_lab/ranking/worker.py:260` | arg-type | typing-only | drain_queue declared client: OllamaClient \| None, but the only production caller (scheduler) always passes a real OllamaClient and rank_pair dereferences it unconditionally; None only appears in tests that stub rank_pair. | Annotated client as OllamaClient (non-optional); no runtime change. |

## boards (`fix-boards.patch`)

| Where | Code | Kind | Why | Fix |
|---|---|---|---|---|
| `src/grant_lab/boards/qr.py:329` | attr-defined | typing-only | qr_for.cache_info was assigned onto a plain function, which mypy types as Callable without that attribute. | Wrap qr_for in a small _QrFor callable class (decorator) that delegates __call__, cache_info and cache_clear to the lru_cached _cached_qr_for; the two attribute assignments are removed. |
| `src/grant_lab/boards/qr.py:330` | attr-defined | typing-only | Same as above for cache_clear. | Same _QrFor wrapper provides a typed cache_clear(). |
| `src/grant_lab/boards/queue.py:85` | arg-type | typing-only | session.scalar returns int\|None, but boards.profile_id is a NOT NULL RESTRICT FK to profiles and profiles.created_by is NOT NULL, so the row and owner always exist. | Use session.execute(...).scalar_one() with a comment giving that reason. |
| `src/grant_lab/boards/queue.py:161` | return-value | typing-only | session.scalar(count(*)) is typed int\|None, but an aggregate count always returns one row. | Use session.execute(...).scalar_one(). |
| `src/grant_lab/boards/auto_publish.py:149` | return-value | typing-only | Same count(*) scalar typed int\|None. | Use session.execute(...).scalar_one(). |
| `src/grant_lab/boards/alerts.py:145` | arg-type | typing-only | dict(Sequence[Row]) is not accepted as Iterable[tuple]; Rows are tuples at runtime. | Build the mapping with a dict comprehension unpacking (board_id, count). |
| `src/grant_lab/boards/alerts.py:203` | list-item | typing-only | owner_id is int\|None; if None, the Subscription query (user_id == NULL) can never match so sub is None and the escalation branch already runs. | Make the existing missing-subscription guard `if owner_id is None or sub is None`, which narrows owner_id with identical behaviour. |
| `src/grant_lab/boards/alerts.py:258` | var-annotated | typing-only | dict() over Rows could not infer key/value types. | Annotate owner_by_profile: dict[int, int] and build it with a comprehension. |
| `src/grant_lab/boards/alerts.py:259` | arg-type | typing-only | Consequence of the missing annotation / Row sequence passed to dict(). | Same comprehension; also drops the redundant `oid is not None` filter since created_by is NOT NULL. |

## ingestion (`fix-ingestion.patch`)

| Where | Code | Kind | Why | Fix |
|---|---|---|---|---|
| `src/grant_lab/ingestion/upsert.py:195` | assignment | typing-only | update_cols was inferred as dict[str, KeyedColumnElement] from stmt.excluded[c], so assigning func.coalesce(...) is rejected; at runtime any SQL expression is valid in on_conflict set_. | Annotate update_cols as dict[str, ColumnElement[Any]] (imports ColumnElement and Any). |
| `src/grant_lab/ingestion/upsert.py:196` | assignment | typing-only | Same inferred dict value type rejects func.now(). | Same annotation as above. |
| `src/grant_lab/ingestion/alerts.py:115` | attr-defined | typing-only | Source.__table__ is typed FromClause, which has no .update(); at runtime it is a Table. | Use sqlalchemy.update(Source), which emits the same UPDATE. |
| `src/grant_lab/ingestion/alerts.py:134` | attr-defined | typing-only | Same FromClause typing of __table__. | Use update(Source). |
| `src/grant_lab/ingestion/alerts.py:180` | arg-type | typing-only | self._admin_email is str \| None; the None case is already excluded by should_attempt_alert, but mypy cannot link that boolean to the attribute. | Read the email into a local before the lock, test it in the should_attempt_alert expression, and return early if the local is None (unreachable when should_attempt_alert is true), then pass the narrowed local to send(). |
| `src/grant_lab/ingestion/alerts.py:303` | attr-defined | typing-only | Same FromClause typing of __table__. | Use update(Source). |
| `src/grant_lab/ingestion/orchestrator.py:188` | attr-defined | typing-only | Same FromClause typing of __table__. | Use update(Source). |

## small (`fix-small.patch`)

| Where | Code | Kind | Why | Fix |
|---|---|---|---|---|
| `src/grant_lab/digest/assemble.py:239,272,289,302` | arg-type | typing-only | excl_clause was `Call.id.notin_(...) if excluded_list else True`, so mypy saw `BinaryExpression[bool] \| bool` passed to .where(). SQLAlchemy coerces True to true() at runtime, so behaviour is correct. | Annotate excl_clause as ColumnElement[bool] and use sqlalchemy.true() instead of the Python literal True (same SQL). |
| `src/grant_lab/auth/oidc.py:128` | arg-type | typing-only | `JWTClaimsRegistry(leeway=60, **claims_options)` with a dict[str, dict] makes mypy think the unpacked keys could bind to the `now`/`leeway` parameters. The keys are only iss/exp/iat/sub/email/aud, so runtime is correct. | Pass the six claim options as explicit keyword arguments (same values) instead of unpacking a dict, and drop the intermediate dict. |
| `src/grant_lab/sources/types.py:162` | dict-item | typing-only | urlsplit(url).hostname is `str \| None`, used as a dict key. resolve_pinned_ip has already returned an IP only after confirming a non-empty hostname, so None is impossible here. | Add `if host is None: return None` after the lookup, which satisfies the type checker and cannot change behaviour. |
| `src/grant_lab/sources/extraction.py:125,210` | func-returns-value | typing-only | `return _reject_redirect(...)` returns the result of a function annotated -> None. It works at runtime and returns None. | Call _reject_redirect(...) and then `return None` on the next line; update the helper docstring to match. |
| `src/grant_lab/profiles/bootstrap_worker.py:61` | attr-defined | typing-only | Session.execute(update(...)) is typed Result[Any], which has no rowcount. At runtime an UPDATE always returns a CursorResult, which does. | Keep the result in a variable, narrow it with `assert isinstance(dead_result, CursorResult)`, then read .rowcount. |

## The alerter (`fix-alerter.patch`)

`scheduler.py` passed `ConsecutiveFailureTracker | _Alerter` where `run_ingestion` asked for
`ConsecutiveFailureTracker`. The root group's first fix was a `cast`, which hides the mismatch.
The patch instead adds one `FailureAlerter` Protocol in `ingestion/alerts.py` (the two methods
ingestion calls), types `run_ingestion(alerter=...)` and `build_scheduler(alerter=...)` with it,
and removes the scheduler's duplicate `_Alerter`. It also re-formats `ingestion/orchestrator.py`,
which the ingestion patch had left one line unformatted. Kind: typing-only.
