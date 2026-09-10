#!/usr/bin/env bash
#
# Production deploy for the student-admin-bot — fast-forward the checkout,
# rebuild the image, snapshot the data directory, restart the stack, and verify
# the web service answers.
#
# WHY A SCRIPT RATHER THAN FIVE COMMANDS
#   Every trap below has already cost a deploy on this host:
#
#   * A hand-edited production checkout blocks the pull. On 2026-09-09 prod
#     carried uncommitted config.yaml / docker-compose.yml edits that had since
#     been committed upstream; `git pull` aborted mid-deploy. This script
#     detects that case and tells you whether the local edits are byte-identical
#     to the incoming version (safe to discard) or genuinely yours (not).
#
#   * `docker compose up -d` without `build` silently runs the OLD image. The
#     Dockerfile COPYs src/ and runs `uv sync --frozen`, so both source and
#     dependency changes live in the image. The containers come up healthy on
#     stale code and nothing says otherwise. This script always builds.
#
#   * A chromadb major-version change in uv.lock migrates ./data/chroma IN
#     PLACE the first time the new client opens it. That is not reversible by
#     switching the image back. The script compares the locked chromadb major
#     across the incoming range and refuses to proceed without
#     --allow-chroma-migration, having taken a backup first.
#
#   * Keys added to .env.example do not appear in .env. The SSO merge added
#     KTH_OIDC_* that way: the code ships, the feature stays dark, and nothing
#     warns you. The script diffs the key NAMES added in the incoming range
#     against .env. Values are never read, printed, or logged.
#
#   * SSO enabled with an empty whitelist locks out every KTH account with a
#     403. The script checks data/web_users is present and non-empty whenever
#     KTH_OIDC_ENABLED is true.
#
# WHAT IT DELIBERATELY DOES NOT DO
#   No reindex. `scripts/reindex.py` is manual by design (CLAUDE.md), takes
#   minutes, and rewrites the vector index — not something a deploy should do
#   behind your back. The script tells you when the incoming range touches
#   ingest code and leaves the decision to you.
#
#   No tests, lint, or eval. CI gates those on main; production is not a test
#   runner. The eval harness needs the model stack and would dominate the
#   deploy. When the range touches retrieval, the gate, or the corpus, the
#   script reminds you to re-run eval/run_eval.py — thresholds are
#   model-specific and must be re-tuned, not assumed.
#
# CONFIGURATION (all optional; override via environment)
#   BOT_BRANCH        Branch to deploy. Default: main
#   BOT_SERVICES      Compose services to restart. Default: "beta-web bot"
#   BOT_HEALTH_URL    Polled after restart. Default:
#                     http://127.0.0.1:8000/api/health
#                     Authenticated by design — 401/403 counts as healthy (it
#                     proves uvicorn is serving and the auth gate is wired).
#                     Only a connection failure or 5xx is treated as down.
#   BOT_HEALTH_WAIT   Seconds to wait for the health check. Default: 120
#                     (the web service loads the embedding + reranker models
#                     on startup, which is slow on a cold page cache)
#   BOT_BACKUP_DIR    Where pre-deploy snapshots go. Default: ~/bot-deploy-backups
#   BOT_STATE_FILE    Records the commit last built here, so staleness can be
#                     judged exactly rather than guessed from image mtimes.
#   BOT_MIN_FREE_GB   Refuse to build below this much free space. Default: 8.
#                     Each build costs roughly 5 GB of cache on this host.
#   BOT_PRUNE_CACHE   Set to 0 to skip pruning build cache after a successful
#                     deploy. Default: on.
#   BOT_CACHE_KEEP_HOURS
#                     Age above which build cache is pruned. Default: 168 (7d).
#
# USAGE
#   scripts/deploy.sh                      # the normal path
#   scripts/deploy.sh --status             # read-only: what is running vs origin
#   scripts/deploy.sh -n                   # dry run: show what would happen
#   scripts/deploy.sh --rebuild            # rebuild + restart at current HEAD
#   scripts/deploy.sh --allow-chroma-migration
#   scripts/deploy.sh --rollback <sha>     # redeploy an earlier commit
#
#   Upgrading the script itself: pull first, then run the new copy —
#     git pull --ff-only && scripts/deploy.sh --rebuild
#
# See docs/DEPLOY.md for the host layer (Caddy, TLS, the Tailscale :443 clash).

