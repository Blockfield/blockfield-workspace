#!/usr/bin/env python3
"""Offline evidence for pack review; never downloads, installs or removes mods."""

import argparse
from collections import Counter, defaultdict
import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import struct
import sys
import tomllib
import zipfile
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parent))
from modmap import load_mapping, owners  # noqa: E402
from tools.nbt_find import rd  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
BUILTINS = {"minecraft", "java", "fabricloader"}
REFERENCE_PATTERNS = {
    "porting_lib_attributes": (
        b"io/github/fabricators_of_create/porting_lib/attributes/",
        *(
            f"porting_lib:{name}".encode()
            for name in (
                "swim_speed",
                "entity_gravity",
                "step_height_addition",
                "reach_distance",
                "attack_range",
            )
        ),
    ),
    "porting_lib_brewing": (b"io/github/fabricators_of_create/porting_lib/brewing/",),
    "porting_lib_chunk_loading": (
        b"io/github/fabricators_of_create/porting_lib/chunk/loading/",
        b"porting_lib_chunk_loading",
    ),
}
ROLES = {
    "porting_lib_attributes": "attribute registration and gameplay mixins; removal candidate needs runtime checks",
    "keybindfixplus": "client keybind fix; server metadata dependency",
    "playeranimator": "client animation; server metadata dependency",
    "fusion": "client connected textures; server metadata dependency",
    "yet_another_config_lib_v3": "client configuration UI; server metadata dependency",
    "porting_lib_gametest": "development tests and common mixins; required by Porting Lib Core",
    "spark": "profiler; installation alone is not an optimization",
    "dynamic_fps": "background client resource conservation",
    "sodium": "client rendering",
    "immediatelyfast": "client rendering",
    "entityculling": "client visibility culling",
    "iris": "client shaders",
    "lithium": "game logic optimization; benefit requires workload measurement",
    "ferritecore": "memory optimization",
    "modernfix": "startup and resource optimization",
    "krypton": "network optimization",
    "fabricproxy-lite": "proxy authentication and forwarding",
    "crossstitch": "proxy protocol compatibility",
    "skinrestorer": "player skins",
    "raknetify": "network transport",
    "voicechat": "voice communication",
}
# Repos that consume mod content at runtime; a mod's own repo is not evidence of use.
CONSUMERS = (
    "server/server",
    "server/tools",
    "server/scripts",
    "localmods/blockfield-mod/common",
    "localmods/blockfield-mod/server",
    "localmods/blockfield-mod/client",
)
SKIP_DIRS = {
    ".git",
    ".gradle",
    "build",
    "out",
    "run",
    "files",
    "sources",
    "node_modules",
}
TEXT = {
    ".java",
    ".kt",
    ".json",
    ".json5",
    ".toml",
    ".mcfunction",
    ".properties",
    ".snbt",
    ".py",
    ".yml",
}
# Mod ids/namespaces as whole words: IDs, Java packages, mixin paths, config file names.
WORD = re.compile(rb"[a-z][a-z0-9_-]{2,}")
SHARED_NAMESPACES = BUILTINS | {"c", "fabric", "forge", "neoforge", "common"}
IDENTIFIER = re.compile(r"(?<![a-z0-9_.-])([a-z0-9_.-]+):[a-z0-9_]")
CONTENT_NAMESPACES = {
    "blockfield": (),
    "blockfield_decor": ("blockfield",),
    "create": ("create", "railways"),
}


def digest(path, algorithm):
    if algorithm not in {"sha512", "sha256", "sha1", "md5"}:
        raise ValueError(f"Unsupported pinned hash: {algorithm}")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def referenced_targets(data):
    normalized = data.replace(b".", b"/")
    return [
        target
        for target, patterns in REFERENCE_PATTERNS.items()
        if any(pattern in normalized for pattern in patterns)
    ]


