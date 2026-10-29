#!/usr/bin/env python3
"""localmods/ mapping: which submodule provides the jar a pack metafile pins.

Prefix matching cannot cover renamed forks (ParCool-*-bf6.jar vs parcool.pw.toml),
so the mapping in localmods/mods.json is explicit, including recipe output paths.
"""

import hashlib
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path, PureWindowsPath

ROOT = Path(__file__).resolve().parent.parent.parent

PIN = "pin"
LOCAL = "local"

_PIN_URL = re.compile(r"/releases/download/([^/]+)/")

# A pin..HEAD diff touching only these paths never changes the built JAR, so the
# published pin stays the newest source. Everything else is relevant (conservative:
# unknown paths rebuild rather than risk a stale pin).
_IRRELEVANT_DIRS = (".github", "docs")
_IRRELEVANT_TEST_DIRS = ("test", "tests")
_IRRELEVANT_NAMES = {
    ".editorconfig",
    ".gitattributes",
    ".gitignore",
    ".prettierrc",
    ".prettierignore",
    ".ruff.toml",
    "ruff.toml",
    "quality.py",  # lint script convention in the fork repos
}
STAMP_VERSION = 1
STAMP_DIRNAME = "localmods-stamps"


class BuildError(Exception):
    """A LOCAL mod could not be built: die, never silently fall back to the pin."""


def load_mapping(path=None):
    """Example defaults plus whole-node local overrides, without writing either."""
    path = Path(path) if path else ROOT / "localmods" / "mods.json"
    example = path.parent / "mods.json.example"
    if not path.is_file():
        path = example
    mapping = json.loads(path.read_text(encoding="utf-8"))
    if path != example and example.is_file():
        mapping = json.loads(example.read_text(encoding="utf-8")) | mapping
    return mapping


def metafiles(entry, side):
    """Metafile names an entry provides for side (client/server)."""
    names = []
    for key in (side, "both"):
        value = entry.get(key)
        if value:
            names.append(value() if callable(value) else value)
    return [n for n in names if n]


def owners(mapping=None):
    """{metafile: dirname} for both sides."""
    mapping = mapping if mapping is not None else load_mapping()
    out = {}
    for dirname, entry in mapping.items():
        if dirname.startswith("_"):
            continue
        for side in ("client", "server"):
            for name in metafiles(entry, side):
                out[name] = dirname
    return out


def owner(metafile, mapping=None):
    """localmods/ dirname owning a pack metafile, or None."""
    return owners(mapping).get(metafile)


# Classifier outputs that are never the shippable mod jar. Gradle's remapJar
# neighbors from dev/oracle-runner.gradle (oracleJar -> -oracle-named,
# remapOracleJar -> -oracle) refresh on every `check` run, so newest-by-mtime
# alone installed the ORACLE test jar as the client mod twice on stand-batch2.
_NON_RUNTIME_CLASSIFIERS = (
    "-sources.jar",
    "-dev.jar",
    "-javadoc.jar",
    "-test.jar",
    "-tests.jar",
    "-test-fixtures.jar",
    "-oracle.jar",
    "-oracle-named.jar",
)


def runtime_jar_name(name):
    """True when a build/libs filename can be the shipped mod jar."""
    return not name.endswith(_NON_RUNTIME_CLASSIFIERS)


def newest_jar(directory, prefix=""):
    """Newest runtime jar in a build/libs dir, ignoring *sources*/*dev* and classifier byproducts."""
    jars = [
        p
        for p in Path(directory).glob(f"{prefix}*.jar")
        if "sources" not in p.name and "dev" not in p.name and runtime_jar_name(p.name)
    ]
    return max(jars, key=lambda p: p.stat().st_mtime, default=None)


