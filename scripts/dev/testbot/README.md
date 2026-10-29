# Dev testbot input

Commands run on the Minecraft client thread. CLI: `just bot <command>`.
The dev MCP `bot_send` forwards the same tokens, for example
`{"command":["scroll","0.25","-1"]}`. No desktop input or mod-specific
Workshop callback is called by these commands.

| Command                     | Contract                                                                                                                                                                                                                                                                                                                                                                                                        |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `scroll <dy> [dx]`          | One real `MouseHandler.onScroll(window, dx, dy)` call. Both offsets are finite doubles; `dx` defaults to 0. Positive `dy` means wheel up. Fractions, negative values and zero pass through unchanged, before vanilla sensitivity/discrete-scroll processing. HEAD injections see the original offsets. A screen receives its normal wheel event.                                                                |
| `use [ticks]`               | Immediately invoke vanilla `Minecraft.startUseItem()` once and hold the current `keyUse` binding for 1–200 client ticks (default 2). Vanilla handles held repeats with `rightClickDelay`, hand selection, hit routing, Fabric callbacks, swing and packets. If already using an item, only extend the hold, as vanilla does.                                                                                    |
| `use_on <x> <y> <z> <face>` | Use once at the centre of the specified block face through `startUseItem()`, with a two-tick hold. Integer coordinates; face is `down`, `up`, `north`, `south`, `west` or `east` (case-insensitive). Block must be loaded, non-air, inside the border and within vanilla interaction reach. The prior crosshair target is restored even if a callback fails; any later held repeat uses the ordinary crosshair. |
| `sneak on` / `sneak off`    | Set the current sneak binding (including toggle-crouch mode) and immediately synchronize local player/input sneak, so a following `use`/`scroll` sees it without a tick wait. Sneak otherwise follows normal gameplay input and screen/disconnect resets.                                                                                                                                                       |

`use`, `use_on` and `sneak` require a connected world and no screen/overlay.
`use` rejects an unavailable crosshair hit. Vanilla busy-hand, block-destruction
and item-cooldown guards still apply after mod HEAD injections.
It preserves every mod injection/cancellation.
The response's `startUseItem: true` confirms invocation, not acceptance by a mod.
The legacy `pressed` and `ticks` response fields are retained; `pressed` now
reports the actual use binding rather than a hard-coded RMB.

`press key.mouse.right` and `rawinput click 3` still test physical input routing;
they are distinct from semantic `use`. `rawinput` buttons are 1=left, 2=middle,
3=right; GLFW codes are 0, 2, 1. For H/G gestures use `press key.keyboard.h`
and `press key.keyboard.g` (or `rawinput key H` / `rawinput key G` when testing
keyboard callbacks). Repeat wheel calls externally to control event pacing;
`scroll 4` is one event of magnitude four, not four separate notches.

`state` includes `windowActive`, `mouseGrabbed`, `overlay`, `useBinding`,
`useDown`, `useClicks`, `rightClickDelay`, `usingItem`, `handsBusy` and
`destroyingBlock`. Record `state`/`keys` before and after a failed physical
RMB to locate the failing routing/gating stage. `use` does not require an Xvfb
window to be focused or grabbed and does not change focus, options or bindings.

Self-check without a game: the testbot wrapper's `inputCommandsCheck` checks
fractional/signed offsets, axis defaults, duration bounds and invalid input.
It is also part of `check`; it does not apply mixins. Use the shared build lock,
the pinned JDK and `-Dorg.gradle.daemon.idletimeout=600000 --max-workers=2` as described in the workspace
[development guide](../../../docs/DEVELOPING.md).
