#!/usr/bin/env python3
"""Opt-in stand proxy: the pinned prod Velocity + raknetify in front of the stand server.

Prod entry path is client -> proxy (RakNet) -> Game; plain stands connect straight
to the server, so transport/gate bugs are never exercised before prod. This module
runs that missing hop locally: loopback clients only, a generated forwarding secret,
a stand gate.json, and a stub Drasl profile lookup (no prod credentials).

Usage (via devstack, same session owns every process):
  just server start --via-proxy     # proxy + stub first, then the server with its
                                    # proxy-only mods enabled and the shared secret
  just server stop                  # stops the server, the proxy and the stub
  just scenario entry ...           # join through the proxy, both gate.json
                                    # backends, reconnect after a proxy restart
"""

import argparse
import base64
import hashlib
import hmac
import http.server
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

# Pinned prod entry path: Velocity 3.5.1 build 615 (fill.papermc.io, RECOMMENDED)
# and the raknetify build from the prod proxy Dockerfile (same digest).
VELOCITY_VERSION = "3.5.1"
VELOCITY_BUILD = 615
VELOCITY_JAR = f"velocity-{VELOCITY_VERSION}-{VELOCITY_BUILD}.jar"
VELOCITY_URL = (
    "https://fill-data.papermc.io/v1/objects"
    "/b4e3164df5377346854dc6cb9e6a78022b1946ff69e89676313f5f6f1c6f0fb3"
    f"/{VELOCITY_JAR}"
)
VELOCITY_SHA256 = "b4e3164df5377346854dc6cb9e6a78022b1946ff69e89676313f5f6f1c6f0fb3"
# Blockfield fork bf3: upstream 76ab30a + SYNC FrameData release fix + second
# RakNet UDP port for the Workshop profile. Upstream Modrinth jar has no
# second port, so the stand cannot use it for the workshop leg.
RAKNETIFY_JAR = "raknetify-velocity-0.1.0+alpha.5.1-bf3-all.jar"
RAKNETIFY_URL = (
    "https://github.com/Blockfield/raknetify/releases/download/bf3/"
    "raknetify-velocity-0.1.0+alpha.5.1-bf3-all.jar"
)
RAKNETIFY_SHA256 = "d8a609bf562d20fbd98d50e9b4c32ffce7b8b51d413b6dc9418af2fd8d07273e"
# Prod plugin source: latest prod release tag at the time of writing (proxy#49
# pins the prod image by digest in Dokploy; the plugin jar is built from here).
PROXY_REPO = "<private-repo>"
PROXY_TAG = "v2.61.1"
PLUGIN_JAR = "blockfield-proxy-1.0.0.jar"

PROXY_TCP = 25565
PROXY_UDP = 25566
# Second RakNet UDP port: the Workshop profile. Routing is by inbound port
# (BackendRouting), because 1.21.1 clients handshake with the resolved address
# rDNS/IP instead of the typed hostname; rDNS games are no longer needed and
# any loopback dial works for either leg.
WORKSHOP_UDP = 25567
STUB_PORT = 25680
WORKSHOP_HOST = (
    "localhost"  # workshop profile connects via localhost, game via 127.0.0.2
)
# Game leg dials 127.0.0.2 or the first global IPv4. Kept from the vhost era:
# harmless under port routing, and 127.0.0.1 stays out of the game leg.
GAME_HOST = "127.0.0.2"