def inspect_jar(data, origin, depth=0):
    if depth > 8:
        raise ValueError("Embedded JAR nesting exceeds eight levels")
    rows, references = [], defaultdict(list)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if "fabric.mod.json" not in archive.namelist():
            return rows, references
        meta = json.loads(archive.read("fabric.mod.json"))
        row = {
            "id": meta["id"],
            "version": meta["version"],
            "origin": origin,
            "environment": meta.get("environment", "*"),
            "provides": meta.get("provides", []),
            "depends": meta.get("depends", {}),
            "entrypoints": meta.get("entrypoints", {}),
            "mixins": [],
            # assets/<ns>/ and data/<ns>/ are what a mod can register or ship.
            "namespaces": sorted(
                {
                    parts[1]
                    for parts in (name.split("/") for name in archive.namelist())
                    if len(parts) > 2 and parts[0] in {"assets", "data"}
                }
                - SHARED_NAMESPACES
            ),
        }
        for declaration in meta.get("mixins", []):
            name = (
                declaration if isinstance(declaration, str) else declaration["config"]
            )
            config = json.loads(archive.read(name))
            row["mixins"].append(
                {
                    "config": name,
                    "environment": "*"
                    if isinstance(declaration, str)
                    else declaration.get("environment", "*"),
                    "common": len(config.get("mixins", [])),
                    "client": len(config.get("client", [])),
                    "server": len(config.get("server", [])),
                    "plugin": config.get("plugin"),
                }
            )
        rows.append(row)
        for name in archive.namelist():
            if not name.endswith((".class", ".json", ".toml", ".properties")):
                continue
            for target in referenced_targets(archive.read(name)):
                if row["id"] != target:
                    references[target].append(
                        {"mod": row["id"], "origin": origin, "member": name}
                    )
        for declaration in meta.get("jars", []):
            name = declaration["file"]
            nested, hits = inspect_jar(
                archive.read(name), origin + "!" + name, depth + 1
            )
            rows.extend(nested)
            for target, entries in hits.items():
                references[target].extend(entries)
    return rows, references


def source_references(root):
    hits = defaultdict(list)
    for directory, children, files in os.walk(root / "localmods"):
        children[:] = sorted(
            name
            for name in children
            if name not in {".git", ".gradle", "build", "out", "files"}
        )
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix not in {".java", ".kt", ".kts"}:
                continue
            relative = path.relative_to(root).as_posix()
            for line, data in enumerate(path.read_bytes().splitlines(), 1):
                for target in referenced_targets(data):
                    if "/attributes/" not in relative:
                        hits[target].append({"file": relative, "line": line})
    return hits


def consumer_references(root):
    hits = defaultdict(set)
    for consumer in CONSUMERS:
        for directory, children, files in os.walk(root / consumer):
            children[:] = sorted(name for name in children if name not in SKIP_DIRS)
            for name in sorted(files):
                path = Path(directory) / name
                if path.suffix not in TEXT or path.stat().st_size > 4 << 20:
                    continue
                relative = path.relative_to(root).as_posix()
                words = set(WORD.findall(path.read_bytes() + b" " + relative.encode()))
                for word in words:
                    hits[word.decode()].add(relative)
    return hits


def palette_counts(states):
    palette = states.get("palette", [])
    if not palette:
        return Counter()
    if len(palette) == 1:
        return Counter({palette[0]["Name"]: 4096})
    bits = max(4, (len(palette) - 1).bit_length())
    per_long, mask = 64 // bits, (1 << bits) - 1
    packed = states["data"]
    if len(packed) != (4096 + per_long - 1) // per_long:
        raise ValueError("Unexpected 1.21 block-state packing")
    counts = Counter()
    for index in range(4096):
        selected = (packed[index // per_long] >> (index % per_long * bits)) & mask
        counts[palette[selected]["Name"]] += 1
    return counts


def nbt_namespaces(chunk):
    pending = [chunk]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value)
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, str):
            yield from (
                namespace
                for namespace in IDENTIFIER.findall(value)
                if namespace != "minecraft"
            )


def world_usage(path, lock):
    seed = path.read_bytes()
    if hashlib.sha256(seed).hexdigest() != lock["sha256"]:
        raise ValueError("World seed does not match current assets.lock.json")
    blocks, regions, chunks = Counter(), 0, 0
    worlds, references = defaultdict(Counter), Counter()
    with zipfile.ZipFile(io.BytesIO(seed)) as archive:
        for name in sorted(archive.namelist()):
            folder = name.split("/")[-2] if "/" in name else ""
            if not name.endswith(".mca") or folder not in {"region", "entities"}:
                continue
            regions += 1
            data = archive.read(name)
            if not data:
                continue
            if len(data) < 8192:
                raise ValueError(f"Truncated region: {name}")
            for index in range(1024):
                offset = int.from_bytes(data[index * 4 : index * 4 + 3], "big") * 4096
                if not offset:
                    continue
                length = struct.unpack_from(">I", data, offset)[0]
                compression = data[offset + 4]
                if compression & 128:
                    raise ValueError(
                        f"External chunks require a separate audit: {name}"
                    )
                payload = data[offset + 5 : offset + 4 + length]
                if compression == 1:
                    payload = gzip.decompress(payload)
                elif compression == 2:
                    payload = zlib.decompress(payload)
                elif compression != 3:
                    raise ValueError(f"Unsupported chunk compression: {compression}")
                if payload[0] != 10:
                    raise ValueError(f"Expected compound NBT: {name}")
                chunks += 1
                header = 3 + struct.unpack_from(">H", payload, 1)[0]
                chunk = rd(payload, header, 10)[0]
                # Entities, block entities and items also name mod content; displays live in entities/.
                # Parse string tags so u16 lengths and numeric arrays cannot become namespaces.
                references.update(nbt_namespaces(chunk))
                if folder != "region":
                    continue
                for section in chunk.get("sections", []):
                    states = section.get("block_states", {})
                    if not any(
                        not entry["Name"].startswith("minecraft:")
                        for entry in states.get("palette", [])
                    ):
                        continue
                    placed = {
                        key: value
                        for key, value in palette_counts(states).items()
                        if not key.startswith("minecraft:")
                    }
                    blocks.update(placed)
                    for key, value in placed.items():
                        world = (
                            name.rsplit("/region/", 1)[0]
                            if "/region/" in name
                            else "<root>"
                        )
                        worlds[world][key.split(":", 1)[0]] += value
    namespaces = Counter()
    for name, count in blocks.items():
        namespaces[name.split(":", 1)[0]] += count
    return {
        "sha256": lock["sha256"],
        "regions": regions,
        "chunks": chunks,
        "placed_blocks_by_namespace": dict(sorted(namespaces.items())),
        "placed_blocks_by_world": {
            name: dict(sorted(counts.items()))
            for name, counts in sorted(worlds.items())
        },
        "nbt_references_by_namespace": dict(sorted(references.items())),
        "scope": "Supplied pinned archives only; live worlds and Workshop drafts are not included",
    }


