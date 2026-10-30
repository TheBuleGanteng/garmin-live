# CLAUDE_PROVISION.md — One-time VM setup for garmin-live

> Audience: Claude Code on the laptop, with SSH to `kt-vm`.
> Read `ai-instructions/CLAUDE.md` first (global + public-repo rules).
>
> Mode: **semi-attended.** Everything is automated except the 🔒 gates, where you print the exact
> diff or command and wait for the user to type `confirmed`. This is the only doc allowed to edit
> shared VM config (compose and nginx), and only additively.
>
> Run this once. Every later release uses `CLAUDE_DEPLOY.md`.

## Step 0 — Preconditions (laptop)
- The project repo is clean and pushed; `origin/main` is the intended commit.
- CI is green on that commit: `gh run list --branch main --limit 1`. If not, STOP.
- The VM SSH alias works: `ssh kt-vm true`. If not, STOP and ask the user to add the `kt-vm`
  entry to `~/.ssh/config`.
- Never write the VM's IP into the garmin-live repo. The `vm-infrastructure` repo is separate,
  and nothing in this doc commits to the public garmin-live repo.
- `docs/LIVETRACK_FINDINGS.md` exists and the decision is **D1 or D3**. If D2, STOP.
- Note `FETCH_STRATEGY` and the measured peak memory from the findings.

## Step 1 — VM reconnaissance (read-only) and baseline
```bash
ssh kt-vm
cd /home/matt/vm-infrastructure
free -m; df -h /; docker stats --no-stream
docker compose config --services
grep -rn "location" nginx/ | grep -v "^#"          # find where site location blocks live (confirm actual nginx dir)
grep -rn "garmin" docker-compose.yml nginx/ || echo "no existing garmin config"
grep -rnE "real_ip|CF-Connecting-IP|X-Real-IP" nginx/   # how client IP is derived behind Cloudflare
```
Record each of the following:
- How the `live-commerce-insights` service gets its env (`env_file:` path pattern) and which
  network nginx shares with apps. Mirror that pattern exactly.
- Which nginx file holds the `kebayorantechnologies.com` server block.

**Baseline the neighbours.** Record status codes now; Step 7 compares against them:
```bash
for u in https://kebayorantechnologies.com/ https://kebayorantechnologies.com/language-learning \
         https://www.kebayorantechnologies.com/live-commerce/ https://mattmcdonnell.net/; do
  printf '%s %s\n' "$(curl -s -o /dev/null -w '%{http_code}' "$u")" "$u"; done
```
Add any other public routes discovered in the nginx config.

**Memory check.** STOP if available memory is below 300 MB (`http`) or below 1.2 GB (`browser`).
Report the numbers and let the user decide (swap or a VM resize are host-level changes this doc
must not make).

STOP if `/garmin-mcp` already exists in nginx or compose. That would mean partial provisioning;
report it rather than layering on top.

## Step 2 — Add the submodule (laptop)
```bash
cd /home/thebuleganteng/01_Repos/06_personal_work/vm-infrastructure
git pull
git submodule add git@github.com:TheBuleGanteng/garmin-live.git apps/garmin-live
```

## Step 3 — Compose service (laptop edit) 🔒 G1
Add **one** service named `garmin-live`, mirroring the style of the live-commerce services:
- `build: ./apps/garmin-live` using its `docker/Dockerfile`; confirm the context and Dockerfile path.
- `env_file: /home/matt/secrets/garmin-live/.env`, using the same path convention as the other apps.
- `volumes: [garmin-live-data:/data]` (declare the named volume).
- `restart: unless-stopped`.
- `mem_limit`: 256m (`http`) or 1g (`browser`).
- Same network as nginx. **No published ports.**

Print `git diff docker-compose.yml`. It must contain **only additions**. Wait for `confirmed`.

## Step 4 — nginx route (laptop edit) 🔒 G2 — highest-risk step
Copy the block from `apps/garmin-live/nginx/garmin-mcp.location.conf` into the
`kebayorantechnologies.com` server block, or include it from there, following the existing
convention. If the VM derives client IP from `CF-Connecting-IP` via `real_ip`, keep
`X-Real-IP $remote_addr`. Otherwise note in the summary that the IP allowlist must stay off.

Print the diff. It must be **additive only** and touch no other `location`, `server`, or global
directive. Wait for `confirmed`.

