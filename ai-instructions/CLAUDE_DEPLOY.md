# CLAUDE_DEPLOY.md — Redeploy pipeline for garmin-live (GCP VM)

> Audience: Claude Code on the laptop, with SSH to `kt-vm`.
> Read `ai-instructions/CLAUDE.md` first (global + public-repo rules).
>
> Deploys the **already-pushed** `garmin-live` `main` to
> **https://kebayorantechnologies.com/garmin-mcp** without disturbing co-hosted apps.
> Mode: **unattended**, except the ⏸ pauses.

## Prerequisites — if any are missing, this is first-time setup: STOP, use CLAUDE_PROVISION.md
- Submodule `apps/garmin-live` exists in `vm-infrastructure`.
- `docker-compose.yml` has the `garmin-live` service.
- nginx has `location /garmin-mcp/`.
- `/home/matt/secrets/garmin-live/.env` exists.

## Does NOT do without explicit sign-off
Any of the following → STOP and ask:
- Editing secrets.
- Editing nginx or compose.
- Touching other services or volumes.
- Deleting data.
- Any host-level change.

## Step 0 — Project repo is pushed (laptop)
```bash
cd /home/thebuleganteng/01_Repos/06_personal_work/garmin-live
git status && git fetch && git log origin/main -1 --oneline
```
The tree must be clean and `HEAD == origin/main`. Otherwise STOP.
CI must be green on that commit (`gh run list --branch main --limit 1`). Otherwise STOP.
This pipeline never commits to the public garmin-live repo; its only commit is the submodule bump
in `vm-infrastructure`.

## Step 1 — Baseline neighbours (laptop) — do this before changing anything
```bash
for u in https://kebayorantechnologies.com/ https://kebayorantechnologies.com/language-learning \
         https://www.kebayorantechnologies.com/live-commerce/ https://mattmcdonnell.net/ \
         https://kebayorantechnologies.com/garmin-mcp/health; do
  printf '%s %s\n' "$(curl -s -o /dev/null -w '%{http_code}' "$u")" "$u"; done
```
Keep this output for Step 7.

## Step 2 — Bump the submodule (laptop)
```bash
cd /home/thebuleganteng/01_Repos/06_personal_work/vm-infrastructure/apps/garmin-live
OLD=$(git rev-parse HEAD); git pull origin main; NEW=$(git rev-parse HEAD)
cd /home/thebuleganteng/01_Repos/06_personal_work/vm-infrastructure   # ROOT, not apps/
git add apps/garmin-live
git commit -m "chore: bump garmin-live submodule" && git push origin main
```
If `OLD == NEW`, report "nothing to deploy" and STOP, unless the user asked for a forced rebuild.

## Step 3 — Pre-flight checks
**Schema-change classification (laptop):**
```bash
git -C apps/garmin-live diff "$OLD" "$NEW" --name-only -- src/garmin_live/store/migrations/
```
For each new migration file, scan for `DROP`, `RENAME`, column-type narrowing, or `NOT NULL`
without a default.
- **Additive** → continue.
- **Destructive** → ⏸ print the SQL and wait for `confirmed`.

**Live-workout guard (VM).** Do not restart in the middle of a real session:
```bash
cd /home/matt/vm-infrastructure
docker compose exec -T garmin-live python -c "from garmin_live.store.repo import active_session_labels as a; print(a(exclude=['selftest']))"
```
Add `active_session_labels` to the app if it doesn't exist; that's a code change, so do it in a
build round first. If any real session is active → ⏸ report it and wait for `confirmed`.
Restarting would interrupt ingest; the poller resumes on start, but data during the gap may be
late.

## Step 4 — Pull, back up, rebuild (VM)
```bash
cd /home/matt/vm-infrastructure && git pull && git submodule update --init --recursive
git -C apps/garmin-live log --oneline -1                 # must equal $NEW
TS=$(date +%Y%m%d-%H%M%S)
docker compose exec -T garmin-live python -m garmin_live.db_backup /data/backups/pre-deploy-$TS.db
docker compose build --no-cache garmin-live
docker compose up -d --force-recreate garmin-live
```
Data is disposable personal telemetry with 30-day retention, so the on-volume backup (newest 5
kept) is the only backup. There is no bucket. Migrations apply automatically at startup. If the
app fails to start because of a schema error, STOP; the backup is the restore point.

## Step 5 — Re-resolve the upstream (VM)
```bash
docker compose exec -T nginx nginx -t && docker compose exec -T nginx nginx -s reload
```
The recreated container may have a new IP that nginx has cached.

## Step 6 — Verify the app
```bash
docker compose ps garmin-live                              # healthy
docker compose logs garmin-live --tail 100                 # no tracebacks; no coords/tokens
docker compose exec -T garmin-live python -m garmin_live.selftest   # exit 0
docker stats --no-stream garmin-live                       # within mem_limit
```
Then the external MCP smoke from the laptop, without printing the secret:
```bash
cd /home/thebuleganteng/01_Repos/06_personal_work/garmin-live
S=$(ssh kt-vm "grep ^MCP_PATH_SECRET= /home/matt/secrets/garmin-live/.env | cut -d= -f2-")
.venv/bin/python tools/mcp_smoke.py --url "https://kebayorantechnologies.com/garmin-mcp/mcp/$S" --expect-session selftest
unset S
```

## Step 7 — Verify the neighbours
Re-run the Step 1 loop. Every non-garmin URL must match the baseline, and
`/garmin-mcp/health` must be 200. **Any neighbour change → STOP and report immediately.**

## Step 8 — Log
Append to `/home/thebuleganteng/01_Repos/06_personal_work/garmin-live/CLAUDE_SUMMARY.md`. Write this
even if the deploy aborted.
- timestamp and **deploy**
- `OLD→NEW` hashes
- infra bump commit
- migrations and their classification
- backup filename
- selftest and smoke results
- neighbour baseline vs after
- memory
- any errors or STOPs, and where the run halted

## Rollback (user-driven only)
```bash
cd /home/thebuleganteng/01_Repos/06_personal_work/vm-infrastructure
git revert <bump-commit> && git push origin main
# then re-run Steps 4–7 on the VM
```
A database restore happens only on explicit instruction: stop the container, copy
`/data/backups/pre-deploy-<TS>.db` over `/data/garmin.db` inside the volume, then start it.

## Common failures
- **404 on `/garmin-mcp/...`**
  - Cause: the prefix is mismatched.
  - Check: the app must serve natively under `BASE_PATH`, and nginx `proxy_pass` must have no URI
    part.
- **400/421 from the MCP endpoint**
  - Cause: the SDK's Host/Origin protection is rejecting the request.
  - Check: `MCP_ALLOWED_HOSTS`.
- **Streaming hangs or times out**
  - Cause: nginx is buffering.
  - Check: `proxy_buffering off` in the block.
- **502 after recreate**
  - Cause: nginx cached the old container IP.
  - Fix: reload nginx (Step 5).
- **Ingest errors 403/429 in logs only in prod**
  - Cause: Garmin is blocking the VM's IP.
  - Action: this is decision D2 from the findings. STOP and report; don't work around it.
- **`LiveTrackSchemaError` in logs**
  - Cause: Garmin changed its format.
  - Action: capture new fixtures and do a build round. This is not a deploy fix.
- **claude.ai connector stops working while smoke passes**
  - Cause: Cloudflare WAF or bot rules are blocking Anthropic's IPs.
  - Action: check Cloudflare Security → Events.