def merge_usage(reports):
    merged = {
        "archives": [r["sha256"] for r in reports],
        "regions": sum(r["regions"] for r in reports),
        "chunks": sum(r["chunks"] for r in reports),
        "placed_blocks_by_world": {},
    }
    for key in ("placed_blocks_by_namespace", "nbt_references_by_namespace"):
        total = Counter()
        for report in reports:
            total.update(report[key])
        merged[key] = dict(sorted(total.items()))
    for report in reports:
        merged["placed_blocks_by_world"].update(report["placed_blocks_by_world"])
    merged["scope"] = reports[0]["scope"]
    return merged


def audit(root, caches, seed=None, maps=()):
    files = defaultdict(list)
    for directory in caches:
        for path in sorted(directory.rglob("*.jar")):
            files[path.name].append(path)
    source_hits = source_references(root)
    consumers = consumer_references(root)
    archives = []
    if seed:
        archives.append(
            world_usage(
                seed,
                json.loads((root / "server/distribution/assets.lock.json").read_text()),
            )
        )
    if maps:
        pinned = json.loads((root / "server/distribution/maps.lock.json").read_text())
        known = {entry["sha256"] for entry in pinned.values()}
        for path in maps:
            checksum = digest(path, "sha256")
            if checksum not in known:
                raise ValueError(f"{path.name} is not pinned in maps.lock.json")
            archives.append(world_usage(path, {"sha256": checksum}))
    usage = merge_usage(archives) if archives else None
    mapping = owners(load_mapping(root / "localmods/mods.json"))
    pins, mods, versions = [], [], {}
    inspected, hash_cache = {}, {}
    for side in ("client", "server"):
        versions[side] = tomllib.loads((root / side / "pack.toml").read_text())[
            "version"
        ]
        side_rows, references = [], defaultdict(list)
        for path in sorted((root / side / "mods").glob("*.pw.toml")):
            pin = tomllib.loads(path.read_text())
            download = pin["download"]
            candidates = files[pin["filename"]]
            match = None
            for candidate in candidates:
                key = (candidate, download["hash-format"])
                if key not in hash_cache:
                    hash_cache[key] = digest(*key)
                if hash_cache[key] == download["hash"]:
                    match = candidate
                    break
            record = {
                "side": side,
                "pin": path.name,
                "filename": pin["filename"],
                "owner": mapping.get(path.name),
                "status": "verified"
                if match
                else "hash-mismatch"
                if candidates
                else "unverified",
            }
            pins.append(record)
            if match is None:
                continue
            if match not in inspected:
                data = match.read_bytes()
                if (
                    hashlib.new(download["hash-format"], data).hexdigest()
                    != download["hash"]
                ):
                    record["status"] = "hash-mismatch"
                    continue
                inspected[match] = inspect_jar(data, pin["filename"])
            rows, hits = inspected[match]
            for row in copy.deepcopy(rows):
                row.update(side=side, pin=path.name)
                side_rows.append(row)
            for target, entries in hits.items():
                references[target].extend(entries)
        providers = defaultdict(set)
        for row in side_rows:
            if row["environment"] not in {"*", side}:
                continue
            for provided in [row["id"], *row["provides"]]:
                providers[provided].add(row["id"])
        required_by = defaultdict(set)
        for row in side_rows:
            if row["environment"] not in {"*", side}:
                continue
            for dependency in row["depends"]:
                for provider in providers.get(dependency, ()):
                    required_by[provider].add(row["id"])
        for row in side_rows:
            # data/create/ inside another mod is compat content owned by Create.
            row["namespaces"] = [
                namespace
                for namespace in row["namespaces"]
                if namespace == row["id"] or namespace not in providers
            ]
        for row in side_rows:
            mod_id = row["id"]
            active = row["environment"] in {"*", side}
            row.update(
                active_on_side=active,
                hard_required_by=sorted(required_by[mod_id]),
                dependencies_without_verified_provider=sorted(
                    set(row["depends"]) - providers.keys() - BUILTINS
                )
                if active
                else [],
                binary_references=references[mod_id],
                source_references=source_hits[mod_id],
                role=ROLES.get(
                    mod_id, "not classified; inspect feature/configuration use"
                ),
                runtime_benefit="not measured",
            )
            row["placed_map_blocks"] = (
                None
                if usage is None
                else sum(
                    usage["placed_blocks_by_namespace"].get(namespace, 0)
                    for namespace in CONTENT_NAMESPACES.get(mod_id, (mod_id,))
                )
            )
            row["map_nbt_references"] = (
                None
                if usage is None
                else sum(
                    usage["nbt_references_by_namespace"].get(namespace, 0)
                    for namespace in row["namespaces"]
                )
            )
            row["consumer_references"] = sorted(
                {f for word in {mod_id, *row["namespaces"]} for f in consumers[word]}
            )[:8]
            row["assessment"] = (
                "required-by-metadata"
                if row["hard_required_by"]
                else "content-used"
                if row["placed_map_blocks"] or row["map_nbt_references"]
                else "consumer-found"
                if row["consumer_references"]
                else "reference-found"
                if row["binary_references"] or row["source_references"]
                else "review-candidate"
                if mod_id == "porting_lib_attributes"
                else "requires-runtime-evidence"
            )
        mods.extend(side_rows)
    candidate_evidence = {}
    for side in versions:
        unverified = sorted(
            pin["pin"]
            for pin in pins
            if pin["side"] == side and pin["status"] != "verified"
        )
        candidate_evidence[side] = {
            "complete": not unverified,
            "unverified_pins": unverified,
        }
    # An unread consumer can require any verified library on its side.
    # Runtime A/B removal tests only: none of these is approved for removal.
    candidates = [
        {
            "side": row["side"],
            "id": row["id"],
            "pin": row["pin"],
            "registers_namespaces": row["namespaces"],
            "why_runtime_needed": "behavior/mixins only; no offline consumer can exist"
            if not row["namespaces"]
            else "ships its namespace; no consumer in supplied evidence",
        }
        for row in mods
        if row["origin"].count("!") == 0
        and candidate_evidence[row["side"]]["complete"]
        and row["active_on_side"]
        and row["assessment"] in {"requires-runtime-evidence", "review-candidate"}
    ]
    return {
        "schema_version": 1,
        "runtime_ab_candidates": candidates,
        "candidate_evidence_by_side": candidate_evidence,
        "pack_versions": versions,
        "pins": pins,
        "mods": mods,
        "world_seed": usage,
        "limits": [
            "Missing or mismatched cached JARs are unverified, never removable by this report.",
            "Runtime A/B candidates are suppressed per side until every pin is verified; an unread consumer can require a verified library.",
            "Dependencies include embedded Fabric JARs and provides aliases; version selection is not a Fabric loader simulation.",
            "Source/binary references cover only three Porting Lib review targets; dynamic/reflection/resource references are not proven absent.",
            "Hard requirements can be startup-only; absence of requirements does not prove a mod useless.",
            "Map evidence covers supplied pinned archives (placed blocks plus namespaced IDs in region/entities NBT), not live worlds or Workshop drafts.",
            "Consumer references are whole-word mod ids/namespaces in server and blockfield-mod sources and paths; common words can over-match (keeps mods), dynamic IDs are not proven absent.",
            "No mod is approved for removal and no performance gain is inferred offline.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--cache",
        type=Path,
        action="append",
        help="Read-only JAR cache; repeatable (default: .dev)",
    )
    parser.add_argument(
        "--map",
        type=Path,
        action="append",
        default=[],
        help="Cached maps.lock overlay ZIP; repeatable, hash must be pinned",
    )
    parser.add_argument(
        "--world-seed",
        type=Path,
        help="Optional cached seed ZIP; must match current assets lock",
    )
    args = parser.parse_args()
    try:
        report = audit(
            args.root, args.cache or [args.root / ".dev"], args.world_seed, args.map
        )
    except (
        OSError,
        ValueError,
        KeyError,
        zipfile.BadZipFile,
        struct.error,
        IndexError,
        zlib.error,
    ) as error:
        parser.exit(1, f"Audit failed: {error}\n")
    print(json.dumps(report, indent=2) + "\n", end="")


if __name__ == "__main__":
    main()
