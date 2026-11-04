"""Build a reproducible scrubbed export of the tracked tree for a future public repo.

The literal scrub policy (exact private strings) lives in a PRIVATE file that is
itself excluded from the export; the export fails closed if it is missing. The
shipped public config holds only generic class gates plus an explicit allowlist
of public-vocabulary tokens; no hashes or other derived data of private tokens
are published anywhere. Public re-verification (`--check`) runs the generic
gates without any private literals.

Archive entries are read into memory and only regular files are ever written, so
tracked symlinks can never make the export read or rewrite files outside it:
internal links are recreated only if their target stays inside the export and is
itself scrubbed, everything else is dropped and reported.

Usage (private repo): python3 -I scripts/public_export.py --ref origin/main --out <empty dir>
Usage (public re-check): python3 -I scripts/public_export.py --check <tree>
"""

import argparse
import codecs
import fnmatch
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from toolchain import download  # noqa: E402

CONFIG_DEFAULT = ROOT / "scripts" / "public_export_config.json"
PRIVATE_DEFAULT = ROOT / "scripts" / "public_export_private.json"
CLIENT_OVERLAY_DEFAULT = ROOT / "scripts" / "public_export_client.json"
MOD_OVERLAY_DEFAULT = ROOT / "scripts" / "public_export_mod.json"

TARGETS = {
    "workspace": {
        "repo": ROOT,
        "config": CONFIG_DEFAULT,
        "private": PRIVATE_DEFAULT,
        "overlay": None,
    },
    "client": {
        "repo": ROOT / "client",
        "config": CONFIG_DEFAULT,
        "private": PRIVATE_DEFAULT,
        "overlay": CLIENT_OVERLAY_DEFAULT,
    },
    "mod": {
        "repo": ROOT / "localmods" / "blockfield-mod",
        "config": CONFIG_DEFAULT,
        "private": PRIVATE_DEFAULT,
        "overlay": MOD_OVERLAY_DEFAULT,
    },
}


def resolve_target(name):
    """Map an export target name to (repo, config, private, overlay) paths."""
    try:
        target = TARGETS[name]
    except KeyError:
        raise SystemExit(
            f"unknown export target: {name} (want one of {sorted(TARGETS)})"
        )
    return target["repo"], target["config"], target["private"], target["overlay"]


GITLEAKS_VERSION = "8.24.3"
GITLEAKS_BASE = (
    f"https://github.com/gitleaks/gitleaks/releases/download/v{GITLEAKS_VERSION}"
)
# Immutable release artifacts + checksums (gitleaks CHECKSUMS for v8.24.3,
# verified against the upstream release file during review of this script).
# shortcut: only linux/darwin x64+arm64 and windows x64 are pinned (v8.24.3 ships
# no windows/arm64 asset); add entries from the release checksums file if needed.
GITLEAKS_SHA256 = {
    ("linux", "x64"): (
        "gitleaks_8.24.3_linux_x64.tar.gz",
        "9991e0b2903da4c8f6122b5c3186448b927a5da4deef1fe45271c3793f4ee29c",
    ),
    ("linux", "arm64"): (
        "gitleaks_8.24.3_linux_arm64.tar.gz",
        "5f2edbe1f49f7b920f9e06e90759947d3c5dfc16f752fb93aaafc17e9d14cf07",
    ),
    ("darwin", "x64"): (
        "gitleaks_8.24.3_darwin_x64.tar.gz",
        "41c44ae8ad1d6eef57d4526ad0fd67d8129eee9a856f55c2b3b9395fd3d9ec0f",
    ),
    ("darwin", "arm64"): (
        "gitleaks_8.24.3_darwin_arm64.tar.gz",
        "b90f13bb8c90ab72083d9b0c842e39dafb82c0e5c3f872f407366b7a58909013",
    ),
    ("windows", "x64"): (
        "gitleaks_8.24.3_windows_x64.zip",
        "3f1a35578631dbfe633cc5b49e6c906e55ff14a4bfd7336a10fb27fe33b6dcd2",
    ),
}

WORKFLOW_PINS = {
    # Full 40-char upstream commits, verified with git ls-remote against
    # refs/tags/v4 and refs/tags/v6 at the time of writing.
    "checkout": "11d5960a326750d5838078e36cf38b85af677262",  # actions/checkout v4
    "setup-python": "ece7cb06caefa5fff74198d8649806c4678c61a1",  # actions/setup-python v6
    "python-version": "3.12.10",
}

# Trusted scanner policy: the pinned default rules and nothing else. It is
# always written to a directory OUTSIDE the scanned payload and passed via
# --config, so a tracked .gitleaks.toml inside the payload cannot weaken the
# scan (verified precedence for the pinned scanner: --config beats
# GITLEAKS_CONFIG / GITLEAKS_CONFIG_TOML, which beat (source)/.gitleaks.toml).
GITLEAKS_TRUSTED_CONFIG = "[extend]\nuseDefault = true\n"
# Env vars the pinned scanner reads for its config; they are stripped from
# the scanner subprocess so an inherited session cannot replace the policy.
GITLEAKS_CONFIG_ENV = ("GITLEAKS_CONFIG", "GITLEAKS_CONFIG_TOML")


_GIT_SELECTING_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


def _sanitized_env(env=None):
    """Caller environment minus repository-selecting Git variables.

    Repository selection always comes from the explicit cwd: an inherited
    GIT_DIR/WORK_TREE/etc. (e.g. an export run from a Git hook) would
    otherwise redirect reads or writes into the caller's repository.
    """
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    for var in _GIT_SELECTING_ENV:
        full_env.pop(var, None)
    return full_env


