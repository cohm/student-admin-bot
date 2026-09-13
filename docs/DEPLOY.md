# Deployment & host operations

Host-level notes for running the bot publicly: the reverse proxy that fronts
the Docker stack, the **Tailscale ↔ nginx port-443 clash** (the one that bites
after every reboot), and how to reach a **LiteLLM gateway** on the tailnet.
The app itself is covered in [`README.md`](../README.md) → *Docker (Compose)*;
this file is only the layer **in front of and around** the container.

---

## Reverse proxy (TLS)

The container binds **`127.0.0.1:8000`** only (see `docker-compose.yml`,
`web`). Public access goes through a reverse proxy that terminates TLS on
**:443**:

```
https://chatbot.particle.kth.se/   --proxy-->   http://127.0.0.1:8000/   (Docker)
```

- **The app is served at the site root**, via `WEB_BASE_PATH: ""` in compose.
  It used to sit under a `/betabot/` prefix on a shared host; it moved to its
  own subdomain on 2026-06-24 and the prefix is gone. `WEB_BASE_PATH` still
  exists for hosts that need a prefix — set it and the proxy must pass the
  prefix through unchanged, **not** strip it.
- The current VM uses **Caddy**. The nginx notes below are from the earlier
  macOS host and are kept because the 443 clash they describe is not
  nginx-specific.
- Auth is two layers (`README.md` → *Web app*): first visit with
  `?access=<WEB_ACCESS_TOKEN>` sets the session cookie, then HTTP Basic from
  `data/web_users` (`student-bot-mkuser <name>`). With KTH SSO enabled, `/`
  redirects to ADFS instead and the account must also appear in
  `data/web_users`. A bare `/` returning **403** is the gate working, not an
  outage.

### nginx must run as root, and survive reboot

Binding :80/:443 requires **root** — nginx started as your user (plain
`brew services start nginx`) can **never** bind 443. Start it as root so
Homebrew installs a **LaunchDaemon** (boots at startup, as root, no login
needed):

```bash
sudo brew services start nginx     # installs /Library/LaunchDaemons/homebrew.mxcl.nginx.plist
```

Brew prints *"must be run as non-root to start at user login"* — ignore it;
the LaunchDaemon is exactly what you want for an always-on public service.
Validate config before (re)starting: `nginx -t`.

---

## ⚠️ The Tailscale ↔ port-443 clash (recurring after reboot)

**Symptom:** the bot is unreachable from the public URL even though the Docker
containers are healthy. nginx fails to start with:

```
[emerg] bind() to 0.0.0.0:443 failed (48: Address already in use)
```

**Cause:** `tailscale serve` / `funnel` binds the host's **:443** (via
`tailscaled`, as **root**), and Tailscale re-applies that config on boot —
grabbing 443 before nginx. Because the socket is root-owned it's **invisible to
non-sudo `lsof`**; confirm with `netstat`:

```bash
netstat -an -p tcp | grep '\.443 '        # shows *.443 LISTEN even when lsof (no sudo) sees nothing
curl -sk https://127.0.0.1/               # HANGS when Tailscale holds 443 (it only serves tailnet traffic)
tailscale serve status                    # shows the conflicting :443 serve config
```

**Fix:**

```bash
tailscale serve reset            # frees 443; does NOT touch tailnet VPN / SSH / screen-sharing
sudo brew services start nginx   # rebind 443 as root
curl -sk -o /dev/null -w "%{http_code}\n" https://127.0.0.1/   # expect 403 (= chain works)
```

**The rule:** on any host that fronts the public site, **never configure
`tailscale serve --https=443`**. The host's 443 belongs to nginx. If you need a
tailnet-only HTTPS service on that machine, put it behind nginx or give it a
different port. `tailscale serve reset` only removes the HTTPS *proxy* config —
it does **not** disconnect Tailscale, so VPN, SSH and screen-sharing keep working.

---

## LiteLLM gateway on the tailnet (LLM provider)

The bot reaches any OpenAI-compatible endpoint via the generic provider in
`bot/llm.py` (`_stream_chat_openai`). A LiteLLM proxy is just another provider —
**config only, no code**. Current setup (`config.yaml` → `llm.providers.litellm`):

