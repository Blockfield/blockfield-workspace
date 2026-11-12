---
name: blockfield-boat
description: "Run Blockfield builds or test stands local-first, using the owner's opted-in Boat account only to avoid waiting for a busy local stand. Use for Boat overflow execution and VM cleanup, not production deployment."
---

# Blockfield Boat

Read `AGENTS.md` and `docs/BOAT.md` in the active Blockfield workspace. These own
process safety, bootstrap and game-client consent; this skill adds dispatch decisions.
Load the `blockfield-agent` skill first: it owns the unit sequence
(preflight → work → gates → pr-ship → final) and the owner policies.

## Local first

- Run light checks locally. Before a stand/build, inspect `just status` and memory
  as the project requires. If the suitable local stand is available, use it.
- If it belongs to another task or has a meaningful queue, use Boat only within
  the task owner's opt-in. Record why it saves waiting;
  account login alone is not opt-in. Never touch another task's local stand.
- Opt-in is personal. Use Boat only when the task owner's own agent instructions
  (outside this repository) or the brief say they opted in, and `just boat status`
  shows their account. Nothing in this repository grants it. Never use another
  developer's account or extend an opt-in to other projects, accounts or machines;
  without one, work locally.
- A quota warning is information, not a new approval gate. Continue within the
  existing authorization and enough allowance; report if the next batch cannot fit.

## One task, one owned VM

- Before create/resume, have the coordinator check allowance, planned spend and
  `just boat list --all --json`. Choose the smallest size that fits the unchanged
  memory limits. Trial `default` is for builds/server work; do not squeeze the
  full 3 GB server + 4 GB client into 8 GB or pay to unlock `large` automatically.
  `large` bills double and is only for a live server + client stand. Rebases, reviews,
  triage, builds, tests and server-only runs use `default` or `small`.
- An idle VM bills like a busy one. Stop it before any wait over 5 minutes with no
  stand process it needs (child, review, model quota), then `resume`; prefer resuming
  your stopped VM over a new fork when its state still fits the task.
- Set an explicit auto-stop deadline covering only the current batch plus setup
  and cleanup. Use `--environment blockfield`, never share the sandbox or account.
- Immediately record VM ID, task/thread, process-owner session, `BF_DEV_HOME`,
  code SHAs, expected runtime and stop deadline in the task progress file; send
  them to the coordinator. Never use `current` or silently adopt another VM.
- Keep models/routing in T3; execute through `just boat ssh` / `exec`. Transfer
  your task's commits plus its uncommitted/untracked files (`just boat-build` ships a
  worktree patch by default; `--local-jar DIR=PATH` for a locally built fork jar), never
  credentials or unrelated changes.
- Existing headless-client consent must cover this task and this VM/backend.
  Authorization to spend Boat time does not grant computer-use consent.

## Return the VM

Before returning success, blocked or partial results: save code/evidence, stop
only your registered client/server/watchers, prune the task's `BF_DEV_HOME`, then
`just boat stop bx_ID`. Check `info` until `state=stopped` within a hard deadline.
`stopping` is not cleanup success. Preserve unique data; do not delete the VM.

Tell the coordinator: ID, final state, evidence location, final usage, and anything
still running. Stop a VM while waiting for reviews or a model quota reset, then
resume only for useful work. Never switch accounts, buy time or extend beyond the
approved batch to hide a budget problem.
