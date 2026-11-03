# Deploy to Cloud Run

Back to the [README](../README.md). For settings, see [Configuration](configuration.md).

## Demo mode

```bash
gcloud run deploy rollout-ready --source . --region us-east1 --allow-unauthenticated
```

The Dockerfile serves on `PORT` (default 8080) and leaves `ROLLOUT_MODE` at `demo`, which is safe for any public URL. `.dockerignore` and `.gcloudignore` leave out `.env`, `runs/` and `.git`.

## Public live mode

[Public live mode](configuration.md#public-live-mode) needs one instance, so every visitor's session, folder and counts stay on one machine. Keep the API key in Google Secret Manager rather than a plain environment variable:

```bash
printf '%s' "$ANTHROPIC_API_KEY" | gcloud secrets create anthropic-api-key --data-file=-
python -c "import secrets; print(secrets.token_urlsafe(32), end='')" | gcloud secrets create rollout-secret-key --data-file=-
```

```bash
gcloud run deploy rollout-ready --source . --region us-east1 --allow-unauthenticated \
  --max-instances 1 \
  --set-env-vars ROLLOUT_MODE=live,ROLLOUT_PUBLIC=true \
  --set-secrets ANTHROPIC_API_KEY=anthropic-api-key:latest,ROLLOUT_SECRET_KEY=rollout-secret-key:latest
```

The Cloud Run service account needs the Secret Manager Secret Accessor role on both secrets. Cloud Run keeps files in memory and resets them when the instance restarts, which also resets sessions and counts.

## Spending cap

The app's limits are a first line of defense. For a hard spending cap, pay for the API with prepaid credits and turn auto-reload off in the Claude Console: when the credits run out, calls stop, whatever the app does.
