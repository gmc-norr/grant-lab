"""Run untrusted file text-extraction in a separate, killable process.

extract_text() runs synchronously in the request path. Byte/page caps bound
memory but not CPU time, so a crafted-but-small file can pin the single app
process. We run the extractor in a ``multiprocessing`` child and terminate it
on timeout — the only mechanism that actually stops CPU-bound work (a thread
cannot be force-killed). On timeout we raise ExtractError, which ingest Phase 2
already maps to text_status='failed'.

The child also caps its own ADDRESS SPACE (review 2026-09-02 §2.5(d)): a
1.99 MB PDF whose one page inflates to 2 GiB made the child allocate 4 GB and
finish inside the CPU budget — the time bound never sees an allocation. With
RLIMIT_AS the allocation fails as MemoryError, reported like any other
extraction failure.
"""

from __future__ import annotations

import multiprocessing as mp
import queue as _queue
import resource

from call_manager.proposal_assist.references.extract import ExtractError, extract_text

# Spawn (not fork): a clean interpreter with no inherited DB connections /
# sockets / locks from the parent app process.
_CTX = mp.get_context("spawn")

# Address-space cap for the extraction child. Measured 2026-09-02: a spawned
# child sits at 48 MB after importing the extractor and extracting a 1 MB text
# file, so 2 GiB leaves real extraction (pdfminer on a 100-page PDF, openpyxl)
# 40× headroom while refusing the 4 GB stream bomb above. Both soft and hard,
# so nothing in the child can raise it again.
_MAX_ADDRESS_SPACE_BYTES = 2 * 1024**3


def _apply_memory_limit() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (_MAX_ADDRESS_SPACE_BYTES, _MAX_ADDRESS_SPACE_BYTES))


def _worker(mime: str, data: bytes, q) -> None:
    try:
        _apply_memory_limit()
        q.put(("ok", extract_text(mime=mime, data=data)))
    except MemoryError:
        # The failed allocation is released before we get here, so this small
        # put succeeds. Named explicitly: "MemoryError: " alone says nothing.
        q.put(
            (
                "err",
                "MemoryError: extraction exceeded the "
                f"{_MAX_ADDRESS_SPACE_BYTES >> 20} MiB memory budget",
            )
        )
    except Exception as e:  # noqa: BLE001 — any failure is surfaced to the parent
        q.put(("err", f"{type(e).__name__}: {e}"))


def extract_text_bounded(
    *, mime: str, data: bytes, timeout_seconds: float
) -> list[tuple[str, str]]:
    """``extract_text`` with a hard wall-clock bound.

    Raises ExtractError on timeout (child terminated), on a child crash, or on
    any error raised by extract_text inside the child. Otherwise returns the
    same ``[(section_path, text), ...]`` list as extract_text.

    We read the result with a bounded ``q.get(timeout=...)`` rather than
    ``join()``-then-``get()``: a multiprocessing.Queue feeder thread blocks once
    the pickled payload exceeds the OS pipe buffer (~64 KiB), so the child can
    only exit after the parent starts reading. Draining while we wait avoids a
    deadlock that would otherwise mark every >64 KiB extraction as a false
    timeout.
    """
    q = _CTX.Queue()
    proc = _CTX.Process(target=_worker, args=(mime, data, q), daemon=True)
    proc.start()
    try:
        status, payload = q.get(timeout=timeout_seconds)
    except _queue.Empty as e:
        proc.terminate()
        proc.join(5)
        if proc.is_alive():  # pragma: no cover — terminate almost always suffices
            proc.kill()
        raise ExtractError(f"extraction exceeded {timeout_seconds:.0f}s budget") from e
    finally:
        proc.join(5)  # reap the child; it has put its result and can now exit
        q.close()
        q.join_thread()
    if status == "err":
        raise ExtractError(payload)
    return payload
