# Under the hood

Back to the [README](../README.md).

## Plain words and technical terms

The app uses plain words on screen. For technical readers, here is what they mean:

| On screen | Technical term | In code and files |
| --- | --- | --- |
| A prompt | A prompt under test, with its versions and test set | a suite: `suites/<id>/suite.yaml` |
| Examples | A regression test set: inputs only, no reference answers | test cases: `cases.yaml` |
| The AI reviewer | LLM-as-judge with blind, per-output scoring against a rubric (no pairwise comparison) | `judge.py` |
| What good looks like | A rubric of 3 to 5 criteria with anchored 1 and 5 descriptions | `rubric:` in `suite.yaml` |
| Deal-breakers | Critical criteria that act as a release gate (score 2 or lower blocks "Ready") | `deal_breaker: true` |
| Quick checks | Deterministic assertions: word limit, banned phrases, required phrases | `checks:` and `checks.py` |
| I disagree with the AI reviewer | Human review of judge output; the overrides start a calibration set for the judge | live: `runs/overrides.json`, keyed `"<current>:<new>"` then case id |
| Example kinds | Test coverage: everyday, messy, complicated, broke before (known regressions) | `kind:` in `cases.yaml` |
| Got worse / Got better / About the same | Regression / improvement / no significant change, with a 0.5 point threshold | `threshold:` and `scoring.py` |
| Behind the scenes | Run details: models, repeats, tokens, timestamps | |

## Scoring details

A criterion score is 1 to 5; a case score is the mean of its criteria, averaged across repeats; change is new minus current, rounded to 2 decimals before comparing to the threshold. A deal-breaker counts as missed when its mean score across repeats is 2 or lower. A newly missed deal-breaker (missed by the new version but not the current one) makes the case worse. Marking or unmarking a deal-breaker does not invalidate saved results, because it changes no scores. Overrides replace the case's result and remove that case's deal-breaker block.

## Prompt files

Prompts are plain files, so they can also live in a repo:

```
suites/<suite-id>/
  suite.yaml            name, team, who_uses_it, model, repeats, threshold, rubric (with deal_breaker), checks
  cases.yaml            id, name, label, kind, input
  versions/v1.md ...    front matter (title, note, created), then the prompt text
  recorded/<v>.json     demo outputs and judge scores
  runs/<v>.json         live results (not committed)
  runs/overrides.json   live overrides (not committed)
```

## Command line (for teams that keep prompts in a repo)

```bash
rollout list
```

```bash
rollout run followup-email --current v2 --new v3 --report summary.md
```

`rollout run` prints the verdict and counts, saves results (in live mode, with `--live`), and can write the summary as Markdown. It exits with code 1 when the verdict is "Not ready to roll out" or "Keep the current version", so it can block a pull request in CI. `rollout serve` starts the web app.

## Demo data

Demo mode has three fictional Northstar Systems prompts, one per team, and all four verdicts:

| Prompt | Team | Versions | Verdict |
| --- | --- | --- | --- |
| Discovery follow-up email | Sales | 2 to 3 (featured) | Not ready to roll out, with a missed deal-breaker on 2 examples |
| Discovery follow-up email | Sales | 2 to 4 (the saved example on Test a change) | Ready to roll out |
| Partner outreach first message | Partnerships | 1 to 2 | Ready to roll out |
| Webinar invite email | Marketing | 1 to 2 | No clear difference |
| Webinar invite email | Marketing | 2 to 3 | Keep the current version, with a missed deal-breaker |

## What the tests cover

The tests cover the threshold edges, check flips, deal-breaker detection at the cutoff, newly missed deal-breakers, the rollout block, all four verdicts, every demo number, overrides (verdict changes, stored scores untouched, removal, demo cookie and live file), the example mix nudge, redirects from the first version's URLs, result reuse, version numbering, file validation, demo mode making no network calls and saving nothing, the 20,000 character limit, public live mode (sessions, limits, the Origin check and the honeypot), a scan of every rendered page for engineering words, a scan that fails if any file contains an em dash, and a check that the README keeps its Part of a set section, stays under 1,000 words of prose and has no broken relative links.
