# Local deployment — running Health Tracker off Cloud Run

This directory is the laptop deployment: the same code that ran on Cloud Run,
started by systemd on `lenovo-agents` instead.

**Only compute moved.** The Google Sheet and the Drive photo folder are unchanged
and still the source of truth — they cost nothing, they are the sovereign storage
the system was designed around, and the iOS app and the nutrition audit both read
them. Moving them would be a different project.

## What replaced what

| Cloud Run | here | notes |
|---|---|---|
| `health-tracker-ingest` (service) | `health-tracker-api.service` | gunicorn on **127.0.0.1:8080** |
| `meal-ingest` (Cloud Tasks) | `health-tracker-queue.service` | `ingest/localqueue.py`, SQLite |
| `health-tracker-daily` (job) | `health-tracker-daily.service` | via `src/daily_runner.py` |
| `health-tracker-daily-trigger` (Scheduler) | `health-tracker-daily.timer` | 11:00 backstop |
| `coach-sweep` (*/30) | `health-tracker-coach@sweep.timer` | **the coach's safety net** |
| `coach-morning` (07:30) | `health-tracker-coach@morning.timer` | |
| `coach-weekly` / `-monthly` / `-yearly` | `health-tracker-coach@{weekly,monthly,yearly}.timer` | |
| `health-tracker-maintenance` (Job) | `python -m src.maintenance`, by hand | nothing scheduled it |
| `com.dneves.coach-worker` (launchd, **MacBook**) | `health-tracker-coach-worker.timer` | **this is what makes cards appear** |
| `com.dneves.nutrition-audit` (launchd, **MacBook**) | `health-tracker-audit.timer` | 11:00 / 15:00 / 23:00 |
| `com.dneves.insights-weekly` (launchd, **MacBook**) | `health-tracker-insights-weekly.timer` | Sunday 20:00 |
| `com.dneves.insights-nextmeal` (launchd, **MacBook**) | *retired* | superseded by the coach |

### The coach worker is not optional, and its failure is silent

**The backend never calls a model for the coach.** It does the arithmetic, builds
the prompt, and *parks a job*. `automation/coach/worker.py` claims that job,
answers it with the local `claude` CLI, and posts the JSON back to be validated.

If nothing runs the worker, generations pile up in `coach/jobs/` and the app keeps
serving the last feed it had — so it looks like the coach has nothing to say, not
like a failure. Nothing logs an error, because nothing failed; a job simply waits.

This is exactly what happened after the Cloud Run decommission: the worker was
still on the MacBook under launchd, pointed at the deleted Cloud Run URL. Cards
stopped and nothing anywhere said so.

**Only one worker may run.** A claim is a 15-minute lease, so a second worker that
claims jobs but cannot finish them (the MacBook, post-decommission) is worse than
no second worker: it takes the job, holds it for 15 minutes, and the healthy worker
skips it as "someone else is on it". To retire the MacBook's:

```bash
launchctl bootout gui/$(id -u)/com.dneves.coach-worker
launchctl bootout gui/$(id -u)/com.dneves.nutrition-audit
rm ~/Library/LaunchAgents/com.dneves.{coach-worker,nutrition-audit}.plist
```

Check for a stuck claim:

```bash
grep -l '"claimed_at": "2' ~/.local/state/health-tracker/coach-store/coach/jobs/*.json
```

⚠️ **`/coach/work` defaults the claimant to `"mac"` server-side** when no `?worker=`
is passed (`main.py`). So a bare `curl .../coach/work` used to diagnose the queue
*claims the job* and stamps it `claimed_by: mac` — which reads exactly like a rogue
MacBook worker competing. It isn't. Diagnose read-only with
`GET /coach/patterns`, or pass `?worker=debug` so the footprint is honest, and
release anything you claimed:

```python
coach_store.release_job("<job-id>", "diagnostic claim", now_iso)
```

