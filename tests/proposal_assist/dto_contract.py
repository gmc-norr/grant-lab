"""The DTO contract: a query result carries data, never a live Session.

Spec 2026-08-18 revision 8 section 4.2. An ATTACHED ORM instance carries
object_session(instance), which is the full Session again — the exact leak that
defeated the forwarding-proxy design. The import gate cannot see this, because
the tool imports nothing to receive it.

Shared by test_f3b_queries.py and test_ports_dto_contract.py so the two cannot
drift; every future query port goes through the same walker.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import enum
import uuid

from sqlalchemy import inspect as sa_inspect

#: Where the walk STOPS. Every entry is a stdlib scalar with no attribute that could
#: be a Session, so there is nothing inside one to walk. Anything that is neither
#: listed here nor a supported container is a hard failure — see the `else` at the
#: bottom of assert_detached. Extending this tuple is a deliberate act; the two
#: shipped DTOs need only NoneType, bool, int, str and uuid.UUID — measured, field by
#: field. float, bytes, the datetime family, Decimal and Enum are here because they
#: are provably inert and the next DTO will want them, NOT because anything uses them
#: today. That is the whole justification; if a type does not fit it, leave it out and
#: let the `else` fire.
INERT_SCALARS = (
    type(None),
    bool,
    int,
    float,
    str,
    bytes,
    uuid.UUID,
    decimal.Decimal,
    datetime.date,  # datetime.datetime is a subclass
    datetime.time,
    datetime.timedelta,
    enum.Enum,
)


def assert_detached(value: object, path: str) -> None:
    """Recursively assert that no ORM instance hides anywhere inside `value`.

    Walks nested dataclasses, lists, tuples, sets and dicts (keys AND values) —
    not just a DTO's own top-level fields, because `dataclasses.asdict` leaves a
    collection of raw ORM instances untouched and a top-level-only check would
    wave it through.

    FAIL-CLOSED. The walk stops on an INERT_SCALARS value and RAISES on anything else
    it cannot see inside. A shape-blind version returns silently for a plain object, a
    `__slots__` object, a `SimpleNamespace`, a `deque` or a `dict_values` view, so
    `Opaque(ref=attached)` passes — and `object_session(o.ref)` is the Session, the
    leak this contract exists to deny. Rejection rather than attribute-recursion is
    the sound answer: a lazy iterator cannot be inspected without consuming the
    caller's own result.

    A cycle raises RecursionError rather than hanging. Deliberately not guarded
    with a visited set: a query DTO containing a cycle is itself a defect, and a
    loud stack overflow says so more clearly than a silent skip.
    """
    assert sa_inspect(value, raiseerr=False) is None, (
        f"{path}: {type(value).__name__} is an ORM-mapped object"
    )
    if isinstance(value, INERT_SCALARS):
        return
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        for field in dataclasses.fields(value):
            assert_detached(getattr(value, field.name), f"{path}.{field.name}")
    elif isinstance(value, dict):
        for key, item in value.items():
            assert_detached(key, f"{path}.key({key!r})")
            assert_detached(item, f"{path}[{key!r}]")
    elif isinstance(value, (list, tuple, set, frozenset)):
        for index, item in enumerate(value):
            assert_detached(item, f"{path}[{index}]")
    else:
        raise AssertionError(
            f"{path}: the walker cannot see inside {type(value).__name__} — a query "
            f"result must be a frozen dataclass of scalars and plain collections. "
            f"Extend INERT_SCALARS deliberately if this type is provably inert."
        )
