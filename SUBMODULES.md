# Submodules in the public workspace

This repo tracks the public sources as submodules:

- `client/` → `Blockfield/blockfield-client`
- `localmods/blockfield-mod/` → `Blockfield/blockfield-mod`

The server pair is not submodules: the public workspace cannot track the
private gameplay repo, and dedicated lives outside this repo. Their pinned
commits are recorded in `pins/dedicated.sha` and `pins/gameplay.sha`.

Developers materialize both with `just server-checkout`: it clones
`Blockfield/blockfield-dedicated` into `server/` (the pack root) and
`Blockfield/blockfield-gameplay` into `gameplay/` (the lobby sources for
`--server-source`), each checked out at its pinned SHA detached. The recipe
is idempotent; `server/` and `gameplay/` are git-ignored — never commit
them. The dedicated clone is public; the gameplay clone needs access to the
private gameplay repo and fails loudly without it.

`Blockfield/blockfield-server` is a frozen private archive (pre-split
history). `pins/server.sha` still records its last commit for release
tooling in transition.

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
