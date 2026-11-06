# Configuration

Back to the [README](../README.md). For hosting, see [Deploy](deploy.md).

## Demo mode (default, no API key)

```bash
rollout serve
```

Open http://127.0.0.1:8080. Demo mode is the default even if an API key is set. It makes no API calls and saves nothing on the server. It ships with three fictional prompts and their release history (see [Demo data](architecture.md#demo-data)).

Overrides work in demo mode too. They are kept in a signed cookie for the browser session only, and the form says so.

## Live mode (calls Claude)

Copy `.env.example` to `.env` or set the variables in your shell:

```bash
export ROLLOUT_MODE=live
export ANTHROPIC_API_KEY=your-key-here
export ROLLOUT_JUDGE_MODEL=claude-sonnet-5-5
rollout serve
```

Live mode saves new prompts, versions, examples, results and overrides under `suites/`. It only runs what is missing: any example either version has no real results for, then one AI reviewer call per output. The sample results that ship with the demo never count as saved results in live mode, so the first live test of a demo prompt runs both versions. Saved results are reused, keyed by example, rubric and model, and a failed call never loses saved work. **Behind the scenes** shows "Sample results" or "Real results" for each version, so a comparison can never mix the two silently. Tokens used are shown there too; dollar costs are not, because prices change.

If `ROLLOUT_MODE=live` is set but there is no key, the app falls back to demo mode and says so on every page. The key is read only on the server. It is never sent to the browser, logged or committed (`.env` is in `.gitignore`). `ROLLOUT_SECRET_KEY` optionally signs the demo override cookie; if it is unset, a random key is made at startup.

Running prompts uses API credits. To open live mode to anyone with the link, turn on the limits in [Public live mode](#public-live-mode); otherwise a public deployment should stay in demo mode.

Note on judge settings: current Claude models and the current SDK no longer accept a temperature setting, so the AI reviewer runs with default sampling. Consistency comes from structured output (a score and reason for every criterion), anchored 5 and 1 descriptions, and the half-point threshold.

## Public live mode

Set `ROLLOUT_PUBLIC=true` together with `ROLLOUT_MODE=live` and `ANTHROPIC_API_KEY` to let anyone with the link test real prompt changes on Claude, with limits. Without `ROLLOUT_PUBLIC`, live mode works exactly as described above, for one person or a trusted team.

What visitors get:

- The demo sets and their sample comparisons stay visible to everyone, labeled "Sample results".
- **Test a change** runs on Claude for real. It starts from the version in use; "Try a saved example change" fills in version 4 of the Sales follow-up assistant as an editable starting point.
- A visitor's test always runs both versions for real, never mixing sample and real results. Real results for the demo's own versions on the demo's own, unchanged examples contain nothing a visitor wrote, so they are cached and shared across visitors. Everything a visitor writes or adds (new versions, examples, edited examples, new prompts, their results and overrides) is private to them.

Privacy: each visitor gets a random session id in a signed cookie. It ends when they close the browser and is refused after 24 hours. Their work lives in a folder for that session under `ROLLOUT_DATA_DIR` (default: a `rollout-ready` folder in the system temp directory) and is never shown to anyone else; folders older than a day are deleted. Test a change and Your prompts say so plainly and ask people not to paste anything confidential. Set `ROLLOUT_SECRET_KEY` so the cookie signature is stable.

Limits (environment variables, with their defaults):

| Variable | Default | What it caps |
| --- | --- | --- |
| `ROLLOUT_MAX_RUNS_PER_SESSION` | 3 | Live runs per visitor session |
| `ROLLOUT_MAX_RUNS_PER_IP_PER_DAY` | 5 | Live runs per address per day (the first address in `X-Forwarded-For`, which is the visitor on Cloud Run) |
| `ROLLOUT_MAX_RUNS_PER_DAY` | 5 | Live runs per day across all visitors |

A run is one test, one "Run the missing examples", one saved new prompt (scoring version 1) or one "Suggest qualities for me". Each test runs on at most 8 examples, and each pasted prompt or example is at most 5,000 characters. When a limit is reached, visitors see "Today's live tests are used up. Try again tomorrow, or explore the sample results." and every sample page keeps working. Test a change shows what is left, for example "2 of 3 live tests left today." Days are counted in UTC. Addresses are stored only as salted hashes, in `usage.json` in the data folder.

Spam protection: every form that can call Claude has a hidden field people never see. If it is filled in, the app shows a normal-looking page and makes no calls. Form posts are only accepted from the app's own pages: the `Origin` header (or `Referer`, if a browser leaves `Origin` out) must match the app's host.

To deploy public live mode, see [Deploy](deploy.md#public-live-mode).