# Refuse to run under `source` / `.`, before `set -euo pipefail` so those
# options never leak into the caller's interactive shell. Sourcing would make
# every `exit` below — including the ordinary "nothing to deploy" path — kill
# the operator's SSH session, which looks exactly like a crash mid-deploy.
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
  printf '[deploy] ERROR: this script must be run, not sourced.\n' >&2
  printf '         Use:  %s %s\n' "${BASH_SOURCE[0]}" "$*" >&2
  return 1 2>/dev/null || exit 1
fi

set -euo pipefail

BRANCH="${BOT_BRANCH:-main}"
SERVICES="${BOT_SERVICES:-beta-web bot}"
HEALTH_URL="${BOT_HEALTH_URL:-http://127.0.0.1:8000/api/health}"
HEALTH_WAIT="${BOT_HEALTH_WAIT:-120}"
BACKUP_DIR="${BOT_BACKUP_DIR:-$HOME/bot-deploy-backups}"

ASSUME_YES=0
DRY_RUN=0
STATUS_ONLY=0
FORCE_REBUILD=0
SKIP_BACKUP=0
ALLOW_CHROMA_MIGRATION=0
ROLLBACK_SHA=""

# Paths whose contents end up in the image or change runtime behaviour.
# An allow-list, so a NEW top-level source directory would be missed until
# added here — a visible failure (the script reports "no rebuild needed" for a
# change you know mattered), not a silent one.
BUILD_PATHS=(
  src scripts eval Dockerfile pyproject.toml uv.lock config.yaml
  docker-compose.yml topics.yaml
)

# Touching these means the vector index or the gate thresholds may no longer
# match the code. Never auto-acted on; surfaced as a reminder.
INGEST_PATHS=(src/student_bot/ingest scripts/reindex.py)
EVAL_PATHS=(src/student_bot/bot/retrieval.py src/student_bot/bot/gate.py
            src/student_bot/bot/pipeline.py src/student_bot/bot/web_retrieval.py
            src/student_bot/ingest/embed.py docs/corpus)
# pipeline.py and web_retrieval.py are listed because they build the query the
# reranker scores — programme-code and jargon expansion live there, and a
# change to either moves top1 across the board without touching retrieval.py.
# config.yaml also matters (embedding model, reranker, gate thresholds) but is
# deliberately absent: it changes on most deploys, and a reminder that always
# fires is a reminder nobody reads.

die()  { printf '\n[FATAL] %s\n' "$*" >&2; exit 1; }
info() { printf '[deploy] %s\n' "$*"; }
warn() { printf '[deploy] WARNING: %s\n' "$*" >&2; }
step() { printf '\n=== %s\n' "$*"; }

# Print the header block as help, so it cannot drift out of sync with a
# hardcoded line range as the header grows.
usage() { awk 'NR>1 { if (!/^#/) exit; sub(/^#[ ]?/, ""); print }' "$0"; exit 0; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    -y|--yes)                   ASSUME_YES=1; shift ;;
    -n|--dry-run)               DRY_RUN=1; shift ;;
    --status)                   STATUS_ONLY=1; shift ;;
    --rebuild)                  FORCE_REBUILD=1; shift ;;
    --skip-backup)              SKIP_BACKUP=1; shift ;;
    --allow-chroma-migration)   ALLOW_CHROMA_MIGRATION=1; shift ;;
    --rollback)                 ROLLBACK_SHA="${2:-}"
                                [[ -n "$ROLLBACK_SHA" ]] || die "--rollback needs a commit SHA"
                                shift 2 ;;
    -h|--help)                  usage ;;
    *)                          die "Unknown option: $1 (try --help)" ;;
  esac
done

# Operate on the repository this script belongs to, not the current directory.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

STATE_FILE="${BOT_STATE_FILE:-$REPO_ROOT/.deploy-state}"

command -v git    >/dev/null || die "git not found in PATH"
command -v curl   >/dev/null || die "curl not found (needed for the health check)"
command -v docker >/dev/null || die "docker not found in PATH"
docker compose version >/dev/null 2>&1 || die "'docker compose' is unavailable"

