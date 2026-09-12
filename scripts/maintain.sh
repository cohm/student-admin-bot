#!/usr/bin/env bash
#
# Weekly unattended corpus refresh for the student-admin-bot: eval -> snapshot
# -> scrape -> reindex -> eval -> restart -> report.
#
# Run from cron on the production VM. Everything goes through containers; the
# VM has no `uv`.
#
# WHY THE EVAL RUNS TWICE
#   Running it only afterwards tells you the index is worse, not why. With a
#   baseline from immediately before the scrape, a drop is attributable: the
#   corpus is the only thing that changed in between. On 2026-09-12 KTH moved
#   the ITM programansvariga list to intra.kth.se and that page fell from 35
#   chunks to 5 — recall@5 caught it, and every "who is PA for ..." question
#   would otherwise have quietly stopped working.
#
# WHY IT RESTARTS THE STACK AT THE END — THE NON-OBVIOUS ONE
#   A reindex by another process is NOT picked up by the running services.
#   Chroma's HNSW segment is read into memory the first time a process queries
#   a collection and is not reloaded afterwards. Verified against chromadb
#   1.5.9 (two processes, one persist directory): after an external upsert the
#   reader's `count()` rose from 1 to 3 — that reads SQLite — while `query()`
#   kept returning only the pre-existing id. Opening a *fresh* PersistentClient
#   in the same process did not help either: chromadb caches the System per
#   path, so it is the same in-memory segment. Only a new process saw the new
#   vectors.
#
#   So without the restart, a reindex writes a new index that nothing serves:
#   the eval (a fresh container each run) reports the new numbers and looks
#   green, while `web` and `mattermost` keep answering from whatever they
#   loaded at startup. That is the worst kind of failure — invisible, and it
#   makes the eval actively misleading.
#
# WHY IN-PLACE RATHER THAN STAGE-AND-PROMOTE
#   `scripts/reindex.py` rebuilds `data/chroma` in place, so by the time the
#   second eval runs, the new index is already on disk. Building to a staging
#   directory and promoting only on green would be the stricter design, but it
#   doubles the index on a 30 GB VM that has already filled once, and the
#   failure it guards against is recoverable in seconds from the snapshot this
#   script takes at step 2.
#
#   The deliberate consequence: this job never decides on its own to roll back.
#   It reports, and puts the exact restore command in the report.
#
# EXIT CODES
#   0  green
#   2  warnings (churn, a new recall miss, gate wobble) — look, don't panic
#   3  recall@5 fell — the index lost something it used to find
#   1  the run itself failed (scrape, reindex, docker, lock held)
#
# CONFIGURATION (all optional; override via environment)
#   BOT_MAINT_DIR        Reports and logs. Default: <repo>/data/maintenance
#   BOT_MAINT_KEEP       Chroma snapshots to retain. Default: 4 (a month)
#   BOT_MAINT_NOTIFY     '@user' / '#channel' for Mattermost. Default:
#                        notify.mattermost_target in config.yaml. ntfy, if
#                        NTFY_TOPIC is set, always fires alongside it.
#   BOT_HEALTH_URL       Polled after the restart, to confirm the service came
#                        back. Default: http://127.0.0.1:8000/api/health.
#                        Authenticated by design — 401/403 count as healthy.
#   BOT_HEALTH_WAIT      Seconds to wait for it. Default: 180 (the models load
#                        on startup, and the page cache is cold after a reindex
#                        has just walked the whole corpus).
#   BOT_MAINT_MAX_MINUTES  Report the run as slow above this. Default: 45.
#                        Steady state is ~9 min and the worst catch-up run
#                        measured was 21 min, so 45 means "stuck", not "busy".
#                        Advisory: reported, never enforced mid-run.
#   BOT_MAINT_MIN_FREE_GB  Refuse to start below this. Default: 4
#   BOT_MAINT_MAX_CHURN  Chunk delta worth mentioning when green. Default: 150
#   BOT_SERVICES         Services to restart. Default: "web mattermost"
#   BOT_MAINT_SKIP_SCRAPE  Reindex from the corpus on disk, no network fetch.
#
# USAGE
#   scripts/maintain.sh              # the weekly run
#   scripts/maintain.sh -n           # dry run: guards + plan, no side effects
#   scripts/maintain.sh --no-notify  # run for real, print instead of posting
#
#   Cron (Sunday 04:00). Deliberately AFTER the host finishes snapshotting this
#   VM at 03:00, not squeezed into the 02:17-03:00 gap: nothing downstream is
#   waiting, so an occasional long catch-up run costs nothing and no schedule
#   arithmetic has to hold for the job to be safe.
#
#     0 4 * * 0 cd $HOME/student-admin-bot && scripts/maintain.sh \
#         >> $HOME/bot-maintenance.log 2>&1
#
# See docs/DEPLOY.md for the surrounding host operations.

