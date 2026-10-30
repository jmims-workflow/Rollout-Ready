"""The web app. Server-rendered pages; every form works without JavaScript."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from .. import config, scoring
from ..compare import build_comparison, summary_text
from ..config import Settings, length_problem, load_settings
from ..demo_criteria import SAMPLE_CRITERIA, SAMPLE_SUITE_NAME, SAMPLE_WHO
from ..judge import draft_criteria
from ..models import Case, Check, Criterion, Suite
from ..provider import ClaudeProvider, Provider, ProviderError
from ..runner import JobManager, estimate_sentence, run_versions
from ..storage import Store, SuiteError, slugify

HERE = Path(__file__).resolve().parent
TABS = ("cases", "rubric", "changes", "details")
READ_ONLY = "Demo mode is read-only, so nothing new can be saved. Set ROLLOUT_MODE=live with an API key to save your own work."


def _when(stamp: str) -> str:
    if not stamp:
        return "Not recorded"
    try:
        dt = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return stamp
    return dt.strftime("%B %-d, %Y at %H:%M UTC")


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
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(
        settings=settings, fmt=scoring.fmt, fmt_change=scoring.fmt_change,
        fmt_crit=scoring.fmt_criterion, when=_when,
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

    # compare -----------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def compare(request: Request, suite: str = "", current: str = "", new: str = "", tab: str = "cases", example: str = ""):
        suites = store.suites()
        if not suites:
            return problem(request, "There are no prompt suites yet.", heading="Nothing to compare",
                           back_url="/suites/new", back_text="Set up a new suite")
        ids = [s.id for s in suites]
        suite_id = suite if suite in ids else (config.FEATURED_SUITE if config.FEATURED_SUITE in ids else ids[0])
        the_suite = next(s for s in suites if s.id == suite_id)
        versions = store.versions(suite_id)
        vids = [v.id for v in versions]
        if not vids:
            return problem(request, "This suite has no versions yet.", status=404)
        if not current and not new and suite_id == config.FEATURED_SUITE and {config.FEATURED_CURRENT, config.FEATURED_NEW} <= set(vids):
            current, new = config.FEATURED_CURRENT, config.FEATURED_NEW
        if current not in vids:
            current = in_use(the_suite, versions)
            if current == vids[-1] and len(vids) > 1:
                current = vids[-2]
        if new not in vids:
            later = [v for v in vids if int(v[1:]) > int(current[1:])]
            new = later[-1] if later else vids[-1]
        tab = tab if tab in TABS else "cases"
        ctx = dict(nav="compare", suites=suites, suite=the_suite, versions=versions,
                   current_id=current, new_id=new, tab=tab, example=example == "1",
                   base_q="/?" + urlencode({"suite": suite_id, "current": current, "new": new}),
                   comp=None, error=None)
        if current == new:
            return page(request, "compare.html", **ctx)
        try:
            comp = build_comparison(store, suite_id, current, new, settings.is_live)
        except SuiteError as exc:
            ctx["error"] = str(exc)
            return page(request, "compare.html", **ctx)
        ctx["comp"] = comp
        if comp.complete:
            ctx["summary"] = summary_text(comp)
        else:
            ctx["estimate"] = estimate_sentence(sum(len(v) for v in comp.missing.values()), the_suite.repeats)
        return page(request, "compare.html", **ctx)

    @app.get("/summary.txt")
    def summary(suite: str, current: str, new: str):
        try:
            comp = build_comparison(store, suite, current, new, settings.is_live)
        except SuiteError as exc:
            return PlainTextResponse(str(exc), status_code=404)
        if not comp.complete:
            return PlainTextResponse("Some results are missing, so there is no summary yet.", status_code=409)
        name = f"rollout-ready-{suite}-{current}-vs-{new}.txt"
        return PlainTextResponse(summary_text(comp), headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/suites/{suite_id}/cases/{case_id}", response_class=HTMLResponse)
    def case_detail(request: Request, suite_id: str, case_id: str, current: str = "", new: str = ""):
        try:
            comp = build_comparison(store, suite_id, current, new, settings.is_live)
        except SuiteError as exc:
            return problem(request, str(exc), status=404, heading="Not found")
        row = comp.row(case_id)
        if row is None:
            return problem(request, "There are no saved results for this test case in both versions yet.",
                           status=404, heading="No results yet",
                           back_url=f"/?suite={suite_id}&current={current}&new={new}")
        return page(request, "case.html", nav="compare", suite=comp.suite, row=row, current_id=current, new_id=new)

    # test a new version ---------------------------------------------------
    def test_context(suite_id: str, against: str) -> dict:
        suites = store.suites()
        ids = [s.id for s in suites]
        if not settings.is_live:
            suite_id, against = config.FEATURED_SUITE, config.FEATURED_CURRENT
        suite_id = suite_id if suite_id in ids else (config.FEATURED_SUITE if config.FEATURED_SUITE in ids else ids[0])
        suite = next(s for s in suites if s.id == suite_id)
        versions = store.versions(suite_id)
        use = in_use(suite, versions)
        against_v = next((v for v in versions if v.id == against), None) or next(v for v in versions if v.id == use)
        cases = store.load_cases(suite_id)
        ctx = dict(nav="test", suites=suites if settings.is_live else [suite], suite=suite, versions=versions if settings.is_live else [against_v],
                   against=against_v, in_use=use, cases=cases, error=None, prompt_text="", note="",
                   estimate=estimate_sentence(len(cases), suite.repeats),
                   next_url="/test?" + urlencode({"suite": suite_id, "against": against_v.id}) + "#add-case")
        if not settings.is_live:
            example = store.load_version(suite_id, config.SAVED_EXAMPLE_VERSION)
            ctx.update(prompt_text=example.text, note=example.note,
                       estimate="The saved example replays 8 recorded results, so it takes a moment and makes no calls to Claude.")
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
            q = urlencode({"suite": config.FEATURED_SUITE, "current": config.FEATURED_CURRENT,
                           "new": config.SAVED_EXAMPLE_VERSION, "example": "1"})
            return RedirectResponse(f"/?{q}", status_code=303)
        suite_id, against = str(form.get("suite", "")), str(form.get("against", ""))
        text, note = str(form.get("prompt_text", "")).strip(), str(form.get("note", "")).strip()
        try:
            ctx = test_context(suite_id, against)
        except SuiteError as exc:
            return problem(request, str(exc), status=404)
        ctx.update(prompt_text=text, note=note)
        err = (length_problem(text, "prompt") or length_problem(note, "note")
               or (None if text else "Paste your new version first.")
               or (None if note else "Add a line about what changed and why, so you can tell versions apart later."))
        if err:
            ctx["error"] = err
            return page(request, "test.html", status=400, **ctx)
        suite_id, against = ctx["suite"].id, ctx["against"].id
        version = store.add_version(suite_id, text, note)
        done = "/?" + urlencode({"suite": suite_id, "current": against, "new": version.id, "tab": "cases"})
        job = jobs.start(f"Testing {version.id} against {against}", done,
                         run_versions(store, suite_id, [against, version.id], make_provider(), settings.judge_model))
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    @app.post("/run")
    async def run_missing(request: Request):
        form = await request.form()
        if not settings.is_live:
            return problem(request, READ_ONLY, status=403, heading="Demo mode", info=True)
        suite_id, current, new = (str(form.get(k, "")) for k in ("suite", "current", "new"))
        try:
            store.load_version(suite_id, current), store.load_version(suite_id, new)
        except SuiteError as exc:
            return problem(request, str(exc), status=404)
        done = "/?" + urlencode({"suite": suite_id, "current": current, "new": new})
        job = jobs.start(f"Running the missing tests for {current} and {new}", done,
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

    # suites and test cases ----------------------------------------------------
    @app.get("/suites", response_class=HTMLResponse)
    def suites_page(request: Request):
        items, error = [], None
        for sid in store.suite_ids():
            try:
                items.append({"suite": store.load_suite(sid), "cases": store.load_cases(sid), "versions": store.versions(sid)})
            except SuiteError as exc:
                error = str(exc)
        return page(request, "suites.html", nav="suites", items=items, error=error)

    @app.get("/suites/new", response_class=HTMLResponse)
    def new_suite_form(request: Request):
        return page(request, "suite_new.html", nav="suites", f={}, errors=[], message=None,
                    estimate=estimate_sentence(3, 1))

    @app.post("/suites/new", response_class=HTMLResponse)
    async def new_suite_submit(request: Request):
        form = await request.form()
        f = {k: str(v) for k, v in form.items()}
        action = f.pop("action", "save")
        cases = [
            (f.get(f"case_name_{i}", "").strip(), f.get(f"case_label_{i}", "").strip(), f.get(f"case_input_{i}", "").strip())
            for i in range(1, 9)
        ]
        cases = [c for c in cases if c[0] or c[2]]
        ctx = dict(nav="suites", f=f, errors=[], message=None, estimate=estimate_sentence(max(len(cases), 1), 1))
        too_long = [m for m in (length_problem(f.get("prompt_text", ""), "prompt"),
                                *(length_problem(c[2], f"test input ({c[0] or 'unnamed'})") for c in cases)) if m]
        if too_long:
            ctx["errors"] = too_long
            return page(request, "suite_new.html", status=400, **ctx)

        if action == "draft":
            if not settings.is_live:
                for i, c in enumerate(SAMPLE_CRITERIA, start=1):
                    f[f"crit_name_{i}"], f[f"crit_five_{i}"], f[f"crit_one_{i}"] = c["name"], c["five_looks_like"], c["one_looks_like"]
                f.setdefault("name", ""), f.setdefault("who_uses_it", "")
                if not f["name"]:
                    f["name"], f["who_uses_it"] = SAMPLE_SUITE_NAME, f["who_uses_it"] or SAMPLE_WHO
                ctx["message"] = "Here are sample criteria for a Renewal risk summary. Edit them to fit your prompt."
                return page(request, "suite_new.html", **ctx)
            if not f.get("prompt_text", "").strip():
                ctx["errors"] = ["Paste your prompt in step 2 first, so Claude knows what to draft criteria for."]
                return page(request, "suite_new.html", status=400, **ctx)
            try:
                drafted = draft_criteria(make_provider(), settings.judge_model, f["prompt_text"], [c[2] for c in cases])
            except ProviderError as exc:
                ctx["errors"] = [str(exc)]
                return page(request, "suite_new.html", status=502, **ctx)
            for i in range(1, 6):
                c = drafted[i - 1] if i <= len(drafted) else {"name": "", "five_looks_like": "", "one_looks_like": ""}
                f[f"crit_name_{i}"], f[f"crit_five_{i}"], f[f"crit_one_{i}"] = c["name"], c["five_looks_like"], c["one_looks_like"]
            ctx["message"] = "Claude drafted these criteria. Read them and edit anything that doesn't fit."
            return page(request, "suite_new.html", **ctx)

        if not settings.is_live:
            ctx["errors"] = [READ_ONLY]
            return page(request, "suite_new.html", status=403, **ctx)
        errors = []
        if not f.get("name", "").strip():
            errors.append("Give the suite a name in step 1.")
        if not f.get("prompt_text", "").strip():
            errors.append("Paste the prompt your team uses today in step 2.")
        if not cases:
            errors.append("Add at least one test input in step 3. Three is a good start.")
        for name, _label, text in cases:
            if not name or not text:
                errors.append(f"Each test input needs a name and the input itself ({name or 'unnamed'}).")
        rubric, used = [], set()
        for i in range(1, 6):
            parts = [f.get(f"crit_{k}_{i}", "").strip() for k in ("name", "five", "one")]
            if not any(parts):
                continue
            if not all(parts):
                errors.append(f"Criterion {i} needs a name, what a 5 looks like and what a 1 looks like.")
                continue
            cid = slugify(parts[0]).replace("-", "_")
            while cid in used:
                cid += "_2"
            used.add(cid)
            rubric.append(Criterion(id=cid, name=parts[0], five_looks_like=parts[1], one_looks_like=parts[2]))
        if not rubric:
            errors.append("Describe at least one criterion in step 4, or use \"Draft criteria for me\".")
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
            suite = Suite(name=f["name"].strip(), who_uses_it=f.get("who_uses_it", "").strip(),
                          model=config.DEFAULT_MODEL, rubric=rubric, checks=checks)
        except ValidationError as exc:
            ctx["errors"] = [e["msg"] for e in exc.errors()]
            return page(request, "suite_new.html", status=400, **ctx)
        new_cases, taken = [], set()
        for name, label, text in cases:
            cid, n = slugify(f"{label} {name}" if label else name), 2
            while cid in taken:
                cid, n = f"{cid}-{n}", n + 1
            taken.add(cid)
            new_cases.append(Case(id=cid, name=name, label=label, input=text))
        suite_id = store.create_suite(suite, new_cases, f["prompt_text"].strip(), note="The prompt the team uses today.")
        job = jobs.start("Scoring v1 to set the baseline", f"/suites/{suite_id}?created=1",
                         run_versions(store, suite_id, ["v1"], make_provider(), settings.judge_model))
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    @app.get("/suites/{suite_id}", response_class=HTMLResponse)
    def suite_page(request: Request, suite_id: str, created: str = "", saved: str = ""):
        try:
            suite = store.load_suite(suite_id)
            versions, cases = store.versions(suite_id), store.load_cases(suite_id)
        except SuiteError as exc:
            return problem(request, str(exc), status=404, heading="Not found")
        message = ("Saved. Version 1 is scored, so every new version you test is compared with it." if created
                   else "Saved." if saved else None)
        return page(request, "suite.html", nav="suites", suite=suite, versions=versions, cases=cases,
                    in_use=in_use(suite, versions), message=message, error=None,
                    next_url=f"/suites/{suite_id}?saved=1#add-case")

    def _case_fields(form) -> tuple[str, str, str, str | None]:
        name, label, text = (str(form.get(k, "")).strip() for k in ("name", "label", "input"))
        err = length_problem(text, "test input")
        if not err and (not name or not text):
            err = "A test case needs a name and an input."
        return name, label, text, err

    def _safe_next(url: str, fallback: str) -> str:
        return url if url.startswith("/") and not url.startswith("//") else fallback

    @app.post("/suites/{suite_id}/cases")
    async def add_case(request: Request, suite_id: str):
        form = await request.form()
        if not settings.is_live:
            if wants_json(request):
                return JSONResponse({"ok": False, "error": READ_ONLY}, status_code=403)
            return problem(request, READ_ONLY, status=403, heading="Demo mode", info=True)
        name, label, text, err = _case_fields(form)
        if err:
            if wants_json(request):
                return JSONResponse({"ok": False, "error": err}, status_code=400)
            return problem(request, err, back_url=f"/suites/{suite_id}")
        try:
            case = store.add_case(suite_id, name, label, text)
        except SuiteError as exc:
            return problem(request, str(exc), status=404)
        if wants_json(request):
            suite = store.load_suite(suite_id)
            count = len(store.load_cases(suite_id))
            return {"ok": True, "case": case.model_dump(), "count": count,
                    "estimate": estimate_sentence(count, suite.repeats)}
        return RedirectResponse(_safe_next(str(form.get("next", "")), f"/suites/{suite_id}?saved=1"), status_code=303)

    @app.post("/suites/{suite_id}/cases/{case_id}")
    async def edit_case(request: Request, suite_id: str, case_id: str):
        form = await request.form()
        if not settings.is_live:
            return problem(request, READ_ONLY, status=403, heading="Demo mode", info=True)
        name, label, text, err = _case_fields(form)
        if err:
            return problem(request, err, back_url=f"/suites/{suite_id}")
        try:
            store.update_case(suite_id, case_id, name, label, text)
        except SuiteError as exc:
            return problem(request, str(exc), status=404)
        return RedirectResponse(f"/suites/{suite_id}?saved=1", status_code=303)

    @app.get("/scoring", response_class=HTMLResponse)
    def scoring_page(request: Request):
        return page(request, "scoring.html", nav="scoring")

    @app.get("/healthz")
    def health() -> Response:
        return PlainTextResponse("ok")

    return app


app = create_app()