# A full disk fails deep inside `docker compose build`, after minutes of work,
# with a layer-export error that reads like a Docker bug rather than a full
# disk. Check up front instead. Real numbers from this host: build cache grew
# to 11.2 GB over two builds on a 30 GB disk, so roughly 5 GB per build.
MIN_FREE_GB="${BOT_MIN_FREE_GB:-8}"
check_free_space() {
  local target avail_kb avail_gb
  # Docker writes images and cache under its own root, which may live on a
  # different filesystem than the checkout.
  target="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
  [[ -d "${target:-}" ]] || target="$REPO_ROOT"
  avail_kb="$(df -Pk "$target" 2>/dev/null | awk 'NR==2 {print $4}')" || return 0
  [[ "$avail_kb" =~ ^[0-9]+$ ]] || return 0
  avail_gb=$(( avail_kb / 1024 / 1024 ))
  info "free space on $target: ${avail_gb} GB"
  if (( avail_gb < MIN_FREE_GB )); then
    die "only ${avail_gb} GB free on $target, want >= ${MIN_FREE_GB} GB.
        Reclaim build cache first (this is usually the bulk of it):
            docker builder prune -f
            docker image prune -f
        Override the threshold with BOT_MIN_FREE_GB if you know better."
  fi
}

info "repository: $REPO_ROOT"
info "services:   $SERVICES"

# ---------------------------------------------------------------------------
# Inspection helpers. Each fails softly so a missing signal degrades to a
# warning rather than aborting the deploy.
# ---------------------------------------------------------------------------

# Locked chromadb version at a git ref, e.g. "1.5.9". Empty if unreadable.
chroma_version_at() {
  git show "$1:uv.lock" 2>/dev/null | awk '
    /^name = "chromadb"$/ { want = 1; next }
    want && /^version = / { gsub(/[",]/, "", $3); print $3; exit }
  '
}

built_sha()        { [[ -f "$STATE_FILE" ]] && cat "$STATE_FILE" 2>/dev/null || return 1; }
record_built_sha() { git rev-parse HEAD > "$STATE_FILE"; }

# Compose services currently running, one per line.
running_services() { docker compose ps --services --filter status=running 2>/dev/null || true; }

changed_paths() {
  local before="$1" after="$2"; shift 2
  git diff --name-only "$before..$after" -- "$@" 2>/dev/null || true
}

# Lines added to .env.example in the incoming range. `^\+[^+]` skips the
# `+++ b/.env.example` file header, which would otherwise be parsed as content.
_env_example_added_lines() {
  git diff "$1..$2" -- .env.example 2>/dev/null | grep -E '^\+[^+]' || true
}

# Key NAMES added to .env.example, split by whether the example line is live or
# commented out. A live line is a key the deploy probably needs set; a commented
# one is an opt-in feature flag, worth mentioning once but not worth nagging
# about. Values are never read, printed, or logged.
#   $1 = live|commented   $2 = before ref   $3 = after ref
env_example_added_keys() {
  local pattern
  case "$1" in
    live)      pattern='^\+[[:space:]]*[A-Z_][A-Z0-9_]*=' ;;
    commented) pattern='^\+[[:space:]]*#[[:space:]]*[A-Z_][A-Z0-9_]*=' ;;
    *)         return 1 ;;
  esac
  _env_example_added_lines "$2" "$3" \
    | grep -E "$pattern" \
    | grep -oE '[A-Z_][A-Z0-9_]*=' \
    | sed 's/=$//' | sort -u || true
}

# Filter a list of key names on stdin down to those not set in .env.
keys_missing_from_env() {
  local present key
  present="$(env_keys_present)"
  while IFS= read -r key; do
    [[ -n "$key" ]] || continue
    grep -qx "$key" <<< "$present" || printf '%s\n' "$key"
  done
}

# Key NAMES set (uncommented) in .env. Values are never read.
env_keys_present() {
  [[ -f .env ]] || return 0
  grep -oE '^[[:space:]]*[A-Z_][A-Z0-9_]*=' .env 2>/dev/null \
    | tr -d '[:space:]' | sed 's/=$//' | sort -u
}

