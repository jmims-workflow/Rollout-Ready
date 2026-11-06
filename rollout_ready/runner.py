"""Live runs: run only what is missing, then judge each output on its own.

Every test goes through evaluate.run_evaluation, which reuses saved answers and
reviews by key, saves each new one as soon as it is made (so a failed call never
loses work), and writes one evaluation record. Demo mode never comes through here.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from . import evaluate
from .compare import missing_cases
from .config import DEFAULT_JUDGE_MODEL
from .provider import Provider, ProviderError
from .storage import Store

SECONDS_PER_CALL = 4


def estimate(case_count: int, repeats: int) -> tuple[int, str]:
    """Number of Claude calls and a plain time estimate."""
    calls = case_count * repeats * 2
    seconds = calls * SECONDS_PER_CALL
    if calls == 0:
        return 0, "no time at all"
    if seconds < 45:
        when = "less than a minute"
    elif seconds < 100:
        when = "about a minute"
    else:
        when = f"about {round(seconds / 60)} minutes"
    return calls, when


def time_sentence(case_count: int, repeats: int) -> str:
    """For Test a change: how long, with no call counts (spec 7c)."""
    calls, when = estimate(case_count, repeats)
    return "Takes no time: every result is saved already." if not calls else f"Takes {when}."


def estimate_sentence(case_count: int, repeats: int) -> str:
    calls, when = estimate(case_count, repeats)
    if not calls:
        return "Everything already has saved results, so nothing needs to run."
    return f"This will make about {calls} calls to Claude and take {when}."


def run_version(
    store: Store,
    suite_id: str,
    version_id: str,
    provider: Provider,
    judge_model: str,
    progress: Callable[[int, int, str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Score one version on its own (a baseline test). Returns calls made.

    Only answers and reviews that aren't saved yet are made. An answer cut off at
    the length limit is never saved or scored and leaves that item incomplete.
    """
    return evaluate.run_evaluation(store, suite_id, version_id, version_id, provider, judge_model,
                                   progress, sleep).calls


def change_test_estimate(store: Store, suite_id: str, against_id: str,
                         reviewer_model: str = DEFAULT_JUDGE_MODEL) -> tuple[int, int]:
    """For Test a change: (examples the compared version still needs, calls the test can make at most).

    The version you're trying is new, so every example and repetition needs an answer and
    its review. The compared version needs both for any example still missing either. A
    whole example is counted (answer and review, every repetition) whenever anything is
    missing, so the estimate is never lower than what a run makes. Retries of a failed call
    (at most 2 each) are not included.
    """
    suite = store.load_suite(suite_id)
    cases = store.load_cases(suite_id)
    against_todo = len(missing_cases(store, suite, cases, against_id, live=True, reviewer_model=reviewer_model))
    return against_todo, (len(cases) + against_todo) * suite.repeats * 2


def pending_for(store: Store, suite_id: str, version_ids: list[str], reviewer_model: str = DEFAULT_JUDGE_MODEL) -> int:
    """Examples, summed over these versions, that still need an answer or a review."""
    suite = store.load_suite(suite_id)
    cases = store.load_cases(suite_id)
    return sum(len(missing_cases(store, suite, cases, v, live=True, reviewer_model=reviewer_model))
               for v in dict.fromkeys(version_ids))


# background jobs for the web app --------------------------------------------
@dataclass
class Job:
    id: str
    title: str
    done_url: str
    total: int = 0
    done: int = 0
    message: str = "Starting"
    error: str | None = None
    finished: bool = False
    owner: str = ""  # the visitor's session in public live mode
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def percent(self) -> int:
        return 100 if self.finished and not self.error else int(100 * self.done / self.total) if self.total else 0


class JobManager:
    def __init__(self):
        self.jobs: dict[str, Job] = {}

    def start(self, title: str, done_url: str, work: Callable[[Callable[[int, int, str], None]], None],
              owner: str = "") -> Job:
        job = Job(id=uuid.uuid4().hex, title=title, done_url=done_url, owner=owner)
        self.jobs[job.id] = job

        def progress(done: int, total: int, message: str) -> None:
            with job.lock:
                job.done, job.total, job.message = done, max(total, job.total), message

        def target() -> None:
            try:
                work(progress)
            except ProviderError as exc:
                job.error = str(exc)
            except Exception:  # keep details out of the page; saved results are kept
                job.error = "Something went wrong while running the test. Any results already saved are kept."
            finally:
                job.finished = True

        threading.Thread(target=target, daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)


def run_versions(store: Store, suite_id: str, version_ids: list[str], provider: Provider, judge_model: str,
                 sleep: Callable[[float], None] = time.sleep):
    """Work function for a job: test the second version against the first (or score one version alone).

    Writes one evaluation record, reusing every saved answer and review that matches.
    """
    ids = list(dict.fromkeys(version_ids))
    baseline, candidate = ids[0], ids[-1]

    def work(progress):
        evaluate.run_evaluation(store, suite_id, baseline, candidate, provider, judge_model, progress, sleep)
    return work