| Secret Manager | `~/.config/health-tracker/env` | mode 600 |
| Cloud Storage (coach store) | `COACH_LOCAL_DIR` | already a supported backend |
| Log-based alert policies | `health-tracker-alert@.service` | Telegram via `agents-notify` |
| Cloud Build | `deploy/install.sh` + the test gate | see below |

`QUEUE_BACKEND=local` is the master switch. Unset (or `cloudtasks`) and the
service behaves exactly as it did on GCP — which is what makes rolling back an env
change plus a restart rather than a redeploy.

## Install

```bash
python3 -m venv backend/venv
backend/venv/bin/pip install -r backend/requirements.txt \
                             -r backend/ingest/requirements.txt gunicorn pytest

install -m 600 deploy/env.example ~/.config/health-tracker/env
${EDITOR:-nano} ~/.config/health-tracker/env      # fill in the secrets

./deploy/install.sh --start
```

Secrets come out of Secret Manager one at a time:

```bash
for s in ingest-token gemini-api-key drive-oauth-token health-oauth-token; do
  echo "== $s"; gcloud secrets versions access latest --secret=$s \
    --project=health-tracker-501322
done
```

Writing the Sheet additionally needs a service-account key, because
`google.auth.default()` no longer finds an attached identity:

```bash
gcloud iam service-accounts keys create ~/.config/health-tracker/sa.json \
  --iam-account=health-tracker-job@health-tracker-501322.iam.gserviceaccount.com
chmod 600 ~/.config/health-tracker/sa.json
```

## Reaching it from the iPhone

Live at **`https://lenovo-agents.tail68e120.ts.net`** (tailnet only).

Nothing binds a routable interface. The phone reaches the API over Tailscale, and
Windows does the proxying — from an **elevated PowerShell on the host**:

```powershell
& 'C:\Program Files\Tailscale\tailscale.exe' serve --bg --https 443 http://127.0.0.1:8080
& 'C:\Program Files\Tailscale\tailscale.exe' serve status
# to undo:
& 'C:\Program Files\Tailscale\tailscale.exe' serve --https=443 off
```

⚠️ **You cannot test the tunnel from inside WSL, and the failure looks real.**
`curl https://lenovo-agents.tail68e120.ts.net/` from WSL returns *connection
refused*, and `getent hosts` resolves the name to 100.97.58.7 as though it should
work. It doesn't, because mirrored networking gives WSL its **own** stack on that
same address — the connection never leaves WSL, and tailscaled is listening on the
*Windows* side. Nothing is broken. Test from the phone, or from PowerShell:

```powershell
Invoke-WebRequest -Uri 'https://lenovo-agents.tail68e120.ts.net/' `
  -Headers @{'X-Auth-Token'='<ingest-token>'} -UseBasicParsing
```

The two facts that make the whole arrangement work, both already true here:

* **`networkingMode=mirrored`** gives bidirectional localhost between Windows and
  WSL, so `tailscale serve`'s `http://127.0.0.1:8080` on the Windows side reaches
  gunicorn inside WSL. Without it you would need a `netsh portproxy` rebuilt on
  every WSL restart, because the NAT'd IP changes.
* **`tailscale serve` terminates TLS with a real Let's Encrypt cert.** Not
  cosmetic: iOS App Transport Security blocks cleartext HTTP, so pointing the app
  at `http://100.97.58.7:8080` fails on the phone while working from curl.

Two things make this work, and both are already true on this machine:

* **`networkingMode=mirrored`** in `.wslconfig` — WSL2 shares the Windows network
  interfaces, so `127.0.0.1:8080` inside WSL *is* `127.0.0.1:8080` on the host.
  Without it you would need a `netsh portproxy` re-created on every WSL restart,
  because the NAT'd IP changes.
* **`tailscale serve` terminates TLS with a real Let's Encrypt cert** for
  `lenovo-agents.<tailnet>.ts.net`. That is not cosmetic: iOS App Transport
  Security blocks cleartext HTTP, so pointing the app at `http://100.97.58.7:8080`
  would fail on the phone while working perfectly from curl.

