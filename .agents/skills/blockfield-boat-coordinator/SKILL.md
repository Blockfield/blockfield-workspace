---
name: blockfield-boat-coordinator
description: "Coordinate opted-in Boat overflow for Blockfield: prefer local stands, warn on low allowance or unexpectedly fast spending, and reconcile VM ownership and cleanup after agent tasks."
---

# Blockfield Boat Coordinator

Use with [blockfield-boat](../blockfield-boat/SKILL.md). Read `docs/BOAT.md` in the
workspace for the shared policy. This adds quota and resource accounting, not
permission to stop another session's process or delegate outside T3 routing.

## Plan overflow

Prefer local work. Put only tasks that would wait for the local stand/build slot
on Boat, within the task owner's personal opt-in. Keep a `boat-vms.json` ledger in
your existing coordination directory, outside tracked sources. Record each VM's
ID, task, owning session/thread, size, deadline, expected minutes and cleanup state.
Children report their own records; verify claims against Boat, not names.

Before each batch and on normal progress/task-completion checkpoints, run from
the workspace root:

```sh
python3 .agents/skills/blockfield-boat-coordinator/scripts/check.py --planned-rate 1
```

`--planned-rate` is the approved total burn in default-VM minutes per wall minute:
one `default` = 1; one `large` = 2; two `large` = 4. The helper only reads Boat,
writes `.dev/boat-budget.json`, and emits JSON with warnings and VM inventory.
Use the batch's actual planned total, not an inflated value to silence warnings.
Record a new planned rate before dispatch; measured burn is compared with the
plan saved at the start of the sampled interval. If `inventoryComplete=false`,
read the remaining `boat list` pages before claiming all VMs are accounted for.
There is no background watcher: check at dispatch, about every 15 minutes during
active work, and when processing results. Do not create a new wake/poll loop.

## Warn usefully

- Remaining allowance ≤20%: warn the owner; ≤10%: mark critical. Report hours and
  the number of planned scenarios still affordable. Warn sooner if the next
  batch's estimated setup + run + cleanup cannot fit.
- Trial allowance is the smaller of plan time and `trialComputeCapSeconds` minus
  `trialComputeUsedSeconds`; the API may show 555 plan hours during a 25-hour trial.
- After at least 5 minutes between samples, burn above 125% of the planned total
  is unexpectedly fast. Report measured versus planned rate and inspect ledger
  mismatches/idle VMs. A short planned parallel burst is not a monthly forecast.
- On missing/stale data, do not claim the balance is safe. Check again before
  spending; do not auto-top-up, upgrade, buy a plan or silently use another account.
- Warnings do not revoke existing opt-in or require repeated confirmation. Avoid
  repeating an unchanged warning; surface a worsening level, overspend or new risk.

## Reconcile and close

At every child result and before your own final handback, compare the ledger with
`just boat list --all --json`, and check exact VM IDs with `info`/`usage` as needed.
Every finished, cancelled, blocked or quota-limited task must have a stopped VM,
saved evidence, and a final usage sample. Keep only explicitly assigned active work.

If a child left a VM running, have its owning session finish cleanup using the
existing task protocol. If ownership is absent or mismatched, warn the owner and
seek specific authorization instead of impersonating a session or forcing a stop.
User ownership and process-safety rules still apply. Report unresolved IDs, owner,
deadline and why they remain live; do not report cleanup complete on `stopping`.
