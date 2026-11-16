---
name: blockfield-boat-coordinator
description: "Coordinate opted-in Boat overflow for Blockfield: run parallel VMs freely and reconcile VM ownership and cleanup after agent tasks."
---

# Blockfield Boat Coordinator

Use with [blockfield-boat](../blockfield-boat/SKILL.md). Read `docs/BOAT.md` in the
workspace for the shared policy. This adds VM accounting, not permission to stop
another session's process or delegate outside T3 routing.

## Plan overflow

Put work that would wait for the local stand/build slot on Boat, within the task
owner's personal opt-in; parallel VMs are fine. There is no budget, allowance or
spend check and no cost approval: speed of parallel development is the goal.
Keep a `boat-vms.json` ledger in your existing coordination directory, outside
tracked sources. Record each VM's ID, task, owning session/thread, size, deadline
and cleanup state. Children report their own records; verify claims against Boat,
not names. If `inventoryComplete=false` in `boat list`, read the remaining pages.

## Reconcile and close

At every child result and before your own final handback, compare the ledger with
`just boat list --all --json`, and check exact VM IDs with `info` as needed.
Every finished, cancelled or blocked task must have a stopped VM and saved evidence.
Keep only explicitly assigned active work.

If a child left a VM running, have its owning session finish cleanup using the
existing task protocol. If ownership is absent or mismatched, warn the owner and
seek specific authorization instead of impersonating a session or forcing a stop.
User ownership and process-safety rules still apply. Report unresolved IDs, owner,
deadline and why they remain live; do not report cleanup complete on `stopping`.
