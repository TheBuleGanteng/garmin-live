# CLAUDE.md — garmin-live

**What this is:** a read-only bridge from a Garmin LiveTrack share link to Claude, exposed as a
remote MCP server (pull model). It is an **open-source, self-hostable** project (MIT): each
deployment serves one person. Matthew's own instance runs at
**https://kebayorantechnologies.com/garmin-mcp** on his VM, but nothing deployment-specific
(domain, path, hosts, VM layout) may be hardcoded in the app — it all comes from config. Push/alerting
is a *future* build — keep the seam for it (see `events` + `Notifier`), but do not implement it.

**Laptop repo:** `/home/thebuleganteng/01_Repos/06_personal_work/garmin-live` (WSL)
**GitHub:** `git@github.com:TheBuleGanteng/garmin-live.git` (branch `main`) — **PUBLIC repo,
already created by the user.** Never create it, change its visibility, or rewrite its history.
**VM:** SSH alias `kt-vm` (defined in the user's `~/.ssh/config`; the real IP must never appear in
this repo), infra repo `/home/matt/vm-infrastructure` (separate repo), submodule `apps/garmin-live`
**Instruction files:** live in `ai-instructions/`. That folder holds instructions only — never put
code, tests, fixtures, config, or logs there, and never edit the instruction files themselves.

## Which doc, when
| Doc | Used for | Invoked by a prompt like |
|---|---|---|
| `CLAUDE_INSTRUCTIONS.md` | Local build, test, fix loop, push | "Build and test using CLAUDE_INSTRUCTIONS.md" |
| `CLAUDE_PROVISION.md` | **One-time** VM setup (compose service, nginx route, secrets) | "Provision using CLAUDE_PROVISION.md" |
| `CLAUDE_DEPLOY.md` | Every redeploy after provisioning | "Deploy and test via CLAUDE_DEPLOY.md" |

## Global hard rules (apply in every doc)
1. **Never weaken, skip, xfail, or delete a test to make a gate pass.** Fix the code, or STOP and report.
2. **Fix-loop cap:** at most 5 fix attempts per failing gate. On the 6th failure, STOP and report what you tried.
3. **Privacy:** never print, log, or commit real coordinates, LiveTrack session IDs/tokens, or secrets.
   Raw captures live only in `fixtures/raw/` (gitignored). Only scrubbed fixtures are committed.
4. **One base path everywhere:** the app serves natively under the configured `BASE_PATH`
   (Matthew's deployment: `/garmin-mcp`; self-hosters may use `""` for root) in local dev, tests,
   and prod. No root-absolute URLs or routes. (This removes the prefix-escape bug class seen in
   live-commerce-insights.)
5. **Single uvicorn worker.** The poller runs in-process; multiple workers would double-poll.
6. **Don't trust memory for library APIs** (MCP Python SDK, Playwright, FastAPI). Check the
   installed version's docs/source before writing integration code, and pin versions.
7. **VM safety:** never touch the `language-learning`, `live-commerce-*`, Next.js, or personal-site
   services, their nginx blocks, or shared nginx config, except the exact additive edits gated in
   `CLAUDE_PROVISION.md`. Never run `sudo` on the VM.
8. **Running log:** append (never overwrite) an ISO-8601-stamped entry to `CLAUDE_SUMMARY.md` at the
   end of every run, including aborted runs. It is gitignored; never stage, commit, or push it.
9. **STOP means STOP:** print the reason, what you need from the user, and write the summary entry.

## Public-repo rules (the GitHub repo is world-readable)
10. **Everything committed is public forever.** Deleting a file later does not remove it from git
    history. A leak that reaches GitHub is treated as burned: rotate the secret, don't "fix forward".
11. **Never commit:** secrets/tokens/keys, `.env` files (only `.env.example` with placeholders),
    the VM's IP address, real LiveTrack URLs/session IDs/tokens, real coordinates, real activity
    dates, the user's email, raw captures, HAR files, databases, or `CLAUDE_SUMMARY.md`.
12. **Secret-scan gate before EVERY commit and push** (Phase 0 sets it up):
    - `gitleaks` scan of the staged changes and of the full history, run via Docker
      (`docker run --rm -v "$PWD:/repo" zricethezav/gitleaks:latest ...`) — pin the image version.
    - `tools/check_denylist.sh`: fails if any literal string listed in the **gitignored** local file
      `.secret-denylist` appears in staged content. The user's private values (VM IP, email, real
      LiveTrack IDs) live only in that file. Create it with placeholder comments if absent and tell
      the user to fill it in; never print its contents.
    - Both are wired into a git `pre-commit` hook **and** run explicitly before each `git push`.
      Never bypass with `--no-verify`. A finding → STOP (do not push), report the file and rule
      only (never the matched value).
13. **Test secrets are obviously fake** (e.g. `test-secret-not-real-0000`) so they never resemble
    real credentials.
14. **Publishable docs:** `docs/LIVETRACK_FINDINGS.md` and anything else committed must use
    placeholders for endpoints' IDs/tokens and contain no real coordinates, elevations, or dates.
