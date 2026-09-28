# job-watch

Private. Watches remote-job boards and emails a filtered digest to Hussam.

Runs on GitHub Actions every 6 hours, so it keeps working when his laptop is off.
Pure Python stdlib — no dependencies to install or rot.

## What it does

1. Polls 5 public job APIs (RemoteOK, Remotive, Arbeitnow, Jobicy, FindAsync)
2. Keeps only roles matching his stack: TypeScript, Node.js, Astro, React,
   PostgreSQL, Firebase, REST APIs
3. Drops roles that are a waste of his limited attention:
   - geo-locked to a country he has no work right for (EU/UK/US/CA/AU/CH)
   - senior / lead / principal — he is honest about being mid-level
   - embedded / Android / iOS / DevOps — he has stated he has none of these
   - non-English postings, which are near-always geo-locked
4. Emails only what is new since the last run

## Files

- `watch.py` — the whole thing
- `state/seen.json` — dedup memory, committed back by the workflow
- `.github/workflows/watch.yml` — the cron schedule

## Setup

Repository secrets (Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `GMAIL_USER` | `hrs18jan@gmail.com` |
| `GMAIL_APP_PASSWORD` | Google app password (16-char, spaces removed) |
| `WATCH_TO` | `hrs18jan@gmail.com` |
| `WATCH_FROM` | `hrs18jan@gmail.com` |

Trigger a manual test from the Actions tab, or run `python watch.py` locally.

## Cost

Free tier is 2,000 minutes/month. This uses roughly 240.