def game_dial_host():
    """First non-loopback IPv4: the game leg must arrive on an address whose
    rDNS name is not localhost (127.0.0.1 always is), and raknetify only binds
    addresses assigned to the VM."""
    try:
        out = subprocess.run(
            ["ip", "-o", "-4", "addr", "show", "scope", "global"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and "/" in parts[3]:
                return parts[3].split("/")[0]
    except (OSError, subprocess.SubprocessError):
        pass
    return GAME_HOST


LEAK_OPT = "-Dio.netty.leakDetection.level=paranoid"


def proxy_dir(home):
    return Path(home) / "proxy"


def _fetch(url, dest, sha256):
    dest = Path(dest)
    if dest.is_file():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    # fill-data.papermc.io answers 403 to urllib's default UA from datacenters.
    request = urllib.request.Request(url, headers={"User-Agent": "blockfield-stand"})
    with (
        urllib.request.urlopen(request, timeout=120) as response,
        open(dest, "wb") as out,
    ):
        shutil.copyfileobj(response, out)
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    if digest != sha256:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"checksum mismatch for {url}: {digest}")


def offline_uuid(name):
    """Velocity offline UUID for a nickname (OfflinePlayer:<name> name-based UUID)."""
    digest = hashlib.md5(("OfflinePlayer:" + name).encode("utf-8")).digest()
    return str(uuid.UUID(bytes=digest, version=3))


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _session_secret(forwarding_secret):
    return hmac.new(
        forwarding_secret.encode("utf-8"), b"blockfield-session-v1", hashlib.sha256
    ).digest()


def mint_session(forwarding_secret, name):
    """Pre-seed a remembered proxy login (mirrors Sessions.issue, same byte layout).

    Lets the stand exercise the saved-session resume path without Drasl passwords.
    shortcut: stub only answers the public profile lookup; password/session endpoints
    stay unimplemented — add them if the stand ever needs a /login leg.
    """
    secret = _session_secret(forwarding_secret)
    header = _b64(b'{"alg":"HS256","typ":"JWT"}')
    now = int(time.time())
    claims = json.dumps(
        {
            "sub": name,
            "uuid": offline_uuid(name).replace("-", ""),
            "jti": secrets.token_hex(16),
            "iat": now,
            "exp": now + 30 * 24 * 3600,
        },
        separators=(",", ":"),
    )
    unsigned = header + "." + _b64(claims.encode("utf-8"))
    sig = hmac.new(secret, unsigned.encode("utf-8"), hashlib.sha256).digest()
    jwt = unsigned + "." + _b64(sig)
    key = hmac.new(
        secret, ("key:" + json.loads(claims)["jti"]).encode(), hashlib.sha256
    )
    return {"token": jwt, "key": key.hexdigest()}


def mint_session_file(forwarding_secret, game_dir, *names):
    """Write <game_dir>/config/blockfield-session.json for the given nicknames."""
    path = Path(game_dir) / "config" / "blockfield-session.json"
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = {}
    for name in names:
        current[name] = mint_session(forwarding_secret, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current), encoding="utf-8")


def ensure_secret(home):
    path = proxy_dir(home) / "forwarding.secret"
    if path.is_file() and path.read_text(encoding="utf-8").strip():
        return path.read_text(encoding="utf-8").strip()
    secret = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret + "\n", encoding="utf-8")
    return secret


def plugin_jar(home):
    return proxy_dir(home) / "plugins" / PLUGIN_JAR


def build_plugin(home, java_exe):
    """Build the prod plugin jar from the pinned tag (Gradle, VM network)."""
    dest = plugin_jar(home)
    if dest.is_file():
        return dest
    src = proxy_dir(home) / "upstream"
    if not (src / "proxy" / "plugin" / "build.gradle").is_file():
        shutil.rmtree(src, ignore_errors=True)
        subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                PROXY_TAG,
                PROXY_REPO,
                str(src),
            ],
            check=True,
            timeout=300,
        )
    env = dict(os.environ, JAVA_HOME=str(Path(java_exe).resolve().parent.parent))
    subprocess.run(
        [
            "./gradlew",
            "build",
            "--max-workers=2",
            "-Dorg.gradle.daemon.idletimeout=600000",
        ],
        cwd=src / "proxy" / "plugin",
        env=env,
        check=True,
        timeout=1800,
    )
    built = src / "proxy" / "plugin" / "build" / "libs" / PLUGIN_JAR
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(built, dest)
    return dest


def setup(home, server_port, java_exe):
    """Download pinned jars and build the pinned plugin jar (idempotent)."""
    root = proxy_dir(home)
    _fetch(VELOCITY_URL, root / VELOCITY_JAR, VELOCITY_SHA256)
    _fetch(RAKNETIFY_URL, root / "plugins" / RAKNETIFY_JAR, RAKNETIFY_SHA256)
    build_plugin(home, java_exe)
    (root / "plugins").mkdir(parents=True, exist_ok=True)
    write_gate(root)
    return root