def run_git(repo, *args, env=None, input=None):
    return subprocess.run(
        ["git", *map(str, args)],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        input=input,
        env=_sanitized_env(env),
    )


def resolve_ref(ref, repo=ROOT):
    return (
        run_git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
        .stdout.decode()
        .strip()
    )


def git_blob(ref, path, repo=ROOT):
    proc = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_sanitized_env(),
    )
    return proc.stdout if proc.returncode == 0 else None


def _ref_has_path(ref, path, repo=ROOT):
    proc = subprocess.run(
        ["git", "cat-file", "-e", f"{ref}:{path}"],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_sanitized_env(),
    )
    return proc.returncode == 0


def read_ref_snapshot(ref, repo=ROOT):
    """Read a ref into memory: {rel: (bytes, mode)}, gitlinks, symlinks, specials."""
    sha = resolve_ref(ref, repo)
    archive = run_git(repo, "archive", sha).stdout
    files, symlinks, specials = {}, [], []
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            if member.isdir():
                continue
            if member.issym() or member.islnk():
                symlinks.append((member.name, member.linkname))
            elif member.isfile():
                reader = tar.extractfile(member)
                files[member.name] = (reader.read(), member.mode)
            else:
                specials.append(member.name)
    gitlinks = []
    raw = run_git(repo, "ls-tree", "-r", "-z", sha).stdout.decode("utf-8")
    for entry in raw.split("\0"):
        if not entry:
            continue
        meta, path = entry.split("\t", 1)
        if meta.split(" ")[0] == "160000":
            gitlinks.append(path)
    # Defensive: archive placeholders must never become payload.
    for link in gitlinks:
        files.pop(link, None)
        for rel in [rel for rel in files if rel.startswith(link + "/")]:
            del files[rel]
    gitmodules = git_blob(ref, ".gitmodules", repo)
    if gitmodules is None and _ref_has_path(ref, ".gitmodules", repo):
        raise SystemExit(
            f"tracked .gitmodules unreadable in export ref {ref}: failing closed"
        )
    return files, gitlinks, symlinks, specials, gitmodules


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    seen = set()
    for section in ("replacements", "forbidden"):
        for rule in config.get(section, []):
            if rule["id"] in seen:
                raise ValueError(f"duplicate rule id: {rule['id']}")
            seen.add(rule["id"])
            if rule.get("regex"):
                re.compile(rule["pattern"])
    for rule in config.get("replacements", []):
        if "replacement" not in rule:
            raise ValueError(f"replacement rule without replacement: {rule['id']}")
    return config


def load_policy(public_path, private_path, overlay_path=None):
    """Merge the shippable generic policy with the private literal policy.

    Fails closed when the private policy is absent: without it the export
    cannot prove it removed the private literals. An overlay (client sterile
    export) replaces exclude_paths/allowed_binaries, appends replacements,
    and patches named forbidden rules with extra except_paths/allow_exact.
    Overlay stub_files (rel path -> public file content) publish a stub for
    an excluded private original (the client deploy workflow); stubs join the
    payload after excludes and pass the same scans and workflow inventory.
    """
    public = load_config(public_path)
    if not Path(private_path).is_file():
        raise SystemExit(
            f"private export policy missing: {private_path} (refusing to export)"
        )
    private = load_config(private_path)
    if overlay_path is not None:
        overlay = json.loads(Path(overlay_path).read_text(encoding="utf-8"))
    else:
        overlay = {}
    forbidden = [dict(rule) for rule in public.get("forbidden", [])]
    if overlay.get("rule_patches"):
        patches = overlay["rule_patches"]
        seen = set()
        for rule in forbidden:
            patch = patches.get(rule["id"])
            if patch is None:
                continue
            seen.add(rule["id"])
            for key in ("except_paths", "allow_exact"):
                if patch.get(key):
                    rule[key] = list(rule.get(key, [])) + list(patch[key])
        unknown = set(patches) - seen
        if unknown:
            raise ValueError(f"overlay patches unknown rules: {sorted(unknown)}")
    banned_tokens = []
    if overlay_path is not None:
        keep_word_paths = set(overlay.get("banned_token_word_paths", []))
    for entry in private.get("banned_tokens", []):
        if overlay_path is not None and isinstance(entry, dict):
            # Path exemptions were reviewed against workspace files, while the
            # client tree is a different repo where the same path may carry
            # private literals: whole-file exempts and spelling exempts never
            # cross targets, and only overlay-declared word paths carry over.
            entry = dict(entry)
            entry.pop("except_paths", None)
            entry.pop("except_spellings", None)
            entry["except_words"] = [
                w
                for w in entry.get("except_words", []) or []
                if w.get("path") in keep_word_paths
            ]
        banned_tokens.append(entry)
    policy = {
        "replacements": public.get("replacements", [])
        + private.get("replacements", []),
        "drop_sections": private.get("drop_sections", []),
        "banned_tokens": banned_tokens,
        "forbidden": forbidden,
        "exclude_paths": overlay.get("exclude_paths", public.get("exclude_paths", [])),
        # Submodule allowlists are target-specific: the workspace public
        # paths must not mark a client submodule public. An overlay carries
        # its own list (empty unless the target reviews its own).
        "public_submodule_paths": (
            overlay.get("public_submodule_paths", [])
            if overlay_path is not None
            else public.get("public_submodule_paths", [])
        ),
        "allowed_binaries": overlay.get(
            "allowed_binaries", public.get("allowed_binaries", {})
        ),
        "single_commit": overlay.get("single_commit", False),
        "single_commit_message": overlay.get("single_commit_message", ""),
        "stub_files": dict(overlay.get("stub_files", {})),
    }
    if "submodule_note_path" in public:
        policy["submodule_note_path"] = public["submodule_note_path"]
    if "workflow_path" in public:
        policy["workflow_path"] = public["workflow_path"]
    # Targets with their own file inventory keep their own license files: the
    # workspace license applies only with no overlay, or when the overlay
    # redefines it (same suppression rule as the generated workflow/note).
    license_source = public
    if overlay_path is not None:
        license_source = overlay if "license_path" in overlay else None
    if license_source is not None:
        if "license_path" in license_source:
            policy["license_path"] = license_source["license_path"]
        if "license_text" in license_source:
            policy["license_text"] = license_source["license_text"]
    if overlay.get("allowed_workflows") is not None:
        policy["allowed_workflows"] = list(overlay["allowed_workflows"])
        # A target with its own workflow inventory keeps its own workflows:
        # suppress the inherited generated workflow/note unless redefined.
        if "submodule_note_path" not in overlay:
            policy.pop("submodule_note_path", None)
        else:
            policy["submodule_note_path"] = overlay["submodule_note_path"]
        if "workflow_path" not in overlay:
            policy.pop("workflow_path", None)
        else:
            policy["workflow_path"] = overlay["workflow_path"]
    return policy


