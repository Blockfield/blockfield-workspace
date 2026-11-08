#!/usr/bin/env python3
"""Bake a golden Boat VM: pinned Temurin 21, repos at origin/next where it
exists (else main), warm caches.

Forks the newest golden (or --from), checks out origin/main everywhere,
installs pinned `just`, masks snapd, caps Gradle caches, warms Gradle/Loom/
Minecraft with one compile+remap pass plus `just setup --testbot`
(cargo/npm/uv toolchains), then stops the VM as the new golden.

Example:
  just boat-bake --name bf-golden-20261201
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import boat_build as bb  # noqa: E402

# Golden VMs predate the public migration: their workspace clone still points
# at the archived private repo, whose main pins a private-only client
# (FALLBACK-MAIN want 7ad39817, unfetchable from the public client repo).
PUBLIC_WORKSPACE_URL = "https://github.com/Blockfield/blockfield-workspace.git"

# Pack-host probe for the client-pack warm (client PR #3 pattern: upstream
# release asset, bounded fetch). A dead host must fail in seconds, not hang
# the warm: urllib applies the timeout to the connect and to every read.
PACK_HOST_URL = "https://blockfield.pro/pack.toml"
FETCH_TIMEOUT = 20


def host_reachable(probe_output):
    """True when the pack-host probe printed PACK-HOST-UP (HTTP errors count:
    the host answered, only the probe path may be missing)."""
    return "PACK-HOST-UP" in probe_output


def checkout_script(remote_root):
    return (
        f"cd {remote_root} && git remote set-url origin {PUBLIC_WORKSPACE_URL} && "
        "git remote -v && git fetch origin -q && "
        "git checkout -q origin/main && git submodule init -q && "
        # A workspace pin can name an object the submodule origin does not
        # serve (rewritten history, mid-push race): fall back to the
        # submodule's integration base (origin/next when it exists, else
        # origin/main) so the bake (a cache, not a build input)
        # stays green and warm.
        "for m in client localmods/blockfield-mod; do "
        "want=$(git ls-tree HEAD \"$m\" | awk '{print $3}'); "
        'if git submodule update -q -- "$m" 2>/dev/null && '
        'git -C "$m" cat-file -e "$want" 2>/dev/null; then '
        'echo "PIN-OK $m $want"; else '
        'git -C "$m" fetch origin -q && '
        '(git -C "$m" checkout -q origin/next 2>/dev/null || '
        'git -C "$m" checkout -q origin/main) && '
        'echo "FALLBACK-MAIN $m $(git -C "$m" rev-parse HEAD) '
        '(want was $want)"; fi; done && '
        "git rev-parse HEAD && git submodule status | sed 's/^ *//'"
    )


def probe_script():
    lines = [
        "import urllib.request, urllib.error",
        f"req = urllib.request.Request({PACK_HOST_URL!r}, "
        "headers={'User-Agent': 'boat-bake/1.0'})",
        "try:",
        f"    urllib.request.urlopen(req, timeout={FETCH_TIMEOUT})",
        "    print('PACK-HOST-UP')",
        "except urllib.error.HTTPError:",
        "    print('PACK-HOST-UP')",
        "except Exception as error:",
        "    print('PACK-HOST-DOWN', type(error).__name__, str(error)[:120])",
    ]
    return "python3 - <<'BOAT_BAKE_EOF'\n" + "\n".join(lines) + "\nBOAT_BAKE_EOF"


def launcher_deps_script():
    # plans/next-release/boat-deps-launcher.txt: mingw-w64 + rustup target,
    # Tauri/WebKitGTK build libs, Xvfb + fonts (launcher Layer-2 e2e).
    return (
        "sudo apt-get update -qq && "
        "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "
        "mingw-w64 xvfb xauth fonts-dejavu-core fonts-liberation "
        "mesa-vulkan-drivers libgl1 libfontconfig-dev libxkbcommon-x11-0 libx11-xcb1 "
        "libwebkit2gtk-4.1-dev libayatana-appindicator3-dev librsvg2-dev patchelf "
        "xdotool mesa-utils imagemagick && "
        "rustup target add x86_64-pc-windows-gnu --toolchain 1.99.0 && "
        "cargo clippy --version && "
        "x86_64-w64-mingw32-gcc --version | head -1 && "
        "Xvfb -help 2>&1 | head -1 && "
        "pkg-config --modversion webkit2gtk-4.1 gtk+-3.0"
    )


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from",
        dest="source",
        default=None,
        help="source VM id (default: newest snapshot-ready golden)",
    )
    parser.add_argument(
        "--vm",
        default=None,
        help="bake on this existing VM instead of forking (it is stopped after)",
    )
    parser.add_argument(
        "--name",
        default="bf-golden-" + datetime.now(timezone.utc).strftime("%Y%m%d"),
        help="name for the baked VM",
    )
    parser.add_argument(
        "--type",
        choices=("small", "default"),
        default="default",
        help="machine size for the bake",
    )
    parser.add_argument(
        "--timeout", type=int, default=1800, help="hard timeout per bake step"
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="leave the VM running instead of stopping it",
    )
    return parser.parse_args(argv)


def run_long(vm_id, script, timeout, label):
    """vm_exec with retries past the 540 s single-call observation cap.

    Long steps cap their own history output (tail to a file first): Boat
    history drops oversized outputs and the verdict would be unrecoverable.
    """
    start = time.time()
    try:
        return bb.vm_exec(vm_id, script, timeout=min(timeout, 540))
    except bb.Running as pending:
        command_id = pending.command_id
        while time.time() - start < timeout:
            budget = min(540, timeout - (time.time() - start))
            done, code, out = bb.remote_status(
                vm_id, command_id, time.time(), budget, None
            )
            if done:
                return code, out
        raise RuntimeError(
            f"{label} still running after {timeout}s on {vm_id} "
            f"(command {command_id}); VM left running for recovery"
        )


def step(vm_id, timings, label, script, timeout, check=True):
    print(f"boat-bake [{label}]: start", flush=True)
    start = time.time()
    rc, out = run_long(vm_id, script, timeout, label)
    timings[label] = round(time.time() - start, 1)
    print(f"boat-bake [{label}]: rc={rc} {timings[label]}s", flush=True)
    if check and rc != 0:
        raise RuntimeError(f"bake step {label} failed:\n{out}")
    return out


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    source = args.source or bb.latest_golden_vm()
    out_dir = Path(f"/tmp/boat-bake-{args.name}")
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.vm:
        vm_id, _ = bb.attach_vm(args.vm, args.type)
        print(f"boat-bake: reusing {vm_id} as {args.name}", flush=True)
    else:
        print(f"boat-bake: forking {source} as {args.name}", flush=True)
        vm_id = bb.fork_vm(source, args.type, out_dir)
        bb.run_boat("rename", vm_id, args.name)
    bake_start = time.time()
    timings = {}
    try:
        home = bb.remote_home(vm_id)
        remote_root = bb.remote_workspace(vm_id, home)

        out = step(
            vm_id,
            timings,
            "checkout-main",
            checkout_script(remote_root),
            args.timeout,
        )
        print(out.strip())
        # Private server is not a submodule: materialize at the pin when access exists.
        step(
            vm_id,
            timings,
            "server-checkout",
            f"cd {remote_root} && (just server-checkout || "
            "python3 scripts/dev/server_checkout.py || echo NO-SERVER-ACCESS) && "
            "if [ -d server/.git ]; then git -C server rev-parse HEAD; else "
            "echo NO-SERVER-ACCESS; fi",
            args.timeout,
            check=False,
        )
        step(
            vm_id,
            timings,
            "pinned-just",
            f"cd {remote_root} && python3 -I scripts/quality.py run just --version && "
            "JBIN=$(python3 -c \"import sys; sys.path.insert(0, 'scripts');"
            " from toolchain import tool; print(tool('just'))\") && "
            'install -m755 "$JBIN" "$HOME/.local/bin/just" && '
            'export PATH="$HOME/.local/bin:$PATH" && just --version',
            args.timeout,
        )
        out = step(
            vm_id,
            timings,
            "launcher-deps",
            launcher_deps_script(),
            args.timeout,
        )
        print(out.strip()[-1500:])
        # Boat images run a broken snapd that burns CPU on every fork; the mask survives snapshots.
        step(
            vm_id,
            timings,
            "mask-snapd",
            "sudo -n systemctl mask --now snapd.service snapd.socket "
            "snapd.seeded.service snapd.snap-repair.service snapd.refresh.timer "
            "2>&1 || echo SNAPD-SKIP:$?",
            args.timeout,
            check=False,
        )
        # Disk hygiene: Gradle cleans version-specific caches past this retention.
        step(
            vm_id,
            timings,
            "gradle-caps",
            'mkdir -p "$HOME/.gradle" && '
            "for k in 'org.gradle.caching=true' 'org.gradle.cache.cleanup=true' "
            "'org.gradle.cache.cleanup.max-age-in-days=14'; do "
            'n=${k%%=*}; grep -q "^$n=" "$HOME/.gradle/gradle.properties" '
            '2>/dev/null || echo "$k" >> "$HOME/.gradle/gradle.properties"; done && '
            'cat "$HOME/.gradle/gradle.properties"',
            args.timeout,
        )
        env_file = bb.write_env(vm_id, remote_root, "boat-bake")
        out = step(
            vm_id,
            timings,
            "pack-host-probe",
            probe_script(),
            FETCH_TIMEOUT + 60,
            check=False,
        )
        print(out.strip())
        if host_reachable(out):
            out = step(
                vm_id,
                timings,
                "setup-testbot",
                f"source {env_file} && cd {remote_root} && "
                "just setup --testbot > /tmp/bake-setup.log 2>&1; rc=$?; "
                "tail -c 6000 /tmp/bake-setup.log; exit $rc",
                args.timeout,
            )
            print(out.strip()[-2000:])
        else:
            # Client-pack fetch needs blockfield.pro: skip the warm when the
            # host is down, keep baking the caches that need no pack host.
            print(
                "boat-bake [setup-testbot]: PACK-HOST-DOWN-SKIP "
                "(JDK/Gradle/Loom warm below needs no pack host)",
                flush=True,
            )
        out = step(
            vm_id,
            timings,
            "warm-compile-remap",
            f"source {env_file} && cd {remote_root}/localmods/blockfield-mod && "
            "./gradlew --max-workers=2 :common:compileJava "
            ":client:compileJava :client:remapJar > /tmp/bake-warm.log 2>&1; rc=$?; "
            "tail -c 6000 /tmp/bake-warm.log; exit $rc",
            args.timeout,
        )
        print(out.strip()[-2000:])
        step(
            vm_id,
            timings,
            "server-warm",
            f"source {env_file} && cd {remote_root} && "
            "if [ -x server/gradlew ]; then (cd server && "
            "./gradlew --max-workers=2 compileJava); else echo NO-SERVER-WARM; fi",
            args.timeout,
            check=False,
        )
        step(
            vm_id,
            timings,
            "toolchains",
            "cargo --version || true; npm --version || true; uv --version || true; "
            'du -sh "$HOME/.cargo" "$HOME/.npm" "$HOME/.cache/uv" 2>/dev/null || true',
            args.timeout,
            check=False,
        )
        out = step(
            vm_id,
            timings,
            "prune-report",
            f"source {env_file} && cd {remote_root}/localmods/blockfield-mod && "
            "./gradlew --stop >/dev/null 2>&1 || true; "
            f"cd {remote_root} && rm -rf .dev/boat-bake && "
            "test -z \"$(git status --short | grep -v '^??')\" && echo TREE-CLEAN; "
            'du -sh "$HOME/.gradle/caches" .cache 2>/dev/null || true',
            args.timeout,
            check=False,
        )
        print(out.strip())
    except Exception:
        print(f"boat-bake: FAILED on {vm_id}; VM left running for recovery", flush=True)
        raise
    finally:
        if not args.keep:
            bb.run_boat("stop", vm_id)
            state = bb.wait_state(vm_id)
            print(f"boat-bake: vm={vm_id} state={state}", flush=True)
            if state != "stopped":
                raise RuntimeError(f"{vm_id} did not reach stopped")
    wall = round(time.time() - bake_start, 1)
    report = {
        "vm": vm_id,
        "name": args.name,
        "source": source,
        "wall_s": wall,
        "steps_s": timings,
    }
    print("BOAT-BAKE-REPORT " + json.dumps(report), flush=True)
    (out_dir / "bake-report.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
