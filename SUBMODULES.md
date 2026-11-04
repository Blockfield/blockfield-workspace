# Submodules in the public workspace

This repo tracks the public sources as submodules:

- `client/` → `Blockfield/blockfield-client`
- `localmods/blockfield-mod/` → `Blockfield/blockfield-mod`

The private server is not a submodule (a public repo cannot track a
private one): its pinned commit is recorded in `pins/server.sha`.

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
The export only keeps jobs that run on `ubuntu-24.04` without private
submodules or secrets (see `.github/workflows/public-export-check.yml`).