def write_gate(root):
    """Stand gate.json: Game open, Workshop closed (both backends asserted by entry)."""
    gate = {
        "schema": 1,
        "backend": "game",
        "gate": "public",
        "message": "",
        "allowed": [],
        "backends": {
            "game": {"gate": "public", "message": "", "allowed": []},
            "workshop": {
                "gate": "closed",
                "message": "Мастерская сейчас недоступна.",
                "allowed": [],
            },
        },
    }
    path = Path(root) / "gate.json"
    path.write_text(json.dumps(gate, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_velocity_toml(home, server_port):
    """Stand velocity.toml: loopback clients only, game/workshop backends, no limbo lobby.

    Unauthenticated players keep Velocity's default try=[game] and are denied by
    the plugin's ServerPreConnect gate, so the stand needs no Limbo: the entry
    scenario only ever connects remembered-session (pre-authed) profiles.
    """
    root = proxy_dir(home)
    secret_path = root / "forwarding.secret"
    # shortcut: one bind cannot cover two loopback virtual hosts (game needs
    # 127.0.0.2, workshop 127.0.0.1) once 127.0.0.1 is known to rDNS to
    # localhost, so the stand binds all VM interfaces; revisit if stands ever
    # share a host.
    toml = f"""config-version = "2.7"
bind = "0.0.0.0:{PROXY_TCP}"
motd = "<gold>BLOCKFIELD STAND"
show-max-players = 20
online-mode = false
force-key-authentication = false
prevent-client-proxy-connections = false
player-info-forwarding-mode = "modern"
forwarding-secret-file = "{secret_path.name}"
announce-forge = false
kick-existing-players = false
ping-passthrough = "disabled"
enable-player-address-logging = true
log-player-connections = true
accepts-transfers = false

[servers]
game = "127.0.0.1:{server_port}"
workshop = "127.0.0.1:1"
try = ["game"]

[forced-hosts]

[advanced]
compression-threshold = 256
compression-level = -1
login-ratelimit = 3000
connection-timeout = 5000
read-timeout = 30000
haproxy-protocol = false
tcp-fast-open = false
bungee-plugin-message-channel = true
show-ping-requests = false
failover-on-unexpected-server-disconnect = true
announce-proxy-commands = true
log-command-executions = false
log-player-connections = true
accepts-transfers = false

[query]
enabled = false
port = {PROXY_TCP}
map = "Velocity"
show-plugins = false
"""
    path = root / "velocity.toml"
    path.write_text(toml, encoding="utf-8")
    return path


def write_server_secret(home, server_dir):
    """FabricProxy-Lite.toml: same secret Velocity forwards with (prod parity)."""
    secret = ensure_secret(home)
    path = Path(server_dir) / "config" / "FabricProxy-Lite.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "hackOnlineMode = true\n"
        "hackEarlySend = true\n"
        "hackMessageChain = true\n"
        f'secret = "{secret}"\n',
        encoding="utf-8",
    )


class _Stub(http.server.BaseHTTPRequestHandler):
    """Stub Drasl: only the public profile lookup the session path needs."""

    def _profile(self, name):
        body = json.dumps(
            {"id": offline_uuid(name).replace("-", ""), "name": name}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/account/users/profiles/minecraft/"):
            self._profile(self.path.rsplit("/", 1)[1].split("?", 1)[0])
        elif self.path.startswith("/session/minecraft/hasJoined"):
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            name = dict(
                part.split("=", 1) for part in query.split("&") if "=" in part
            ).get("username", "")
            self._profile(name)
        else:
            self.send_error(404)

    def do_POST(self):  # noqa: N802
        self.send_error(404)

    def log_message(self, *args):  # noqa: N802
        pass


def run_stub():
    http.server.HTTPServer(("127.0.0.1", STUB_PORT), _Stub).serve_forever()


def count_leaks(*logs):
    """Count Netty LEAK lines (raknetify FrameData shape) across log files."""
    total = 0
    for log in logs:
        try:
            total += sum(
                1
                for line in Path(log)
                .read_text(encoding="utf-8", errors="replace")
                .splitlines()
                if "LEAK" in line
            )
        except OSError:
            pass
    return total


def proxy_pid_file(home):
    return proxy_dir(home) / "proxy.pid"


def stub_pid_file(home):
    return proxy_dir(home) / "stub.pid"


def start(home, server_port, paranoid=False):
    """Write configs and start the stub + Velocity (idempotent, same-session owned)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from devstack import SERVER, java_exe, port_open, recorded_pid, start_process

    home, root = Path(home), proxy_dir(home)
    setup(home, server_port, java_exe())
    secret = ensure_secret(home)
    write_velocity_toml(home, server_port)
    write_gate(root)
    write_server_secret(home, SERVER)
    if recorded_pid(stub_pid_file(home)) is None:
        stub_pid_file(home).unlink(missing_ok=True)
        start_process(
            [sys.executable, "-I", str(Path(__file__).resolve()), "stub"],
            root,
            stub_pid_file(home),
            root / "stub.out",
            env=dict(os.environ),
        )
    command = [java_exe(), "-Xms64m", "-Xmx256m"]
    if paranoid:
        command.append(LEAK_OPT)
    command += [
        "-Draknetify.velocity.portOverride=" + str(PROXY_UDP),
        "-Draknetify.velocity.workshopPort=" + str(WORKSHOP_UDP),
        "-jar",
        str(root / VELOCITY_JAR),
    ]
    env = dict(
        os.environ,
        VELOCITY_FORWARDING_SECRET=secret,
        BLOCKFIELD_GATE_FILE=str(root / "gate.json"),
        BLOCKFIELD_WORKSHOP_HOST=WORKSHOP_HOST,
        BLOCKFIELD_RAKNET_WORKSHOP_PORT=str(WORKSHOP_UDP),
        BLOCKFIELD_DRASL_URL=f"http://127.0.0.1:{STUB_PORT}",
    )
    pid, _ = start_process(
        command, root, proxy_pid_file(home), root / "velocity.out", env=env
    )
    for _ in range(60):
        if port_open(PROXY_TCP):
            break
        time.sleep(2)
    else:
        raise SystemExit(f"proxy did not open 127.0.0.1:{PROXY_TCP}, see {root}")
    dial = f"raknet;{game_dial_host()}:{PROXY_UDP}"
    (root / "game-dial.txt").write_text(dial + "\n", encoding="utf-8")
    workshop_dial = f"raknet;localhost:{WORKSHOP_UDP}"
    (root / "workshop-dial.txt").write_text(workshop_dial + "\n", encoding="utf-8")
    print(
        f"proxy ready: 127.0.0.1:{PROXY_TCP} TCP + :{PROXY_UDP}/:{WORKSHOP_UDP} RakNet (pid {pid})"
    )
    print(f"game leg dials {dial} (workshop leg dials {workshop_dial})")
    return pid


def stop(home, stop_stub=True):
    from devstack import stop_process

    stop_process(proxy_pid_file(home))
    if stop_stub:
        stop_process(stub_pid_file(home))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("setup", "start", "stop", "restart", "stub", "mint", "count")
    )
    parser.add_argument("--paranoid", action="store_true")
    parser.add_argument("--game-dir", default=None)
    parser.add_argument("names", nargs="*")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    if args.action == "stub":
        run_stub()
        return 0
    from devstack import HOME as home, LOG as server_log, PORT

    if args.action == "mint":
        # Pre-seed remembered proxy logins so the stand headless client resumes
        # its session without Drasl passwords (local-only stand secret).
        from devstack import default_game_dir

        game_dir = Path(args.game_dir) if args.game_dir else default_game_dir()
        if not args.names:
            raise SystemExit("mint needs at least one nickname")
        mint_session_file(ensure_secret(home), game_dir, *args.names)
        print(f"minted {len(args.names)} session(s) in {game_dir}")
        return 0
    if args.action == "count":
        print(
            count_leaks(
                proxy_dir(home) / "velocity.out", server_log, home / "client.out"
            )
        )
        return 0
    root = proxy_dir(home)
    if args.action == "setup":
        from devstack import java_exe

        setup(home, PORT, java_exe())
        print(f"proxy jars ready in {root}")
        return 0
    if args.action in ("stop", "restart"):
        stop(home)
    if args.action in ("start", "restart"):
        start(home, PORT, args.paranoid)
    return 0


if __name__ == "__main__":
    main()
