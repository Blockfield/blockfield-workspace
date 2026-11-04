"""Live tablet reachability and screenshot matrix on an already-owned headless stand."""

import json
import re
import shutil
import struct
import time
from pathlib import Path

from .body_pose import verify_client
from .common import wait_for

SCENARIO = {
    "name": "tablet",
    "help": "tablet lifecycle, reopen cycles and resolution/GUI-scale captures",
}
SIZES = (
    (1280, 720),
    (1366, 768),
    (1920, 1080),
    (2560, 1440),
    (3840, 2160),
    (1024, 768),
)
# Second roster seat for ranked rooms: carpet fake player on the other team so the
# room leaves PREPARING and reaches GAME with a single real client.
SPAR_BOT = "TabletBot"


def wait_for_battle(bot, rcon, room_id, name, timeout=180):
    """Bring a ranked room to GAME: carpet bot takes the other team, both mark ready.

    GAME is detected through the auto-deploy: the start teleport closes the
    open spawn-selection deployment. (The battle map UI packet already arrives
    while waiting, and the start teleport keeps the room dimension, so neither
    signals the transition.)
    """
    spawned = rcon(f"player {SPAR_BOT} spawn")
    if spawned is None:
        raise AssertionError("no RCON reply to carpet spawn")
    # Carpet materialises the fake player asynchronously: joining before it is
    # online resolves to no entity and silently skips the room.
    ok, _, _ = wait_for(
        lambda remaining: {"text": rcon("list") or ""},
        lambda out: SPAR_BOT.lower() in out["text"].lower(),
        60,
        1.0,
    )
    if not ok:
        raise AssertionError("carpet bot never appeared online")
    joined = rcon(f"execute as {SPAR_BOT} run bf join {room_id}")
    if joined is None or "закрыт" in joined:
        raise AssertionError(f"sparring bot could not join room {room_id}: {joined}")
    deadline = time.monotonic() + timeout
    marked = 0
    while time.monotonic() < deadline:
        if time.monotonic() - marked >= 5:
            marked = time.monotonic()
            mine = rcon(f"execute as {name} run ready on")
            spar = rcon(f"execute as {SPAR_BOT} run ready on")
            print(
                f"battle wait: room={room_id} mine={mine!r} spar={spar!r}", flush=True
            )
        view = bot("bftablet")
        if view.get("deployment") is None and not view.get("booting"):
            return
        time.sleep(0.5)
    raise AssertionError("room never entered GAME: need one ready player per team")


def matches(view, expected):
    return (
        view.get("open")
        and view.get("screen") == "TabletScreen"
        and all(view.get(key) == value for key, value in expected.items())
    )


def wait_player(bot, timeout=60):
    ok, state, _ = wait_for(
        lambda remaining: bot("state", timeout=remaining),
        lambda state: (
            state.get("connected")
            and bool(state.get("name"))
            and not state.get("loadingOverlay")
        ),
        timeout,
    )
    if not ok:
        raise TimeoutError(f"client did not join the owned stand: {state}")
    return state