# True when KTH_OIDC_ENABLED is truthy in .env. Reads that one flag only.
# \042 / \047 are " and ' — spelled octally to keep the quoting legible.
# Lowercasing via tr rather than ${v,,}: macOS ships bash 3.2, where the
# ${var,,} expansion is a syntax error, and DEPLOY.md still documents a
# macOS host layout.
sso_enabled() {
  [[ -f .env ]] || return 1
  local v
  v="$(grep -E '^[[:space:]]*KTH_OIDC_ENABLED=' .env 2>/dev/null \
        | tail -1 | cut -d= -f2- \
        | tr -d '\042\047[:space:]' \
        | tr '[:upper:]' '[:lower:]')"
  case "$v" in
    1|true|yes|on) return 0 ;;
    *)             return 1 ;;
  esac
}

# ---------------------------------------------------------------------------
# Pre-deploy snapshot.
#
# Plain tar, deliberately, rather than `student-bot-backup`: the safety net
# must not depend on the image being current or the app importable — on a first
# deploy of the backup tooling the running image does not have the command at
# all. `student-bot-backup --only chroma` is the right tool for a snapshot you
# intend to KEEP or hand to someone (checksummed MANIFEST, online SQLite copy);
# this is a throwaway pre-deploy rollback point.
#
# The stack is stopped before this runs, so nothing is mid-write.
# ---------------------------------------------------------------------------
take_backup() {
  local stamp; stamp="$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$BACKUP_DIR"

  step "Pre-deploy snapshot"
  if [[ -d data/chroma ]]; then
    tar -czf "$BACKUP_DIR/chroma-$stamp.tar.gz" -C data chroma
    info "chroma  -> $BACKUP_DIR/chroma-$stamp.tar.gz ($(du -h "$BACKUP_DIR/chroma-$stamp.tar.gz" | cut -f1))"
  else
    warn "data/chroma not found — nothing to snapshot"
  fi

  if [[ -f data/logs.sqlite ]]; then
    tar -czf "$BACKUP_DIR/logs-$stamp.tar.gz" -C data logs.sqlite
    info "logs    -> $BACKUP_DIR/logs-$stamp.tar.gz ($(du -h "$BACKUP_DIR/logs-$stamp.tar.gz" | cut -f1))"
    warn "the logs archive contains qa_log student text — keep it on this host"
  fi

  if [[ -f data/web_users ]]; then
    cp data/web_users "$BACKUP_DIR/web_users-$stamp"
    info "web_users -> $BACKUP_DIR/web_users-$stamp"
  fi
}

# ---------------------------------------------------------------------------
# Build + restart + verify. Shared by the deploy, rebuild and rollback paths.
# ---------------------------------------------------------------------------
build_and_restart() {
  check_free_space
  # Build first, while the old stack still serves: this is the slow step and
  # it needs no downtime. Only then stop, snapshot, and bring the new one up.
  step "Building image"
  docker compose build

  step "Stopping $SERVICES"
  # SERVICES is an intentional word list, not one argument.
  # shellcheck disable=SC2086
  docker compose stop $SERVICES

  if [[ $SKIP_BACKUP -eq 1 ]]; then
    warn "skipping the pre-deploy snapshot (--skip-backup)"
  else
    take_backup
  fi

  step "Starting $SERVICES"
  # shellcheck disable=SC2086
  docker compose up -d $SERVICES

  record_built_sha
  info "recorded built commit $(git rev-parse --short HEAD) in $STATE_FILE"

  step "Health check"
  info "polling $HEALTH_URL for up to ${HEALTH_WAIT}s"
  local waited=0 code=000
  while [[ $waited -lt $HEALTH_WAIT ]]; do
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$HEALTH_URL" 2>/dev/null || echo 000)"
    # 401/403 mean the app is serving and the auth gate is wired — healthy.
    case "$code" in
      200|401|403) break ;;
    esac
    sleep 3; waited=$((waited + 3))
  done

  case "$code" in
    200|401|403)
      info "health check OK (HTTP $code after ${waited}s)"
      ;;
    *)
      printf '\n'
      docker compose ps || true
      docker compose logs --tail=40 beta-web || true
      die "web service did not answer at $HEALTH_URL after ${HEALTH_WAIT}s (last: $code).
        Inspect:   docker compose logs -f beta-web
        Roll back: scripts/deploy.sh --rollback <previous-sha>"
      ;;
  esac

  step "Container status"
  docker compose ps

  # Only after a green health check: keep recent cache so an immediate
  # re-deploy is still fast, drop the rest. Non-fatal — an old Docker without
  # this filter must not fail a deploy that has already succeeded.
  if [[ "${BOT_PRUNE_CACHE:-1}" != "0" ]]; then
    step "Pruning build cache older than ${BOT_CACHE_KEEP_HOURS:-168}h"
    docker builder prune -f --filter "until=${BOT_CACHE_KEEP_HOURS:-168}h" \
      || warn "build cache prune failed (harmless); run 'docker builder prune -f' by hand"
  fi
}

