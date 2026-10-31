# Rollout Ready: notes for future sessions

## What it is

A Python web app that lets the owner of a team's shared AI prompt test a change before the team sees it. They paste the version they're trying in the browser, it runs on saved examples, every output is scored (quick checks plus a blind AI reviewer), and the app gives one verdict. Tagline: "Test a prompt change before your team sees it." README opener: "The output reveals the gap."

Users are the people who own a shared prompt in sales, marketing and partnerships teams: enablement leads, marketing ops managers, team leads. They are not engineers and don't use GitHub, files or a terminal. Every screen must make sense to them without a glossary. The browser flow is the product; the CLI is secondary.

## Plain words (user-facing text)

Every word a user sees (pages, buttons, labels, verdict sentences, the copied summary, error messages, How it works) uses the right-hand column. Code, file formats, YAML keys, CLI output and the README's "Under the hood" section keep the technical names. Do not rename files, YAML keys or CLI commands. `tests/test_web.py` scans rendered pages for the technical words.

| Technical | On screen |
| --- | --- |
| suite | prompt (one saved prompt with its versions and examples) |
| test case | example |
| rubric | what good looks like |
| criterion | quality |
| judge | AI reviewer |
| automatic checks | quick checks |
| Worse / Better / No clear change | Got worse / Got better / About the same |
| current version / new version | the version your team uses now / the version you're trying |
| Compare page | See results (`/results`) |
| Test a new version page | Test a change (`/test`) |
| Suites page | Your prompts (`/prompts`) |
| How scoring works page | How it works (`/how-it-works`) |
| Run details tab | Behind the scenes |
| Prompt changes tab | What changed (diff labels Kept, Removed, Added) |
| Cases tab | Examples |
| Rubric tab | What good looks like |
| Mode pill | "Demo: sample results" or "Live: runs on Claude" |

Never show the 0.5 threshold as a number on a results screen; say "a move of half a point or more". The verdict names ("Keep the current version" etc.) stay as they are.

## Run and test

```bash
pip install -e ".[dev]"
rollout serve            # demo mode at http://127.0.0.1:8080
pytest
rollout run followup-email --current v2 --new v3   # exits 1 (Not ready)
```

Live mode: `ROLLOUT_MODE=live` plus `ANTHROPIC_API_KEY`. Public live mode adds `ROLLOUT_PUBLIC=true` (see below). Screenshots: run the app, then `node scripts/screenshots.js`.

## Layout

- `rollout_ready/scoring.py` scoring and verdict rules (pure functions)
- `rollout_ready/compare.py` builds a comparison from saved results; shared by demo, live, web and CLI
- `rollout_ready/checks.py` automatic checks
- `rollout_ready/provider.py` the only module that calls a model (Claude via `messages.parse`)
- `rollout_ready/judge.py` judge and criteria-drafter prompts
- `rollout_ready/runner.py` live runs (only missing results) and background jobs
- `rollout_ready/public.py` public live mode: per-visitor `SessionStore`, shared demo results cache, usage limits, Origin check
- `rollout_ready/storage.py` suite files: `suites/<id>/suite.yaml, cases.yaml, versions/vN.md, recorded/, runs/` (live overrides in `runs/overrides.json`)
- `rollout_ready/web/` FastAPI app, Jinja2 templates, plain CSS, small vanilla JS. Start screen at `/`; old URLs `/suites` and `/scoring` redirect.
- Demo prompts: `followup-email` (Sales, featured v2 to v3), `partner-outreach` (Partnerships), `webinar-invite` (Marketing).

## Scoring rules

