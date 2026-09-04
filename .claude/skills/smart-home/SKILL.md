---
name: smart-home
description: Operational knowledge for the smart_home / Core homelab repo — SSH access, deploy workflow, known infra gotchas, and working conventions. Use whenever debugging, deploying, or documenting anything related to the Core server (Docker stack, Vaultwarden, nginx, the personal portal, Grafana/Prometheus) in this repo.
---

# Smart Home / Core — operational skill

This repo (`smart_home`) is a **one-way config backup mirror** of a live homelab
server called **Core**, not the live source of truth. `collect_configs.sh` +
`git_push.sh` copy Core → this repo nightly at 03:00; nothing flows the other
direction automatically. Editing files here does **not** change anything on
Core until you explicitly deploy.

## Golden rule: always ask before touching anything

**Never edit, revert, or deploy any file — including fixing your own
mistakes — without the user's explicit approval first.** This applies to:
- editing any file in this repo or on Core
- deploying to Core (scp, cp, docker compose up/pull/restart)
- any git operation that changes state (commit, push, checkout, reset)
- any database write (even a single `UPDATE`)

Read-only actions (viewing logs, `git status`/`git diff`, SSH `SELECT`
queries, `curl` health checks) don't need approval — use them freely to
diagnose *before* proposing a fix. Present the diagnosis and the exact
command(s) you intend to run, then wait for a clear go-ahead ("апрув") before
executing. This is a hard, standing rule for this user, confirmed multiple
times across different repos — treat it as non-negotiable.

## Core server access

- SSH: `ssh shadmin@192.168.2.111` (password auth; ask the user for the
  current password if not already in context — don't assume it's stable
  across sessions).
- `shadmin` has sudo (password-prompted, same password).
- Live paths on Core:
  - `/opt/smart-home/` — Docker Compose project (`docker-compose.yml`, `.env`
    secrets, `config/nginx/`)
  - `/opt/smart-home-git/` — this same git repo, checked out live (target of
    the nightly backup cron)
  - `/data/` — persistent volumes for every container (homeassistant,
    mosquitto, zigbee2mqtt, esphome, prometheus, grafana, vaultwarden,
    homer) plus `/data/portal/` (personal portal) and `/data/backups/`
- Tailscale hostname: `core.tail751bc9.ts.net` — Funnel (public) on 443,
  various dedicated `tailscale serve`/`funnel` ports for specific services
  (see gotchas below).

## Deploying the personal portal

The portal (`config/portal/`) is FastAPI + a single-file SPA
(`portal.html`), run as a **root** systemd service (`portal.service`) on
Core, serving `portal.html` via `FileResponse` — **it reads the file fresh
from disk on every request, so editing `portal.html` needs no service
restart**, just replace the file.

Workflow that's worked well:
1. Edit `config/portal/portal.html` (or `main.py`) in this repo locally —
   get a clean diff, easy to review.
2. Get approval, then back up the live file on Core first:
   `cp /data/portal/portal.html /data/portal/portal.html.bak_$(date +%Y%m%d_%H%M%S)`
