"""The web app. Server-rendered pages; every form works without JavaScript.

Pages use the plain words from CLAUDE.md: a suite is a "prompt", a test case
is an "example", the judge is the "AI reviewer". Code and files keep the
technical names.
"""

from __future__ import annotations

import os
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.middleware.sessions import SessionMiddleware

from .. import config, demo_session, evaluate, public, releases, scoring
from ..compare import (MIX_NUDGE, Comparison, build_comparison, comparison_for_record, example_mix,
                       load_results, needs_harder_examples, summary_text)
from ..config import Settings, length_problem, load_settings
from ..demo_criteria import SAMPLE_CRITERIA, SAMPLE_SUITE_NAME, SAMPLE_WHO
from ..judge import draft_criteria
from ..evidence import OverrideEvent
from ..models import CASE_KINDS, Case, Check, Criterion, Suite, Version
from ..provider import ClaudeProvider, Provider, ProviderError
from ..runner import Job, JobManager, change_test_estimate, estimate_sentence, pending_for, run_versions
from ..public import SessionStore, Sessions, SharedResults, Usage
from ..storage import Store, SuiteError, slugify

HERE = Path(__file__).resolve().parent
TABS = ("examples", "good", "changes", "details")
OLD_TABS = {"cases": "examples", "rubric": "good"}
READ_ONLY = ("This demo is read-only, so nothing new can be saved. "
             "Whoever runs the app can turn on live mode, with an API key, to save your own work.")
CALLS = {
    "not_worse": "It didn't actually get worse",
    "not_better": "It didn't actually get better",
    "other": "Something else",
}
VERDICT_PHRASES = {
    scoring.READY: "is ready to roll out",
    scoring.NO_DIFFERENCE: "made no clear difference",
    scoring.NOT_READY: "is not ready to roll out",
    scoring.NEEDS_REVIEW: "needs review before it rolls out",
    scoring.INCOMPLETE: "has an incomplete test",
}


def _when(stamp: str) -> str:
    if not stamp:
        return "Not recorded"
    try:
        dt = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return stamp
    return dt.strftime("%B %-d, %Y at %H:%M UTC")


def _day(stamp: str) -> str:
    """'2026-10-02T16:00:00Z' becomes 'October 2, 2026'."""
    try:
        return datetime.strptime(stamp[:10], "%Y-%m-%d").strftime("%B %-d, %Y")
    except ValueError:
        return stamp or "Not recorded"