def path_matches(rel, patterns):
    return any(fnmatch.fnmatchcase(rel, pattern) for pattern in patterns)


def decode_text(data):
    """Split archive bytes into text (rewritable, gateable) or binary passthrough.

    BOM-marked UTF-32/UTF-16 first (UTF-16 counts as binary: the pinned
    scanner reads bytes, so decoded UTF-16 text would bypass secret
    detection), then NUL-containing bytes are binary (this also catches
    unmarked UTF-16, which interleaves NULs around ASCII text; the caller
    allowlists binaries by hash, anything else fails the export), then
    strict UTF-8, then NUL-free bytes as Latin-1 (a 1:1 mapping, exact for
    ASCII patterns). Failed decoding is never a silent bypass.
    """
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return None, None
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return None, None
    if b"\x00" in data:
        return None, None
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    return data.decode("latin-1"), "latin-1"


def apply_text(rel, text, rules):
    counts = {}
    for rule in rules:
        if not path_matches(rel, rule["paths"]):
            continue
        if rule.get("regex"):
            text, n = re.compile(rule["pattern"]).subn(rule["replacement"], text)
        else:
            n = text.count(rule["pattern"])
            if n:
                text = text.replace(rule["pattern"], rule["replacement"])
        if n:
            counts[rule["id"]] = n
    return text, counts


def apply_drop_sections(text, sections, rel):
    for section in sections:
        if not path_matches(rel, section["paths"]):
            continue
        lines = text.split("\n")
        start = next(
            (i for i, line in enumerate(lines) if section["start"] in line), None
        )
        if start is None:
            raise SystemExit(
                f"drop-section start marker missing in {rel}: gate fails closed"
            )
        end = next(
            (
                i
                for i in range(start, len(lines))
                if lines[i].startswith(section["end"])
            ),
            None,
        )
        if end is None:
            raise SystemExit(
                f"drop-section end marker missing in {rel}: gate fails closed"
            )
        lines[start:end] = [section["replacement"]]
        text = "\n".join(lines)
    return text


def scan_text(rel, text, forbidden):
    """Find residual private metadata. Reports (rule, path, line) only, never values.

    Rules may carry an "allow_exact" list of public-vocabulary tokens that the
    pattern would otherwise flag (fail-closed additions go through review).
    """
    findings = []
    for rule in forbidden:
        if not path_matches(rel, rule["paths"]):
            continue
        if any(fnmatch.fnmatchcase(rel, exc) for exc in rule.get("except_paths", [])):
            continue
        allowed = rule.get("allow_exact", [])

        def exempted(match):
            idents = [
                value
                for name, value in match.groupdict().items()
                if name.startswith("tok") and value is not None
            ] or [match.group(0)]
            return any(token == ident for token in allowed for ident in idents)

        if rule.get("regex"):
            pattern = re.compile(rule["pattern"])
            for lineno, line in enumerate(text.split("\n"), 1):
                for match in pattern.finditer(line):
                    if exempted(match):
                        continue
                    key = (rule["id"], rel, lineno)
                    if key not in findings:
                        findings.append(key)
                    break
            if rule.get("multiline"):
                for match in pattern.finditer(text):
                    if exempted(match):
                        continue
                    lineno = text.count("\n", 0, match.start()) + 1
                    if (rule["id"], rel, lineno) not in findings:
                        findings.append((rule["id"], rel, lineno))
        else:
            needle = rule["pattern"]
            for lineno, line in enumerate(text.split("\n"), 1):
                if needle in line and not any(token in line for token in allowed):
                    findings.append((rule["id"], rel, lineno))
    return findings


def _containing_word(text, start, end):
    """Maximal [A-Za-z0-9_]+ run holding the match at [start, end)."""
    i, j = start, end
    while i > 0 and (text[i - 1].isalnum() or text[i - 1] == "_"):
        i -= 1
    while j < len(text) and (text[j].isalnum() or text[j] == "_"):
        j += 1
    return text[i:j]