if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
  printf '[maintain] ERROR: this script must be run, not sourced.\n' >&2
  return 1 2>/dev/null || exit 1
fi

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

MAINT_DIR="${BOT_MAINT_DIR:-$REPO_ROOT/data/maintenance}"
KEEP="${BOT_MAINT_KEEP:-4}"
MAX_MINUTES="${BOT_MAINT_MAX_MINUTES:-45}"
HEALTH_URL="${BOT_HEALTH_URL:-http://127.0.0.1:8000/api/health}"
HEALTH_WAIT="${BOT_HEALTH_WAIT:-180}"
MIN_FREE_GB="${BOT_MAINT_MIN_FREE_GB:-4}"
MAX_CHURN="${BOT_MAINT_MAX_CHURN:-150}"
SERVICES="${BOT_SERVICES:-web mattermost}"
NOTIFY_TARGET="${BOT_MAINT_NOTIFY:-}"

DRY_RUN=0
DO_NOTIFY=1
SKIP_SCRAPE="${BOT_MAINT_SKIP_SCRAPE:-0}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--dry-run)   DRY_RUN=1; shift ;;
    --no-notify)    DO_NOTIFY=0; shift ;;
    --skip-scrape)  SKIP_SCRAPE=1; shift ;;
    -h|--help)      awk 'NR>1 { if (!/^#/) exit; sub(/^#[ ]?/, ""); print }' "$0"; exit 0 ;;
    *)              printf '[maintain] unknown option: %s (try --help)\n' "$1" >&2; exit 1 ;;
  esac
done

info() { printf '[maintain] %s\n' "$*"; }
warn() { printf '[maintain] WARNING: %s\n' "$*" >&2; }
step() { printf '\n=== %s (%s)\n' "$*" "$(date '+%H:%M:%S')"; }
die()  { printf '\n[maintain] FATAL: %s\n' "$*" >&2; notify_failure "$*"; exit 1; }

STARTED_EPOCH=$(date +%s)
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------
command -v docker >/dev/null || { echo "[maintain] FATAL: docker not found" >&2; exit 1; }
command -v curl   >/dev/null || { echo "[maintain] FATAL: curl not found" >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "[maintain] FATAL: no 'docker compose'" >&2; exit 1; }

mkdir -p "$MAINT_DIR"

# One run at a time. A weekly job that overlaps itself would have two
# processes rewriting data/chroma, which is not a recoverable state. `noclobber`
# makes the create-or-fail atomic without needing flock (absent on some minimal
# images).
LOCK="$MAINT_DIR/.lock"
if ! (set -o noclobber; printf '%s pid=%s\n' "$(date -Is)" "$$" > "$LOCK") 2>/dev/null; then
  printf '[maintain] FATAL: another run holds %s:\n' "$LOCK" >&2
  cat "$LOCK" >&2 || true
  printf '           If no maintain.sh is running, remove it and re-run.\n' >&2
  exit 1
fi
cleanup() { rm -f "$LOCK"; }
trap cleanup EXIT

# A reindex needs room for a second copy of the HNSW segments while it writes.
avail_kb="$(df -Pk "$REPO_ROOT" 2>/dev/null | awk 'NR==2 {print $4}')" || avail_kb=""
if [[ "$avail_kb" =~ ^[0-9]+$ ]]; then
  avail_gb=$(( avail_kb / 1024 / 1024 ))
  info "free space: ${avail_gb} GB"
  if (( avail_gb < MIN_FREE_GB )); then
    echo "[maintain] FATAL: only ${avail_gb} GB free, want >= ${MIN_FREE_GB} GB" >&2
    exit 1
  fi
fi

BEFORE_JSON="$MAINT_DIR/eval-$STAMP-before.json"
AFTER_JSON="$MAINT_DIR/eval-$STAMP-after.json"
REPORT="$MAINT_DIR/report-$STAMP.md"