def _vname(version_id: str) -> str:
    """'v3' becomes 'version 3'."""
    return f"version {version_id[1:]}" if re.fullmatch(r"v\d+", version_id or "") else version_id


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def create_app(
    settings: Settings | None = None,
    provider_factory: Callable[[Settings], Provider] | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    store = Store(settings.suites_dir)
    jobs = JobManager()

    def make_provider() -> Provider:
        if provider_factory:
            return provider_factory(settings)
        return ClaudeProvider(settings.api_key or "")

    app = FastAPI(title="Rollout Ready", docs_url=None, redoc_url=None, openapi_url=None)
    # Demo overrides, and the visitor's session id in public live mode, live in a signed
    # cookie that ends with the browser session (max_age=None).
    secret = os.environ.get("ROLLOUT_SECRET_KEY", "").strip() or secrets.token_urlsafe(32)
    sessions = Sessions(settings.data_dir / "sessions")
    shared = SharedResults(settings.data_dir / "shared")
    usage = Usage(settings.data_dir / "usage.json", settings.limits, secret)
    app.add_middleware(SessionMiddleware, secret_key=secret, session_cookie="rollout_ready",
                       max_age=None, same_site="lax")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(
        settings=settings, fmt=scoring.fmt, fmt_change=scoring.fmt_change,
        fmt_crit=scoring.fmt_criterion, when=_when, day=_day, vname=_vname, plural=_plural,
        in_use_help=releases.IN_USE_HELP, names_help=releases.NAMES_HELP, recorded_note=releases.RECORDED_NOTE,
        public_release_note=("Only you can see this. It lasts 24 hours or until you close this browser, "
                             "whichever comes first. Putting a version into use doesn't use any of your live tests."),
        demo_release_note="In this demo, your changes last until you close this browser.",
        result_labels=scoring.RESULT_LABELS, kinds=CASE_KINDS, calls=CALLS,
        mix_nudge=MIX_NUDGE, cutoff=scoring.DEAL_BREAKER_CUTOFF,
        private_note=public.PRIVATE_NOTE, used_up_note=public.USED_UP, honeypot_name=public.HONEYPOT,
    )
    app.state.settings, app.state.store, app.state.jobs = settings, store, jobs
    app.state.usage, app.state.sessions = usage, sessions

    def page(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    def problem(request: Request, message: str, status: int = 400, heading: str = "Something needs fixing", **kw):
        return page(request, "error.html", status=status, heading=heading, message=message, **kw)

    def wants_json(request: Request) -> bool:
        return request.headers.get("x-requested-with") == "fetch"

    if settings.is_public:
        @app.middleware("http")
        async def only_our_own_pages(request: Request, call_next):
            if request.method == "POST" and not public.same_origin(request.headers):
                return problem(request, public.OTHER_SITE, status=403, heading="Please try again")
            return await call_next(request)

    # public live mode: each visitor has their own folder --------------------
    def session_id(request: Request) -> str:
        """The visitor's private session. Empty unless the app is in public live mode."""
        if not settings.is_public:
            return ""
        sess, now = request.session, time.time()
        sid = sess.get("sid", "")
        if not public.SESSION_ID_RE.fullmatch(str(sid)) or now - float(sess.get("started", 0)) > public.SESSION_SECONDS:
            sid = secrets.token_hex(16)
            sess["sid"], sess["started"] = sid, now
            sessions.prune(now)
        return sid

    def store_for(request: Request) -> Store:
        if not settings.is_public:
            return store
        return SessionStore(sessions.folder(session_id(request)), store, shared)

    def client_ip(request: Request) -> str:
        return public.client_ip(request.headers, request.client.host if request.client else None)

    def runs_left(request: Request) -> int:
        return usage.left(session_id(request), client_ip(request))

    def take_run(request: Request) -> bool:
        """Use one live run. Always True outside public live mode."""
        return not settings.is_public or usage.take(session_id(request), client_ip(request))

    def is_spam(form) -> bool:
        """The hidden field people never see. Anything typed in it came from a bot."""
        return settings.is_public and bool(str(form.get(public.HONEYPOT, "")).strip())

    def too_many_examples(cases) -> str | None:
        if settings.is_public and len(cases) > config.PUBLIC_MAX_EXAMPLES:
            return (f"Live tests here run on at most {config.PUBLIC_MAX_EXAMPLES} examples, "
                    f"and this prompt has {len(cases)}.")
        return None

    def start_job(request: Request, title: str, done_url: str, work) -> Job:
        return jobs.start(title, done_url, work, owner=session_id(request))

    # release history: shipped events, then live files or the demo cookie ---------
    def release_log(request: Request, suite_id: str) -> releases.ReleaseLog:
        if settings.is_live:
            return releases.StoreLog(store_for(request), suite_id)
        return releases.ListLog(demo_session.releases(request.session, suite_id))

    def release_history(request: Request, suite_id: str) -> list[releases.ReleaseEvent]:
        return releases.history(store_for(request), suite_id, release_log(request, suite_id))

    def in_use(request: Request, suite: Suite, versions) -> str:
        """The version in the latest release event (derived, never stored)."""
        return releases.in_use(release_history(request, suite.id), [v.id for v in versions], suite.in_use)

    def ordered(suites: list[Suite]) -> list[Suite]:
        """The featured prompt first, then by team and name."""
        return sorted(suites, key=lambda s: (s.id != config.FEATURED_SUITE, s.team.lower() or "~", s.name.lower()))

    def default_pair(request: Request, suite: Suite, versions: list[Version]) -> tuple[str, str] | None:
        vids = [v.id for v in versions]
        if not vids:
            return None
        if suite.id == config.FEATURED_SUITE and {config.FEATURED_CURRENT, config.FEATURED_NEW} <= set(vids):
            return config.FEATURED_CURRENT, config.FEATURED_NEW
        current = in_use(request, suite, versions)
        if current == vids[-1] and len(vids) > 1:
            current = vids[-2]
        later = [v for v in vids if int(v[1:]) > int(current[1:])]
        return current, (later[-1] if later else vids[-1])

    def latest_pair(request: Request, st: Store, suites: list[Suite]) -> tuple[str, str, str] | None:
        """The result the start screen and See results open on.

        Demo mode, and public live mode, show the featured comparison. Local live
        mode shows the most recent saved test, whichever pair it compared.
        """
        ids = [s.id for s in suites]
        if (not settings.is_live or settings.is_public) and config.FEATURED_SUITE in ids:
            suite = next(s for s in suites if s.id == config.FEATURED_SUITE)
            pair = default_pair(request, suite, st.versions(suite.id))
            if pair:
                return (suite.id, *pair)
        if settings.is_live:
            newest = None
            for s in suites:
                for rid in st.evaluation_ids(s.id):
                    rec = st.load_evaluation(s.id, rid)
                    if rec and rec.kind == "comparison" and (newest is None or rid > newest[0]):
                        newest = (rid, s.id, rec.baseline.id, rec.candidate.id)
            if newest:
                return newest[1:]
        for s in suites:
            pair = default_pair(request, s, st.versions(s.id))
            if pair and pair[0] != pair[1]:
                return (s.id, *pair)
        return None

    # overrides: append-only events keyed by the evidence's fingerprint and example.
    # Live: files (private to the session in public mode). Demo: the signed session cookie,
    # so nothing is written to disk. -----------------------------------------------------
    def override_events(request: Request, suite_id: str) -> list[OverrideEvent]:
        if settings.is_live:
            return store_for(request).override_events(suite_id)
        return demo_session.overrides(request.session, suite_id)

    def add_override_event(request: Request, suite_id: str, event: OverrideEvent) -> None:
        if settings.is_live:
            store_for(request).append_override_event(suite_id, event)
            return
        demo_session.add_override(request.session, suite_id, event)  # CookieFull if there's no room

    def with_overrides(request: Request, comp: Comparison) -> Comparison:
        if comp.record is None or comp.current.id == comp.new.id:
            return comp
        active = evaluate.active_override_events(override_events(request, comp.suite.id), comp.record.fingerprint)
        return comp.apply_overrides(evaluate.as_overrides(active))

    def comparison(request: Request, suite_id: str, current: str, new: str) -> Comparison:
        st = store_for(request)
        comp = build_comparison(st, suite_id, current, new, settings.is_live, reviewer_model=settings.judge_model)
        if settings.is_public and not comp.complete:
            # Public visitors still see the demo's sample comparisons. A comparison is all
            # real results or all sample results, never a mix.
            sample = build_comparison(st, suite_id, current, new, False)
            if sample.complete:
                return with_overrides(request, sample)
        return with_overrides(request, comp)

    def results_url(suite_id: str, current: str, new: str, **extra) -> str:
        return "/results?" + urlencode({"suite": suite_id, "current": current, "new": new, **extra})

    def example_url(suite_id: str, case_id: str, current: str, new: str, **extra) -> str:
        return f"/prompts/{suite_id}/examples/{case_id}?" + urlencode({"current": current, "new": new, **extra})

    templates.env.globals.update(results_url=results_url, example_url=example_url)

    # start screen ----------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        params = request.query_params
        if params.get("suite") or params.get("current") or params.get("new"):
            # Links from the first version pointed the comparison at /.
            return RedirectResponse("/results?" + str(params), status_code=301)
        st = store_for(request)
        try:
            suites = ordered(st.suites())
        except SuiteError as exc:
            return problem(request, str(exc), status=500)
        latest, banner = None, ""
        pick = latest_pair(request, st, suites)
        if pick:
            try:
                comp = comparison(request, *pick)
            except SuiteError:
                comp = None
            if comp and comp.complete:
                latest = comp
                banner = (f"Latest result: {comp.new.full_name.lower()} of the {comp.suite.name.lower()} "
                          f"{VERDICT_PHRASES[comp.verdict]}.")
                if comp.worse:
                    banner += f" It broke {_plural(comp.worse, 'example')}."
        saved = 0
        for s in suites:
            real = evaluate.versions_with_results(st, s.id) if settings.is_live else set()
            for v in st.versions(s.id):
                if (v.id in real or (not settings.is_live or settings.is_public)
                        and load_results(st, s.id, v.id, False).cases):
                    saved += 1
        teams = [s.team.lower() for s in suites if s.team]
        prompts_meta = _plural(len(suites), "prompt") + (": " + ", ".join(dict.fromkeys(teams)) if teams else "")
        return page(request, "home.html", nav="home", latest=latest, banner=banner,
                    saved_meta=_plural(saved, "saved result"), prompts_meta=prompts_meta)

    # results -----------------------------------------------------------
    @app.get("/results", response_class=HTMLResponse)
    def results(request: Request, suite: str = "", current: str = "", new: str = "", tab: str = "examples",
                example: str = ""):
        return results_view(request, suite, current, new, tab, example == "1")

    def results_view(request: Request, suite: str, current: str, new: str, tab: str = "examples",
                     example: bool = False, status: int = 200, **extra):
        st = store_for(request)
        suites = ordered(st.suites())
        if not suites:
            return problem(request, "There are no prompts yet.", heading="Nothing to show yet",
                           back_url="/prompts/new", back_text="Add a new prompt")
        ids = [s.id for s in suites]
        if suite not in ids:
            pick = latest_pair(request, st, suites)
            suite = pick[0] if pick else ids[0]
            if pick and not current and not new:
                current, new = pick[1], pick[2]
        the_suite = next(s for s in suites if s.id == suite)
        versions = st.versions(suite)
        vids = [v.id for v in versions]
        if not vids:
            return problem(request, "This prompt has no versions yet.", status=404)
        pair = default_pair(request, the_suite, versions)
        if current not in vids:
            current = pair[0]
        if new not in vids:
            later = [v for v in vids if int(v[1:]) > int(current[1:])]
            new = later[-1] if later else vids[-1]
        tab = OLD_TABS.get(tab, tab)
        tab = tab if tab in TABS else "examples"
        ctx = dict(nav="results", suites=suites, suite=the_suite, versions=versions,
                   current_id=current, new_id=new, tab=tab, example=example,
                   base_q=results_url(suite, current, new), comp=None, error=None)
        if current == new:
            return page(request, "compare.html", **ctx)
        try:
            comp = comparison(request, suite, current, new)
        except SuiteError as exc:
            ctx["error"] = str(exc)
            return page(request, "compare.html", **ctx)
        ctx["comp"] = comp
        ctx["nudge"] = needs_harder_examples([r.case for r in comp.rows])
        if comp.complete:
            ctx["summary"] = summary_text(comp)
        else:
            ctx["estimate"] = estimate_sentence(sum(len(v) for v in comp.missing.values()), the_suite.repeats)
        ctx.update(decision_ctx(request, st, the_suite, versions, comp))
        ctx.update(extra)
        return page(request, "compare.html", status=status, **ctx)

    # deciding: notices and the decision block on the results page -----------------
    def decision_ctx(request: Request, st: Store, suite: Suite, versions: list[Version], comp: Comparison) -> dict:
        """What the results page shows about rolling out. Every check runs again on submit (releases.py)."""
        use = in_use(request, suite, versions)
        number = {v.id: v.number for v in versions}
        rec = comp.record
        live_record = rec is not None and rec.source == "live"
        others = evaluate.records_for_pair(st, suite.id, comp.current.id, comp.new.id) if live_record else []
        stale = live_record and not comp.saved and any(r.final for r in others)
        separate = live_record and not comp.saved and comp.complete and not others
        relied_on = rec is not None and (comp.saved or rec.source == "recorded")
        changed = comp.complete and rec is not None and comp.current.id != use and comp.new.id != use
        can_decide = (comp.complete and relied_on and not stale and rec.kind == "comparison"
                      and comp.new.id != use)
        kind = None
        if can_decide:
            kind = "rollout" if comp.verdict == scoring.READY and not changed else "exception"
        items = rec.items if rec is not None else []
        return dict(
            in_use_id=use, in_use_number=number.get(use, 0), stale=stale, separate=separate, changed=changed,
            interrupted=comp.status == "interrupted",
            finished_items=sum(i.status == "complete" for i in items), total_items=len(items),
            cut_off=sum(i.status == "cut_off" for i in items),
            saved_incomplete=live_record and comp.saved and comp.status == "incomplete",
            decide_kind=kind, token=releases.new_token(),
            miss_rows=[r for r in comp.rows if r.blocking_misses],
            new_is_in_use=comp.new.id == use,
        )

    def evaluation_comparison(request: Request, suite_id: str, evaluation_id: str) -> Comparison | None:
        """The evaluation a decision relies on, rebuilt now with the overrides in force."""
        st = store_for(request)
        pair = evaluate.parse_recorded_id(evaluation_id)
        if pair:
            comp = build_comparison(st, suite_id, *pair, live=False)
        else:
            comp = comparison_for_record(st, suite_id, evaluation_id)
        return with_overrides(request, comp) if comp is not None else None

    def record_decision(request: Request, suite_id: str, decision: releases.Decision, comp: Comparison | None):
        """Check and record a decision. Demo events go in the cookie, within its budget."""
        st = store_for(request)
        log = release_log(request, suite_id)
        active = []
        if comp is not None and comp.record is not None:
            active = list(evaluate.active_override_events(override_events(request, suite_id),
                                                          comp.record.fingerprint).values())
        if settings.is_public:
            decision.confirmed_by = "you"
        event, found, created = releases.decide(st, suite_id, decision, log, comp, [o.id for o in active])
        if created and not settings.is_live:
            for e in log.added:  # CookieFull if there's no room: nothing is saved
                demo_session.add_release(request.session, suite_id, e)
        return event, found

    def _decision(form, kind: str) -> releases.Decision:
        return releases.Decision(
            kind=kind, version_id=str(form.get("version", "")), token=str(form.get("token", "")),
            confirmed_by=" ".join(str(form.get("name", "")).split()), reason=str(form.get("reason", "")),
            baseline_ack=bool(form.get("baseline_ack")), deal_breaker_ack=bool(form.get("deal_breaker_ack")))

    @app.post("/prompts/{suite_id}/release")
    async def release(request: Request, suite_id: str):
        form = await request.form()
        kind = str(form.get("kind", ""))
        if kind not in ("rollout", "exception"):
            return problem(request, "Choose how to put this version into use.", status=400)
        try:
            comp = evaluation_comparison(request, suite_id, str(form.get("evaluation", "")))
        except SuiteError as exc:
            return problem(request, str(exc), status=404, heading="Not found")
        if comp is None:
            return problem(request, "That test couldn't be found. Open the result again.", status=404,
                           heading="Not found")
        decision = _decision(form, kind)
        event, found = record_decision(request, suite_id, decision, comp)
        if event is None:
            return results_view(request, suite_id, comp.current.id, comp.new.id, status=400,
                                decision_errors=found, decision_form=dict(form))
        return RedirectResponse(f"/prompts/{suite_id}/releases/{event.id}", status_code=303)

    @app.post("/prompts/{suite_id}/return")
    async def put_back(request: Request, suite_id: str):
        form = await request.form()
        try:
            store_for(request).load_suite(suite_id)
        except SuiteError as exc:
            return problem(request, str(exc), status=404, heading="Not found")
        decision = _decision(form, "return")
        event, found = record_decision(request, suite_id, decision, None)
        if event is None:
            return suite_view(request, suite_id, status=400, return_errors=found, return_form=dict(form),
                              put_back=decision.version_id)
        return RedirectResponse(f"/prompts/{suite_id}/releases/{event.id}", status_code=303)

    @app.get("/prompts/{suite_id}/releases/{event_id}", response_class=HTMLResponse)
    def release_record(request: Request, suite_id: str, event_id: str):
        st = store_for(request)
        try:
            suite = st.load_suite(suite_id)
            events = release_history(request, suite_id)
        except SuiteError as exc:
            return problem(request, str(exc), status=404, heading="Not found")
        event = next((e for e in events if e.id == event_id), None)
        if event is None:
            return problem(request, "There's no record of that change.", status=404, heading="Not found")
        versions = st.versions(suite_id)
        use = in_use(request, suite, versions)
        based_on = None
        if event.evaluation_id:
            try:
                comp = evaluation_comparison(request, suite_id, event.evaluation_id)
            except SuiteError:
                comp = None
            if comp is not None:
                url = (results_url(suite_id, comp.current.id, comp.new.id) if event.evaluation_source == "recorded"
                       else f"/prompts/{suite_id}/tests/{event.evaluation_id}")
                based_on = dict(comp=comp, url=url)
        overrides = []
        if event.override_ids:
            by_id = {o.id[:8]: o for o in override_events(request, suite_id)}
            overrides = [by_id[i[:8]] for i in event.override_ids if i[:8] in by_id]
        names = {c.id: c.name for c in st.load_cases(suite_id)}
        from_status = releases.status(event.from_version, events, use) if event.from_version else ""
        return page(request, "release.html", nav="prompts", suite=suite, event=event, based_on=based_on,
                    overrides=overrides, case_names=names, from_status=from_status,
                    from_text=next((v.text for v in versions if v.id == event.from_version), ""))

    @app.get("/prompts/{suite_id}/tests/{evaluation_id}", response_class=HTMLResponse)
    def saved_test(request: Request, suite_id: str, evaluation_id: str):
        """One saved test, showing its own evidence."""
        try:
            comp = evaluation_comparison(request, suite_id, evaluation_id)
        except SuiteError:
            comp = None
        if comp is None:
            return problem(request, "That test couldn't be found.", status=404, heading="Not found")
        return results_view(request, suite_id, comp.current.id, comp.new.id)

    @app.get("/summary.txt")
    def summary(request: Request, suite: str, current: str, new: str):
        try:
            comp = comparison(request, suite, current, new)
        except SuiteError as exc:
            return PlainTextResponse(str(exc), status_code=404)
        if not comp.complete:
            return PlainTextResponse("Some results are missing, so there is no summary yet.", status_code=409)
        name = f"rollout-ready-{suite}-{current}-vs-{new}.txt"
        return PlainTextResponse(summary_text(comp), headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # one example ---------------------------------------------------------
    def example_page(request: Request, suite_id: str, case_id: str, current: str, new: str,
                     status: int = 200, **extra):
        try:
            comp = comparison(request, suite_id, current, new)
        except SuiteError as exc:
            return problem(request, str(exc), status=404, heading="Not found")
        row = comp.row(case_id)
        if row is None:
            return problem(request, "There are no saved results for this example in both versions yet.",
                           status=404, heading="No results yet", back_url=results_url(suite_id, current, new),
                           back_text="Back to the results")
        return page(request, "case.html", status=status, nav="results", suite=comp.suite, comp=comp, row=row,
                    current_id=current, new_id=new, **extra)

    @app.get("/prompts/{suite_id}/examples/{case_id}", response_class=HTMLResponse)
    def case_detail(request: Request, suite_id: str, case_id: str, current: str = "", new: str = "", saved: str = ""):
        message = {"1": "Saved. This example now counts the way you said, for these results only. "
                        "If the examples or what good looks like change, you'll decide again.",
                   "removed": "Removed. This example counts the way the AI reviewer scored it again."}.get(saved)
        return example_page(request, suite_id, case_id, current, new, message=message)

    @app.post("/prompts/{suite_id}/examples/{case_id}/override")
    async def save_override(request: Request, suite_id: str, case_id: str):
        form = await request.form()
        current, new = str(form.get("current", "")), str(form.get("new", ""))
        call = str(form.get("call", ""))
        reason = " ".join(str(form.get("reason", "")).split())
        count_as = str(form.get("count_as", ""))
        try:
            comp = comparison(request, suite_id, current, new)
        except SuiteError as exc:
            return problem(request, str(exc), status=404, heading="Not found")
        row = comp.row(case_id)
        if row is None:
            return problem(request, "There are no saved results for this example in both versions yet.", status=404)
        ai = row.ai_result
        result, error = None, None
        if call == "not_worse" and ai == scoring.WORSE or call == "not_better" and ai == scoring.BETTER:
            result = scoring.SAME
        elif call == "other":
            if count_as in scoring.RESULT_LABELS and count_as != ai:
                result = count_as
            else:
                error = "Choose how this example should count instead."
        else:
            error = "Choose what your call is."
        if not reason:
            error = "Add a one-line reason, so you and your team know why you made this call."
        elif len(reason) > 200:
            error = "Keep the reason to one line, under 200 characters."
        if error:
            return example_page(request, suite_id, case_id, current, new, status=400, form_error=error,
                                form={"call": call, "reason": reason, "count_as": count_as})
        add_override_event(request, suite_id, evaluate.override_event(
            comp.record.fingerprint, comp.record.id, case_id, call=call, result=result, reason=reason))
        return RedirectResponse(example_url(suite_id, case_id, current, new, saved="1") + "#your-call",
                                status_code=303)

    @app.post("/prompts/{suite_id}/examples/{case_id}/override/remove")
    async def remove_override(request: Request, suite_id: str, case_id: str):
        form = await request.form()
        current, new = str(form.get("current", "")), str(form.get("new", ""))
        try:
            comp = comparison(request, suite_id, current, new)
        except SuiteError as exc:
            return problem(request, str(exc), status=404, heading="Not found")
        row = comp.row(case_id)
        if row is not None and row.override is not None:  # removing appends an event; nothing is deleted
            add_override_event(request, suite_id, evaluate.override_event(
                comp.record.fingerprint, comp.record.id, case_id, remove=True))
        return RedirectResponse(example_url(suite_id, case_id, current, new, saved="removed") + "#your-call",
                                status_code=303)

    # test a change ---------------------------------------------------------
    def test_estimate(st: Store, suite: Suite, cases: list[Case], against: str) -> tuple[int, str]:
        """Examples the compared version still needs to run, and the call estimate for the whole test.

        The version you're trying runs every example. The version you compare with runs any
        example it has no real results for, which is all of them when it has only sample results.
        """
        against_todo, _calls = change_test_estimate(st, suite.id, against, settings.judge_model)
        return against_todo, estimate_sentence(len(cases) + against_todo, suite.repeats)

    def test_context(request: Request, suite_id: str, against: str) -> dict:
        st = store_for(request)
        suites = ordered(st.suites())
        ids = [s.id for s in suites]
        if not settings.is_live:
            suite_id, against = config.FEATURED_SUITE, config.FEATURED_CURRENT
        suite_id = suite_id if suite_id in ids else (config.FEATURED_SUITE if config.FEATURED_SUITE in ids else ids[0])
        suite = next(s for s in suites if s.id == suite_id)
        versions = st.versions(suite_id)
        use = in_use(request, suite, versions)
        against_v = next((v for v in versions if v.id == against), None) or next(v for v in versions if v.id == use)
        cases = st.load_cases(suite_id)
        ctx = dict(nav="test", suites=suites if settings.is_live else [suite], suite=suite,
                   versions=versions if settings.is_live else [against_v],
                   against=against_v, in_use=use, cases=cases, error=None, prompt_text="", note="",
                   nudge=needs_harder_examples(cases), mix=example_mix(cases),
                   against_todo=0, estimate="", prefilled=False, runs_line="", used_up=False, examples_problem=None,
                   next_url="/test?" + urlencode({"suite": suite_id, "against": against_v.id}) + "#add-example")
        if settings.is_live:
            ctx["against_todo"], ctx["estimate"] = test_estimate(st, suite, cases, against_v.id)
        else:
            example = st.load_version(suite_id, config.SAVED_EXAMPLE_VERSION)
            ctx.update(prompt_text=example.text, note=example.note,
                       estimate=f"The saved example replays {len(cases)} recorded results, so it takes a moment and makes no calls to Claude.")
        if settings.is_public:
            left = runs_left(request)
            ctx.update(runs_line=f"{left} of {_plural(settings.limits.per_session, 'live test')} left today.",
                       used_up=left == 0, examples_problem=too_many_examples(cases))
            if suite_id == config.FEATURED_SUITE:
                # An editable starting point, so a first-time visitor can run a real test straight away.
                example = st.load_version(suite_id, config.SAVED_EXAMPLE_VERSION)
                ctx.update(prompt_text=example.text, note=example.note, prefilled=True)
        return ctx

    @app.get("/test", response_class=HTMLResponse)
    def test_form(request: Request, suite: str = "", against: str = ""):
        try:
            return page(request, "test.html", **test_context(request, suite, against))
        except SuiteError as exc:
            return problem(request, str(exc), status=404)

    @app.post("/test")
    async def test_run(request: Request):
        form = await request.form()
        if not settings.is_live:
            return RedirectResponse(results_url(config.FEATURED_SUITE, config.FEATURED_CURRENT,
                                                config.SAVED_EXAMPLE_VERSION, example="1"), status_code=303)
        if is_spam(form):
            return RedirectResponse("/test", status_code=303)
        suite_id, against = str(form.get("suite", "")), str(form.get("against", ""))
        text, note = str(form.get("prompt_text", "")).strip(), str(form.get("note", "")).strip()
        try:
            ctx = test_context(request, suite_id, against)
        except SuiteError as exc:
            return problem(request, str(exc), status=404)
        ctx.update(prompt_text=text, note=note, prefilled=False)
        err = (length_problem(text, "prompt", settings.max_chars) or length_problem(note, "note", settings.max_chars)
               or (None if text else "Paste the version you're trying first.")
               or (None if note else "Add a line about what changed and why, so you can tell versions apart later.")
               or too_many_examples(ctx["cases"]))
        if err:
            ctx["error"] = err
            return page(request, "test.html", status=400, **ctx)
        if not take_run(request):
            ctx["error"] = public.USED_UP
            return page(request, "test.html", status=429, **ctx)
        st = store_for(request)
        suite_id, against = ctx["suite"].id, ctx["against"].id
        version = st.add_version(suite_id, text, note)
        done = results_url(suite_id, against, version.id)
        job = start_job(request, f"Testing {_vname(version.id)} against {_vname(against)}", done,
                        run_versions(st, suite_id, [against, version.id], make_provider(), settings.judge_model))
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    @app.post("/run")
    async def run_missing(request: Request):
        form = await request.form()
        if not settings.is_live:
            return problem(request, READ_ONLY, status=403, heading="Demo", info=True)
        suite_id, current, new = (str(form.get(k, "")) for k in ("suite", "current", "new"))
        st = store_for(request)
        try:
            st.load_version(suite_id, current), st.load_version(suite_id, new)
            err = too_many_examples(st.load_cases(suite_id))
            nothing_to_run = not pending_for(st, suite_id, [current, new], settings.judge_model)
        except SuiteError as exc:
            return problem(request, str(exc), status=404)
        done = results_url(suite_id, current, new)
        if is_spam(form):
            return RedirectResponse(done, status_code=303)
        if nothing_to_run:
            # Every answer and review is saved already (for example, only a quick check changed, or the
            # two versions ran in separate tests). Saving a record for this pair makes no calls, so it
            # runs straight away and uses none of a visitor's live tests.
            evaluate.run_evaluation(st, suite_id, current, new, make_provider(), settings.judge_model)
            return RedirectResponse(done, status_code=303)
        if err:
            return problem(request, err, back_url=done, back_text="Back to the results")
        if not take_run(request):
            return problem(request, public.USED_UP, status=429, heading="No live tests left today", info=True,
                           back_url="/results", back_text="Explore the sample results")
        job = start_job(request, f"Running the missing examples for {_vname(current)} and {_vname(new)}", done,
                        run_versions(st, suite_id, [current, new], make_provider(), settings.judge_model))
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    def find_job(request: Request, job_id: str) -> Job | None:
        """A job, if it belongs to this visitor. In public live mode nobody sees another visitor's run."""
        job = jobs.get(job_id)
        return job if job and job.owner == session_id(request) else None

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str):
        job = find_job(request, job_id)
        if not job:
            return problem(request, "That run has finished or the server restarted. Saved results are kept.",
                           status=404, heading="Run not found")
        if job.finished and not job.error:
            return RedirectResponse(job.done_url, status_code=303)
        return page(request, "job.html", nav="test", job=job)

    @app.get("/jobs/{job_id}/status")
    def job_status(request: Request, job_id: str):
        job = find_job(request, job_id)
        if not job:
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"done": job.done, "total": job.total, "percent": job.percent, "message": job.message,
                "finished": job.finished, "error": job.error, "done_url": job.done_url}

    # your prompts and their examples -----------------------------------------
    @app.get("/prompts", response_class=HTMLResponse)
    def prompts_page(request: Request):
        items, error, st = [], None, store_for(request)
        for sid in st.suite_ids():
            try:
                items.append({"suite": st.load_suite(sid), "cases": st.load_cases(sid), "versions": st.versions(sid)})
            except SuiteError as exc:
                error = str(exc)
        order = {s.id: i for i, s in enumerate(ordered([i["suite"] for i in items]))}
        items.sort(key=lambda i: order[i["suite"].id])
        return page(request, "suites.html", nav="prompts", items=items, error=error)

    def new_prompt_ctx(f: dict, cases_count: int = 3, **kw) -> dict:
        return dict(nav="prompts", f=f, errors=[], message=None, estimate=estimate_sentence(cases_count, 1), **kw)

    @app.get("/prompts/new", response_class=HTMLResponse)
    def new_suite_form(request: Request):
        return page(request, "suite_new.html", **new_prompt_ctx({}))

    @app.post("/prompts/new", response_class=HTMLResponse)
    async def new_suite_submit(request: Request):
        form = await request.form()
        f = {k: str(v) for k, v in form.items()}
        action = f.pop("action", "save")
        spam = is_spam(form)
        f.pop(public.HONEYPOT, None)
        st = store_for(request)
        cases = [
            (f.get(f"case_name_{i}", "").strip(), f.get(f"case_label_{i}", "").strip(),
             f.get(f"case_input_{i}", "").strip(), _kind(f.get(f"case_kind_{i}", "")))
            for i in range(1, 9)
        ]
        cases = [c for c in cases if c[0] or c[2]]
        ctx = new_prompt_ctx(f, max(len(cases), 1))
        too_long = [m for m in (length_problem(f.get("prompt_text", ""), "prompt", settings.max_chars),
                                *(length_problem(c[2], f"example ({c[0] or 'unnamed'})", settings.max_chars)
                                  for c in cases)) if m]
        if too_long:
            ctx["errors"] = too_long
            return page(request, "suite_new.html", status=400, **ctx)

        if action == "draft":
            def fill(drafted: list[dict]) -> None:
                for i in range(1, 6):
                    c = drafted[i - 1] if i <= len(drafted) else {}
                    f[f"crit_name_{i}"] = c.get("name", "")
                    f[f"crit_five_{i}"] = c.get("five_looks_like", "")
                    f[f"crit_one_{i}"] = c.get("one_looks_like", "")
                    f[f"crit_deal_{i}"] = "1" if c.get("deal_breaker") else ""

            if not settings.is_live:
                fill(SAMPLE_CRITERIA)
                if not f.get("name", ""):
                    f["name"], f["who_uses_it"] = SAMPLE_SUITE_NAME, f.get("who_uses_it", "") or SAMPLE_WHO
                ctx["message"] = ("Here are sample qualities for a Renewal risk summary, with two suggested "
                                  "deal-breakers. Edit them to fit your prompt.")
                return page(request, "suite_new.html", **ctx)
            if not f.get("prompt_text", "").strip():
                ctx["errors"] = ["Paste your prompt in step 2 first, so Claude knows what to suggest qualities for."]
                return page(request, "suite_new.html", status=400, **ctx)
            if spam:
                return page(request, "suite_new.html", **ctx)
            if not take_run(request):
                ctx["errors"] = [public.USED_UP]
                return page(request, "suite_new.html", status=429, **ctx)
            try:
                drafted = draft_criteria(make_provider(), settings.judge_model, f["prompt_text"], [c[2] for c in cases])
            except ProviderError as exc:
                ctx["errors"] = [str(exc)]
                return page(request, "suite_new.html", status=502, **ctx)
            fill(drafted)
            ctx["message"] = ("Claude suggested these qualities and marked the ones it thinks are deal-breakers. "
                              "Read them and edit anything that doesn't fit.")
            return page(request, "suite_new.html", **ctx)

        if not settings.is_live:
            ctx["errors"] = [READ_ONLY]
            return page(request, "suite_new.html", status=403, **ctx)
        errors = []
        if not f.get("name", "").strip():
            errors.append("Give the prompt a name in step 1.")
        if not f.get("prompt_text", "").strip():
            errors.append("Paste the prompt your team uses today in step 2.")
        if not cases:
            errors.append("Add at least one example in step 3. Three is a good start.")
        for name, _label, text, _k in cases:
            if not name or not text:
                errors.append(f"Each example needs a name and the input itself ({name or 'unnamed'}).")
        rubric, used = [], set()
        for i in range(1, 6):
            parts = [f.get(f"crit_{k}_{i}", "").strip() for k in ("name", "five", "one")]
            if not any(parts):
                continue
            if not all(parts):
                errors.append(f"Quality {i} needs a name, what a 5 looks like and what a 1 looks like.")
                continue
            cid = slugify(parts[0]).replace("-", "_")
            while cid in used:
                cid += "_2"
            used.add(cid)
            rubric.append(Criterion(id=cid, name=parts[0], five_looks_like=parts[1], one_looks_like=parts[2],
                                    deal_breaker=bool(f.get(f"crit_deal_{i}"))))
        if not rubric:
            errors.append("Describe at least one quality in step 4, or use \"Suggest qualities for me\".")
        checks = []
        if f.get("max_words", "").strip():
            if not f["max_words"].strip().isdigit() or int(f["max_words"]) < 1:
                errors.append("The word limit should be a whole number, like 150.")
            else:
                checks.append(Check(type="max_words", value=int(f["max_words"]), label=f"Under {int(f['max_words'])} words"))
        any_of = [p.strip() for p in f.get("must_contain_any", "").split(",") if p.strip()]
        if any_of:
            checks.append(Check(type="must_contain_any", value=any_of))
        banned = []
        for p in f.get("must_not_contain", "").split(","):
            p = p.strip()
            if re.fullmatch(r"em[ -]?dash(es)?", p, flags=re.I):
                banned.append("\u2014")
            elif p:
                banned.append(p)
        if banned:
            checks.append(Check(type="must_not_contain", value=banned))
        if errors:
            ctx["errors"] = errors
            return page(request, "suite_new.html", status=400, **ctx)
        if spam:
            return RedirectResponse("/prompts", status_code=303)
        if not take_run(request):
            ctx["errors"] = [public.USED_UP]
            return page(request, "suite_new.html", status=429, **ctx)
        try:
            suite = Suite(name=f["name"].strip(), team=f.get("team", "").strip(),
                          who_uses_it=f.get("who_uses_it", "").strip(),
                          model=config.DEFAULT_MODEL, rubric=rubric, checks=checks)
        except ValidationError as exc:
            ctx["errors"] = [e["msg"] for e in exc.errors()]
            return page(request, "suite_new.html", status=400, **ctx)
        new_cases, taken = [], set()
        for name, label, text, kind in cases:
            cid, n = slugify(f"{label} {name}" if label else name), 2
            while cid in taken:
                cid, n = f"{cid}-{n}", n + 1
            taken.add(cid)
            new_cases.append(Case(id=cid, name=name, label=label, kind=kind, input=text))
        suite_id = st.create_suite(suite, new_cases, f["prompt_text"].strip(), note="The prompt the team uses today.")
        # Version 1 is recorded as in use by the owner. In public mode the name is "you".
        releases.StoreLog(st, suite_id).append(releases.baseline_event("v1", "you" if settings.is_public else ""))
        job = start_job(request, "Scoring version 1 to set the starting point", f"/prompts/{suite_id}?created=1",
                        run_versions(st, suite_id, ["v1"], make_provider(), settings.judge_model))
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    @app.get("/prompts/{suite_id}", response_class=HTMLResponse)
    def suite_page(request: Request, suite_id: str, created: str = "", saved: str = "", put_back: str = ""):
        message = ("Saved. Version 1 is scored, so every version you try is compared with it." if created
                   else "Saved." if saved else None)
        return suite_view(request, suite_id, message=message, put_back=put_back)

    def last_test(request: Request, st: Store, suite_id: str, version_id: str) -> tuple[str, str] | None:
        """(the version it was compared with, the date) for this version's latest complete test."""
        best = None
        if settings.is_live:
            for rid in reversed(st.evaluation_ids(suite_id)):
                rec = st.load_evaluation(suite_id, rid)
                if rec and rec.candidate.id == version_id and rec.status == "complete":
                    best = (rec.baseline.id, rec.finished)
                    break
        if best is None:
            for e in reversed(release_history(request, suite_id)):
                pair = evaluate.parse_recorded_id(e.evaluation_id)
                if e.to_version == version_id and pair:
                    rec = evaluate.recorded_evidence(st, suite_id, *pair).record
                    best = (rec.baseline.id, rec.finished)
                    break
        return best

    def suite_view(request: Request, suite_id: str, status: int = 200, message: str | None = None,
                   put_back: str = "", **extra):
        st = store_for(request)
        try:
            suite = st.load_suite(suite_id)
            versions, cases = st.versions(suite_id), st.load_cases(suite_id)
            events = release_history(request, suite_id)
        except SuiteError as exc:
            return problem(request, str(exc), status=404, heading="Not found")
        use = in_use(request, suite, versions)
        statuses = {v.id: releases.status(v.id, events, use) for v in versions}
        tested = {v.id: last_test(request, st, suite_id, v.id) for v in versions if statuses[v.id] == "Retired"}
        return page(request, "suite.html", status=status, nav="prompts", suite=suite, versions=versions,
                    cases=cases, in_use=use, statuses=statuses, since=releases.since(events, use),
                    tested=tested, events=list(reversed(events)), put_back=put_back, token=releases.new_token(),
                    message=message, error=None, nudge=needs_harder_examples(cases), mix=example_mix(cases),
                    next_url=f"/prompts/{suite_id}?saved=1#add-example", **extra)

    def _kind(value: str) -> str:
        return value if value in CASE_KINDS else "everyday"

    def _case_fields(form) -> tuple[str, str, str, str, str | None]:
        name, label, text = (str(form.get(k, "")).strip() for k in ("name", "label", "input"))
        err = length_problem(text, "example", settings.max_chars)
        if not err and (not name or not text):
            err = "An example needs a name and an input."
        return name, label, text, _kind(str(form.get("kind", ""))), err

    def _safe_next(url: str, fallback: str) -> str:
        return url if url.startswith("/") and not url.startswith("//") else fallback

    @app.post("/prompts/{suite_id}/examples")
    async def add_case(request: Request, suite_id: str):
        form = await request.form()
        if not settings.is_live:
            if wants_json(request):
                return JSONResponse({"ok": False, "error": READ_ONLY}, status_code=403)
            return problem(request, READ_ONLY, status=403, heading="Demo", info=True)
        st = store_for(request)
        name, label, text, kind, err = _case_fields(form)
        if not err and settings.is_public:
            try:
                if len(st.load_cases(suite_id)) >= config.PUBLIC_MAX_EXAMPLES:
                    err = (f"Each prompt can have up to {config.PUBLIC_MAX_EXAMPLES} examples here, "
                           "and this one is full.")
            except SuiteError as exc:
                return problem(request, str(exc), status=404)
        if err:
            if wants_json(request):
                return JSONResponse({"ok": False, "error": err}, status_code=400)
            return problem(request, err, back_url=f"/prompts/{suite_id}", back_text="Back to the prompt")
        try:
            case = st.add_case(suite_id, name, label, text, kind)
        except SuiteError as exc:
            return problem(request, str(exc), status=404)
        if wants_json(request):
            suite = st.load_suite(suite_id)
            cases = st.load_cases(suite_id)
            against = str(form.get("against", ""))
            if against not in {v.id for v in st.versions(suite_id)}:
                # Count the version in use too, so the estimate never comes in low.
                against = in_use(request, suite, st.versions(suite_id))
            estimate = test_estimate(st, suite, cases, against)[1]
            return {"ok": True, "case": case.model_dump(), "kind_label": case.kind_label, "count": len(cases),
                    "estimate": estimate,
                    "nudge": needs_harder_examples(cases)}
        return RedirectResponse(_safe_next(str(form.get("next", "")), f"/prompts/{suite_id}?saved=1"), status_code=303)

    @app.post("/prompts/{suite_id}/examples/{case_id}")
    async def edit_case(request: Request, suite_id: str, case_id: str):
        form = await request.form()
        if not settings.is_live:
            return problem(request, READ_ONLY, status=403, heading="Demo", info=True)
        name, label, text, kind, err = _case_fields(form)
        if err:
            return problem(request, err, back_url=f"/prompts/{suite_id}", back_text="Back to the prompt")
        try:
            store_for(request).update_case(suite_id, case_id, name, label, text, kind)
        except SuiteError as exc:
            return problem(request, str(exc), status=404)
        return RedirectResponse(f"/prompts/{suite_id}?saved=1", status_code=303)

    @app.get("/how-it-works", response_class=HTMLResponse)
    def how_it_works(request: Request):
        return page(request, "scoring.html", nav="how")

    # old addresses from the first version ---------------------------------------
    @app.get("/scoring")
    def old_scoring():
        return RedirectResponse("/how-it-works", status_code=301)

    @app.get("/suites")
    def old_suites():
        return RedirectResponse("/prompts", status_code=301)

    @app.get("/suites/{rest:path}")
    def old_suite_pages(request: Request, rest: str):
        rest = re.sub(r"^([^/]+)/cases/", r"\1/examples/", rest)
        query = f"?{request.query_params}" if request.query_params else ""
        return RedirectResponse(f"/prompts/{rest}{query}", status_code=301)

    # demo mode: the cookie that holds a visitor's changes is full ----------------------
    @app.exception_handler(demo_session.CookieFull)
    async def cookie_full(request: Request, exc: demo_session.CookieFull):
        return problem(request, str(exc), status=409, heading="No room for more changes", info=True,
                       start_over=True)

    @app.post("/demo/hide-strip")
    async def hide_strip(request: Request):
        """Hide the demo note for the rest of this browser session."""
        form = await request.form()
        request.session["strip_hidden"] = 1
        return RedirectResponse(_safe_next(str(form.get("next", "")), "/"), status_code=303)

    @app.post("/demo/start-over")
    async def start_over(request: Request):
        """Clear this visit's demo changes: overrides and releases. Nothing on disk is touched."""
        demo_session.start_over(request.session)
        return RedirectResponse("/", status_code=303)

    @app.get("/healthz")
    def health() -> Response:
        return PlainTextResponse("ok")

    return app


app = create_app()