def scan_literal_tokens(rel, text, banned_tokens):
    """Plaintext gate over the private token inventory. Reports (hint, path, line).

    Tokens are matched case-insensitively (casefold: DNS names and most
    identifiers survive a change of capitalization). Every occurrence is a
    hit, including ones glued to dates or digits in contents and filenames;
    the only exemptions are except_paths (documented functional exceptions),
    except_spellings (a single exact spelling at an exact path: reviewed
    public provenance), and except_words (a reviewed ordinary word that
    merely contains the token, scoped to an exact path — e.g. a generic
    comment adjective — so date-stamped backup names stay rejected).
    """
    findings = []
    folded = text.casefold()
    for entry in banned_tokens:
        token = entry["token"] if isinstance(entry, dict) else entry
        hint = (
            entry.get("hint", "private-token")
            if isinstance(entry, dict)
            else "private-token"
        )
        except_paths = entry.get("except_paths", []) if isinstance(entry, dict) else []
        if any(fnmatch.fnmatchcase(rel, exc) for exc in except_paths):
            continue
        spellings = entry.get("except_spellings", []) if isinstance(entry, dict) else []
        words = entry.get("except_words", []) if isinstance(entry, dict) else []
        needle = token.casefold()
        start = folded.find(needle)
        while start != -1:
            end = start + len(needle)
            if any(
                rel == exc.get("path")
                and _containing_word(text, start, end).casefold()
                == (exc.get("word") or "").casefold()
                for exc in words
            ):
                start = folded.find(needle, start + 1)
                continue
            if any(
                rel == exc.get("path") and text[start:end] == exc.get("spelling")
                for exc in spellings
            ):
                start = folded.find(needle, start + 1)
                continue
            lineno = folded.count("\n", 0, start) + 1
            key = (f"token:{hint}", rel, lineno)
            if key not in findings:
                findings.append(key)
            start = folded.find(needle, start + 1)
    return findings


def submodule_rules(gitmodules, public_paths):
    """Derive private-submodule URL rewrite+gate rules from the source manifest.

    Anything in .gitmodules whose path is not listed as public is private: its
    repository URL must not appear anywhere in the export. Matching is
    case-insensitive and accepts http(s), a missing scheme, and www.
    """
    rewrite, gate = [], []
    if not gitmodules:
        return rewrite, gate
    path, url = None, None
    entries = []
    for line in gitmodules.decode("utf-8", "replace").splitlines():
        head = re.match(r'\s*\[submodule "([^"]+)"\]', line)
        if head:
            if path and url:
                entries.append((path, url))
            path, url = None, None
        elif match := re.match(r"\s*path\s*=\s*(\S+)", line):
            path = match.group(1)
        elif match := re.match(r"\s*url\s*=\s*(\S+)", line):
            url = match.group(1)
    if path and url:
        entries.append((path, url))
    for path, url in entries:
        if path in public_paths:
            continue
        stem = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", url)
        stem = re.sub(r"^www\.", "", stem, flags=re.IGNORECASE)
        stem = stem[:-4] if stem.lower().endswith(".git") else stem
        base = r"(?i)(?:https?://)?(?:www\.)?" + re.escape(stem)
        tail = r"(?:\.git)?(?![A-Za-z0-9_.-])"
        rewrite.append(
            {
                "id": f"x-submodule-url:{stem.rsplit('/', 1)[-1]}",
                "paths": ["**"],
                "regex": True,
                "pattern": base + tail,
                "replacement": "<private-submodule>",
            }
        )
        gate.append(
            {
                "id": "f-submodule-urls",
                "paths": ["**"],
                "regex": True,
                "pattern": base + tail,
            }
        )
    return rewrite, gate


def render_submodule_note(gitlinks, dropped_symlinks):
    lines = [
        "# Submodules in the private workspace",
        "",
        "This public export contains no submodule contents: every gitlink present",
        "in the private tree was dropped, and `.gitmodules` was removed, so no",
        "private repository is referenced here as cloneable.",
        "",
        "Paths that were submodule gitlinks in the private tree:",
        "",
    ]
    lines += [f"- `{path}/`" for path in sorted(gitlinks)]
    if dropped_symlinks:
        lines += ["", "Symlinks dropped because their target left the export:", ""]
        lines += [f"- `{path}`" for path in sorted(dropped_symlinks)]
    lines += [
        "",
        "Jobs and docs that need these checkouts stay in the private repo.",
        "The export only keeps jobs that run on `ubuntu-24.04` without private",
        "submodules or secrets (see `.github/workflows/public-export-check.yml`).",
        "",
    ]
    return "\n".join(lines)


def render_workflow():
    sha = GITLEAKS_SHA256[("linux", "x64")]
    pins = WORKFLOW_PINS
    return f"""name: Public export check
on:
  push:
  pull_request:
permissions:
  contents: read
concurrency:
  group: ${{{{ github.workflow }}}}-${{{{ github.ref }}}}
  cancel-in-progress: ${{{{ github.event_name == 'pull_request' }}}}
jobs:
  check:
    runs-on: ubuntu-24.04
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@{pins["checkout"]} # v4
        with:
          persist-credentials: false
          submodules: false
          fetch-depth: 1
      - uses: actions/setup-python@{pins["setup-python"]} # v6
        with:
          python-version: "{pins["python-version"]}"
      - name: Rewrite engine unit tests
        run: python3 -m unittest discover -s scripts/dev/tests -p "test_public_export*.py" -v
      - name: Public policy re-check (generic gates, no private literals)
        run: python3 -I scripts/public_export.py --check .
      - name: Pinned gitleaks scan (fail-closed)
        run: |
          set -eu
          unset GITLEAKS_CONFIG GITLEAKS_CONFIG_TOML
          url="{GITLEAKS_BASE}/{sha[0]}"
          curl -fsSL -o /tmp/gitleaks-scan.tar.gz "$url"
          echo "{sha[1]}  /tmp/gitleaks-scan.tar.gz" | sha256sum -c -
          rm -rf /tmp/gitleaks-scan && mkdir -p /tmp/gitleaks-scan
          tar -xzf /tmp/gitleaks-scan.tar.gz -C /tmp/gitleaks-scan
          printf '[extend]\\nuseDefault = true\\n' > /tmp/gitleaks-trusted.toml
          rm -rf /tmp/gitleaks-no-ignore && mkdir -p /tmp/gitleaks-no-ignore
          /tmp/gitleaks-scan/gitleaks detect --no-git --source . --config /tmp/gitleaks-trusted.toml --gitleaks-ignore-path /tmp/gitleaks-no-ignore --ignore-gitleaks-allow --redact --exit-code 1
"""


