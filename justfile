set quiet
set dotenv-load := false
set positional-arguments
set default-script
set script-interpreter := ["python", "-I", "-X", "utf8"]

default:
    import runpy, sys
    sys.argv = ["bf.py", "--help"]
    runpy.run_path("bf.py", run_name="__main__")

lint *args:
    import runpy, sys
    sys.argv = ["scripts/quality.py", "lint", *sys.argv[1:]]
    runpy.run_path("scripts/quality.py", run_name="__main__")

format-check *args:
    import runpy, sys
    sys.argv = ["scripts/quality.py", "format-check", *sys.argv[1:]]
    runpy.run_path("scripts/quality.py", run_name="__main__")

format *args:
    import runpy, sys
    sys.argv = ["scripts/quality.py", "format", *sys.argv[1:]]
    runpy.run_path("scripts/quality.py", run_name="__main__")

test *args:
    import runpy, sys
    sys.argv = ["scripts/quality.py", "test", *sys.argv[1:]]
    runpy.run_path("scripts/quality.py", run_name="__main__")

check *args:
    import runpy, sys
    sys.argv = ["scripts/quality.py", "check", *sys.argv[1:]]
    runpy.run_path("scripts/quality.py", run_name="__main__")

compile-check *args:
    import runpy, sys
    sys.argv = ["scripts/quality.py", "compile", *sys.argv[1:]]
    runpy.run_path("scripts/quality.py", run_name="__main__")

rclone *args:
    import runpy, sys
    sys.argv = ["scripts/quality.py", "run", "rclone", *sys.argv[1:]]
    runpy.run_path("scripts/quality.py", run_name="__main__")

boat *args:
    import runpy, sys
    sys.argv = ["scripts/quality.py", "run", "boat", "--no-update", *sys.argv[1:]]
    runpy.run_path("scripts/quality.py", run_name="__main__")

assets *args:
    import runpy, sys
    sys.argv = ["server/scripts/toolchain.py", "server/scripts/publish-assets.py", *sys.argv[1:]]
    runpy.run_path("server/scripts/toolchain.py", run_name="__main__")

dev *args:
    import runpy, sys
    sys.argv = ["bf.py", *sys.argv[1:]]
    runpy.run_path("bf.py", run_name="__main__")

setup *args:
    import runpy, sys
    sys.argv = ["bf.py", *["setup", *sys.argv[1:]]]
    runpy.run_path("bf.py", run_name="__main__")

build-mods *args:
    import runpy, sys
    sys.argv = ["bf.py", *["build-mods", *sys.argv[1:]]]
    runpy.run_path("bf.py", run_name="__main__")

build-testbot *args:
    import runpy, sys
    sys.argv = ["bf.py", *["build-testbot", *sys.argv[1:]]]
    runpy.run_path("bf.py", run_name="__main__")

scenario *args:
    import runpy, sys
    sys.argv = ["bf.py", *["scenario", *sys.argv[1:]]]
    runpy.run_path("bf.py", run_name="__main__")

watch *args:
    import runpy, sys
    sys.argv = ["bf.py", *["watch", *sys.argv[1:]]]
    runpy.run_path("bf.py", run_name="__main__")

iterate side="both" *args:
    import runpy, sys
    sys.argv = ["bf.py", "watch", *sys.argv[1:], "--once"]
    runpy.run_path("bf.py", run_name="__main__")

capture *args:
    import runpy, sys
    sys.argv = ["bf.py", "capture", *sys.argv[1:]]
    runpy.run_path("bf.py", run_name="__main__")

server *args:
    import runpy, sys
    sys.argv = ["bf.py", *["server", *(sys.argv[1:] or ["start"])]]
    runpy.run_path("bf.py", run_name="__main__")

play *args:
    import runpy, sys
    sys.argv = ["bf.py", *["play", *sys.argv[1:]]]
    runpy.run_path("bf.py", run_name="__main__")

client *args:
    import runpy, sys
    sys.argv = ["bf.py", *["client", *sys.argv[1:]]]
    runpy.run_path("bf.py", run_name="__main__")

bot *args:
    import runpy, sys
    sys.argv = ["bf.py", *["bot", *sys.argv[1:]]]
    runpy.run_path("bf.py", run_name="__main__")

status *args:
    import runpy, sys
    sys.argv = ["bf.py", *["status", *sys.argv[1:]]]
    runpy.run_path("bf.py", run_name="__main__")

release *args:
    import runpy, sys
    sys.argv = ["scripts/release.py", *sys.argv[1:]]
    runpy.run_path("scripts/release.py", run_name="__main__")

mod *args:
    import runpy, sys
    sys.argv = ["bf.py", *["mod", *sys.argv[1:]]]
    runpy.run_path("bf.py", run_name="__main__")

mcp *args:
    import runpy, sys
    sys.argv = ["bf.py", *["mcp", *sys.argv[1:]]]
    runpy.run_path("bf.py", run_name="__main__")

wsai-bench *args:
    import runpy, sys
    sys.argv = ["scripts/wsai_bench.py", *sys.argv[1:]]
    runpy.run_path("scripts/wsai_bench.py", run_name="__main__")