# ---------------------------------------------------------------------------
# Status path — read-only
# ---------------------------------------------------------------------------
if [[ $STATUS_ONLY -eq 1 ]]; then
  step "Status"
  info "branch:      $(git rev-parse --abbrev-ref HEAD)"
  info "HEAD:        $(git rev-parse --short HEAD)  $(git log -1 --format=%s | cut -c1-48)"
  if bs="$(built_sha)"; then
    if [[ "$bs" == "$(git rev-parse HEAD)" ]]; then
      info "last built:  $(git rev-parse --short "$bs") — matches HEAD"
    else
      warn "last built:  $(git rev-parse --short "$bs") — does NOT match HEAD; a rebuild is due"
    fi
  else
    warn "last built:  unknown ($STATE_FILE absent — written by the first deploy through this script)"
  fi

  if git fetch --quiet origin "$BRANCH" 2>/dev/null; then
    if [[ "$(git rev-parse HEAD)" == "$(git rev-parse "origin/$BRANCH")" ]]; then
      info "origin/$BRANCH: $(git rev-parse --short "origin/$BRANCH") — checkout in sync"
    else
      info "origin/$BRANCH: $(git rev-parse --short "origin/$BRANCH") — checkout differs"
      git --no-pager log --oneline "HEAD..origin/$BRANCH" | sed 's/^/[deploy]   /'
    fi
  else
    warn "could not fetch origin/$BRANCH — remote comparison skipped"
  fi

  if ! git diff --quiet || ! git diff --cached --quiet; then
    warn "working tree is DIRTY:"
    git --no-pager status --short | sed 's/^/[deploy]   /'
  fi

  step "Containers"
  docker compose ps
  exit 0
fi

# ---------------------------------------------------------------------------
# Forced rebuild path
# ---------------------------------------------------------------------------
if [[ $FORCE_REBUILD -eq 1 ]]; then
  step "Rebuilding at $(git rev-parse --short HEAD) (no pull)"
  if [[ $DRY_RUN -eq 1 ]]; then info "dry run: would rebuild and restart"; exit 0; fi
  if [[ $ASSUME_YES -eq 0 ]]; then
    read -r -p "Rebuild and restart $SERVICES at $(git rev-parse --short HEAD)? [yes/N] " reply
    [[ "$reply" == "yes" ]] || die "aborted"
  fi
  build_and_restart
  printf '\n[deploy] Rebuilt at %s.\n' "$(git rev-parse --short HEAD)"
  exit 0
fi

# ---------------------------------------------------------------------------
# Rollback path
# ---------------------------------------------------------------------------
if [[ -n "$ROLLBACK_SHA" ]]; then
  git rev-parse --verify --quiet "${ROLLBACK_SHA}^{commit}" >/dev/null \
    || die "not a commit in this repository: $ROLLBACK_SHA"
  TARGET="$(git rev-parse --short "$ROLLBACK_SHA")"
  warn "rolling back to $TARGET — leaves a detached HEAD"
  warn "this does NOT undo a chromadb persist-directory migration: if the"
  warn "deploy you are undoing migrated data/chroma, restore the snapshot from"
  warn "$BACKUP_DIR after this finishes, then rebuild."
  if [[ $DRY_RUN -eq 1 ]]; then info "dry run: would roll back to $TARGET"; exit 0; fi
  if [[ $ASSUME_YES -eq 0 ]]; then
    read -r -p "Roll back to $TARGET? [yes/N] " reply
    [[ "$reply" == "yes" ]] || die "aborted"
  fi
  git checkout --quiet "$ROLLBACK_SHA"
  build_and_restart
  printf '\n[deploy] Rolled back to %s. Return to the branch with: git checkout %s\n' "$TARGET" "$BRANCH"
  exit 0