def _inside(root_rel, target):
    """Lexically resolve a symlink target against its directory; None if it escapes."""
    if target.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", target):
        return None
    parent = root_rel.rsplit("/", 1)[0] if "/" in root_rel else ""
    parts = []
    for part in (parent + "/" + target).split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


def build_payload(files, gitlinks, symlinks, specials, gitmodules, policy):
    """Pure transform: archive bytes -> {rel: ("file", bytes, mode) | ("link", target)}."""
    sub_rewrite, sub_gate = submodule_rules(
        gitmodules, policy.get("public_submodule_paths", [])
    )
    rules = policy["replacements"] + sub_rewrite
    forbidden = policy["forbidden"] + sub_gate
    banned_tokens = policy.get("banned_tokens", [])
    excluded = policy["exclude_paths"]
    payload, applied = {}, {}
    for rel in sorted(files):
        if path_matches(rel, excluded):
            continue
        data, mode = files[rel]
        text, encoding = decode_text(data)
        if text is None:
            payload[rel] = ("file", data, mode)
            continue
        scrubbed = False
        if policy.get("drop_sections"):
            dropped = apply_drop_sections(text, policy["drop_sections"], rel)
            scrubbed = dropped != text
            text = dropped
        new_text, counts = apply_text(rel, text, rules)
        if counts or scrubbed:
            payload[rel] = ("file", new_text.encode(encoding), mode)
        else:
            payload[rel] = ("file", data, mode)
        for rule_id, n in counts.items():
            applied[rule_id] = applied.get(rule_id, 0) + n
    # Stubs publish a public replacement for an excluded private original
    # (excludes above already dropped the original); they join the payload
    # before the scans and workflow inventory so both still cover them.
    for rel, content in policy.get("stub_files", {}).items():
        payload[rel] = ("file", content.encode("utf-8"), 0o644)
    # The sterile tree carries the public license verbatim, replacing any
    # source LICENSE before the scans so they still cover the shipped text.
    license_path = policy.get("license_path")
    if license_path is not None:
        license_text = policy.get("license_text")
        if license_text is None:
            raise SystemExit("license path without license text: failing closed")
        payload[license_path] = ("file", license_text.encode("utf-8"), 0o644)
    scan_texts = {}
    for rel, entry in payload.items():
        text, _ = decode_text(entry[1])
        if text is not None:
            scan_texts[rel] = text
    kept_links, dropped_links, dropped_specials = {}, [], []
    for rel, target in sorted(symlinks):
        if path_matches(rel, excluded):
            dropped_links.append(rel)
            continue
        resolved = _inside(rel, target)
        if resolved is None or resolved not in scan_texts:
            dropped_links.append(rel)
            continue
        if scan_text(f"link:{rel}", target, forbidden):
            dropped_links.append(rel)
            continue
        kept_links[rel] = target
    for rel in sorted(specials):
        if not path_matches(rel, excluded):
            dropped_specials.append(rel)
    note_path = policy.get("submodule_note_path")
    workflow_path = policy.get("workflow_path")
    allowed_workflows = policy.get("allowed_workflows")
    if note_path is not None:
        scan_texts[note_path] = render_submodule_note(gitlinks, dropped_links)
    if workflow_path is not None:
        scan_texts[workflow_path] = render_workflow()
    if note_path is not None:
        payload[note_path] = ("file", scan_texts[note_path].encode("utf-8"), 0o644)
    if workflow_path is not None:
        payload[workflow_path] = (
            "file",
            scan_texts[workflow_path].encode("utf-8"),
            0o644,
        )
    for rel, target in kept_links.items():
        payload[rel] = ("link", target)
    expected_workflows = (
        sorted(allowed_workflows)
        if allowed_workflows is not None
        else ([workflow_path] if workflow_path is not None else None)
    )
    workflows = sorted(r for r in payload if r.startswith(".github/workflows/"))
    if expected_workflows is not None and workflows != expected_workflows:
        raise SystemExit(f"unexpected workflow inventory in export: {workflows}")
    findings = scan_payload_paths(payload, forbidden, banned_tokens)
    findings += scan_suppression_names(payload)
    for rel, text in scan_texts.items():
        findings += scan_text(rel, text, forbidden)
        findings += scan_literal_tokens(rel, text, banned_tokens)
    for rel, target in kept_links.items():
        findings += scan_text(rel, target, forbidden)
        findings += scan_literal_tokens(rel, target, banned_tokens)
    if findings:
        raise SystemExit(
            "forbidden metadata remains in export "
            f"({len(findings)} hits, rule path:line, values withheld): "
            + ", ".join(f"{rule} {rel}:{line}" for rule, rel, line in findings[:20])
        )
    return payload, applied, {"symlinks": dropped_links, "specials": dropped_specials}