def local_jars(root=None, mapping=None):
    """{metafile: jar_path} of built localmods/ jars.

    An entry's literal jar path selects only that recipe output. Other entries
    scan localmods/<dir>/{client,server,}/build/libs/*.jar. A dirname's jar is
    offered under every assigned metafile. Missing builds fall back to pins.
    """
    root = Path(root) if root else ROOT
    mapping = mapping if mapping is not None else load_mapping()
    jars = {}
    for dirname, entry in mapping.items():
        if dirname.startswith("_"):
            continue
        explicit_jar = None
        found = {}
        if "jar" in entry:
            value = entry["jar"]
            if (
                not isinstance(value, str)
                or not value
                or any(c in value for c in "*?[]")
            ):
                raise ValueError(f"Invalid literal JAR path for {dirname}: {value!r}")
            relative = PureWindowsPath(value)
            if (
                relative.drive
                or relative.root
                or ".." in relative.parts
                or relative.suffix.lower() != ".jar"
            ):
                raise ValueError(f"Invalid relative JAR path for {dirname}: {value!r}")
            directory = root / "localmods" / dirname
            explicit_jar = directory.joinpath(*relative.parts)
            if not explicit_jar.resolve().is_relative_to(directory.resolve()):
                raise ValueError(f"JAR path escapes {dirname}: {value!r}")
            if not explicit_jar.is_file():
                continue
        else:
            found = {
                sub: newest_jar(root / "localmods" / dirname / sub / "build" / "libs")
                for sub in ("client", "server", "")
            }
            if not any(found.values()):
                continue
        if classify(root, dirname, entry)[0] == PIN:
            continue
        for side in ("client", "server"):
            names = metafiles(entry, side)
            if not names:
                continue
            jar = explicit_jar or _side_jar(found, dirname, side)
            if jar:
                for name in names:
                    jars[name] = jar
    return jars


def _side_jar(found, dirname, side):
    """blockfield-mod ships per-side jars (client/ vs server/); forks share one jar."""
    if dirname != "blockfield-mod":
        candidates = [j for j in found.values() if j]
        return max(candidates, key=lambda p: p.stat().st_mtime, default=None)
    if side == "client":
        return found["client"]
    return found["server"]


