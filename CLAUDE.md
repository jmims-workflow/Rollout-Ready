# Rollout Ready: notes for future sessions

## What it is

A Python web app that lets the owner of a team's shared AI prompt test a change before the team sees it. They paste a new version in the browser, it runs against saved test inputs, every output is scored (code checks plus a blind Claude judge), and the app gives one verdict. Tagline: "Test a prompt change before your team sees it."

Users are enablement, ops and team leads who don't use GitHub, files or a terminal. The browser flow is the product; the CLI is secondary.

## Run and test

```bash
pip install -e ".[dev]"
rollout serve            # demo mode at http://127.0.0.1:8080
pytest
rollout run followup-email --current v2 --new v3   # exits 1 (Not ready)
```

Live mode: `ROLLOUT_MODE=live` plus `ANTHROPIC_API_KEY`. Screenshots: run the app, then `node scripts/screenshots.js`.

## Layout

- `rollout_ready/scoring.py` scoring and verdict rules (pure functions)
- `rollout_ready/compare.py` builds a comparison from saved results; shared by demo, live, web and CLI
- `rollout_ready/checks.py` automatic checks
- `rollout_ready/provider.py` the only module that calls a model (Claude via `messages.parse`)
- `rollout_ready/judge.py` judge and criteria-drafter prompts
- `rollout_ready/runner.py` live runs (only missing results) and background jobs
- `rollout_ready/storage.py` suite files: `suites/<id>/suite.yaml, cases.yaml, versions/vN.md, recorded/, runs/`
- `rollout_ready/web/` FastAPI app, Jinja2 templates, plain CSS, small vanilla JS

## Scoring rules

- Criterion score 1 to 5. Case score = mean of criteria, averaged across repeats.
- Change = new minus current, rounded to 2 decimals before comparing.
- Worse: change <= -threshold, or a check that passed now fails.
- Better: change >= threshold, or a check that failed now passes. Else no clear change.
- Verdict: 0 worse and >= 1 better "Ready to roll out"; 0 and 0 "No clear difference"; some worse and more better than worse "Not ready to roll out"; otherwise "Keep the current version".
- Show scores to one decimal place. Demo numbers are pinned by `tests/test_demo_data.py`; change demo data only if those still pass.

## Project rules

- Browser first. Never require files, Git or a terminal for the main flow.
- Fictional data only (Northstar Systems; FlowOS, Connect, Insight; invented people and customers).
- Demo mode must always work with no key, make no API calls and save nothing.
- API keys stay server-side. Never send them to the browser, log or commit them.
- No em dashes anywhere: code, copy, docs, prompts, sample data, commit messages. In YAML and Python write the character only as the escape `\u2014`. `tests/test_no_em_dash.py` enforces this.
- Accessible on any screen size: labels on every input, visible focus, 44px targets, 4.5:1 contrast, no sideways scroll at 320px, reduced motion respected. No gradients, no emoji.
- Plain UI copy for non-engineers: say "prompt", "test", "got worse", not "system prompt", "eval", "regression".
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