def scan_suppression_names(payload):
    """Reject tracked scanner-suppression files fail-closed.

    The pinned scanner unconditionally honors <source>/.gitleaksignore on
    top of any explicit --gitleaks-ignore-path (verified against its
    sources), so such a file would silently suppress both this scan and the
    generated public CI scan. Refusing the payload is the only isolation
    that cannot be bypassed from inside the scanned tree.
    """
    return [
        ("scanner-suppression", rel, 0)
        for rel in sorted(payload)
        if rel == ".gitleaksignore" or rel.endswith("/.gitleaksignore")
    ]


def scan_payload_paths(payload, forbidden, banned_tokens):
    """Gate payload PATHS (not just contents) against the metadata policy.

    Path hits are reported with line 0 (content hits always start at line 1).
    """
    findings = []
    for rel in sorted(payload):
        for rule_id, _matched, _line in scan_text(rel, rel, forbidden):
            findings.append((rule_id, rel, 0))
        for hint, _matched, _line in scan_literal_tokens(rel, rel, banned_tokens):
            findings.append((hint, rel, 0))
    return findings


def verify_allowed_binaries(payload, allowed):
    for rel, entry in payload.items():
        if entry[0] != "file":
            continue
        text, _ = decode_text(entry[1])
        if text is not None:
            continue
        digest = hashlib.sha256(entry[1]).hexdigest()
        if allowed.get(rel) != digest:
            raise SystemExit(f"binary file not allowlisted by hash in export: {rel}")


def scan_export_payload(payload, config):
    """Gate an export-shaped payload with the generic public policy.

    Includes the workflow inventory check and the payload-path gate. Private
    literals are enforced at export time, never from shipped data.
    """
    findings = scan_payload_paths(payload, config["forbidden"], [])
    findings += scan_suppression_names(payload)
    for rel, entry in sorted(payload.items()):
        if entry[0] == "link":
            findings += scan_text(rel, entry[1], config["forbidden"])
            continue
        text, _ = decode_text(entry[1])
        if text is None:
            continue
        findings += scan_text(rel, text, config["forbidden"])
    allowed_workflows = config.get("allowed_workflows")
    if allowed_workflows is not None:
        expected = sorted(allowed_workflows)
    elif config.get("workflow_path") is not None:
        expected = [config["workflow_path"]]
    else:
        expected = None
    workflows = sorted(r for r in payload if r.startswith(".github/workflows/"))
    if expected is not None and workflows != expected:
        raise SystemExit(f"unexpected workflow inventory in export: {workflows}")
    return findings


def tree_digest(payload):
    digest = hashlib.sha256()
    for rel in sorted(payload):
        kind = payload[rel][0]
        digest.update(rel.encode("utf-8") + b"\0" + kind.encode() + b"\0")
        body = payload[rel][1]
        digest.update((body.encode("utf-8") if kind == "link" else body) + b"\0")
    return digest.hexdigest()


def write_output(out, payload):
    for rel in sorted(payload):
        entry = payload[rel]
        target = out / rel
        if entry[0] == "link":
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                raise SystemExit(
                    f"refusing to overwrite existing path with symlink: {rel}"
                )
            target.symlink_to(entry[1])
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(entry[1])
        if entry[2] & 0o111:
            target.chmod(0o755)


def commit_tree(root, message, date):
    """Turn an export directory into a single-commit git tree; return the SHA.

    The commit is assembled with plumbing (hash-object --no-filters,
    update-index --index-info, write-tree, commit-tree) so staged bytes equal
    the on-disk payload regardless of core.autocrlf/core.eol, attribute files
    on this machine, or a .gitattributes shipped inside the payload. The repo
    is initialized with a pinned SHA-1 object format so GIT_DEFAULT_HASH (or
    an equivalent local default) cannot change the commit ID. Identity
    and timestamps are pinned to the given source commit date, so the same
    source SHA always yields the same commit id.
    """
    root = Path(root)
    # An explicitly empty template: the caller's template directory (or its
    # configured equivalent) must not copy private hooks/files into the
    # output .git after the payload scans.
    with tempfile.TemporaryDirectory(prefix="export-git-template-") as template:
        run_git(root, "init", "-q", "--object-format=sha1", f"--template={template}")
    rels = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if ".git" not in path.relative_to(root).parts
        and (path.is_file() or path.is_symlink())
    )
    index_lines = []
    for rel in rels:
        target = root / rel
        if target.is_symlink():
            blob = (
                run_git(
                    root,
                    "hash-object",
                    "-t",
                    "blob",
                    "-w",
                    "--stdin",
                    input=os.readlink(target).encode("utf-8"),
                )
                .stdout.decode()
                .strip()
            )
            mode = "120000"
        else:
            blob = (
                run_git(
                    root, "hash-object", "-t", "blob", "-w", "--no-filters", "--", rel
                )
                .stdout.decode()
                .strip()
            )
            mode = "100755" if target.stat().st_mode & 0o111 else "100644"
        # C-quote paths with specials so --index-info parses exactly one
        # entry per line (payload names with spaces hit this in practice).
        if re.search(r'[\s"\\]', rel):
            quoted = (
                '"'
                + rel.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\n", "\\n")
                .replace("\t", "\\t")
                + '"'
            )
        else:
            quoted = rel
        index_lines.append(f"{mode} {blob}\t{quoted}")
    run_git(
        root,
        "update-index",
        "--index-info",
        input="".join(line + "\n" for line in index_lines).encode("utf-8"),
    )
    tree = run_git(root, "write-tree").stdout.decode().strip()
    commit = (
        run_git(
            root,
            "-c",
            "i18n.commitEncoding=UTF-8",
            "commit-tree",
            tree,
            "-m",
            message,
            env={
                "GIT_AUTHOR_NAME": "blockfield-export",
                "GIT_AUTHOR_EMAIL": "export@localhost",
                "GIT_COMMITTER_NAME": "blockfield-export",
                "GIT_COMMITTER_EMAIL": "export@localhost",
                "GIT_AUTHOR_DATE": date,
                "GIT_COMMITTER_DATE": date,
            },
        )
        .stdout.decode()
        .strip()
    )
    run_git(root, "update-ref", "HEAD", commit)
    return commit