- Criterion score 1 to 5. Case score = mean of criteria, averaged across repeats.
- Change = new minus current, rounded to 2 decimals before comparing.
- Deal-breaker (`deal_breaker: true` on a criterion) is missed when its mean score is 2 or lower (`DEAL_BREAKER_CUTOFF`).
- Worse: change <= -threshold, a check that passed now fails, or the new version newly misses a deal-breaker.
- Better: change >= threshold, or a check that failed now passes. Else no clear change.
- Verdict: 0 worse and >= 1 better "Ready to roll out"; 0 and 0 "No clear difference"; some worse and more better than worse "Not ready to roll out"; otherwise "Keep the current version". Never "Ready" while the new version misses a deal-breaker on any non-overridden example (it becomes "Not ready").
- Live mode reads only `runs/`, never `recorded/`. Any example either version lacks live results for runs for real. Behind the scenes labels each version "Sample results" or "Real results".
- Overrides replace one example's result for one pair of versions, never change stored scores, and lift that example's deal-breaker block. Live: files; demo: signed session cookie.
- Example kinds: everyday, messy, complicated, broke_before. Nudge when there are no messy or complicated examples.
- Show scores to one decimal place. Demo numbers are pinned by `tests/test_demo_data.py`; change demo data only if those still pass.

## Public live mode (ROLLOUT_PUBLIC=true, plus live mode and a key)

- Without `ROLLOUT_PUBLIC`, local live mode must behave exactly as before: no limits, no Origin check, no honeypot, files under `suites/`.
- Every request uses `store_for(request)`, never the shared `store` directly. In public mode that is a `SessionStore` over `ROLLOUT_DATA_DIR/sessions/<sid>/`: demo prompts are read from `suites/` and copied into the session folder on first change. Never write to `suites/` in public mode.
- Privacy: anything a visitor writes or adds (versions, examples, edits, new prompts, results, overrides, jobs) is visible only to that session. Session id is in the signed cookie, ends with the browser and is refused after 24 hours; old folders are pruned.
- Shared cache (`ROLLOUT_DATA_DIR/shared/`) holds only real results for an unchanged demo version on an unchanged demo example with the demo rubric and model. Nothing a visitor wrote may ever go there.
- Comparisons are all real or all sample, never mixed. Public visitors see the demo's sample comparison when real results are incomplete and sample results are complete.
- Limits: `ROLLOUT_MAX_RUNS_PER_SESSION` (3), `ROLLOUT_MAX_RUNS_PER_IP_PER_DAY` (5, first `X-Forwarded-For` address, stored hashed), `ROLLOUT_MAX_RUNS_PER_DAY` (10). A run is a test, "Run the missing examples", saving a new prompt, or "Suggest qualities for me". At most 8 examples per test, 5,000 characters per prompt or example. Check limits after form validation and before any call; a refused run makes no calls. Sample pages must keep working when limits are used up.
- Spam: the honeypot field (`website`) on every form that can call Claude; if filled, show a normal page and make no calls. POSTs must pass the Origin check.
- Copy: "Your tests are private to you and last until you close this browser or for 24 hours. Don't paste anything confidential." on Test a change, Your prompts and Add a new prompt. "N of 3 live tests left today." on Test a change. Limit message: "Today's live tests are used up. Try again tomorrow, or explore the sample results."
- Deploy with `--max-instances 1` and the key in Secret Manager. `tests/test_public.py` covers all of this.

## Project rules

- Browser first. Never require files, Git or a terminal for the main flow.
- Fictional data only (Northstar Systems; FlowOS, Connect, Insight; invented people and customers).
- Demo mode must always work with no key, make no API calls and save nothing.
- API keys stay server-side. Never send them to the browser, log or commit them.
- No em dashes anywhere: code, copy, docs, prompts, sample data, commit messages. In YAML and Python write the character only as the escape `\u2014`. `tests/test_no_em_dash.py` enforces this.
- Accessible on any screen size: labels on every input, visible focus, 44px targets, 4.5:1 contrast, no sideways scroll at 320px, reduced motion respected. No gradients, no emoji.
- Plain UI copy for non-engineers: follow the word list above. Never "system prompt", "eval", "regression".
- Colors: plum and blush, each defined once as a CSS custom property in `style.css`; use only those.
- Every verdict shows the evidence line: "Based on N examples. Even when a version is ready, spot-check real outputs in its first week."
- Don't add features beyond the spec without asking.

## Git

```bash
git config user.name "Juliana Mims"
git config user.email "313165559+jmims-workflow@users.noreply.github.com"
```

End every commit message with a blank line and then:

```
Co-Authored-By: Claude <noreply@anthropic.com>
```
