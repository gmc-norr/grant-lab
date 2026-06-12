from datetime import UTC, datetime

from call_manager.scheduler import trigger_job_now


class _FakeJob:
    def __init__(self) -> None:
        self.next_run_time = "UNSET"

    def modify(self, **kwargs) -> None:
        self.next_run_time = kwargs["next_run_time"]


class _FakeScheduler:
    def __init__(self, jobs: dict) -> None:
        self._jobs = dict(jobs)

    def get_job(self, job_id: str):
        return self._jobs.get(job_id)


def test_trigger_job_now_false_when_scheduler_none():
    assert trigger_job_now(None, "ingest:vinnova") is False


def test_trigger_job_now_false_when_job_absent():
    sched = _FakeScheduler({})
    assert trigger_job_now(sched, "digest:weekly") is False


def test_trigger_job_now_nudges_job_and_returns_true():
    job = _FakeJob()
    sched = _FakeScheduler({"ingest:vinnova": job})
    before = datetime.now(UTC)
    result = trigger_job_now(sched, "ingest:vinnova")
    after = datetime.now(UTC)
    assert result is True
    assert isinstance(job.next_run_time, datetime)
    # Nudged to a UTC-aware "now"
    assert job.next_run_time.tzinfo is not None
    assert before <= job.next_run_time <= after
