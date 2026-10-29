# Submodules in the private workspace

This public export contains no submodule contents: every gitlink present
in the private tree was dropped, and `.gitmodules` was removed, so no
private repository is referenced here as cloneable.

Paths that were submodule gitlinks in the private tree:

- `client/`
- `localmods/blockfield-decor/`
- `localmods/blockfield-mod/`
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

Jobs and docs that need these checkouts stay in the private repo.
The export only keeps jobs that run on `ubuntu-24.04` without private
submodules or secrets (see `.github/workflows/public-export-check.yml`).
