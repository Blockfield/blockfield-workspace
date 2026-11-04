# Release gate

No tag without a passing RC record. An owner bypass must name the skipped
gates in the same message — bypassing looks different from passing afterwards.

## Gates (from audit-275-E §8)

| Gate                  | What must be green                                                                                                 | Command                                                       |
| --------------------- | ------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------- |
| G0 identity           | `bf version` + client log vs `client/mods/*.pw.toml` + `server/mods/*.pw.toml`; mismatch = FAIL + stop             | `just scenario rc` (version gate runs first)                  |
| G1 unit               | `just check` + `java-check` + `oracleTest` WITHOUT `BF_SKIP_ORACLE`; `:server:test` + GameTests 18/18; client boot | Boat VM per `common-rules.txt` TESTS BEFORE PUSH              |
| G2 wire/compat        | U10 + wire-gate, all cohorts within bounds; protocol/pin bump checklist                                            | `dev/anim_sync_report.py`, `run_anim_sync_wire_matrix.sh`     |
| G3 RC stand ≤60 min   | full `just scenario rc` battery on a Boat large VM; 2-real-client pass for pair releases                           | `just scenario rc [--record rc.json]`                         |
| G4 deploy dry-run     | export-e2e vs real data incl. controller-phase matrix; `move-approved-tag` + preflights green                      | server `export-e2e` harness + `scripts/deploy-webhooks.py`    |
| G5 post-deploy probes | `/api/rooms` build; `build.json` contract; Game-only entry probe; proxy `gate.json` Game leg; Game join probe      | automatic in `just release ship`; REQUIRED lines need a human |
| G6 perf               | 15-bot normal-arena TPS 20.0, MSPT mean ≤25 ms p95 <50 ms; client FPS floor via pre-cohort gate                    | U11-SCALE rule, `env-fps-gate` (ws#135)                       |

## G0 enforcement (`scripts/release.py`)

`just release ship` and `release.push_tag` refuse to tag unless an RC record
for **exactly** the release builds exists:

```json
{
  "format": "blockfield-rc-record/1",
  "battery": "just scenario rc",
  "releases": { "client": "v2.80.0", "server": "v2.80.1" },
  "checks": [
    {
      "name": "versions loaded match pins",
      "result": "PASS",
      "evidence": "..."
    },
    {
      "name": "long-range kill hamburg",
      "result": "SKIP",
      "evidence": "no second client"
    }
  ]
}
```

- `releases` must equal the tagged sides and versions exactly.
- Every check must be PASS. SKIP is allowed only for checks that need a
  second client (`long-range kill…`, `tdm death/respawn…`) on server-only
  releases.
- `just release ship --rc-record rc.json`.
- Waiver (owner bypass, visible not silent):
  `just release ship --gate-waiver "<reason>"` prints `[WARN] release gate
WAIVED` in the release log and adds `Release-gate waiver:` to the release
  PR bodies.

## G5 probes (`post_deploy_probes`, runs after each deploy in `ship`)

Automated: poll `/api/rooms` until it serves the release builds, then check
the `build.json` contract and run the Game-only entry probe
(`entry_liveness.py --game-only`, when the pinned server supports it). No
read path from this tool (printed REQUIRED, needs a human): prod
`server.properties` view/simulation/broadcast values, proxy gate Game backend
open, join via the Game entry. A Workshop release (explicit
move-approved-tag dispatch) verifies the Workshop legs on its own; Game
ship never checks them.

Process pins: G0–G3 recorded in the release exec report; one stand retry max;
VM stopped within 60 min of verdict; no re-run-green accepts.