fi

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
step "Preflight"

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[[ "$CURRENT_BRANCH" == "$BRANCH" ]] \
  || die "on branch '$CURRENT_BRANCH', expected '$BRANCH'. Set BOT_BRANCH to deploy something else."

info "fetching origin/$BRANCH"
git fetch --quiet origin "$BRANCH"

BEFORE="$(git rev-parse HEAD)"
AFTER="$(git rev-parse "origin/$BRANCH")"

# A dirty tree means someone edited production in place. Deploying over it
# either fails the fast-forward or silently discards their work. Distinguish
# the two cases rather than just refusing: on 2026-09-09 the local edits were
# byte-identical to the incoming commit, so discarding them was a no-op — but
# that is only safe to say after checking.
if ! git diff --quiet || ! git diff --cached --quiet; then
  warn "working tree is dirty:"
  git --no-pager status --short | sed 's/^/[deploy]   /'
  if git diff --quiet "origin/$BRANCH" -- . 2>/dev/null; then
    info "...but every modified file is byte-identical to origin/$BRANCH."
    info "These edits are already committed upstream; discarding them is a no-op:"
    info "    git checkout -- ."
  else
    info "Files differing from origin/$BRANCH:"
    git --no-pager diff --name-only "origin/$BRANCH" -- . | sed 's/^/[deploy]   /'
  fi
  die "refusing to deploy over local edits. Commit, stash, or discard them first."
fi

info "current commit: $(git rev-parse --short "$BEFORE") $(git log -1 --format=%s | cut -c1-60)"

if [[ "$BEFORE" == "$AFTER" ]]; then
  info "already at origin/$BRANCH — nothing to pull"
  if bs="$(built_sha)" && [[ "$bs" == "$BEFORE" ]]; then
    info "the running image was built from this commit — nothing to deploy"
    exit 0
  fi
  warn "the checkout is current but the last build does not match (or is unknown)"
  warn "rebuilding in place — same as: scripts/deploy.sh --rebuild"
  if [[ $DRY_RUN -eq 1 ]]; then info "dry run: would rebuild and restart"; exit 0; fi
  if [[ $ASSUME_YES -eq 0 ]]; then
    read -r -p "Rebuild and restart at $(git rev-parse --short HEAD)? [yes/N] " reply
    [[ "$reply" == "yes" ]] || die "aborted"
  fi
  build_and_restart
  exit 0
fi

git merge-base --is-ancestor "$BEFORE" "$AFTER" \
  || die "local $BRANCH has diverged from origin/$BRANCH — refusing to deploy.
        Production must fast-forward only.
        Investigate: git log --oneline --graph $BRANCH origin/$BRANCH"

step "Incoming changes ($(git rev-parse --short "$BEFORE") → $(git rev-parse --short "$AFTER"))"
git --no-pager log --oneline "$BEFORE..$AFTER"
printf '\n'
git --no-pager diff --stat "$BEFORE..$AFTER" | tail -20

# ---------------------------------------------------------------------------
# Hazard checks
# ---------------------------------------------------------------------------
step "Checks"

