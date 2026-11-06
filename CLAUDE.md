# Rollout Ready: notes for future sessions

## What it is

A Python web app for the owner of a team's shared AI instructions (prompts and snippets, Claude Projects, GPT or Gem instructions, skills, agent instructions). They edit the version in use in the browser, it runs on saved examples, every answer is scored (quick checks plus a blind AI reviewer), and the app gives one verdict. They update the real tool by hand and confirm; Rollout Ready records the decision. Tagline: "Test, review and roll out changes to your team's AI instructions." README opener: "The output reveals the gap."

Users are the people who own shared AI instructions in sales, marketing, business development and revenue operations teams: enablement leads, marketing ops managers, team leads. They are not engineers and don't use GitHub, files or a terminal. Every screen must make sense to them without a glossary. The browser flow is the product; the CLI is secondary.

## Plain words (user-facing text)

Every word a user sees (pages, buttons, labels, verdict sentences, the copied summary, error messages, How it works) uses the right-hand column. Code, file formats, YAML keys, CLI output and the "Under the hood" page (`docs/architecture.md`) keep the technical names. Do not rename files, YAML keys or CLI commands. `tests/test_web.py` scans rendered pages for the technical words.

| Technical | On screen |
| --- | --- |
| suite | AI instructions (one set, with its versions, examples and what good looks like) |
| test case | example |
| rubric | what good looks like |
| criterion | quality |
| judge | AI reviewer |
| automatic checks | quick checks |
| Worse / Better / No clear change | Got worse / Got better / About the same |
| current version / new version | the version your team uses now / the version you're trying |
| Start screen | Manage (`/`) |
| Suite page | the instructions page (`/i/<id>`): Test history, Version history, Examples, What good looks like |
| Test a new version page | Test a change (`/i/<id>/test`) |
| Compare page | the result page (`/i/<id>/result`) |
| How scoring works page | How it works (`/how-it-works`) |
| Example kinds | Everyday, Messy, Tricky, Caused a problem before |
| Run details tab | Behind the scenes |
| Prompt changes tab | What changed (diff labels Kept, Removed, Added) |
| Cases tab | Examples |
| Rubric tab | What good looks like |
| Mode pill | "Demo: sample results" or "Live: runs on Claude" |

Never show the 0.5 threshold as a number on a results screen; say "a move of half a point or more". The verdict names (Ready to roll out, Not ready to roll out, Needs review, No clear difference, Test incomplete) stay as they are.

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
- `rollout_ready/compare.py` builds a comparison from an evaluation record; shared by demo, live, web and CLI
- `rollout_ready/evidence.py` reuse keys, the evidence fingerprint, and record shapes (answers, reviews, items, evaluations, override events)
- `rollout_ready/evaluate.py` runs only what is missing and writes one evaluation record per test; builds demo records from `recorded/`
- `rollout_ready/releases.py` release history, derived status (In use, Retired, Draft), confirmation checks, one-time tokens
- `rollout_ready/records.py` the storage interface: read, write-once, compare-and-swap update, list
- `rollout_ready/demo_session.py` demo overrides and releases in the signed cookie, in a short form with a size budget
- `rollout_ready/checks.py` automatic checks
- `rollout_ready/provider.py` the only module that calls a model (Claude via `messages.parse`)
- `rollout_ready/judge.py` judge and criteria-drafter prompts
- `rollout_ready/runner.py` estimates, the Test a change estimate, and background jobs
- `rollout_ready/public.py` public live mode: per-visitor `SessionStore`, shared answers and reviews for demo versions, usage limits, Origin check
- `rollout_ready/storage.py` suite files: `suites/<id>/suite.yaml, cases.yaml, versions/vN.md, recorded/, history.json`, and records under `runs/` (`answers/`, `reviews/`, `evaluations/`, `releases.json`, `override-events.json`)
- `rollout_ready/web/` FastAPI app, Jinja2 templates, plain CSS, small vanilla JS. Pages: Manage `/`, instructions `/i/<id>`, Test a change `/i/<id>/test`, result `/i/<id>/result`, How it works. Old addresses (`/prompts`, `/prompts/<id>`, `/test`, `/results`, `/suites`, `/scoring`) redirect. Header nav: Manage, How it works.
- Demo sets: `followup-email` "Sales follow-up assistant" (Sales, featured v2 to v3), "Partner outreach" and "Webinar invite writer", `partner-outreach` (Business development; v2 to v3 is Needs review), `webinar-invite` (Marketing). Each has `history.json`: version 2 is in use for all three; webinar version 2 is a roll out anyway on a No clear difference result.