class Diagnostic:
    def __init__(self, bot, rcon, output, matrix=False, only=()):
        self.bot, self.rcon, self.output, self.matrix = bot, rcon, output, matrix
        # Matrix subset for bounded batches: full cycles always run, only the
        # listed states pay the 24-capture cost.
        self.only = set(only)
        self.rows = []
        self.output.mkdir(parents=True, exist_ok=False)

    def wait(self, expected, timeout=20):
        ok, view, _ = wait_for(
            lambda remaining: self.bot("bftablet", timeout=remaining),
            lambda view: matches(view, expected) and not view.get("booting"),
            timeout,
        )
        if not ok:
            raise AssertionError(f"tablet never reached {expected}: {view}")
        return view

    def key(self, name):
        self.bot("rawinput key " + name)
        # A second callback must follow the first key's two-tick release lease.
        first = self.bot("state")["tick"]
        ok, _, _ = wait_for(
            lambda remaining: self.bot("state", timeout=remaining),
            lambda state: state["tick"] >= first + 3,
            5,
        )
        if not ok:
            raise TimeoutError("client input tick did not advance")

    def tab(self, name):
        for _ in range(7):
            if self.bot("bftablet")["tab"] == name:
                return
            self.bot("rawinput combo Control_L+Tab")
            self.key("Shift_L")
        raise AssertionError(f"tab {name} is unreachable")

    def menu_action(self, action):
        self.tab("MENU")
        for _ in range(80):
            self.key("Tab")
            if self.bot("bfui").get("focus") == action:
                self.key("Return")
                return
        raise AssertionError(f"normal keyboard navigation cannot reach {action}")

    def shot(self, name, expected):
        view = self.wait(expected)
        first = view["frame"]
        # Software-renderer stands hitch for tens of seconds under capture
        # bursts; retry settled frames rather than failing the whole run.
        ok = False
        for _ in range(3):
            ok, view, _ = wait_for(
                lambda remaining: self.bot("bftablet", timeout=remaining),
                lambda current: (
                    current["frame"] >= first + 3 and matches(current, expected)
                ),
                30,
            )
            if ok:
                break
            view = self.wait(expected)
            first = view["frame"]
        if not ok:
            raise AssertionError("no stable rendered tablet frames")
        receipt = self.bot("screenshot " + name)
        source = Path(receipt["file"])
        ok, _, _ = wait_for(
            lambda _: (
                {"bytes": source.read_bytes()} if source.exists() else {"bytes": b""}
            ),
            lambda image: (
                len(image["bytes"]) >= 24
                and image["bytes"].startswith(b"\x89PNG\r\n\x1a\n")
                and image["bytes"].endswith(b"IEND\xaeB`\x82")
                and list(struct.unpack(">II", image["bytes"][16:24])) == view["window"]
            ),
            10,
        )
        if not ok:
            raise AssertionError(
                "screenshot not complete or framebuffer dimensions differ"
            )
        destination = self.output / (name + ".png")
        shutil.copy2(source, destination)
        row = {
            "state": name,
            "expected": expected,
            "view": view,
            "screenshot": str(destination),
            "sourceScreenshot": str(source),
        }
        try:
            row["ui"] = self.bot("bfui")
            row["data"] = self.bot("bfmenu")
        except RuntimeError:
            # Map/result tabs have no MenuScreen layout/data.
            if view["tab"] not in ("MAP", "RESULT"):
                raise
        self.rows.append(row)
        self.save()

    def save(self, error=None):
        (self.output / "manifest.json").write_text(
            json.dumps(
                {"captures": self.rows, "error": error}, ensure_ascii=False, indent=2
            )
            + "\n",
            encoding="utf8",
        )

    def cycle(self, name, expected, target_tab=None):
        self.wait(expected)
        self.shot(name + "-open", expected)
        # One Escape can close only the top layer (a ballot over its result screen
        # leaves the screen open); a player presses until it closes.
        closed = False
        for _ in range(3):
            self.key("Escape")
            ok, _, _ = wait_for(
                lambda remaining: self.bot("bftablet", timeout=remaining),
                lambda view: not view["open"] and view["screen"] is None,
                10,
            )
            if ok:
                closed = True
                break
        if not closed:
            raise AssertionError(name + " did not close through Escape")
        self.key("m")
        if target_tab:
            self.tab(target_tab)
        self.shot(name + "-reopen", expected)
        if self.matrix and (not self.only or name in self.only):
            self.capture_matrix(name, expected)
        print("PASS " + name + ": open / Escape / M / reopen", flush=True)

    def capture_matrix(self, name, expected):
        original = self.bot("bftablet")
        self.rcon("tick freeze")
        try:
            for width, height in SIZES:
                for scale in range(1, 5):
                    self.bot(f"uisize {width} {height}")
                    self.bot(f"uiscale {scale}")
                    ok, _, _ = wait_for(
                        lambda remaining: self.bot("bftablet", timeout=remaining),
                        # Minecraft clamps GUI scale on small windows; the requested
                        # scale stays in the file name, the actual one in manifest.
                        lambda view: view["window"] == [width, height],
                        10,
                    )
                    if not ok:
                        raise AssertionError(
                            f"requested framebuffer {width}x{height} not applied"
                        )
                    self.shot(f"{name}-{width}x{height}-s{scale}", expected)
        finally:
            self.bot("uisize " + " ".join(map(str, original["window"])))
            # A fresh client reports requested scale 0 (auto); restore effective.
            scale = original["guiScale"] or round(original.get("actualScale") or 1)
            self.bot(f"uiscale {max(1, min(4, scale))}")
            self.rcon("tick unfreeze")


