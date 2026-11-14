---
name: blockfield-agent
description: "Blockfield agent playbook: preflight, work, gates, pr-ship, final. Load first on any Blockfield unit."
---

# Blockfield Agent Playbook

Load `blockfield-boat` too when a stand, build, or test needs a VM. Boat is
personal opt-in: the Boat parts below apply only when the task owner opted in
(see `blockfield-boat`); otherwise work locally as `AGENTS.md` describes.

## The sequence (every unit, no exceptions)

1. `just agent-preflight` — first command. Refuses when the checkout is wrong:
   main checkout instead of your T3 worktree, behind origin, mid-merge/rebase.
   Fix what it says, never work around it.
2. Work in your T3 worktree only. `pwd` must stay inside it
   (`…/.t3-worktrees/…`). Never touch the coordinator's main
   checkout (no branch switches, no commits, no submodule updates there).
   Work only in the owning-repository workspace the brief supplies; if the brief
   supplies none, report the missing workspace (`BLOCKED:`) and do not create an
   alternate checkout.
3. Gates before every push: the repo's pinned gate (`just check`, mod
   `dev/quality.py`, `java-check`/Spotless for Java). With Boat, run them on
   your VM, not on the task owner's machine. Gradle anywhere refuses without
   `BLOCKFIELD_BOAT_VM=1` (helper sets it) or CI; `BF_ALLOW_LOCAL_GRADLE`
   only when the brief or common rules grant TEMPORARY LOCAL BUILDS. Never run bare `./gradlew`/`java`
   on a VM: the `just` recipes pick the pinned JDK 21 themselves — in a bare
   shell `source .dev/<task>/boat-env.sh` (or the BOAT.md JAVA_HOME prelude) first.
4. Push the branch, then finish — never wait on the bot-review gate or CI, never poll PR/CI state for validation
   (T3 forbids children to wait on PRs). Finish
   with `PR <url>, head <sha>, checks and reviews <observed state>`; the coordinator
   watches the PR and dispatches the findings round or the merge. Never
   copy scripts to /tmp, never `sleep`-poll output files.
   Push budget (default 2, brief may change): push 1 after green gates;
   when a findings brief arrives, read the review App body and every unresolved inline thread
   on the current head (checks and comments on older heads are superseded) and handle them per the `babysit-pr` skill (it wins over
   this playbook on PR handling): verify each against the source, fix real bugs in one
   batch, decline the rest (unneeded tests, unimportant edge-case guards, extra
   compatibility) with a written reason, resolve those threads and list them as follow-up
   issues; push 2. Findings left after the budget →
   `BLOCKED: review findings` with the list + local SHA. When the base moved (BEHIND,
   conflicts, or new base commits touch the PR's files), rebase the PR branch onto its base
   (origin/next in product repos, origin/main in workspace) and `git push --force-with-lease`
   — only your own PR branch, never next/main/tags. Product-repo PRs branch from and target `next`; only the coordinator merges next→main.
   No reruns, no empty pushes, no `--no-verify` without saying why.
5. Sources and evidence: read another repo/submodule at the ref the brief
   names (`git fetch`; default origin/next in product repos, origin/main in workspace), cite repo+SHA; the workspace
   pin can be old. Evidence goes to `.dev/<task>/`, never only `/tmp`.
6. Final response: what changed, gates run on the VM, PR URL + CI state,
   with Boat also VM-minutes used and all your VMs `state=stopped`. Then stop: end the turn
   only when nothing you started is still running (PID, Boat command).

## Boat without burning money (opted-in owners)

- Starts are the binding limit: start/resume nothing without the brief's
  start grant. Batch the plan on one VM with `--keep`; keep it through
  gaps ≤15 min, stop it for longer waits (reports, CI, review) and after
  the last command; confirm `state=stopped`.
- Small/default size unless a live server+client stand needs large; TTL =
  batch + 15 min. Never idle a VM; never leave one running across edits.
- Read status from Boat history (`just boat-build`/`await.py --boat` use
  history `exitCode`/`finishedAt`), never from one exec stream: a dropped
  stream looks like a failure while the command still runs. If a run does
  not come up on the first attempt, stop the VM and return `BLOCKED:` —
  no retry loops.
- Never touch another task's VM, never use `current`, never create named
  snapshots. Stand identity first: compare server/client log `BlockField
<version>` against the workspace pins before trusting a stand.

## Scenario helpers (never hand-roll RCON/testbot drivers)

- `just scenario duo` — two-client lane search (`face_off` tries lanes until
  HP moves; first-shot needs `kit_or_raise` first).
- `just scenario dm` — deathmatch: `wait_deployed` (both bots hold the kit
  gun) before aims/teleports, in-`dm_arena`-dimension waits, `wait_vulnerable`
  (spawn protection), kills confirmed against the server log.
- `just scenario hitreg` — motion/fps/delay matrix, clientless by default.
- `pose_overlay place-verify`, `stress` (bots, no client), `tablet`,
  `parkour`, `check_gameplay`, `check_consent`, `check_benelli`.

## Harness vs product (stop misdiagnosing)

- Zero damage + walled lane = harness lane, use `face_off` lane search.
- Kitless first-shot, spawn-protection window, `dm_arena` map missing =
  harness setup, not a product bug. `testbot:*` falls back to product.
- Cold-VM timing flakes, `runGameTest` shutdown hangs, shutdown races =
  harness/VM, route to the matching backlog unit, do not "fix" product code.
- "Pre-existing"/"flake"/"noise" is never acceptable: stop and return
  `BLOCKED: pre-existing failure` with command, output tail, and diagnosis.

## Project policies (non-negotiable)

- Follow the task owner's machine policy: when their instructions keep Gradle,
  stands or game clients off their machine, use Boat or CI. No model ids in
  commits/PRs. PR title is enough (CI writes release text).
- Prod is read-only.
- One PR per task in the owning repo; batch fixes, no follow-up PRs.
- Never rebase/merge in the main checkout; never `pkill`/`killall` anything.
- GUI apps under Xvfb (Tauri/WebKitGTK/Slint launcher, any GTK app): run them as
  `dbus-run-session -- <cmd>` (or `DBUS_SESSION_BUS_ADDRESS=disabled:` when no bus is
  needed). Without it WebKitGTK autolaunches a session bus plus portals/gvfs/at-spi that
  outlive the test and exhaust the host user's D-Bus and inotify limits. After the run,
  prove nothing of yours remains: no processes with your session env, no FUSE mounts
  under your temp `HOME` (`mount | grep <tmp-home>`).

## Coordinator backlog repos

Backlog issues (`coordinator-backlog`) live in the public Blockfield/blockfield-workspace for
workspace, tools, stand, CI, client, launcher and process items, and in the private server, proxy,
control-plane and workshop repos for server code, deploy and prod state.