# Paths as the container sees them. Only ./data is bind-mounted (at /app/data),
# so a MAINT_DIR outside it would leave the eval writing its JSON *inside* a
# `--rm` container: the file vanishes, the comparison silently has nothing to
# read, and the job reports "could not judge" every week. Refuse instead.
case "$MAINT_DIR" in
  "$REPO_ROOT"/data/*) ;;
  *) echo "[maintain] FATAL: BOT_MAINT_DIR must live under $REPO_ROOT/data" >&2
     echo "           (only ./data is bind-mounted into the containers)" >&2
     exit 1 ;;
esac
c_maint="/app/data/${MAINT_DIR#"$REPO_ROOT"/data/}"
c_before="$c_maint/$(basename "$BEFORE_JSON")"
c_after="$c_maint/$(basename "$AFTER_JSON")"

dc() { docker compose "$@"; }

# notify <body> <severity>. Severity drives ntfy's priority (and whether a
# channel fires at all), so it must reflect the verdict, not the fact that a
# message exists.
notify() {
  local body="$1" severity="${2:-ok}" target_args=()
  [[ -n "$NOTIFY_TARGET" ]] && target_args=(--to "$NOTIFY_TARGET")
  if [[ $DO_NOTIFY -eq 0 ]]; then
    info "notification suppressed (--no-notify); report follows"
    printf '%s\n' "$body"
    return 0
  fi
  # `${a[@]+"${a[@]}"}` rather than `"${a[@]}"`: expanding an EMPTY array is an
  # unbound-variable error under `set -u` in bash before 4.4, and the failure
  # lands here — on the notification, on the run where there is no explicit
  # --to. Losing the alert is the one failure this job cannot have.
  #
  # Never let a notification failure mask the result of the run itself: the
  # report is already on disk and in this log either way.
  if ! printf '%s' "$body" \
      | dc run --rm -T web student-bot-notify --severity "$severity" \
          ${target_args[@]+"${target_args[@]}"}; then
    warn "could not post the report to Mattermost; it is at $REPORT"
  fi
}

notify_failure() {
  # Called from die(), i.e. mid-run. Keep it short and dependency-free.
  [[ $DRY_RUN -eq 1 ]] && return 0
  # Deliberately does NOT claim the stack is untouched — whether it is depends
  # on which step died, and the message from die() already says. Claiming it
  # here would contradict the reindex case, where the index IS half-written.
  notify "**Weekly maintenance FAILED on $(hostname)**

\`\`\`
$1
\`\`\`
Full log: \`$HOME/bot-maintenance.log\`" critical || true
}

# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------
info "repository: $REPO_ROOT"
info "reports:    $MAINT_DIR"
info "version:    $(git describe --tags --exact-match HEAD 2>/dev/null || git rev-parse --short HEAD)"
[[ "$SKIP_SCRAPE" == "1" ]] && info "scrape:     SKIPPED (--skip-scrape)"

if [[ $DRY_RUN -eq 1 ]]; then
  cat <<PLAN

[maintain] dry run — would, in order:
  1. eval (baseline)          -> $BEFORE_JSON
  2. snapshot chroma          -> data/backups/, keeping $KEEP
  3. scrape the URL corpus    $( [[ "$SKIP_SCRAPE" == "1" ]] && echo "(skipped)" )
  4. reindex data/chroma      (in place)
  5. eval (after)             -> $AFTER_JSON
  6. restart: $SERVICES       (required — see the header)
  7. poll $HEALTH_URL, and confirm every service is still running
  8. compare and report to ${NOTIFY_TARGET:-<notify.mattermost_target>}
PLAN
  exit 0
fi

# ---------------------------------------------------------------------------
# 1. Baseline eval. A failure here is not fatal: the run is still worth doing,
#    it just loses the before/after comparison and says so.
# ---------------------------------------------------------------------------
step "Baseline eval"
t0=$(date +%s)
if ! dc run --rm -T web python -m eval.run_eval --json-out "$c_before"; then
  warn "baseline eval failed — continuing without a comparison point"
  rm -f "$BEFORE_JSON"
fi
EVAL_BEFORE_S=$(( $(date +%s) - t0 ))

# ---------------------------------------------------------------------------
# 2. Snapshot. This is what makes "report, don't roll back" a safe default.
#    --only chroma: rotation counts per component-set, so these never age out
#    the nightly full archives.
# ---------------------------------------------------------------------------
step "Snapshot of the current index"
t0=$(date +%s)
dc run --rm -T web student-bot-backup --only chroma --keep "$KEEP" \
  || die "snapshot failed — refusing to reindex without a rollback point"
SNAPSHOT_S=$(( $(date +%s) - t0 ))
RESTORE_HINT="$(ls -1t data/backups/*chroma*.tar.gz 2>/dev/null | head -1 || true)"

# ---------------------------------------------------------------------------
# 3. Scrape. The one service with the corpus mounted read-write.
# ---------------------------------------------------------------------------
SCRAPE_S=0
if [[ "$SKIP_SCRAPE" != "1" ]]; then
  step "Scraping the URL corpus"
  t0=$(date +%s)
  dc run --rm -T scrape || die "scrape failed — corpus and index left untouched"
  SCRAPE_S=$(( $(date +%s) - t0 ))
fi

# ---------------------------------------------------------------------------
# 4. Reindex, in place.
# ---------------------------------------------------------------------------
step "Reindexing"
t0=$(date +%s)
REINDEX_LOG="$MAINT_DIR/reindex-$STAMP.log"
if ! dc run --rm -T web python -m scripts.reindex 2>&1 | tee "$REINDEX_LOG"; then
  die "reindex failed. The index may be half-written; restore with:
    tar -xzf ${RESTORE_HINT:-data/backups/<newest chroma archive>} -C data
    docker compose restart $SERVICES"
fi
REINDEX_S=$(( $(date +%s) - t0 ))
# The summary line is `upserted=N deleted=M collection_size=K`.
REINDEX_SUMMARY="$(grep -Eo 'upserted=[0-9]+ deleted=[0-9]+ collection_size=[0-9]+' \
  "$REINDEX_LOG" | tail -1 || true)"

# ---------------------------------------------------------------------------
# 5. Eval again — a fresh container, so it reads the new index from disk.
# ---------------------------------------------------------------------------
step "Eval after reindex"
t0=$(date +%s)
dc run --rm -T web python -m eval.run_eval --json-out "$c_after" \
  || die "post-reindex eval failed to run. Index is the NEW one; restore with:
    tar -xzf ${RESTORE_HINT:-data/backups/<newest chroma archive>} -C data
    docker compose restart $SERVICES"
EVAL_AFTER_S=$(( $(date +%s) - t0 ))

# ---------------------------------------------------------------------------
# 6. Restart, so the running services actually serve the new index.
#    See the header: without this the reindex is invisible to students.
# ---------------------------------------------------------------------------
step "Restarting $SERVICES to pick up the new index"
# shellcheck disable=SC2086
dc restart $SERVICES || die "restart failed — services may still be serving the OLD index"

# `docker compose restart` returns when the CONTAINERS are up, not when the app
# is serving: web loads bge-m3 and the cross-encoder on startup, which takes
# appreciably longer than the restart itself. Without this poll the job would
# report a cheerful green while the service that is supposed to answer students
# never came back — at 04:00, unattended, with nobody to notice until morning.
# Same semantics as scripts/deploy.sh: 401/403 prove uvicorn is serving and the
# auth gate is wired, so they count as healthy.
step "Waiting for $HEALTH_URL"
t0=$(date +%s)
waited=0
code=000
while (( waited < HEALTH_WAIT )); do
  # curl already prints 000 on a connection failure, so `|| echo 000` would
  # concatenate and report "000000". Catch the non-zero exit instead.
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$HEALTH_URL" 2>/dev/null)" || code=""
  code="${code:-000}"
  case "$code" in 200|401|403) break ;; esac
  sleep 3
  waited=$(( waited + 3 ))
done
case "$code" in
  200|401|403) info "healthy (HTTP $code after ${waited}s)" ;;
  *)
    dc ps || true
    dc logs --tail=40 web || true
    die "web did not answer at $HEALTH_URL after ${HEALTH_WAIT}s (last: $code).
    The index was rebuilt and the eval ran; it is the RESTART that failed.
    Inspect: docker compose logs -f web" ;;
esac

# The health poll only covers `web`. The Mattermost bot has no HTTP endpoint
# and is the interface most students actually use, so it would fail silently:
# a bad config throws on startup, `restart: unless-stopped` loops it, and the
# job reports green while the bot is answering nobody.
#
# Settle first. `docker compose restart` returns once the container is running,
# which is before a startup exception has had time to happen — checking
# immediately would see "running" for a container about to die.
sleep 5
running="$(dc ps --services --filter status=running 2>/dev/null || true)"
for svc in $SERVICES; do
  if ! printf '%s\n' "$running" | grep -qx "$svc"; then
    dc ps || true
    dc logs --tail=40 "$svc" || true
    die "service '$svc' is not running after the restart (crashed, or looping).
    The index was rebuilt and the eval ran; it is the RESTART that failed.
    Inspect: docker compose logs -f $svc"
  fi
done
info "running: $(printf '%s' "$SERVICES")"
RESTART_S=$(( $(date +%s) - t0 ))

# ---------------------------------------------------------------------------
# 7. Compare and report.
# ---------------------------------------------------------------------------
step "Comparing"
compare_args=(--after "$c_after" --max-churn "$MAX_CHURN"
              --title "Weekly maintenance on $(hostname)")
[[ -f "$BEFORE_JSON" ]] && compare_args+=(--before "$c_before")

# stderr goes to a FILE, not into the captured output. `docker compose run`
# writes its progress there ("Container ... Creating"), and with 2>&1 those two
# lines landed at the top of every report — in the Mattermost post and the ntfy
# push, above the headline. Keep them for diagnosis, out of the message.
COMPARE_ERR="$MAINT_DIR/compare-$STAMP.err"
set +e
COMPARISON="$(dc run --rm -T web python -m scripts.eval_compare "${compare_args[@]}" \
              2>"$COMPARE_ERR")"
VERDICT=$?
set -e

# Exit 1 from eval_compare means the comparison itself broke (unreadable
# report, missing file) — NOT that the index is fine. An empty section here
# would read as "nothing to say", which is the opposite of the truth.
if (( VERDICT == 1 )); then
  # Here the stderr IS the diagnosis, so fold it in — docker noise and all.
  COMPARISON="**Weekly maintenance on $(hostname) — ⚠️ could not judge the result**

The reindex completed, but comparing the evals failed:

\`\`\`
$COMPARISON
$(tail -5 "$COMPARE_ERR" 2>/dev/null)
\`\`\`
Check \`$AFTER_JSON\` by hand."
  VERDICT=2
fi

# Measured here, after the comparison, rather than before it: the compare and
# the notification each start a container, and a duration that silently omits
# them is the wrong number to track drift against.
TOTAL_S=$(( $(date +%s) - STARTED_EPOCH ))

fmt() { printf '%dm %02ds' $(( $1 / 60 )) $(( $1 % 60 )); }

TIMINGS="| phase | duration |
|---|---:|
| eval (baseline) | $(fmt $EVAL_BEFORE_S) |
| snapshot | $(fmt $SNAPSHOT_S) |
| scrape | $(fmt $SCRAPE_S) |
| reindex | $(fmt $REINDEX_S) |
| eval (after) | $(fmt $EVAL_AFTER_S) |
| restart + health | $(fmt ${RESTART_S:-0}) |
| **total** | **$(fmt $TOTAL_S)** |"

# Duration, not wall-clock: the job runs at 04:00, after the host snapshot, so
# finishing late is no longer a safety problem — but a run that takes several
# times as long as usual still means something is wrong (a scrape retrying, a
# corpus that grew by hundreds of files, a starved VM).
DEADLINE_NOTE=""
if (( TOTAL_S > MAX_MINUTES * 60 )); then
  DEADLINE_NOTE="
⏰ Took $(fmt $TOTAL_S), over the ${MAX_MINUTES} min expected. Steady state is
~9 min; check the scrape and reindex timings above."
  warn "run took $(fmt $TOTAL_S), over ${MAX_MINUTES} min"
fi

{
  printf '%s\n\n' "$COMPARISON"
  [[ -n "$REINDEX_SUMMARY" ]] && printf '`%s`\n\n' "$REINDEX_SUMMARY"
  printf '%s\n' "$TIMINGS"
  [[ -n "$DEADLINE_NOTE" ]] && printf '%s\n' "$DEADLINE_NOTE"
  if (( VERDICT >= 2 )); then
    printf '\nRestore the previous index if this looks wrong:\n'
    printf '```\ntar -xzf %s -C data\ndocker compose restart %s\n```\n' \
      "${RESTORE_HINT:-data/backups/<newest chroma archive>}" "$SERVICES"
  fi
} > "$REPORT"

info "report -> $REPORT"
case "$VERDICT" in
  0) SEVERITY=ok ;;
  3) SEVERITY=critical ;;
  *) SEVERITY=warn ;;
esac
notify "$(cat "$REPORT")" "$SEVERITY"

printf '\n[maintain] done in %s (verdict: %s)\n' "$(fmt $TOTAL_S)" "$VERDICT"
exit "$VERDICT"