def ensure_gitleaks():
    system = {"Linux": "linux", "Darwin": "darwin", "Windows": "windows"}[
        platform.system()
    ]
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}[
        platform.machine().lower()
    ]
    try:
        filename, checksum = GITLEAKS_SHA256[(system, arch)]
    except KeyError:
        raise SystemExit(f"no pinned gitleaks for {system}/{platform.machine()}")
    cached = download(f"gitleaks/{filename}", f"{GITLEAKS_BASE}/{filename}", checksum)
    with tempfile.TemporaryDirectory() as tmp:
        member = Path(tmp) / filename
        member.write_bytes(cached.read_bytes())
        if filename.endswith(".zip"):
            with zipfile.ZipFile(member) as archive:
                names = archive.namelist()
                exe = "gitleaks.exe" if "gitleaks.exe" in names else names[0]
                exe_bytes = archive.read(exe)
        else:
            with tarfile.open(member, "r:gz") as archive:
                names = archive.getnames()
                exe = "gitleaks" if "gitleaks" in names else names[0]
                exe_bytes = archive.extractfile(exe).read()
    binary = (
        cached.parent
        / GITLEAKS_VERSION
        / ("gitleaks.exe" if system == "windows" else "gitleaks")
    )
    if not binary.is_file() or binary.read_bytes() != exe_bytes:
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(exe_bytes)
        if system != "windows":
            binary.chmod(0o755)
    return binary


def export_tree(
    ref,
    out,
    config_path=CONFIG_DEFAULT,
    private_path=PRIVATE_DEFAULT,
    repo=ROOT,
    overlay_path=None,
):
    policy = load_policy(config_path, private_path, overlay_path)
    for pin in ("checkout", "setup-python"):
        if not re.fullmatch(r"[0-9a-f]{40}", WORKFLOW_PINS[pin]):
            raise SystemExit(f"workflow pin is not a full commit SHA: {pin}")
    sha = resolve_ref(ref, repo)
    files, gitlinks, symlinks, specials, gitmodules = read_ref_snapshot(ref, repo)
    payload, applied, dropped = build_payload(
        files, gitlinks, symlinks, specials, gitmodules, policy
    )
    verify_allowed_binaries(payload, policy["allowed_binaries"])
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"refusing to write into non-empty dir: {out}")
    out.mkdir(parents=True, exist_ok=True)
    write_output(out, payload)
    rewritten = sum(
        1
        for rel, entry in payload.items()
        if entry[0] == "file" and rel in files and entry[1] != files[rel][0]
    )
    return {
        "ref": ref,
        "sha": sha,
        "gitlinks_dropped": len(gitlinks),
        "symlinks_dropped": dropped["symlinks"],
        "specials_dropped": dropped["specials"],
        "rewritten_files": rewritten,
        "applied": applied,
        "tree_files": len(payload),
        "tree_digest": tree_digest(payload),
    }


IGNORED_IN_CHECK = (".git", ".cache", "__pycache__")


def read_disk_payload(root):
    """Read a payload from a directory on disk.

    Inside a git work tree the tracked file list is authoritative: files that
    are tracked are inspected even when their names look generated (generated
    paths can be force-tracked), while untracked caches and bytecode are
    naturally excluded. Outside a repository, generated names are skipped by
    pattern instead.
    """
    root = Path(root)
    tracked = None
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        tracked = [p for p in proc.stdout.decode("utf-8").split("\0") if p]
    except (subprocess.CalledProcessError, OSError):
        tracked = None
    payload = {}
    if tracked is not None:
        for rel in sorted(tracked):
            target = root / rel
            if target.is_symlink():
                payload[rel] = ("link", os.readlink(target))
            elif target.is_file():
                payload[rel] = ("file", target.read_bytes(), target.stat().st_mode)
            # else: gitlink dirs etc. carry no payload of their own; skip.
        return payload
    for target in sorted(root.rglob("*")):
        rel = target.relative_to(root).as_posix()
        if (
            rel in IGNORED_IN_CHECK
            or rel.startswith(".git/")
            or rel.startswith(".cache/")
            or "/__pycache__/" in rel
            or rel.endswith((".pyc", ".pyo"))
        ):
            continue
        if not target.is_file() and not target.is_symlink():
            continue
        if target.is_symlink():
            payload[rel] = ("link", os.readlink(target))
        else:
            payload[rel] = ("file", target.read_bytes(), target.stat().st_mode)
    return payload