## Step 5 — Secrets on the VM 🔒 G3
Create `/home/matt/secrets/garmin-live/.env` with `chmod 600`. Generate the secret **on the VM
without printing it**:
```bash
mkdir -p /home/matt/secrets/garmin-live && umask 077
SECRET=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))")
cat > /home/matt/secrets/garmin-live/.env <<EOF
BASE_PATH=/garmin-mcp
MCP_PATH_SECRET=$SECRET
DB_PATH=/data/garmin.db
RETENTION_DAYS=30
FETCH_STRATEGY=<from findings>
POLL_MIN_SECONDS=20
MAX_SESSION_HOURS=12
ENFORCE_IP_ALLOWLIST=false
MCP_ALLOWED_HOSTS=kebayorantechnologies.com,www.kebayorantechnologies.com
ATHLETE_MAX_HR=
LOG_LEVEL=INFO
EOF
unset SECRET
```
Show the command (not the secret value) and wait for `confirmed`.

## Step 6 — Ship
**Laptop:**
```bash
cd /home/thebuleganteng/01_Repos/06_personal_work/vm-infrastructure
git add .gitmodules apps/garmin-live docker-compose.yml <nginx file>
git commit -m "feat: provision garmin-live (garmin-mcp) service" && git push origin main
```
Record the commit hash; it is the rollback point.

**VM:**
```bash
cd /home/matt/vm-infrastructure && git pull && git submodule update --init --recursive
docker compose build --no-cache garmin-live
docker compose up -d garmin-live
docker compose exec -T nginx nginx -t        # MUST pass before reload
docker compose exec -T nginx nginx -s reload # reload, not restart: keeps neighbours' connections
```
If `nginx -t` fails, **do not reload**. Revert the nginx edit (Rollback below) and STOP.

## Step 7 — Verify
1. **Neighbours first.** Re-run the Step 1 baseline loop. **Any change from baseline → immediate
   rollback**, then STOP and report.
2. Check `https://kebayorantechnologies.com/garmin-mcp/health` returns 200 and
   `docker compose ps garmin-live` shows healthy.
3. Run `docker compose exec -T garmin-live python -m garmin_live.selftest` and confirm it exits 0.
4. **External MCP smoke from the laptop.** Read the secret into a variable without echoing it:
   ```bash
   S=$(ssh kt-vm "grep ^MCP_PATH_SECRET= /home/matt/secrets/garmin-live/.env | cut -d= -f2-")
   .venv/bin/python tools/mcp_smoke.py --url "https://kebayorantechnologies.com/garmin-mcp/mcp/$S" --expect-session selftest
   unset S
   ```
5. Check the logs: `docker compose logs garmin-live --tail 100`. There must be no tracebacks and no
   coordinates or tokens.
6. Check `docker stats --no-stream garmin-live` is within `mem_limit`.

## Step 8 — Human handoff (print these for the user)
1. **Get the connector URL.** Run this yourself:
   `ssh kt-vm "grep ^MCP_PATH_SECRET= /home/matt/secrets/garmin-live/.env"`.
   The connector URL is `https://kebayorantechnologies.com/garmin-mcp/mcp/<secret>`.
2. **Add the connector.** claude.ai → Customize → Connectors → Add custom connector → paste the
   URL. Leave the OAuth fields blank.
3. **Test it.** In a new chat, enable the connector and ask "list my Garmin sessions". You should
   see `selftest`.
4. **If the connection fails:**
   - Check the Cloudflare dashboard → Security → Events, filtered to path `/garmin-mcp`. Anthropic's
     servers make the requests, so a WAF, Bot Fight or challenge rule may be blocking them.
   - If claude.ai reports an OAuth registration error against an authless server, the v1 auth
     approach isn't accepted. Report back; the fix is an OAuth build, which is a separate spec.
5. **Optional:** set `ATHLETE_MAX_HR` in the env file to enable HR zones. Takes effect on the next
   deploy.

## Step 9 — Log
Append to `CLAUDE_SUMMARY.md`:
- provisioning commit hash
- gates passed
- baseline vs post status codes
- selftest and smoke results
- memory
- client-IP derivation finding
- any STOP

## Rollback (provisioning)
```bash
# laptop
cd /home/thebuleganteng/01_Repos/06_personal_work/vm-infrastructure
git revert <provisioning-commit> && git push origin main
# VM
cd /home/matt/vm-infrastructure && git pull
docker compose stop garmin-live && docker compose rm -f garmin-live
docker compose exec -T nginx nginx -t && docker compose exec -T nginx nginx -s reload
```
Re-run the neighbour baseline afterwards. Leave the `garmin-live-data` volume and the secrets file
in place unless the user says otherwise.