CHROMA_BEFORE="$(chroma_version_at "$BEFORE")"
CHROMA_AFTER="$(chroma_version_at "$AFTER")"
if [[ -n "$CHROMA_BEFORE" && -n "$CHROMA_AFTER" && "$CHROMA_BEFORE" != "$CHROMA_AFTER" ]]; then
  info "chromadb: $CHROMA_BEFORE -> $CHROMA_AFTER"
  if [[ "${CHROMA_BEFORE%%.*}" != "${CHROMA_AFTER%%.*}" ]]; then
    warn "MAJOR chromadb change. ./data/chroma is migrated IN PLACE the first"
    warn "time the new client opens it, and switching the image back does not"
    warn "undo that — only restoring the snapshot does."
    if [[ $ALLOW_CHROMA_MIGRATION -eq 0 ]]; then
      die "refusing to deploy a chromadb major upgrade without --allow-chroma-migration.
        Re-run as: scripts/deploy.sh --allow-chroma-migration
        (a snapshot is taken automatically unless you also pass --skip-backup)"
    fi
    if [[ $SKIP_BACKUP -eq 1 ]]; then
      die "--skip-backup with a chromadb major upgrade would leave no way back. Refusing."
    fi
    warn "proceeding with the migration (--allow-chroma-migration)"
  fi
else
  info "chromadb: unchanged${CHROMA_AFTER:+ ($CHROMA_AFTER)}"
fi

MISSING_REQUIRED="$(env_example_added_keys live "$BEFORE" "$AFTER" | keys_missing_from_env)"
MISSING_OPTIONAL="$(env_example_added_keys commented "$BEFORE" "$AFTER" | keys_missing_from_env)"

if [[ -n "$MISSING_REQUIRED" ]]; then
  warn ".env.example gained these keys as live settings, and .env does not set them:"
  printf '%s\n' "$MISSING_REQUIRED" | sed 's/^/[deploy]   /' >&2
  warn "the code ships either way — if the feature is meant to be on, set them"
fi
if [[ -n "$MISSING_OPTIONAL" ]]; then
  info "optional keys newly documented in .env.example (commented out there, unset here):"
  printf '%s\n' "$MISSING_OPTIONAL" | sed 's/^/[deploy]   /'
fi
if [[ -z "$MISSING_REQUIRED$MISSING_OPTIONAL" ]]; then
  info ".env: no new keys introduced by this range"
fi

if sso_enabled; then
  if [[ ! -s data/web_users ]]; then
    die "KTH_OIDC_ENABLED is true but data/web_users is missing or empty.
        Every KTH account would authenticate and then get a 403.
        Add users first: docker compose run --rm beta-web student-bot-mkuser <kthid>"
  fi
  info "SSO enabled; data/web_users has $(grep -cvE '^\s*(#|$)' data/web_users) entr(y|ies)"
fi

if [[ -n "$(changed_paths "$BEFORE" "$AFTER" "${INGEST_PATHS[@]}")" ]]; then
  warn "ingest code changed — the existing index was built by the old code."
  warn "consider: docker compose run --rm beta-web python -m scripts.reindex"
fi

if [[ -n "$(changed_paths "$BEFORE" "$AFTER" "${EVAL_PATHS[@]}")" ]]; then
  warn "retrieval / gate / corpus changed — gate thresholds are model-specific."
  warn "re-run eval/run_eval.py and re-tune rerank_top1_min / rerank_meanK_min."
fi

if [[ -z "$(changed_paths "$BEFORE" "$AFTER" "${BUILD_PATHS[@]}")" ]]; then
  info "no changes under the image paths — this is a docs-only deploy"
fi

# ---------------------------------------------------------------------------
# Confirm and go
# ---------------------------------------------------------------------------
if [[ $DRY_RUN -eq 1 ]]; then
  printf '\n'
  info "dry run: nothing changed. Would deploy $(git rev-parse --short "$AFTER")."
  exit 0
fi

if [[ $ASSUME_YES -eq 0 ]]; then
  printf '\n'
  read -r -p "Deploy $(git rev-parse --short "$AFTER") to $SERVICES? [yes/N] " reply
  [[ "$reply" == "yes" ]] || die "aborted"
fi

step "Fast-forwarding to origin/$BRANCH"
# merge --ff-only rather than `git pull`: a production checkout must never grow
# a merge commit, whatever the local pull.rebase / pull.ff configuration says.
git merge --ff-only "origin/$BRANCH"

build_and_restart

printf '\n[deploy] Done. %s → %s\n' "$(git rev-parse --short "$BEFORE")" "$(git rev-parse --short "$AFTER")"
printf '[deploy] Roll back with: scripts/deploy.sh --rollback %s\n' "$(git rev-parse --short "$BEFORE")"