def run(args):
    from mcp import game

    if args.plan:
        print(
            "lobby, matches, stats, team assignment, loadout, spawn select, map, in-match menu, death lobby, reconnect, result, post-match vote, next match"
        )
        print(
            "matrix: "
            + ", ".join(f"{w}x{h}" for w, h in SIZES)
            + "; requested GUI scales 1..4 (actual scale recorded)"
        )
        return 0
    if not 60 <= args.timeout <= 1200:
        raise ValueError("--timeout must be 60..1200 seconds")
    for map_id in (args.team_map, args.dm_map):
        if not re.fullmatch(r"[a-z0-9_]+", map_id):
            raise ValueError("map ids must be lowercase identifiers")
    token = game.DEADLINE.set(time.monotonic() + args.timeout)
    diagnostic = None
    rooms = []
    rules_path = game.devstack.SERVER / "blockfield/modes/deathmatch.json"
    backups = {}

    def bot(command, timeout=30):
        nested = game.DEADLINE.set(min(game.DEADLINE.get(), time.monotonic() + timeout))
        try:
            return game.bot({}, 1, command)
        finally:
            game.DEADLINE.reset(nested)

    def start(mode, map_id):
        mark = game.devstack.LOG.stat().st_size
        bot(f"bfaction play:{mode}:{map_id}")
        with game.devstack.LOG.open(encoding="utf8") as log:
            log.seek(mark)
            ok, room, _ = wait_for(
                lambda _: {"text": log.read()},
                lambda entry: bool(re.search(r"Комната (\d+) открыта", entry["text"])),
                15,
            )
        if not ok:
            raise AssertionError("no newly created room in the owned server log")
        rooms.append(re.search(r"Комната (\d+) открыта", room["text"])[1])

    def park_spar_bot():
        # Carpet fake players cannot run bfaction: stop at the console instead.
        try:
            game.rcon(f"player {SPAR_BOT} kill")
        except Exception:
            pass

    def deploy(room_id, player_name):
        diagnostic.key("Return")
        view = diagnostic.wait(
            {"tab": "DEPLOY", "deployment": "SpawnSelectionScreen"}, 45
        )
        diagnostic.cycle(
            "spawn-select", {"tab": "DEPLOY", "deployment": view["deployment"]}
        )
        # The deploy button only works once the match runs: mark both roster
        # seats ready and wait for the start, which auto-deploys.
        wait_for_battle(diagnostic.bot, diagnostic.rcon, room_id, player_name)
        ok, _, _ = wait_for(
            lambda remaining: bot("bftablet", timeout=remaining),
            lambda current: current["deployment"] is None,
            60,
        )
        if not ok:
            raise AssertionError("match start did not deploy the player")
        ok, _, _ = wait_for(
            lambda remaining: bot("state", timeout=remaining),
            lambda state: state.get("matchMap") is not None,
            30,
        )
        if not ok:
            raise AssertionError("deploy closed the tablet but left no match")

    try:
        game.require_process(game.devstack.PID_FILE, game.devstack.RCON_PORT)
        verify_client(game, {}, 1)
        state = wait_player(bot)
        diagnostic = Diagnostic(
            bot,
            game.rcon,
            Path(args.output),
            args.matrix,
            [s for s in args.states.split(",") if s],
        )
        if args.current:
            view = bot("bftablet")
            expected = {
                "tab": view["tab"],
                "menuId": view["menuId"],
                "deployment": view["deployment"],
            }
            diagnostic.cycle("current", expected, view["tab"])
            return 0
        name = state["name"]
        if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", name):
            raise ValueError("invalid player name")
        bot("bfaction leave")
        bot("closescreen")
        if bot("bftablet").get("deployment") is not None:
            # A previous run may have stopped a room with a deployment screen
            # open; the client keeps the stale deployment until it rejoins.
            bot("disconnect")
            bot("connect 127.0.0.1:" + str(game.devstack.PORT))
            ok, _, _ = wait_for(
                lambda remaining: bot("bftablet", timeout=remaining),
                lambda view: view.get("deployment") is None,
                60,
            )
            if not ok:
                raise AssertionError("stale deployment survived a reconnect")
        diagnostic.key("m")
        diagnostic.cycle("lobby", {"tab": "MENU", "menuId": "hub"})
        for tab, menu in (("MATCHES", "rooms"), ("STATS", "leaderboard")):
            diagnostic.tab(tab)
            diagnostic.cycle(tab.lower(), {"tab": tab, "menuId": menu}, tab)
        start("room", args.team_map)
        diagnostic.wait({"tab": "DEPLOY", "deployment": "ClassSelectionScreen"}, 60)
        diagnostic.cycle(
            "team-assignment-loadout",
            {"tab": "DEPLOY", "deployment": "ClassSelectionScreen"},
        )
        deploy(rooms[-1], name)
        diagnostic.key("m")
        diagnostic.cycle("in-match-map", {"tab": "MAP"})
        diagnostic.tab("MENU")
        diagnostic.cycle("in-match-menu", {"tab": "MENU", "menuId": "match"}, "MENU")
        diagnostic.key("Escape")
        view = bot("bftablet")
        # Alive in battle the tablet offers no DEPLOY tab; the battle map packet
        # proves the auto-deploy landed.
        if bot("state").get("matchMap") is None:
            raise AssertionError(f"death step needs a deployed player: {view}")
        game.rcon(f"damage {name} 1000 minecraft:generic_kill")
        view = diagnostic.wait({"tab": "DEPLOY"}, 30)
        diagnostic.cycle(
            "death-lobby", {"tab": "DEPLOY", "deployment": view["deployment"]}
        )
        bot("disconnect")
        bot("connect 127.0.0.1:" + str(game.devstack.PORT))
        diagnostic.wait({"tab": "DEPLOY"}, 60)
        diagnostic.cycle("reconnect-mid-deployment", {"tab": "DEPLOY"})
        bot("bfaction leave")
        game.rcon("bf stop room " + rooms[-1])
        rooms.pop()
        park_spar_bot()
        # Room A: short preparation to reach GAME fast, long ballot to hold the
        # result/vote states through their matrices.
        backups[rules_path] = rules_path.read_bytes() if rules_path.exists() else None
        rules_path.parent.mkdir(parents=True, exist_ok=True)
        rules_path.write_text(
            json.dumps(
                {
                    "kill_limit": 30,
                    "time_limit_seconds": 90,
                    "preparation_seconds": 60,
                    "minimum_players": 1,
                    "respawn_seconds": 3,
                    "protection_seconds": 1,
                    "vote_seconds": 600,
                }
            )
        )
        start("deathmatch", args.dm_map)
        diagnostic.wait({"tab": "DEPLOY", "deployment": "ClassSelectionScreen"}, 60)
        diagnostic.cycle(
            "deathmatch-loadout",
            {"tab": "DEPLOY", "deployment": "ClassSelectionScreen"},
        )
        diagnostic.key("Return")
        # FFA deploys directly after class choice; its post-match ballot has its own server route.
        # The ballot can replace the result screen immediately, so accept either order.
        ok, view, _ = wait_for(
            lambda remaining: bot("bftablet", timeout=remaining),
            lambda v: (
                matches(v, {"tab": "RESULT"})
                or matches(v, {"tab": "MENU", "menuId": "vote"})
            ),
            200,
        )
        if not ok or view is None:
            raise AssertionError(f"DM match never ended: {view}")
        if view.get("tab") != "RESULT":
            diagnostic.tab("RESULT")
        diagnostic.cycle("post-match-result", {"tab": "RESULT"}, "RESULT")
        diagnostic.wait({"tab": "MENU", "menuId": "vote"}, 25)
        diagnostic.cycle("post-match-vote", {"tab": "MENU", "menuId": "vote"})
        bot("bfaction leave")
        game.rcon("bf stop room " + rooms[-1])
        rooms.pop()
        # Room B: long preparation holds the next match's loadout for its matrix.
        rules_path.write_text(
            json.dumps(
                {
                    "kill_limit": 30,
                    "time_limit_seconds": 90,
                    "preparation_seconds": 600,
                    "minimum_players": 1,
                    "respawn_seconds": 3,
                    "protection_seconds": 1,
                    "vote_seconds": 600,
                }
            )
        )
        start("deathmatch", args.dm_map)
        diagnostic.wait({"tab": "DEPLOY", "deployment": "ClassSelectionScreen"}, 60)
        diagnostic.cycle(
            "next-match-loadout",
            {"tab": "DEPLOY", "deployment": "ClassSelectionScreen"},
        )
        return 0
    except Exception as error:
        if diagnostic:
            diagnostic.save(str(error))
        raise
    finally:
        # A fresh cleanup budget applies only to resources created by this scenario.
        game.DEADLINE.reset(token)
        cleanup = game.DEADLINE.set(time.monotonic() + 30)
        try:
            try:
                # Leave before stopping rooms: stopping with a deployment screen
                # open leaves a stale client deployment for the next run.
                bot("bfaction leave")
                bot("closescreen")
            except Exception:
                pass
            for room in rooms:
                game.rcon("bf stop room " + room)
            park_spar_bot()
            for path, backup in backups.items():
                if backup is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(backup)
        finally:
            game.DEADLINE.reset(cleanup)


def register(sub):
    parser = sub.add_parser("tablet", help=SCENARIO["help"])
    parser.add_argument(
        "--plan", action="store_true", help="list coverage without connecting"
    )
    parser.add_argument(
        "--current",
        action="store_true",
        help="diagnose the currently open live state only",
    )
    parser.add_argument(
        "--matrix",
        action="store_true",
        help="capture every state at six sizes and scales 1..4",
    )
    parser.add_argument(
        "--states",
        default="",
        help="with --matrix, capture only these comma-separated states",
    )
    parser.add_argument("--team-map", default="fields")
    parser.add_argument("--dm-map", default="dust2")
    parser.add_argument("--output", default=".dev/tablet-diagnostic")
    parser.add_argument("--timeout", type=int, default=600)
    parser.set_defaults(func=run)