```yaml
litellm:
  kind: openai_compatible
  base_url: http://100.75.42.33:4000/v1   # 100.x tailnet IP of the gateway host
  display_name: LiteLLM (lokal Qwen via Spark)
  api_key_env: LITELLM_API_KEY
  timeout_seconds: 120
  discloses_external: false               # models run LOCALLY on the Spark — suppress the cloud notice
```

Key points:

- **Use the `100.x` IP, not the MagicDNS `.ts.net` name**, in `base_url`. A
  Docker container on a native Linux host won't resolve MagicDNS unless you
  point it at Tailscale's resolver (`--dns 100.100.100.100`); the IP avoids that.
- **Plain `http://` is fine** — the tailnet encrypts in transit (WireGuard). No
  need to put the gateway behind TLS.
- **`LITELLM_API_KEY`** in `.env` is the gateway **virtual key** (scoped/rotatable
  from the LiteLLM UI → Virtual Keys). The loader auto-reads any provider's
  `api_key_env` (`config.py`), sent as `Authorization: Bearer`.
- **`discloses_external: false`** marks this provider as local so the "external
  cloud model, don't share sensitive info" notice does **not** fire (it's keyed
  on this flag, not on `provider_kind`). Default (`None`) derives from kind:
  `ollama` → local, everything else → external. **Set `false` only for gateways
  whose models actually run locally** — keep the student-scoped key restricted to
  local models; do not route student content to third-party clouds (OpenRouter).
- **Model entries** are keyed `litellm/<alias>` where `<alias>` matches the name
  registered in LiteLLM. `num_ctx` is **informational on this path** (not sent;
  the real window is set in Ollama/LiteLLM on the gateway host).
- **Reasoning models** (e.g. `qwen3.6-think`): LiteLLM streams chain-of-thought
  as `delta.reasoning_content`, then the answer as `delta.content` — use
  `thinking_style: openai_reasoning_field` so the CoT is filtered out and only
  surfaced as a "thinking…" indicator.

**Availability:** the committed default (`llm.active`) is `litellm/gemma4-31b`, so
the gateway + Spark are now a **hard runtime dependency for all traffic** — if the
Spark or the tailnet is down, the bot can't generate answers. The fast fallback is
to switch back to the on-host model without a rebuild: set `LLM_ACTIVE=ollama/gemma-4-E4B-it-GGUF:UD-Q4_K_XL`
in `.env` and `docker compose up -d` (or export it for a CLI run).

Quick gateway checks (run from the host, with `LITELLM_API_KEY` exported):

```bash
curl -s  http://100.75.42.33:4000/v1/models -H "Authorization: Bearer $LITELLM_API_KEY" | jq '.data[].id'
LLM_ACTIVE=litellm/qwen3.6 uv run student-bot-cli "Vilka krav gäller för masterbehörighet?"
```

---

## Moving to a new host — checklist

When relocating (the new host also runs Tailscale):

1. **Reverse proxy:** install the proxy (Caddy on the current VM), copy its
   server block,
   install TLS certs, and start it **as root** so it can bind 443 (LaunchDaemon
   on macOS, a systemd unit on Linux — order it **after** `tailscaled` if both
   run there).
2. **Port 443:** ensure **no `tailscale serve` on 443** (see the clash section).
3. **Container → tailnet reachability:** verify the bot container can reach the
   LiteLLM `100.x` address. Test from inside the container:
   ```bash
   docker compose exec web python -c "import socket; socket.create_connection(('100.75.42.33',4000),5)"
   ```
   On Docker Desktop/macOS this works out of the box (the VM routes via the host
   Tailscale). On native Linux Docker it generally works too; if MagicDNS is
   needed, add `--dns 100.100.100.100` to the container.
4. **App env:** keep `WEB_SESSION_SECRET` stable (or sessions invalidate), set
   `WEB_ACCESS_TOKEN`, and create `data/web_users` **before** the server stays up.
5. **Reindex** the corpus on the new host (`scripts/reindex.py`) — the Chroma
   index is host-specific (see `README.md` *Image notes* re: chromadb mismatch).

---

