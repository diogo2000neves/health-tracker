#!/usr/bin/env bash
# Install (or re-install) the local Health Tracker deployment.
#
# Idempotent: safe to re-run after editing a unit. Follows the agent-server
# convention — units are SYMLINKED from this repo into ~/.config/systemd/user/, so
# the repo stays the source of truth and `git pull` updates the machine.
#
#   ./deploy/install.sh          # install/refresh units, then report status
#   ./deploy/install.sh --start  # ...and start everything
#
# NEVER run `systemctl --user disable` on these units: the unit file IS the
# symlink, and disable deletes it. Use `stop`, or remove the wants-symlink.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="${REPO}/deploy/systemd"
UNIT_DST="${HOME}/.config/systemd/user"
ENV_FILE="${HOME}/.config/health-tracker/env"
STATE_DIR="${HOME}/.local/state/health-tracker"

UNITS=(
  health-tracker-api.service
  health-tracker-queue.service
  health-tracker-daily.service
  health-tracker-daily.timer
  health-tracker-alert@.service
  # The coach's five schedules, one templated service + a timer each. These
  # replace the Cloud Scheduler jobs coach-{sweep,morning,weekly,monthly,yearly}
  # and are easy to forget: the app keeps serving the existing feed, so a missing
  # timer looks like "the coach has gone quiet" rather than a failure.
  health-tracker-coach@.service
  health-tracker-coach@sweep.timer
  health-tracker-coach@morning.timer
  health-tracker-coach@weekly.timer
  health-tracker-coach@monthly.timer
  health-tracker-coach@yearly.timer
  # The two launchd jobs that lived on the MacBook. The worker is the one that
  # makes cards appear at all — the backend only PARKS coach jobs, it never calls
  # a model itself.
  health-tracker-coach-worker.service
  health-tracker-coach-worker.timer
  health-tracker-audit.service
  health-tracker-audit.timer
  # The app calls GET /insights/weekly directly and that endpoint only ever SERVES
  # what this job wrote — without it the weekly screen stays "pending" forever.
  # (insights next-meal is deliberately NOT here: the coach supersedes it.)
  health-tracker-insights-weekly.service
  health-tracker-insights-weekly.timer
  # Mirrors the local Claude call log (calls.jsonl) into the shared Sheet. Without
  # it the log only ever exists on this machine.
  health-tracker-calls-sync.service
  health-tracker-calls-sync.timer
)
# What actually gets enabled. The daily and coach *services* are deliberately
# absent: they are started by their timers (and the daily one by a weigh-in),
# never at boot.
#
# ONLY timers and long-running services belong here. The oneshot .service units
# (daily, coach@, coach-worker, audit) are started BY their timers — enabling them
# as well would also run every one of them at boot, which for the audit means a
# full Claude re-analysis of the day's meals every time the laptop restarts.
ENABLE=(
  health-tracker-api.service
  health-tracker-queue.service
  health-tracker-daily.timer
  health-tracker-coach@sweep.timer
  health-tracker-coach@morning.timer
  health-tracker-coach@weekly.timer
  health-tracker-coach@monthly.timer
  health-tracker-coach@yearly.timer
  # The two that lived on the MacBook under launchd. coach-worker is the one that
  # makes cards appear at all — the backend only PARKS coach jobs, it never calls
  # a model itself.
  health-tracker-coach-worker.timer
  health-tracker-audit.timer
  health-tracker-insights-weekly.timer
  health-tracker-calls-sync.timer
)

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m warn:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# ---- preflight ---------------------------------------------------------------
[ -d "${REPO}/backend/venv" ] || die "no venv at backend/venv — create it first:
  python3 -m venv backend/venv
  backend/venv/bin/pip install -r backend/requirements.txt -r backend/ingest/requirements.txt gunicorn"

[ -x "${REPO}/backend/venv/bin/gunicorn" ] || die "gunicorn missing from the venv"

if [ ! -r "${ENV_FILE}" ]; then
  warn "${ENV_FILE} does not exist yet — the services will fail to start."
  warn "Copy deploy/env.example there (mode 600) and fill in the secrets."