def check_tree(root, config_path=CONFIG_DEFAULT, overlay_path=None):
    """Public re-check: generic gates only, no private literals anywhere."""
    config = load_config(config_path)
    if overlay_path is not None:
        overlay = json.loads(Path(overlay_path).read_text(encoding="utf-8"))
        forbidden = [dict(rule) for rule in config.get("forbidden", [])]
        for rule in forbidden:
            patch = (overlay.get("rule_patches") or {}).get(rule["id"])
            if not patch:
                continue
            for key in ("except_paths", "allow_exact"):
                if patch.get(key):
                    rule[key] = list(rule.get(key, [])) + list(patch[key])
        config = dict(config)
        config["forbidden"] = forbidden
        if overlay.get("allowed_workflows") is not None:
            config["allowed_workflows"] = list(overlay["allowed_workflows"])
        if overlay.get("allowed_binaries") is not None:
            config["allowed_binaries"] = dict(overlay["allowed_binaries"])
    payload = read_disk_payload(Path(root))
    verify_allowed_binaries(payload, config.get("allowed_binaries", {}))
    findings = scan_export_payload(payload, config)
    if findings:
        raise SystemExit(
            f"public re-check failed ({len(findings)} hits, rule path:line, values withheld): "
            + ", ".join(f"{rule} {rel}:{line}" for rule, rel, line in findings[:20])
        )
    return {"tree_files": len(payload), "tree_digest": tree_digest(payload)}


def run_gitleaks_scan(out, repo):
    """Pinned-scanner gate over an export directory; returns the process result."""
    binary = ensure_gitleaks()
    scan_env = {
        key: value
        for key, value in os.environ.items()
        if key not in GITLEAKS_CONFIG_ENV
    }
    with tempfile.TemporaryDirectory(prefix="gitleaks-trusted-") as trusted:
        trusted_config = Path(trusted) / "gitleaks.toml"
        trusted_config.write_text(GITLEAKS_TRUSTED_CONFIG, encoding="utf-8")
        empty_ignore = Path(trusted) / "no-ignore"
        empty_ignore.mkdir()
        return subprocess.run(
            [
                str(binary),
                "detect",
                "--no-git",
                "--source",
                str(out),
                "--config",
                str(trusted_config),
                "--gitleaks-ignore-path",
                str(empty_ignore),
                "--ignore-gitleaks-allow",
                "--redact",
                "--exit-code",
                "1",
            ],
            cwd=repo,
            env=scan_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )


def report_export(summary):
    print(f"export ref {summary['ref']} = {summary['sha']}")
    print(
        f"files: {summary['tree_files']} in output, "
        f"{summary['rewritten_files']} rewritten, "
        f"{summary['gitlinks_dropped']} submodule gitlinks dropped, "
        f"{len(summary['symlinks_dropped'])} symlinks dropped, "
        f"{len(summary['specials_dropped'])} special entries dropped"
    )
    if summary["symlinks_dropped"]:
        print("dropped symlinks: " + ", ".join(summary["symlinks_dropped"]))
    for rule_id in sorted(summary["applied"]):
        print(f"  {rule_id}: {summary['applied'][rule_id]} replacement(s)")
    print(f"tree sha256: {summary['tree_digest']}")


def fail_gitleaks(out, result):
    print(f"gitleaks exit={result.returncode} (output redacted, values withheld):")
    print(result.stdout[-4000:])
    try:
        shutil.rmtree(out)
        print("removed unscanned export output after gitleaks failure")
    except OSError as error:
        print(f"warning: could not remove unscanned output: {error}")
    raise SystemExit(f"gitleaks gate failed on export (exit {result.returncode})")


def main(argv=None, repo=ROOT):
    if argv is None:
        argv = sys.argv[1:]
    if argv[:1] == ["export"]:
        if len(argv) != 4:
            raise SystemExit("usage: public_export.py export <target> <sha> <outdir>")
        _, name, sha, outdir = argv
        target_repo, config, private, overlay = resolve_target(name)
        out = Path(outdir).resolve()
        summary = export_tree(
            sha,
            out,
            str(config),
            str(private),
            repo=target_repo,
            overlay_path=str(overlay) if overlay is not None else None,
        )
        result = run_gitleaks_scan(out, target_repo)
        report_export(summary)
        if result.returncode != 0:
            fail_gitleaks(out, result)
        print("gitleaks: no leaks found in export")
        policy = load_policy(
            str(config),
            str(private),
            str(overlay) if overlay is not None else None,
        )
        if policy.get("single_commit"):
            date = (
                run_git(target_repo, "show", "-s", "--format=%cI", summary["sha"])
                .stdout.decode()
                .strip()
            )
            head = commit_tree(
                out,
                policy["single_commit_message"].format(sha=summary["sha"]),
                date,
            )
            print(f"single commit: {head}")
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="origin/main")
    parser.add_argument("--out", default=None)
    parser.add_argument("--config", default=str(CONFIG_DEFAULT))
    parser.add_argument("--private", default=str(PRIVATE_DEFAULT))
    parser.add_argument(
        "--check", default=None, help="public re-check of an export tree"
    )
    parser.add_argument(
        "--overlay", default=None, help="client overlay for --check of a client tree"
    )
    args = parser.parse_args(argv)
    if args.check is not None:
        summary = check_tree(Path(args.check), args.config, args.overlay)
        print(
            f"public re-check clean: {summary['tree_files']} files, "
            f"tree sha256: {summary['tree_digest']}"
        )
        return
    if not args.out:
        raise SystemExit("--out is required for export mode")
    out = Path(args.out).resolve()
    summary = export_tree(args.ref, out, args.config, args.private, repo=repo)
    result = run_gitleaks_scan(out, repo)
    report_export(summary)
    if result.returncode != 0:
        fail_gitleaks(out, result)
    print("gitleaks: no leaks found in export")


if __name__ == "__main__":
    main()
