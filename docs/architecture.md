# Under the hood

Back to the [README](../README.md).

## Pages

| Page | Address | Old addresses that redirect |
| --- | --- | --- |
| Manage (start screen) | `/` | `/prompts`, `/suites` |
| Instructions page | `/i/<id>` | `/prompts/<id>`, `/suites/<id>` |
| Test a change | `/i/<id>/test` (`?from=vN` for Edit and test again) | `/test` |
| Result | `/i/<id>/result?current=v2&new=v3`; one saved test at `/i/<id>/tests/<evaluation id>` | `/results`, `/?suite=...`, `/prompts/<id>/tests/<id>` |
| How it works | `/how-it-works` | `/scoring` |

Add AI instructions (`/prompts/new`), example detail pages, release records and every form post keep their addresses.

## Plain words and technical terms

The app uses plain words on screen. For technical readers, here is what they mean:

| On screen | Technical term | In code and files |
| --- | --- | --- |
| AI instructions (a set) | A prompt under test, with its versions and test set | a suite: `suites/<id>/suite.yaml` |
| Examples | A regression test set: inputs only, no reference answers | test cases: `cases.yaml` |
| The AI reviewer | LLM-as-judge with blind, per-output scoring against a rubric (no pairwise comparison) | `judge.py` |
| What good looks like | A rubric of 3 to 5 criteria with anchored 1 and 5 descriptions | `rubric:` in `suite.yaml` |
| Deal-breakers | Critical criteria that act as a release gate (score 2 or lower blocks "Ready") | `deal_breaker: true` |
| Quick checks | Deterministic assertions: word limit, banned phrases, required phrases | `checks:` and `checks.py` |
| I disagree with the AI reviewer | Human review of judge output; the overrides start a calibration set for the judge | append-only events keyed by evidence fingerprint and case id: live `runs/override-events.json`, demo the session cookie |
| In use, Retired, Draft | Release status, derived from an append-only release history | `history.json` (shipped), `runs/releases.json` (added), `releases.py` |
| Put into use, Roll out anyway, Put back into use | Release events: rollout, exception, return (plus baseline) | `releases.py` |
| Example kinds | Test coverage: everyday, messy, complicated, broke before (known regressions) | `kind:` in `cases.yaml` |
| Got worse / Got better / About the same | Regression / improvement / no significant change, with a 0.5 point threshold | `threshold:` and `scoring.py` |
| Behind the scenes | Run details: models, repeats, tokens, timestamps | |

## Scoring details

A criterion score is 1 to 5; a case score is the mean of its criteria, averaged across repeats; change is new minus current, rounded to 2 decimals before comparing to the threshold. A deal-breaker counts as missed when it is scored 2 or lower in any repeat, even if its mean across repeats is higher. A newly missed deal-breaker (missed by the new version but not the current one) makes the case worse. Overrides replace the case's result but never remove a deal-breaker miss.

Verdicts are applied in this order, after overrides: Test incomplete (any answer or review missing, cut off or failed; no verdict), Not ready to roll out (any deal-breaker miss, or some worse and no more better than worse), Needs review (some worse, more better, no miss), Ready to roll out (none worse, some better, no miss), No clear difference (none either way, no miss).

## Evidence and reuse

Every live test writes one evaluation record (`runs/evaluations/<id>.json`): the exact version texts, examples, qualities, quick checks, models, settings and revisions it used; a reference to each answer and review; per-example classifications; the outcome; a status; and an evidence fingerprint. A record is written as `running`, then once more as final (`complete`, `incomplete` or `interrupted`), and never again: storage refuses any other update. A record left running by a restart reads as interrupted, and finishing it runs only what is missing. Demo records are built from `recorded/` the same way every time (ids `recorded-<baseline>-<candidate>`) and are never written.

Answers and reviews are stored once under their keys (`runs/answers/<key>.json`, `runs/reviews/<key>.json`):

- Answer key: version text, example input, generation model, generation settings, repetition and `GENERATION_WRAPPER_REVISION`.
- Review key: the answer's text hash, every quality field (deal-breaker flags included), reviewer model and `REVIEWER_PROMPT_REVISION`.