## Scoring rules

- Criterion score 1 to 5. Case score = mean of criteria, averaged across repeats.
- Change = new minus current, rounded to 2 decimals before comparing.
- Deal-breaker (`deal_breaker: true` on a criterion) is missed when the reviewer scores it 2 or lower (`DEAL_BREAKER_CUTOFF`) in any repetition, even if the average across repetitions is higher. Results name the example and repetition.
- Worse: change <= -threshold, a check that passed now fails, or the new version newly misses a deal-breaker.
- Better: change >= threshold, or a check that failed now passes. Else no clear change.
- Verdicts, in this order, after overrides: (1) "Test incomplete" if any answer or review is missing, cut off or failed (no verdict); (2) "Not ready to roll out" if the new version misses a deal-breaker on any example in any repetition, or at least one got worse and no more got better than got worse; (3) "Needs review" if some got worse, more got better, and no deal-breaker was missed; (4) "Ready to roll out" if none got worse, at least one got better, and no deal-breaker was missed; (5) "No clear difference" otherwise. "Keep the current version" no longer exists.
- `rollout run` exits 0 for Ready to roll out and No clear difference, 1 for Not ready to roll out and Needs review, 2 for Test incomplete or an error.
- Reuse keys (`evidence.py`): an answer is reused only when the version text, example input, model, generation settings, repetition and wrapper revision all match; a review only when the answer's text, every quality field (deal-breaker flags included), reviewer model and reviewer prompt revision match. Quick checks are in neither key: changing one reruns nothing and recomputes into a new record. The evidence fingerprint covers everything that affects generation, scoring or comparison. Bump `REVIEWER_PROMPT_REVISION` or `GENERATION_WRAPPER_REVISION` when the reviewer prompt or the generation call changes (`tests/test_evidence.py` fails until you do).
- Every live test writes one evaluation record (running, then final once: complete, incomplete or interrupted). A final record never changes. Demo records are built from `recorded/` the same way every time and never written. Live mode reads only `runs/`, never `recorded/`; results saved before V3 show as incomplete and are never reused. Behind the scenes labels each version "Sample results" or "Real results".
- Cut-off answers or reviews are incomplete items and never retried automatically. Temporary failures retry at most twice (our code; the SDK's retries are off).
- Overrides belong to one evaluation's evidence (fingerprint plus example), are append-only (removing one appends a removal event), never change stored scores, never carry over to changed evidence, and never hide or lift a deal-breaker miss. Live: `runs/override-events.json`; demo: signed session cookie.
- Release boundary: Rollout Ready records a person's decision and never updates the external tool. Release history is append-only (baseline, rollout, exception, return); In use, Retired and Draft are derived from it, never stored. A rollout relies on a saved, complete, current evaluation against the version in use; Roll out anyway needs a reason, the deal-breaker box on Not ready, and an acknowledgment when a different version is now in use, and keeps its verdict; Put back into use needs a reason and relies on no test. Every check runs again on submit, and a one-time token turns a double submit into the existing release.
- Example kinds (YAML keys): everyday, messy, complicated, broke_before; on screen Everyday, Messy, Tricky, Caused a problem before. Nudge when there are no messy or tricky examples.
- Show scores to one decimal place. Demo numbers are pinned by `tests/test_demo_data.py`; change demo data only if those still pass.

## Public live mode (ROLLOUT_PUBLIC=true, plus live mode and a key)

- Without `ROLLOUT_PUBLIC`, local live mode must behave exactly as before: no limits, no Origin check, no honeypot, files under `suites/`.
- Every request uses `store_for(request)`, never the shared `store` directly. In public mode that is a `SessionStore` over `ROLLOUT_DATA_DIR/sessions/<sid>/`: demo prompts are read from `suites/` and copied into the session folder on first change. Never write to `suites/` in public mode.
- Privacy: anything a visitor writes or adds (versions, examples, edits, new prompts, results, overrides, jobs) is visible only to that session. Session id is in the signed cookie, ends with the browser and is refused after 24 hours; old folders are pruned.
- Shared cache (`ROLLOUT_DATA_DIR/shared/`) holds only real results for an unchanged demo version on an unchanged demo example with the demo rubric and model. Nothing a visitor wrote may ever go there.
- Comparisons are all real or all sample, never mixed. Public visitors see the demo's sample comparison when real results are incomplete and sample results are complete.
- Limits: `ROLLOUT_MAX_RUNS_PER_SESSION` (3), `ROLLOUT_MAX_RUNS_PER_IP_PER_DAY` (5, first `X-Forwarded-For` address, stored hashed), `ROLLOUT_MAX_RUNS_PER_DAY` (5). A run is a test, "Run the missing examples", saving a new prompt, or "Suggest qualities for me". At most 8 examples per test, 5,000 characters per prompt or example. Check limits after form validation and before any call; a refused run makes no calls. Sample pages must keep working when limits are used up.
- Spam: the honeypot field (`website`) on every form that can call Claude; if filled, show a normal page and make no calls. POSTs must pass the Origin check.
- Copy: "Your tests are private to you and last until you close this browser or for 24 hours. Don't paste anything confidential." on Test a change, Manage and Add AI instructions. "N of 3 live tests left today. Uses 1 of your 3 live tests today." on Test a change. The demo strip says how long a visitor's work lasts. Limit message: "Today's live tests are used up. Try again tomorrow, or explore the sample results."
- Deploy with `--max-instances 1` and the key in Secret Manager. `tests/test_public.py` covers all of this.

## Screens (PR 2a decisions)

- Manage has no latest-result banner. Set cards use neutral status lines: no warning icon, no alert colors. Warning styling is for the result page only.
- The result page shows one clear answer: above the fold only the verdict, one sentence, the evidence line, one short line for any missed deal-breaker (never hidden) and one main action. Everything else (What this test covers, About this test, help) is behind a disclosure or in tabs: What went wrong (opens first for Not ready and Needs review), Examples (opens first otherwise), What changed, Behind the scenes.
- "What's this?" help at the first mention on each page of the verdict, example kinds, deal-breakers and the AI reviewer; on the result page the verdict and deal-breaker help sit in "What this test covers".
- Claims rule: describe evidence precisely; never say a change is safe, proven or guaranteed. Banned anywhere: "Only what passes rolls out", "Proven on real examples", "can't break what already worked without you knowing", "Nothing left out".
- Not built yet (do not mention on screen): the Library, Report a bad answer, the six new sets, the new Add AI instructions page and fields, suggested examples from reports. The README lists them under "What I'd build next".

## Project rules

- Browser first. Never require files, Git or a terminal for the main flow.
- Fictional data only (Northstar Systems; FlowOS, Connect, Insight; invented people and customers).
- Demo mode must always work with no key, make no API calls and save nothing.
- API keys stay server-side. Never send them to the browser, log or commit them.
- No em dashes anywhere: code, copy, docs, prompts, sample data, commit messages. In YAML and Python write the character only as the escape `\u2014`. `tests/test_no_em_dash.py` enforces this.
- Accessible on any screen size: labels on every input, visible focus, 44px targets, 4.5:1 contrast, no sideways scroll at 320px, reduced motion respected. No gradients, no emoji.
- Plain UI copy for non-engineers: follow the word list above. Never "system prompt", "eval", "regression".
- Colors: plum and blush, each defined once as a CSS custom property in `style.css`; use only those.
- Every verdict shows the evidence line "Based on N examples."; Ready to roll out adds "Even when a version is ready, spot-check real outputs in its first week."
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