fi

# The audit used to need its own interactively-minted token (Sheets + drive.file),
# which was the one credential that could not follow the code to a new machine. It
# now falls back to the SAME split the rest of the system already uses — service
# account for the Sheet, DRIVE_OAUTH_TOKEN for the photos — so all it needs is the
# env file, and there is nothing machine-specific left to forget.
if ! grep -q '^GOOGLE_APPLICATION_CREDENTIALS=.\+' "${ENV_FILE}" 2>/dev/null \
   || ! grep -q '^DRIVE_OAUTH_TOKEN=.\+' "${ENV_FILE}" 2>/dev/null; then
  warn "The audit needs GOOGLE_APPLICATION_CREDENTIALS + DRIVE_OAUTH_TOKEN in ${ENV_FILE}."
  warn "  Verify with: backend/venv/bin/python automation/nutrition-audit/audit.py --check"
fi

# Not fatal, but worth saying: without a real key FDC falls back to DEMO_KEY, which
# is ~30 requests/hour instead of ~1000, so grounding starts silently degrading to
# "kept the model estimate" on busy days.
if ! grep -q '^FDC_API_KEY=.\+' "${ENV_FILE}" 2>/dev/null; then
  warn "FDC_API_KEY is unset — USDA lookups will use the rate-limited DEMO_KEY."
fi

# The queue DB and the coach store both live here. Created now rather than lazily
# so a permissions problem surfaces at install time, not at 3am on a meal photo.
#
# coach-store, not coach: COACH_LOCAL_DIR is the BUCKET equivalent, and
# coach_store._local_path joins the same "coach/..." key it would use against GCS.
# Pointing it at a directory called `coach` yields coach/coach/state.json, which
# reads like a bug to whoever debugs this next.
mkdir -p "${STATE_DIR}" "${STATE_DIR}/coach-store"
mkdir -p "${UNIT_DST}"

# ---- units -------------------------------------------------------------------
say "linking units into ${UNIT_DST}"
for unit in "${UNITS[@]}"; do
  ln -sfn "${UNIT_SRC}/${unit}" "${UNIT_DST}/${unit}"
  printf '    %s\n' "${unit}"
done

systemctl --user daemon-reload

say "enabling"
systemctl --user enable "${ENABLE[@]}" >/dev/null
printf '    %s\n' "${ENABLE[@]}"

# Lingering is what lets these run with nobody logged in. agent-server already
# enables it; assert rather than assume, because without it the whole deployment
# silently stops the moment the session ends.
if ! loginctl show-user "${USER}" --property=Linger 2>/dev/null | grep -q 'Linger=yes'; then
  warn "systemd lingering is OFF for ${USER} — services will stop when you log out."
  warn "  sudo loginctl enable-linger ${USER}"
fi

if [ "${1:-}" = "--start" ]; then
  say "starting"
  systemctl --user restart health-tracker-api.service health-tracker-queue.service
  # Every timer in ENABLE, derived rather than listed again — a hand-maintained
  # second list is how the coach timers ended up enabled but never started.
  for u in "${ENABLE[@]}"; do
    case "$u" in *.timer) systemctl --user start "$u" ;; esac
  done
fi

say "status"
systemctl --user --no-pager --lines=0 status \
  health-tracker-api.service health-tracker-queue.service 2>/dev/null \
  | grep -E 'Loaded|Active|●' || true
systemctl --user list-timers 'health-tracker*' --no-pager 2>/dev/null | head -12

cat <<'NEXT'

Next:
  journalctl --user -u health-tracker-api -f        # watch the API
  curl -s -H "X-Auth-Token: $INGEST_TOKEN" http://127.0.0.1:8080/   # smoke test
  systemctl --user list-timers health-tracker-daily.timer

Expose it to the iPhone from an ELEVATED PowerShell on the Windows host:
  & 'C:\Program Files\Tailscale\tailscale.exe' serve --bg --https 443 http://127.0.0.1:8080
NEXT