def pin_tag_for(root, side, metafile):
    """Published release tag pinning a pack metafile, or None.

    Read from the metafile's download URL (.../releases/download/<tag>/...);
    blockfield-mod pins may differ per side.
    """
    metadata = Path(root) / side / "mods" / metafile
    if not metadata.is_file():
        return None
    try:
        url = tomllib.loads(metadata.read_text(encoding="utf-8"))["download"]["url"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(url, str):
        return None
    match = _PIN_URL.search(url)
    return match.group(1) if match else None


def _git(directory, *args):
    """Run git in a submodule checkout; None when it is not a git repo."""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=directory,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout


def pin_commit(directory, tag):
    """Pin tag's commit via the full ref (a tag may share its name with a branch)."""
    out = _git(directory, "rev-parse", f"refs/tags/{tag}^{{commit}}")
    return out.strip() if out and out.strip() else None


def _dirty(directory):
    """Uncommitted changes, including untracked non-ignored files."""
    out = _git(directory, "status", "--porcelain")
    return out.strip() if out and out.strip() else ""


def _diff_names(directory, commit):
    out = _git(directory, "diff", commit, "HEAD", "--name-only", "--no-renames", "-z")
    return out.split("\0") if out else []


def irrelevant_path(path):
    """True when a pin..HEAD diff path cannot change the built JAR."""
    parts = Path(path).parts
    if not parts:
        return True
    if parts[0] in _IRRELEVANT_DIRS:
        return True
    # A test dir counts only outside src/main: a production package may be named "test".
    if any(
        part in _IRRELEVANT_TEST_DIRS and "main" not in parts[:i]
        for i, part in enumerate(parts)
    ):
        return True
    name = parts[-1]
    if name in _IRRELEVANT_NAMES or name.endswith(".md"):
        return True
    return False


def classify(root, dirname, entry):
    """(PIN|LOCAL, reason): the newest source for one mapping entry.

    PIN means local_jars must not offer this mod, so the published pin wins.
    Unclassifiable checkouts (no pin tag, not git) keep the legacy behavior and
    offer whatever is built.
    """
    root = Path(root)
    directory = root / "localmods" / dirname
    if not directory.is_dir():
        return PIN, "not checked out"
    # Legacy local override: a jar under .cache/ (never built) forces the published pin.
    if str(entry.get("jar", "")).startswith(".cache/"):
        return PIN, "forced to the pin by localmods/mods.json"
    tags = {}
    for side in ("client", "server"):
        for metafile in metafiles(entry, side):
            tag = pin_tag_for(root, side, metafile)
            if tag and metafile not in tags:
                tags[metafile] = tag
    if not tags:
        return LOCAL, "no published pin found, offer built jar"
    head = _git(directory, "rev-parse", "HEAD")
    head = head.strip() if head else ""
    if not head:
        return LOCAL, "not a git checkout, offer built jar"
    verdicts = []
    dirty = _dirty(directory)
    for metafile, tag in tags.items():
        commit = pin_commit(directory, tag)
        if not commit:
            verdicts.append((metafile, LOCAL, f"pin tag {tag} not in local clone"))
            continue
        if dirty:
            verdicts.append((metafile, LOCAL, "uncommitted changes"))
            continue
        if head == commit:
            verdicts.append((metafile, PIN, f"HEAD is pin {tag}"))
            continue
        if _git(directory, "merge-base", "--is-ancestor", "HEAD", commit) is not None:
            verdicts.append((metafile, PIN, f"HEAD is older than pin {tag}"))
            continue
        relevant = [
            p for p in _diff_names(directory, commit) if p and not irrelevant_path(p)
        ]
        if not relevant:
            verdicts.append(
                (metafile, PIN, f"only jar-irrelevant paths changed since {tag}")
            )
        else:
            shown = ", ".join(relevant[:3])
            extra = f" (+{len(relevant) - 3} more)" if len(relevant) > 3 else ""
            verdicts.append(
                (metafile, LOCAL, f"relevant changes since {tag}: {shown}{extra}")
            )
    picked = [v for v in verdicts if v[1] == LOCAL]
    if not picked:
        return PIN, "; ".join(f"{m}: {r}" for m, _, r in verdicts)
    return LOCAL, "; ".join(f"{m}: {r}" for m, _, r in picked)


def decisions(root=None, mapping=None, names=None):
    """[(dirname, PIN|LOCAL, reason)] for the plan table and build_mods."""
    root = Path(root) if root else ROOT
    mapping = mapping if mapping is not None else load_mapping()
    selected = names or [d for d in mapping if not d.startswith("_")]
    return [(d, *classify(root, d, mapping[d])) for d in selected if d in mapping]


def _scan_jars(root, dirname, entry):
    """{metafile: jar} currently built for one entry, before source filtering."""
    found = {}
    if "jar" in entry:
        value = entry["jar"]
        relative = PureWindowsPath(value)
        jar = (Path(root) / "localmods" / dirname).joinpath(*relative.parts)
        if jar.is_file():
            for side in ("client", "server"):
                for name in metafiles(entry, side):
                    found[name] = jar
        return found
    scanned = {
        sub: newest_jar(Path(root) / "localmods" / dirname / sub / "build" / "libs")
        for sub in ("client", "server", "")
    }
    for side in ("client", "server"):
        jar = _side_jar(scanned, dirname, side)
        if jar:
            for name in metafiles(entry, side):
                found[name] = jar
    return found


def stamp_file(home, dirname):
    return Path(home) / STAMP_DIRNAME / f"{dirname}.json"


def _hash_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _untracked_digest(directory):
    """Hash of untracked non-ignored files: new sources must invalidate the stamp."""
    out = _git(directory, "ls-files", "--others", "--exclude-standard", "-z")
    digest = hashlib.sha256()
    for relative in sorted(out.split("\0") if out else []):
        if not relative:
            continue
        path = Path(directory) / relative
        digest.update(relative.encode("utf-8", "replace") + b"\0")
        targets = (
            [path]
            if path.is_file()
            else sorted(p for p in path.rglob("*") if p.is_file())
            if path.is_dir()
            else []
        )
        for target in targets:
            try:
                if not target.is_symlink():
                    digest.update(_hash_file(target).encode())
            except OSError:
                digest.update(b"unreadable")
    return digest.hexdigest()


def fingerprint(directory):
    """Source identity for a stamp: None outside a git checkout."""
    head = _git(directory, "rev-parse", "HEAD")
    diff = _git(directory, "diff", "HEAD", "--no-renames")
    if not head or diff is None:
        return None
    return {
        "head": head.strip(),
        "diff": hashlib.sha256(diff.encode("utf-8", "replace")).hexdigest(),
        "untracked": _untracked_digest(directory),
    }


def stamp_fresh(home, dirname, directory, jars):
    """True when the stamp matches current sources and the built jars."""
    try:
        saved = json.loads(stamp_file(home, dirname).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    current = fingerprint(directory)
    if (
        not current
        or saved.get("version") != STAMP_VERSION
        or saved.get("source") != current
    ):
        return False
    expected = saved.get("jars", {})
    if set(expected) != set(jars):
        return False
    for name, jar in jars.items():
        try:
            if _hash_file(jar) != expected[name]:
                return False
        except OSError:
            return False
    return True


def write_stamp(home, dirname, directory, jars):
    current = fingerprint(directory)
    if not current:
        raise BuildError(f"{dirname}: cannot stamp sources outside a git checkout")
    hashes = {name: _hash_file(jar) for name, jar in jars.items()}
    path = stamp_file(home, dirname)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": STAMP_VERSION, "source": current, "jars": hashes}),
        encoding="utf-8",
    )


def _default_runner(command, cwd):
    completed = subprocess.run(
        [str(c) for c in command],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if completed.returncode != 0:
        raise BuildError(
            f"command failed ({completed.returncode}): {' '.join(map(str, command))}\n"
            + (completed.stdout or "")[-4000:]
        )


def build_command(root, directory, recipe):
    """Assemble, not build: the stamp only needs the jar, not test runs."""
    if recipe:
        return ["just", "build"]
    wrapper = directory / ("gradlew.bat" if sys.platform == "win32" else "gradlew")
    task = ["assemble", "--no-daemon", "--max-workers=2", "-Dorg.gradle.jvmargs=-Xmx2G"]
    if sys.platform == "win32":
        return [str(wrapper), *task]
    slot = Path(root) / ".dev" / "coord-20261009" / "gradle-slot"
    if slot.is_file():
        return [str(slot), "--", "sh", str(wrapper), *task]
    # The slot file lives in gitignored .dev/, so clones without coordination
    # still build; flags keep the AGENTS.md resource bounds either way.
    print(
        f"modmap: no gradle slot at {slot}, building without the shared lock",
        file=sys.stderr,
    )
    return ["sh", str(wrapper), *task]


def ensure_built(root, mapping, home, names=None, runner=None):
    """Build LOCAL mods whose stamp differs; [(dirname, source, action)].

    PIN mods need nothing. Failures raise BuildError: the caller dies instead of
    silently falling back to the pin.
    """
    root = Path(root)
    mapping = mapping if mapping is not None else load_mapping()
    runner = runner or _default_runner
    selected = names or [d for d in mapping if not d.startswith("_")]
    report = []
    for dirname in selected:
        entry = mapping.get(dirname)
        if entry is None or dirname.startswith("_"):
            continue
        directory = root / "localmods" / dirname
        source, reason = classify(root, dirname, entry)
        if source == PIN:
            report.append((dirname, source, f"skip: {reason}"))
            continue
        if not directory.is_dir():
            raise BuildError(
                f"{dirname}: {reason}, but {directory} is missing: "
                f"git submodule update --init localmods/{dirname}"
            )
        recipe = "jar" in entry and (directory / "justfile").is_file()
        jars = _scan_jars(root, dirname, entry)
        if jars and stamp_fresh(home, dirname, directory, jars):
            report.append((dirname, source, "fresh"))
            continue
        command = build_command(root, directory, recipe)
        try:
            runner(command, directory)
        except BuildError:
            raise
        except Exception as error:
            raise BuildError(f"{dirname}: build failed: {error}")
        jars = _scan_jars(root, dirname, entry)
        if not jars:
            expected = entry.get("jar", "build/libs/*.jar")
            raise BuildError(f"{dirname}: build did not produce {expected}")
        write_stamp(home, dirname, directory, jars)
        report.append((dirname, source, f"built {', '.join(sorted(jars))}"))
    return report