## ⚠️ One-time: the v0.1.0 service rename

The compose services were `bot` and `beta-web` (containers `student-bot` and
`student-bot-beta-web`) until v0.1.0. They are now `mattermost` and `web`
(containers `student-bot-mm` and `student-bot-web`). "beta" had been wrong for
months — the web UI has been the production interface since June.

**Do not deploy this with a plain `up -d`.** Compose keys containers off the
service name, so the renamed services start as *new* containers and the old
ones keep running as orphans. For `web` the second bind to `127.0.0.1:8000`
merely fails, noisily but harmlessly. For the Mattermost bot **both containers
stay connected and both answer every DM** — students see doubled replies.

Once, on the upgrade:

```bash
cd ~/student-admin-bot
docker compose down                 # stops BOTH old containers by their old names
git pull --ff-only                  # or: scripts/deploy.sh --release v0.1.0
scripts/deploy.sh --rebuild
docker compose ps                   # expect exactly student-bot-mm + student-bot-web
```

`docker compose down` removes containers and the project network, not named
volumes, so `hf_cache` survives and nothing re-downloads. `./data` is a bind
mount and is untouched either way.

After this, ordinary deploys go back to the normal path.

## Deploying: `scripts/deploy.sh`

Run it **on the VM**, from the repo root, as the user that owns the checkout:

```bash
cd ~/student-admin-bot
scripts/deploy.sh --status      # read-only: HEAD vs origin, last build, containers
scripts/deploy.sh -n            # dry run: incoming commits + every check, changes nothing
scripts/deploy.sh               # the real thing (prompts before it acts)
```

It fast-forwards to `origin/main`, **always** rebuilds the image, snapshots
`data/` while the stack is down, restarts, and polls the web service. `--help`
prints the full rationale; the short version is that each guard exists because
the corresponding mistake has already been made here:

| Guard | What it prevents |
|---|---|
| refuses a dirty checkout | a hand-edited prod tree aborting `git pull` mid-deploy (2026-09-09). It also reports whether the local edits are byte-identical to the incoming commit, i.e. safe to `git checkout -- .` |
| `--ff-only`, never `git pull` | a merge commit appearing on the production checkout |
| always `docker compose build` | `up -d` silently re-running the **old** image — the Dockerfile bakes `src/` and `uv sync --frozen`, so containers come up healthy on stale code |
| chromadb **major** gate | `data/chroma` being migrated in place before you have a snapshot. Needs `--allow-chroma-migration`, and refuses to combine that with `--skip-backup` |
| `.env.example` key diff | a new feature shipping dark because its env keys were never added to `.env` (exactly how `KTH_OIDC_*` arrived). Key **names** only — values are never read or logged |
| SSO whitelist check | `KTH_OIDC_ENABLED=true` with an empty `data/web_users`, which 403s every KTH account |
| health check | leaving a broken build running unnoticed |

The health check treats **401/403 as healthy**: `/api/health` calls
`require_access`, so an authenticated-by-design refusal still proves uvicorn is
serving and the auth chain is wired. Only a connection failure or 5xx counts as
down.

**It deliberately does not reindex** — `scripts/reindex.py` is manual, slow, and
rewrites the index; the script only tells you when the incoming range touched
ingest code. It likewise reminds you to re-run `eval/run_eval.py` when
retrieval, the gate, or the corpus changed, because the gate thresholds are
model-specific and must be re-tuned rather than assumed.

**Rollback** redeploys an earlier commit on a detached HEAD:

```bash
scripts/deploy.sh --rollback <sha>
```

That reverts *code*, not data. If the deploy you are undoing migrated
`data/chroma`, restore the snapshot from `$BOT_BACKUP_DIR`
(default `~/bot-deploy-backups`) as well — see below.

The pre-deploy snapshot is plain `tar`, on purpose: the safety net must not
depend on the image being current or the app being importable. For a snapshot
you intend to keep or hand to someone, use `student-bot-backup --only chroma`
instead — it adds checksums, a manifest, and an online SQLite copy.

---

## Refreshing the corpus on prod

The VM has no `uv`, so everything runs through containers. Two steps, and they
use different services on purpose:

