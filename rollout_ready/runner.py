"""Live runs: run only what is missing, then judge each output on its own.

Results are saved after every case, so a failed call never loses work.
Demo mode never comes through here.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .compare import missing_cases
from .judge import judge_output
from .models import CaseResult, Run, VersionResults
from .provider import Provider, ProviderError
from .storage import Store, input_hash, rubric_hash

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
) -> int:
    """Run and judge the cases that have no usable results yet. Returns calls made."""
    suite = store.load_suite(suite_id)
    cases = store.load_cases(suite_id)
    version = store.load_version(suite_id, version_id)
    todo = missing_cases(store, suite, cases, version_id, live=True)
    total = len(todo) * suite.repeats * 2
    done = 0
    saved = store.load_runs(suite_id, version_id) or VersionResults(version=version_id)
    saved.model, saved.judge_model = suite.model, judge_model
    for case in todo:
        runs = []
        for _ in range(suite.repeats):
            if progress:
                progress(done, total, f"Writing an answer for “{case.name}”")
            gen = provider.generate(model=suite.model, prompt=version.text, user_input=case.input)
            done += 1
            if progress:
                progress(done, total, f"Scoring the answer for “{case.name}”")
            scores, parsed = judge_output(provider, judge_model, suite, case, gen.text)
            done += 1
            runs.append(Run(
                output=gen.text, scores=scores,
                input_tokens=gen.input_tokens, output_tokens=gen.output_tokens,
                judge_input_tokens=parsed.input_tokens, judge_output_tokens=parsed.output_tokens,
            ))
        saved.cases[case.id] = CaseResult(
            model=suite.model, rubric_hash=rubric_hash(suite), input_hash=input_hash(case), runs=runs,
        )
        saved.ran_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        store.save_runs(suite_id, saved)
    if progress:
        progress(total, total, "Done")
    return done


def pending_for(store: Store, suite_id: str, version_ids: list[str]) -> int:
    suite = store.load_suite(suite_id)
    cases = store.load_cases(suite_id)
    return sum(len(missing_cases(store, suite, cases, v, live=True)) for v in dict.fromkeys(version_ids))


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


def run_versions(store: Store, suite_id: str, version_ids: list[str], provider: Provider, judge_model: str):
    """Work function for a job that fills in every missing result for these versions."""
    def work(progress):
        offset_total = pending_for(store, suite_id, version_ids) * store.load_suite(suite_id).repeats * 2
        done_so_far = 0

        def scaled(done, total, message):
            progress(done_so_far + done, offset_total, message)

        for v in dict.fromkeys(version_ids):
            done_so_far += run_version(store, suite_id, v, provider, judge_model, scaled)
    return work
