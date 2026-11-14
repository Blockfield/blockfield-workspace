# Submodules in the public workspace

This repo tracks the public sources as submodules:

- `client/` → `Blockfield/blockfield-client`
- `localmods/blockfield-mod/` → `Blockfield/blockfield-mod`

The private server is not a submodule (a public repo cannot track a
private one): its pinned commit is recorded in `pins/server.sha`.

Developers with access to the private server repo materialize it with
`just server-checkout`: it clones `Blockfield/blockfield-server` into
`server/` (if missing) and checks out the pinned SHA detached. The recipe is
idempotent; without access the clone fails loudly. `server/` is git-ignored —
never commit it.

At the planned server split cutover, `Blockfield/blockfield-server` becomes a
frozen private archive, and `Blockfield/blockfield-dedicated` and
`Blockfield/blockfield-gameplay` take over. Until that cutover, `pins/server.sha`
and `just server-checkout` continue to use `Blockfield/blockfield-server`.

Paths that remain private-only checkouts in the private workspace:

- `localmods/blockfield-decor/`
- `localmods/blockfield-music/`
- `localmods/cozy-home/`
- `localmods/create-fabric/`
- `localmods/elite-x/`
- `localmods/gucci-vuitton-attachment/`
- `localmods/lr-tactical/`
- `localmods/parcool-fabric/`
- `localmods/porting-lib/`
- `localmods/simplebedrockmodel-fabric/`
- `localmods/superbwarfare-fabric/`
- `localmods/tacz-refabricated/`
- `localmods/vanilla-backport/`
- `localmods/yuushya/`
- `server/`

Jobs and docs that need those checkouts stay in the private repo.