```bash
cd ~/student-admin-bot
docker compose run --rm scrape                          # scrape -> docs/corpus
docker compose run --rm web python -m scripts.reindex   # corpus -> data/chroma
docker compose run --rm web python -m eval.run_eval     # confirm nothing broke
docker compose restart web mattermost                   # REQUIRED — see below
```

Or let `scripts/maintain.sh` do all four with a before/after comparison — see
[Weekly corpus maintenance](#weekly-corpus-maintenance-scriptsmaintainsh).

**The restart is not optional.** A reindex run by another process is invisible
to the services already running. Chroma loads a collection's HNSW segment into
memory the first time that process queries it and never reloads it, so `web`
and `mattermost` keep answering from the index they had at startup. The trap is
that nothing looks wrong: `count()` reads SQLite and does climb to the new
number, and an eval — a fresh container every time — reports the *new* index
and comes back green while students are still being served the old one.
Opening a new Chroma client inside the same process does not help either;
chromadb caches the System per persist directory. Verified against chromadb
1.5.9; `tests/test_chroma_reload.py` pins the behaviour so a future version
that fixes it shows up as a failing test rather than as folklore.

**Why a separate `scrape` service.** `web` and `mattermost` mount the corpus
`:ro`, because a running app must never modify its own knowledge base. The
scraper writes to it, so in those services it fails with `[Errno 30]
Read-only file system`. `scrape` is the one service that mounts it read-write,
and it sits behind a `tools` profile so `docker compose up -d` never starts
it.

**Always run the eval afterwards.** A scrape can silently lose content when
KTH restructures a page: on 2026-09-12 the ITM programansvariga list moved to
intra.kth.se and that page dropped from 35 chunks to 5, which would have
quietly broken every "who is PA for ..." question. Recall@5 caught it. Expect
`44/45` as of v0.1.1; if it is lower, run with `--show-failures` and look at
what moved before doing anything else.

Files written this way are root-owned, which is fine here because every corpus
operation on this host goes through a container. On a host that does have
`uv`, prefer `uv run student-bot-fetch-url-corpus` so ownership stays sane.

## Weekly corpus maintenance: `scripts/maintain.sh`

The four commands above, unattended and with a verdict. Cron entry:

```cron
# Weekly corpus refresh, Sunday 04:00.
0 4 * * 0 cd $HOME/student-admin-bot && scripts/maintain.sh \
    >> $HOME/bot-maintenance.log 2>&1
```

Cron runs with a minimal environment, and `docker` not being found is the quiet
way a cron job never runs. Rather than adding a defensive `PATH=` line and
hoping, rehearse it — `env -i` strips the login environment, so this is what
cron will actually see:

```bash
env -i HOME="$HOME" PATH=/usr/bin:/bin SHELL=/bin/sh \
    sh -c 'cd $HOME/student-admin-bot && scripts/maintain.sh -n'
```

`-n` checks docker, disk and the lock, then prints the plan without touching
anything. On this host it passes as-is, so no `PATH` line is needed; if it ever
fails with "docker not found", add `PATH=/usr/local/bin:/usr/bin:/bin` as its
own line above the schedule (crontab files accept `NAME=value` assignments,
which apply to every job below them). Worth re-running after any deploy that
adds a new command-line dependency to the script.

Measured on prod, 2026-09-12 (`--skip-scrape`, nothing to re-embed):

| phase | duration |
|---|---:|
| eval (baseline) | 1 m 48 s |
| snapshot | 1 s |
| reindex (0 chunks changed) | 3 m 22 s |
| eval (after) | 1 m 45 s |
| **total** | **~7 m** |

A real weekly run adds the scrape (~1 min) and a handful of changed chunks, so
expect **8–9 min**. Note the reindex floor: 3 m 22 s with *nothing* to embed,
because parsing all 187 corpus files is not incremental — only embedding is.

**Why 04:00 and not the 02:17–03:00 gap.** Squeezing the run between the
nightly backup and the host snapshot would make the schedule load-bearing:
correct only as long as every run stays under ~40 minutes, which a catch-up
after a month of corpus drift does not (21 m 31 s is already on record for a
single large import). Starting after the host snapshot removes the constraint
entirely — nothing downstream is waiting, so a long run costs nothing. What is
left is a *duration* check: `BOT_MAINT_MAX_MINUTES` (default 45) puts a note in
the report when a run takes several times the ~9 minute steady state, which
means something is stuck rather than merely busy.

Order of operations, and why:

1. **eval (baseline)** — `--json-out`, so the comparison is machine-readable.
2. **snapshot** `student-bot-backup --only chroma --keep 4`. Rotation counts
   per component set, so these never age out the nightly full archives.
3. **scrape** in the `scrape` service (the corpus is `:ro` everywhere else).
4. **reindex**, in place.
5. **eval (after)**, into a second JSON.
6. **restart** `web mattermost` — see the corpus-refresh section above; without
   this the reindex is invisible to the running services — then poll
   `BOT_HEALTH_URL` until it answers. `docker compose restart` returns when the
   containers are up, not when the app is serving; `web` loads bge-m3 and the
   cross-encoder on startup. Without the poll the job could report green while
   the service never came back, at 04:00, with nobody to notice until morning.
7. **compare and report** via `scripts/eval_compare.py`, sent by
   `student-bot-notify` to every configured channel.

**Why the eval runs twice.** Running it only afterwards tells you the index got
worse, not what made it worse. With a baseline taken minutes earlier, the
corpus is the only thing that changed in between, so a drop is attributable to
the scrape rather than to whatever was merged that week.

**Why it reports instead of rolling back.** `scripts/reindex.py` rebuilds
`data/chroma` in place, so by the time the second eval runs the new index is
already on disk. Staging it elsewhere and promoting only on green is the
stricter design, but it doubles the index on a 30 GB VM that has already
filled once — and the failure it guards against is recoverable in seconds from
the step-2 snapshot. So the job never decides to roll back on its own; it puts
the exact restore command in the report.

Exit codes, which are also the notification's headline:

| code | meaning |
|---|---|
| 0 | green |
| 2 | warnings — churn, a swapped recall failure, a gate wobble |
| 3 | recall@5 fell: the index lost something it used to find |
| 1 | the run itself failed (scrape, reindex, docker, lock held) |

Only recall@5 is critical. Gate pass-rate and OOD refuse-rate move with
cross-encoder scores, which are unbounded and drift slightly with any corpus
change; treating every wobble as critical trains you to ignore the alert.
Recall is a statement about whether the right document is reachable at all.

**Score distributions jitter, and nothing branches on them.** Expect the OOD
minimum to move between runs on a loaded host — two prod evals bracketing a
reindex that changed nothing differed by 0.8 there while every count, every
in-domain distribution and the OOD median and max stayed identical. The index
is not the cause: a no-op reindex leaves the HNSW binaries byte-identical (only
`chroma.sqlite3` changes, from opening the collection for write), and four
consecutive evals on an idle machine agree on every field. It is float
non-determinism, visible first on off-topic queries because they sit in a flat
low-similarity region where candidate ordering is fragile — and at roughly -6
against a gate threshold of -0.5 it cannot change an outcome. The numbers are
in the report to be eyeballed, not alerted on.

### Notification channels

Every configured channel gets every notification at or above `min_severity` —
there is no primary and no fallback. Two are supported, and running both is the
intended starting point: the comparison is what decides which one stays.

```bash
# .env on the prod VM — NOT config.yaml, see below
NOTIFY_MATTERMOST_TARGET=@chohm     # or "#bot-ops"; empty disables
```

```yaml
# config.yaml — defaults that apply everywhere
notify:
  min_severity: "ok"            # ok | warn | critical
```

**Per-host settings go in `.env`, not `config.yaml`.** Who gets notified, and
which ntfy server is used, differ per host — and `config.yaml` is tracked, so
editing it on the server makes the checkout dirty, which blocks the next
`git pull --ff-only` and is refused outright by `scripts/deploy.sh`. That has
already cost one deploy on this host (the SSO merge, 2026-09-09).

**Mattermost** posts as the bot account. The credentials are already in `.env`,
so there is no webhook URL to provision or rotate, and the report lands where
the bot's other conversations are. `student-bot-notify` is a separate entry
point from the bot on purpose: the job has to be able to report that the bot is
broken.

**ntfy** is what actually reaches a phone at 04:00, when a Mattermost DM would
sit unread until morning. Severity maps to ntfy's priority, so a recall drop
rings through a silenced phone (`5`) while a green week stays below default
(`2`):

| verdict | priority | tag |
|---|---:|---|
| green | 2 | ✅ |
| warnings | 4 | ⚠️ |
| recall dropped / run failed | 5 | 🚨 |

**Point it at the self-hosted instance** on the docker-private VM — the
existing one; nothing new to run. Both the server and the topic go in `.env`,
not `config.yaml`: the topic is effectively a password (knowing it is enough to
read *and* publish), and an internal hostname does not belong in a file that is
committed to a **public** repository.

```bash
# .env on the prod VM
NTFY_SERVER=https://ntfy.example.internal     # or http://100.x.y.z on the tailnet
NTFY_TOPIC=<the existing topic>
NTFY_TOKEN=                                   # if the server requires auth
NTFY_CA_BUNDLE=                               # only for a private CA
```

Empty `NTFY_TOPIC` disables the channel. Unset `NTFY_SERVER` falls back to
`https://ntfy.sh`.

Three things to check for a self-hosted server:

- **Reachability from inside the container**, which is not the same as from the
  VM's shell. A tailnet address works through the host's routing, but verify
  rather than assume:
  `docker compose run --rm web python -c "import httpx; print(httpx.get('$NTFY_SERVER/v1/health', timeout=5).text)"`
- **Auth**, if the server runs `auth-default-access: deny-all`. Mint a token
  with `ntfy token add <user>` and put it in `NTFY_TOKEN`; without it the
  publish fails with 403 — loudly, in the job's log, not silently.
- **TLS**, if it sits behind a private CA. Point `NTFY_CA_BUNDLE` at the root.
  Certificate verification is never disabled, only redirected.

Plain `http://` is fine on the tailnet or the LAN and the code stays quiet
about it. To a public host it warns, because the topic travels in the URL path
and is a publish credential. (Tailscale's `100.64/10` needed an explicit case:
Python's `ipaddress` does not report CGNAT addresses as private, so the check
would otherwise have nagged about a perfectly sound tailnet setup.)

**What may be sent this way.** The report is built from `eval/eval_set.yml`
(checked into the repo) plus chunk counts — no `qa_log` text. Self-hosted, that
keeps it inside our own infrastructure; it is also what would have made
ntfy.sh acceptable. Anything that would quote real student questions needs the
self-hosted server, and a second look.

Check the wiring without sending anything real:

```bash
docker compose run --rm web student-bot-notify -n --severity critical \
    --message 'test'
```

Then send one for real, to confirm it arrives on the phone:

```bash
docker compose run --rm web student-bot-notify --severity critical \
    --message 'test from prod'
```

`min_severity` starts at `ok` so both channels see identical traffic while you
compare them. Once that is settled, a weekly all-green push is the kind of
notification people learn to swipe away — `warn` is the likely resting place
for ntfy. An explicit `--to` always sends regardless of the floor.

Useful flags: `-n` (guards and plan, no side effects), `--no-notify` (run for
real, print the report instead of posting), `--skip-scrape` (reindex from the
corpus already on disk). A lock under `data/maintenance/` stops two runs from
rewriting `data/chroma` at once.

## Backups, and handing a snapshot to a collaborator

`uv run student-bot-backup` (`scripts/backup.py`) snapshots the on-disk state
into a timestamped `.tar.gz` under `data/backups/`. SQLite files are copied via
SQLite's **online backup API**, so the stack does not have to be stopped and any
`-wal` contents are folded in. The Chroma **HNSW segment binaries** are plain
files that `scripts/reindex.py` rewrites wholesale — stop the stack first
(`uv run student-bot-down`) if a reindex might be running at the same time.

Every archive carries a `MANIFEST.json` with a sha256 per file plus the
**chromadb and Python versions that wrote the index** — which is exactly what
decides whether a persist directory opens cleanly somewhere else.

```bash
uv run student-bot-backup                       # chroma + logs + web cache
uv run student-bot-backup --only chroma         # vector index only
uv run student-bot-backup --out /tmp            # write elsewhere
uv run student-bot-backup --verify FILE.tar.gz  # re-check checksums (also works on the receiving end)
```

### Nightly rolling backup on prod

The host machine snapshots the VMs themselves **from 03:00**, and that is the
real safety net. These in-VM archives exist only so a bad reindex or deploy
can be reverted in seconds without going to the host, so three is plenty.

`--keep N` prunes older archives after a successful write, so a cron entry is
the whole mechanism. Rotation counts **per component set**, so a nightly
`--only chroma` run never ages out the full pre-deploy snapshots
`scripts/deploy.sh` takes — they are different things kept for different
reasons.

Run it in the container: the host may not have `uv`, and `./data` is
bind-mounted so the archive lands on the host either way.

```cron
# 3-deep rolling full backup, 02:17 nightly — finishes well before the host
# snapshots this VM at 03:00, so that snapshot contains a complete archive.
17 2 * * * cd $HOME/student-admin-bot && docker compose run --rm web \
    student-bot-backup --keep 3 >> $HOME/bot-backup.log 2>&1
```

**Back up everything, not just Chroma.** `data/chroma` can always be rebuilt
with `scripts.reindex`; `logs.sqlite` cannot — it is the only irreplaceable
thing on the host. A full archive is ~10 MB, so three is ~30 MB.

Notes:

- The SQLite files are copied through SQLite's online backup API, so the stack
  keeps running. The Chroma HNSW `.bin` files are plain files, so **this must
  not overlap a reindex**. The weekly maintenance run is scheduled at 04:00,
  well clear of both this backup and the 03:00 host snapshot, so no schedule
  arithmetic has to hold for either to be safe.
- The container runs as root, so archives are root-owned on the host. Reading
  and `scp` are fine; removing one by hand needs `sudo`.
- Full archives contain `qa_log` student text and must stay on this host. The
  script prints a warning to that effect on every run.
- Restore is a plain extract; verify first:
  `uv run student-bot-backup --verify <archive>`.

### ⚠️ Which parts may leave the host

| component | contents | shareable? |
|---|---|---|
| `chroma` | vectors + text chunks of the **public** corpus (KTH pages, study plans) | **yes** |
| `logs` | `qa_log.question` / `qa_log.answer` — **real student questions**, salted-hashed user ids | **no** — internal only |
| `cache` | `web_cache.sqlite`, fetched public pages | yes, but rarely useful |

So a snapshot for a fellow developer is always `--only chroma`. The script prints
a warning when an archive contains `logs.sqlite`.

### Recipe: prod index → another developer

On the prod host, pick whichever of these fits how that host is set up — all
three land the archive in `~/student-admin-bot/data/backups/`:

```bash
cd ~/student-admin-bot

# a) host has uv + a synced venv
uv run student-bot-backup --only chroma          # ~11 MB for the current corpus

# b) no uv on the host — run it in the container (needs an image built after
#    this script landed; `./data` is bind-mounted, so the output is on the host)
docker compose run --rm web student-bot-backup --only chroma

# c) neither — plain tar. No MANIFEST/checksums, and stop the stack first if a
#    reindex could be running: `docker compose stop mattermost web`
tar -czf data/backups/chroma-$(date +%F).tar.gz -C data chroma
```

Then, from your laptop:

```bash
scp cohm@chatbot:'~/student-admin-bot/data/backups/student-bot-backup-chroma-*.tar.gz' .
```

On the receiving side:

```bash
uv run student-bot-backup --verify student-bot-backup-chroma-*.tar.gz
tar -xzf student-bot-backup-chroma-*.tar.gz
cp -R data/chroma data/chroma.bak          # keep their own index first
rm -rf data/chroma && mv student-bot-backup-chroma-*/chroma data/chroma
uv run student-bot-cli "Vem är programansvarig för CTFYS?"
```

A prod index written by an **older chromadb** is migrated in place the first
time a 1.x client opens it (see `README.md` *Image notes*), so this doubles as
the check that a dev environment's dependency set can actually read prod data.
`data/backups/` is gitignored.
