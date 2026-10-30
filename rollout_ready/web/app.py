"""The web app. Server-rendered pages; every form works without JavaScript.

Pages use the plain words from CLAUDE.md: a suite is a "prompt", a test case
is an "example", the judge is the "AI reviewer". Code and files keep the
technical names.
"""

from __future__ import annotations

import os
import re
import secrets
from datetime import date, datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.middleware.sessions import SessionMiddleware

from .. import config, scoring
from ..compare import (MIX_NUDGE, Comparison, build_comparison, example_mix, load_results,
                       needs_harder_examples, summary_text)
from ..config import Settings, length_problem, load_settings
from ..demo_criteria import SAMPLE_CRITERIA, SAMPLE_SUITE_NAME, SAMPLE_WHO
from ..judge import draft_criteria
from ..models import CASE_KINDS, Case, Check, Criterion, Override, Suite, Version
from ..provider import ClaudeProvider, Provider, ProviderError
from ..runner import JobManager, estimate_sentence, run_versions
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
    scoring.KEEP_CURRENT: "is not ready, so keep the version your team uses now",
}


def _when(stamp: str) -> str:
    if not stamp:
        return "Not recorded"
    try:
        dt = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return stamp
    return dt.strftime("%B %-d, %Y at %H:%M UTC")


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
    # Demo overrides live in a signed cookie that ends with the browser session (max_age=None).
    secret = os.environ.get("ROLLOUT_SECRET_KEY", "").strip() or secrets.token_urlsafe(32)
    app.add_middleware(SessionMiddleware, secret_key=secret, session_cookie="rollout_ready",
                       max_age=None, same_site="lax")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(
        settings=settings, fmt=scoring.fmt, fmt_change=scoring.fmt_change,
        fmt_crit=scoring.fmt_criterion, when=_when, vname=_vname, plural=_plural,
        result_labels=scoring.RESULT_LABELS, kinds=CASE_KINDS, calls=CALLS,
        mix_nudge=MIX_NUDGE, cutoff=scoring.DEAL_BREAKER_CUTOFF,
    )
    app.state.settings, app.state.store, app.state.jobs = settings, store, jobs

    def page(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    def problem(request: Request, message: str, status: int = 400, heading: str = "Something needs fixing", **kw):
        return page(request, "error.html", status=status, heading=heading, message=message, **kw)

    def wants_json(request: Request) -> bool:
        return request.headers.get("x-requested-with") == "fetch"

    def in_use(suite: Suite, versions) -> str:
        ids = [v.id for v in versions]
        return suite.in_use if suite.in_use in ids else (ids[-1] if ids else "")

    def ordered(suites: list[Suite]) -> list[Suite]:
        """The featured prompt first, then by team and name."""
        return sorted(suites, key=lambda s: (s.id != config.FEATURED_SUITE, s.team.lower() or "~", s.name.lower()))

    def default_pair(suite: Suite, versions: list[Version]) -> tuple[str, str] | None:
        vids = [v.id for v in versions]
        if not vids:
            return None
        if suite.id == config.FEATURED_SUITE and {config.FEATURED_CURRENT, config.FEATURED_NEW} <= set(vids):
            return config.FEATURED_CURRENT, config.FEATURED_NEW
        current = in_use(suite, versions)
        if current == vids[-1] and len(vids) > 1:
            current = vids[-2]
        later = [v for v in vids if int(v[1:]) > int(current[1:])]
        return current, (later[-1] if later else vids[-1])

    def latest_pair(suites: list[Suite]) -> tuple[str, str, str] | None:
        """The result the start screen and See results open on.

        Demo mode always shows the featured comparison. Live mode picks the
        prompt whose newest version ran most recently.
        """
        ids = [s.id for s in suites]
        if not settings.is_live and config.FEATURED_SUITE in ids:
            suite = next(s for s in suites if s.id == config.FEATURED_SUITE)
            pair = default_pair(suite, store.versions(suite.id))
            if pair:
                return (suite.id, *pair)
        best = None
        for s in suites:
            pair = default_pair(s, store.versions(s.id))
            if not pair or pair[0] == pair[1]:
                continue
            ran = load_results(store, s.id, pair[1], settings.is_live).ran_at
            if best is None or ran > best[0]:
                best = (ran, s.id, *pair)
        return best[1:] if best else None

    # overrides: files in live mode, a signed session cookie in demo mode -----
    def _cookie_key(suite_id: str, current: str, new: str) -> str:
        return f"{suite_id}|{current}|{new}"

    def get_overrides(request: Request, suite_id: str, current: str, new: str) -> dict[str, Override]:
        if settings.is_live:
            return store.load_overrides(suite_id, current, new)
        raw = request.session.get("overrides", {}).get(_cookie_key(suite_id, current, new), {})
        out = {}
        for cid, data in raw.items():
            try:
                out[cid] = Override.model_validate(data)
            except ValidationError:
                continue
        return out

    def put_override(request: Request, suite_id: str, current: str, new: str, case_id: str, o: Override | None):
        if settings.is_live:
            if o:
                store.save_override(suite_id, current, new, case_id, o)
            else:
                store.remove_override(suite_id, current, new, case_id)
            return
        all_o = dict(request.session.get("overrides", {}))
        pair = dict(all_o.get(_cookie_key(suite_id, current, new), {}))
        if o:
            pair[case_id] = o.model_dump()
        else:
            pair.pop(case_id, None)
        if pair:
            all_o[_cookie_key(suite_id, current, new)] = pair
        else:
            all_o.pop(_cookie_key(suite_id, current, new), None)
        request.session["overrides"] = all_o

    def comparison(request: Request, suite_id: str, current: str, new: str) -> Comparison:
        overrides = get_overrides(request, suite_id, current, new) if current != new else {}
        return build_comparison(store, suite_id, current, new, settings.is_live, overrides)

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
        try:
            suites = ordered(store.suites())
        except SuiteError as exc:
            return problem(request, str(exc), status=500)
        latest, banner = None, ""
        pick = latest_pair(suites)
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
            for v in store.versions(s.id):
                if load_results(store, s.id, v.id, settings.is_live).cases:
                    saved += 1
        teams = [s.team.lower() for s in suites if s.team]
        prompts_meta = _plural(len(suites), "prompt") + (": " + ", ".join(dict.fromkeys(teams)) if teams else "")
        return page(request, "home.html", nav="home", latest=latest, banner=banner,
                    saved_meta=_plural(saved, "saved result"), prompts_meta=prompts_meta)

    # results -----------------------------------------------------------
    @app.get("/results", response_class=HTMLResponse)
    def results(request: Request, suite: str = "", current: str = "", new: str = "", tab: str = "examples",
                example: str = ""):
        suites = ordered(store.suites())
        if not suites:
            return problem(request, "There are no prompts yet.", heading="Nothing to show yet",
                           back_url="/prompts/new", back_text="Add a new prompt")
        ids = [s.id for s in suites]
        if suite not in ids:
            pick = latest_pair(suites)
            suite = pick[0] if pick else ids[0]
            if pick and not current and not new:
                current, new = pick[1], pick[2]
        the_suite = next(s for s in suites if s.id == suite)
        versions = store.versions(suite)
        vids = [v.id for v in versions]
        if not vids:
            return problem(request, "This prompt has no versions yet.", status=404)
        pair = default_pair(the_suite, versions)
        if current not in vids:
            current = pair[0]
        if new not in vids:
            later = [v for v in vids if int(v[1:]) > int(current[1:])]
            new = later[-1] if later else vids[-1]
        tab = OLD_TABS.get(tab, tab)
        tab = tab if tab in TABS else "examples"
        ctx = dict(nav="results", suites=suites, suite=the_suite, versions=versions,
                   current_id=current, new_id=new, tab=tab, example=example == "1",
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
        return page(request, "compare.html", **ctx)

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
        message = {"1": "Saved. This example now counts the way you said, for these two versions only.",
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
        put_override(request, suite_id, current, new, case_id,
                     Override(call=call, result=result, reason=reason, created=date.today().isoformat()))
        return RedirectResponse(example_url(suite_id, case_id, current, new, saved="1") + "#your-call",
                                status_code=303)

    @app.post("/prompts/{suite_id}/examples/{case_id}/override/remove")
    async def remove_override(request: Request, suite_id: str, case_id: str):
        form = await request.form()
        current, new = str(form.get("current", "")), str(form.get("new", ""))
        try:
            store.load_version(suite_id, current), store.load_version(suite_id, new)
        except SuiteError as exc:
            return problem(request, str(exc), status=404, heading="Not found")
        put_override(request, suite_id, current, new, case_id, None)
        return RedirectResponse(example_url(suite_id, case_id, current, new, saved="removed") + "#your-call",
                                status_code=303)

    # test a change ---------------------------------------------------------
    def test_context(suite_id: str, against: str) -> dict:
        suites = ordered(store.suites())
        ids = [s.id for s in suites]
        if not settings.is_live:
            suite_id, against = config.FEATURED_SUITE, config.FEATURED_CURRENT
        suite_id = suite_id if suite_id in ids else (config.FEATURED_SUITE if config.FEATURED_SUITE in ids else ids[0])
        suite = next(s for s in suites if s.id == suite_id)
        versions = store.versions(suite_id)
        use = in_use(suite, versions)
        against_v = next((v for v in versions if v.id == against), None) or next(v for v in versions if v.id == use)
        cases = store.load_cases(suite_id)
        ctx = dict(nav="test", suites=suites if settings.is_live else [suite], suite=suite,
                   versions=versions if settings.is_live else [against_v],
                   against=against_v, in_use=use, cases=cases, error=None, prompt_text="", note="",
                   nudge=needs_harder_examples(cases), mix=example_mix(cases),
                   estimate=estimate_sentence(len(cases), suite.repeats),
                   next_url="/test?" + urlencode({"suite": suite_id, "against": against_v.id}) + "#add-example")
        if not settings.is_live:
            example = store.load_version(suite_id, config.SAVED_EXAMPLE_VERSION)
            ctx.update(prompt_text=example.text, note=example.note,
                       estimate=f"The saved example replays {len(cases)} recorded results, so it takes a moment and makes no calls to Claude.")
        return ctx

    @app.get("/test", response_class=HTMLResponse)
    def test_form(request: Request, suite: str = "", against: str = ""):
        try:
            return page(request, "test.html", **test_context(suite, against))
        except SuiteError as exc:
            return problem(request, str(exc), status=404)

    @app.post("/test")
    async def test_run(request: Request):
        form = await request.form()
        if not settings.is_live:
            return RedirectResponse(results_url(config.FEATURED_SUITE, config.FEATURED_CURRENT,
                                                config.SAVED_EXAMPLE_VERSION, example="1"), status_code=303)
        suite_id, against = str(form.get("suite", "")), str(form.get("against", ""))
        text, note = str(form.get("prompt_text", "")).strip(), str(form.get("note", "")).strip()
        try:
            ctx = test_context(suite_id, against)
        except SuiteError as exc:
            return problem(request, str(exc), status=404)
        ctx.update(prompt_text=text, note=note)
        err = (length_problem(text, "prompt") or length_problem(note, "note")
               or (None if text else "Paste the version you're trying first.")
               or (None if note else "Add a line about what changed and why, so you can tell versions apart later."))
        if err:
            ctx["error"] = err
            return page(request, "test.html", status=400, **ctx)
        suite_id, against = ctx["suite"].id, ctx["against"].id
        version = store.add_version(suite_id, text, note)
        done = results_url(suite_id, against, version.id)
        job = jobs.start(f"Testing {_vname(version.id)} against {_vname(against)}", done,
                         run_versions(store, suite_id, [against, version.id], make_provider(), settings.judge_model))
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    @app.post("/run")
    async def run_missing(request: Request):
        form = await request.form()
        if not settings.is_live:
            return problem(request, READ_ONLY, status=403, heading="Demo", info=True)
        suite_id, current, new = (str(form.get(k, "")) for k in ("suite", "current", "new"))
        try:
            store.load_version(suite_id, current), store.load_version(suite_id, new)
        except SuiteError as exc:
            return problem(request, str(exc), status=404)
        job = jobs.start(f"Running the missing examples for {_vname(current)} and {_vname(new)}",
                         results_url(suite_id, current, new),
                         run_versions(store, suite_id, [current, new], make_provider(), settings.judge_model))
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str):
        job = jobs.get(job_id)
        if not job:
            return problem(request, "That run has finished or the server restarted. Saved results are kept.",
                           status=404, heading="Run not found")
        if job.finished and not job.error:
            return RedirectResponse(job.done_url, status_code=303)
        return page(request, "job.html", nav="test", job=job)

    @app.get("/jobs/{job_id}/status")
    def job_status(job_id: str):
        job = jobs.get(job_id)
        if not job:
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"done": job.done, "total": job.total, "percent": job.percent, "message": job.message,
                "finished": job.finished, "error": job.error, "done_url": job.done_url}

    # your prompts and their examples -----------------------------------------
    @app.get("/prompts", response_class=HTMLResponse)
    def prompts_page(request: Request):
        items, error = [], None
        for sid in store.suite_ids():
            try:
                items.append({"suite": store.load_suite(sid), "cases": store.load_cases(sid), "versions": store.versions(sid)})
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
        cases = [
            (f.get(f"case_name_{i}", "").strip(), f.get(f"case_label_{i}", "").strip(),
             f.get(f"case_input_{i}", "").strip(), _kind(f.get(f"case_kind_{i}", "")))
            for i in range(1, 9)
        ]
        cases = [c for c in cases if c[0] or c[2]]
        ctx = new_prompt_ctx(f, max(len(cases), 1))
        too_long = [m for m in (length_problem(f.get("prompt_text", ""), "prompt"),
                                *(length_problem(c[2], f"example ({c[0] or 'unnamed'})") for c in cases)) if m]
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
        suite_id = store.create_suite(suite, new_cases, f["prompt_text"].strip(), note="The prompt the team uses today.")
        job = jobs.start("Scoring version 1 to set the starting point", f"/prompts/{suite_id}?created=1",
                         run_versions(store, suite_id, ["v1"], make_provider(), settings.judge_model))
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    @app.get("/prompts/{suite_id}", response_class=HTMLResponse)
    def suite_page(request: Request, suite_id: str, created: str = "", saved: str = ""):
        try:
            suite = store.load_suite(suite_id)
            versions, cases = store.versions(suite_id), store.load_cases(suite_id)
        except SuiteError as exc:
            return problem(request, str(exc), status=404, heading="Not found")
        message = ("Saved. Version 1 is scored, so every version you try is compared with it." if created
                   else "Saved." if saved else None)
        return page(request, "suite.html", nav="prompts", suite=suite, versions=versions, cases=cases,
                    in_use=in_use(suite, versions), message=message, error=None,
                    nudge=needs_harder_examples(cases), mix=example_mix(cases),
                    next_url=f"/prompts/{suite_id}?saved=1#add-example")

    def _kind(value: str) -> str:
        return value if value in CASE_KINDS else "everyday"

    def _case_fields(form) -> tuple[str, str, str, str, str | None]:
        name, label, text = (str(form.get(k, "")).strip() for k in ("name", "label", "input"))
        err = length_problem(text, "example")
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
        name, label, text, kind, err = _case_fields(form)
        if err:
            if wants_json(request):
                return JSONResponse({"ok": False, "error": err}, status_code=400)
            return problem(request, err, back_url=f"/prompts/{suite_id}", back_text="Back to the prompt")
        try:
            case = store.add_case(suite_id, name, label, text, kind)
        except SuiteError as exc:
            return problem(request, str(exc), status=404)
        if wants_json(request):
            suite = store.load_suite(suite_id)
            cases = store.load_cases(suite_id)
            return {"ok": True, "case": case.model_dump(), "kind_label": case.kind_label, "count": len(cases),
                    "estimate": estimate_sentence(len(cases), suite.repeats),
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
            store.update_case(suite_id, case_id, name, label, text, kind)
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

    @app.get("/healthz")
    def health() -> Response:
        return PlainTextResponse("ok")

    return app


app = create_app()