Then point the app (`ios/HealthTracker/Config.swift`, gitignored — copy it from
`ios/Config.example.swift`) and the iPhone Shortcut at that URL. `X-Auth-Token` is
still checked: the tailnet is a private network, not an authenticated one, and
every device on it would otherwise be able to write meals.

**The phone must have Tailscale running** — this URL does not resolve off the
tailnet. That is the one real usability difference from the public Cloud Run URL.

## Deploying a change

There is no build and no CI. `cloudbuild.yaml` and both Dockerfiles were deleted
with the rest of GCP — `install.sh` re-links the units and the code is read
straight from the working tree.

```bash
git pull
backend/venv/bin/python -m pytest backend/tests -q     # the gate Cloud Build was
./deploy/install.sh --start
```

**Run the tests.** They are the same suite Cloud Build ran before it would build an
image, and they are now the *only* thing between an edit and production — with
nothing enforcing it but you.

## Operating it

```bash
systemctl --user status health-tracker-api health-tracker-queue
journalctl --user -u health-tracker-api -f
systemctl --user list-timers health-tracker-daily.timer

cd backend/ingest && ../venv/bin/python queue_worker.py --stats
```

`--stats` is the one to check when a meal doesn't appear. `pending` that never
drains means the API is down or refusing the token; **`dead` means a meal was
lost** after the full 8-attempt window, and those rows are kept forever precisely
so they can be found.

### What is worse than it was on GCP

Worth knowing rather than discovering:

* **Uptime is yours now.** A Wi-Fi drop at 08:30 means the weigh-in trigger
  doesn't fire; the 11:00 backstop covers it, but nothing pages you.
* **A reboot ends everything in flight.** Windows Update restarts are confined to
  23:00–07:00 (see `agent-server`), which is why that window is quiet.
* **`agents-doctor` asserts nothing binds `0.0.0.0`.** This deployment binds
  loopback only, so it still passes — keep it that way. If you ever change
  `--bind`, update the check rather than silencing it.

## The audit's credentials (and why there is nothing machine-specific left)

The audit originally held **one** user token carrying both Sheets and `drive.file`,
minted interactively by `authorize.py`. That token lived only on the machine that
ran the consent flow, which made it the single credential a move between machines
could not carry — and it is what silently broke the audit after this migration.

It is now split along the lines `CONTEXT.md` §4 already documents for everything
else, so nothing new was introduced and no re-consent is needed:

| what | identity | why |
|---|---|---|
| write the `meals` tab | **service account** | the Sheet is shared with it as Editor |
| read the meal photos | **`DRIVE_OAUTH_TOKEN`** | a service account has zero Drive quota |

`drive.file` grants access to files the app itself created, and the photos were
uploaded by this same OAuth client — verified by listing and downloading one.

`audit.py` still prefers a legacy combined token when `backend/credentials/
token_nutrition_audit.json` exists, so a machine that already has one (the MacBook)
behaves exactly as before. Verify either path with:

```bash
backend/venv/bin/python automation/nutrition-audit/audit.py --check
```

## Insights: one job migrated, one deliberately retired

`automation/insights/generate.py` has two modes, and they are **not** in the same
situation:

* **`weekly` — migrated** (`health-tracker-insights-weekly.timer`, Sunday 20:00).
  The iOS app calls `GET /insights/weekly` directly, and that endpoint is read-only:
  it serves whatever this job last wrote to the `weekly_reports` tab and reports
  `status: pending` until it has run. Without the timer that screen stays empty.
  Sunday because `_sunday_ref()` anchors the report's `week_start` to the most
  recent Sunday.
* **`next-meal` — retired, no timer.** The coach supersedes it: it emits its own
  `next_meal` cards through `/coach/feed`, and nothing in the app calls
  `/insights/next-meal`. The script is kept for manual/dry-run use.

Both had been failing on the MacBook since ~2026-07-25 for a reason unrelated to
this migration: **`GEMINI_API_KEY` was never set there**. It is set here, from
Secret Manager, so the weekly job works on this machine.