3. `scp` the edited file to `/data/portal/portal.html` (or
   `/data/portal/backend/main.py`, which *does* need
   `systemctl restart portal` since it's a running Python process).
4. Verify: `curl -s -o /dev/null -w '%{http_code}' http://localhost:7000/`
5. Commit + push in this repo (short, one-line commit message — see
   conventions below). This repo's `git push` needs the credential in
   `git remote -v` to be valid on whichever machine you're pushing from —
   it can be stale on a laptop clone even though Core's own cron push works
   fine (different stored credential per machine).

## Known infra gotchas (hard-won, don't re-discover these)

- **Self-hosted services with their own web client should never be
  sub-pathed** (e.g. `nginx.conf`'s `location /vault/`) unless the service
  explicitly supports it. Vaultwarden's Rocket backend hardcodes routes at
  root regardless of `DOMAIN`'s path — setting `DOMAIN` with a path
  suffix 404s *everything*. The working pattern: give it a dedicated
  `tailscale serve --bg --https=<port> http://127.0.0.1:<local_port>` and a
  root-level `DOMAIN`/`PUBLIC_URL` env var. Vaultwarden lives at
  `https://core.tail751bc9.ts.net:8446` for exactly this reason — verified
  to survive reboots (state lives in tailscaled, no extra systemd unit
  needed).
- **`tailscale cert`-issued certs last ~1–3 months, not a year.** The
  `nginx.conf` TLS cert (used by nginx's own `listen ... ssl` blocks —
  `:8443`/`:8445` — NOT the public Funnel on 443, which gets its own
  tailscaled-managed cert) has already expired once from being renewed on
  a "yearly" cadence. It's now monitored: Prometheus scrapes
  `cert_expiry_timestamp_seconds` (emitted by
  `scripts/check_connectivity.sh`'s textfile-collector metric, same file
  as `tailscale_up`/`telegram_reachable`) and Grafana's **System — Core**
  dashboard (uid `addz6pb`) has a "TLS Cert" stat panel (days remaining,
  🟢>21 / 🟠7-21 / 🔴<7). Auto-renewal itself is still a manual TODO — check
  that panel periodically, or ask before it comes up again.
- **Telegram reachability is not routed through Tailscale.**
  `check_connectivity.sh`'s `telegram_reachable` metric is a plain
  `curl https://api.telegram.org` over the default route — it does not pin
  to the AmneziaWG interface despite the metric's HELP text implying that.
  AmneziaWG (`awg-quick@wg0`) is the actual intended VPN path for bypassing
  Telegram restrictions; Tailscale is unrelated (mesh network for the
  user's own devices + Funnel).
- **Vaultwarden browser-extension sync bugs can have two totally different
  root causes** that look similar (skeleton placeholders forever): (a) a
  server-side `DOMAIN`/CORS issue (fixable server-side), or (b) corrupted
  local extension state in the browser's own storage — fixed only by fully
  removing and reinstalling the extension, not just logout/login. Diagnose
  via the extension's own Service Worker DevTools
  (`chrome://extensions` → card → "Инспектировать" → "service worker" →
  Console/Network), not just server-side logs — a WASM crash
  (`bitwarden_wasm_internal`) in that console is the local-state case.
- **Frontend `portal.html` background auto-refresh can race in-flight
  saves.** `softRefresh()` (every 5 min + on tab focus/visibility) reloads
  every module including Notes/Quick-access; `portalBusy()` gates it but
  historically missed some modals (e.g. the "Быстрый доступ" quick-access
  modal) and never guarded against a fire-and-forget `PATCH` still in
  flight — meaning a background refresh could silently revert a just-saved
  edit back to stale server data. Pattern to check for when debugging
  "my edit reverted for no reason" reports: does the relevant save path
  await its request, and does `portalBusy()` know about the surface the
  user is editing? A `quickSaveInFlight`-style in-flight counter is the
  fix already applied for the quick-access case — extend the same pattern
  if a similar report comes in for another module.
- **`portal_nav` (active tab + selected project) persistence has bitten us
  twice**: (1) don't unconditionally re-apply a saved nav value on every
  data reload — only restore it once per tab session, otherwise a
  concurrently-open tab's more recent save can silently steal your
  selection; (2) never call `persistNav()` synchronously before the async
  data load that actually determines the real current value has resolved
  — it will persist `null`/stale and wipe the real saved selection. Both
  were live bugs, both fixed — see git history
  (`665463a`, `3228878`) for the exact diffs if this class of bug
  resurfaces elsewhere in the SPA.

## Working conventions for this user

- **Commit messages: short, one line.** No multi-bullet bodies — summarize,
  don't enumerate, even for commits touching several things.
- When something needs live verification (UI behavior, browser console,
  extension state) that you can't observe yourself over SSH, say so plainly
  and ask the user to check DevTools/screenshots rather than guessing —
  this repo's hardest bugs (Vaultwarden extension, portal notes desync)
  were only cracked by combining live SSH log-tailing with the user's own
  browser DevTools output.
- Read-only DB inspection is fine for diagnosis, but treat stored secrets
  (Vaultwarden ciphertext, portal `quick_secrets.value` which is **plaintext
  in the DB**) as sensitive — prefer structural checks (`md5(value)`
  hashes, column shapes, key names) over reading raw secret values, unless
  the user explicitly asks you to read/compare a specific value.
