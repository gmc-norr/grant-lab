"""Loader options shared by every query that reads Call rows for a response.

One home, because three copies of the same three column names is how they
drift -- and drift here is silent: a query that stops deferring gets slower and
stays correct, so nothing goes red.
"""

from __future__ import annotations

from sqlalchemy.orm import defer

from grant_lab.db.models import Call


def call_columns_no_response_renders():
    """`Call` columns that no list response contains, deferred.

    `embedding` is the expensive one and not by a little: a 1024-dim vector
    ships as ~9 kB of TEXT for pgvector's PURE-PYTHON parser to turn back into
    1024 floats, per row. `full_text` and `raw` are simply large.

    MEASURED on this tree, 200 rows, best of five, same seeded data:

        board_queue          118.53 ms  ->  21.58 ms
        public_board_items   117.63 ms  ->  14.06 ms

    Run-to-run variance on a loaded box is a few ms; the ~100 ms is not noise.

    `raiseload=True` makes it a contract rather than a hope: a consumer that
    reads one of these off such a row raises, instead of silently issuing one
    extra SELECT per row and turning a saved 100 ms into an N+1. If a response
    ever needs one of these columns, DROP it from this list at that call site --
    do not catch the error.

    A function rather than a module-level tuple: loader options are consumed by
    the statement they are passed to, and sharing one built instance across
    queries is the kind of reuse that works until it does not.
    """
    return (
        defer(Call.embedding, raiseload=True),
        defer(Call.full_text, raiseload=True),
        defer(Call.raw, raiseload=True),
    )