Quick checks are in neither key, so changing one makes no calls and recomputes into a new record. The fingerprint covers everything that affects generation, scoring or comparison; a record whose fingerprint no longer matches is stale for rollout. Older `runs/<version>.json` files (before V3) are shown as incomplete and never reused. Retries: a temporary failure is retried at most twice with increasing waits (the SDK's own retries are off); a reply cut off at the length limit is never retried and leaves its item incomplete.

## Releases

Rolling out is recorded, not performed. A set's history is `history.json` (shipped) followed by `runs/releases.json` (live), the visitor's own folder (public) or the session cookie (demo). Status is derived: In use is the latest event's version; Retired was in use and was superseded; Draft was never confirmed in use. A set saved before V3 gets one baseline event built in memory from its old `in_use` setting or its newest version. Every confirmation reruns the checks at submission (saved, complete, current evaluation of this version; the version in use, or an acknowledgment; the deal-breaker box and a reason for Roll out anyway) and carries a one-time token, so a double submit returns the release it already made. Releases store the evaluation's verdict and the override ids in force.

## Storage and concurrent writes

New records go through a small storage interface (`records.py`): read, write-once, update and list, with a local folder backend. The app runs as one instance (`--max-instances 1`), so the main protection is a lock per file: writers queue on it, and none of them starve. Compare-and-swap is kept as a backstop for a writer outside that lock. A lock per set covers read-modify-write steps that touch more than one record, such as checking a token and appending a release. Files are written to a temporary file, then renamed.

Demo mode keeps a visitor's overrides and releases in the signed cookie in a short form (16-character fingerprints). A change that would take the cookie past 3,600 bytes is refused, nothing is saved, and the page offers Start over.

## Prompt files

Prompts are plain files, so they can also live in a repo:

```
suites/<suite-id>/
  suite.yaml                  name, type, team, owner_name, owner_team, where_text, where_url, who_uses_it,
                              model, repeats, threshold, rubric (with deal_breaker), checks
  cases.yaml                  id, name, label, kind, input
  versions/v1.md ...          front matter (title, note, created), then the prompt text
  recorded/<v>.json           demo outputs and judge scores
  history.json                release history that ships with the app
  runs/answers/<key>.json     live answers (not committed)
  runs/reviews/<key>.json     live reviews (not committed)
  runs/evaluations/<id>.json  evaluation records (not committed)
  runs/releases.json          release events added since history.json (not committed)
  runs/override-events.json   overrides and their removals (not committed)
```

## Command line (for teams that keep prompts in a repo)

```bash
rollout list
```

```bash
rollout run followup-email --current v2 --new v3 --report summary.md
```

`rollout run` prints the verdict and counts, saves results (in live mode, with `--live`), and can write the summary as Markdown. It exits with code 0 for "Ready to roll out" and "No clear difference", 1 for "Not ready to roll out" and "Needs review", and 2 for "Test incomplete" or an error, so it can block a pull request in CI. `rollout serve` starts the web app.

## Demo data

Demo mode has three fictional Northstar Systems sets of AI instructions, one per team, and all four final verdicts. Version 2 is in use for all three, from `history.json`:

| Prompt | Team | Versions | Verdict |
| --- | --- | --- | --- |
| Sales follow-up assistant | Sales | 2 to 3 (featured) | Not ready to roll out, with a missed deal-breaker on 2 examples |
| Sales follow-up assistant | Sales | 2 to 4 (the saved example on Test a change) | Ready to roll out |
| Partner outreach | Business development | 1 to 2 | Ready to roll out |
| Partner outreach | Business development | 2 to 3 | Needs review: 3 better, 1 worse, 2 about the same |
| Webinar invite writer | Marketing | 1 to 2 | No clear difference |
| Webinar invite writer | Marketing | 2 to 3 | Not ready to roll out, with a missed deal-breaker |

## What the tests cover

The tests cover the threshold edges, check flips, deal-breaker detection at the cutoff and in any repetition, newly missed deal-breakers, the rollout block, every verdict in order, exit codes, every demo number, reuse by key (with call counts), immutable evaluation records, cut-off answers, bounded retries, interrupted tests, overrides (result changes, deal-breaker misses kept, stored scores untouched, append-only removal, no carry-over to changed evidence, demo cookie and live file), releases (confirmation checks at submission, changed version in use, roll out anyway, put back, one-time tokens, concurrent submits), the demo cookie budget, concurrent record updates, the example mix nudge, redirects from the first version's URLs, result reuse, version numbering, file validation, demo mode making no network calls and saving nothing, the 20,000 character limit, public live mode (sessions, limits, the Origin check and the honeypot), a scan of every rendered page for engineering words, a scan that fails if any file contains an em dash, and a check that the README keeps its Part of a set section, stays under 1,000 words of prose and has no broken relative links.
