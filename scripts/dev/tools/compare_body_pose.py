#!/usr/bin/env python3
"""Independent emitted-quad / server-OBB measurements; stdlib only, metres."""

import argparse
import itertools
import json
import math
import re
import statistics
from pathlib import Path
from uuid import UUID

PARTS = ("HEAD", "TORSO", "LEFT_ARM", "RIGHT_ARM", "LEFT_LEG", "RIGHT_LEG")
LABELS = (*PARTS, "MISS")
EPS = 1e-7
BOX_EPS = 2e-5  # Native float vertices at world positions have rounding noise.
MAX_VERTICES = 4096  # v2 oracle: many boxes per part; bound is per part emission.
PX = 1 / 16  # Model pixels to metres.


def add(a, b):
    return tuple(x + y for x, y in zip(a, b))


def sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def mul(a, k):
    return tuple(x * k for x in a)


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def norm(a):
    return math.sqrt(dot(a, a))


def unit(a):
    length = norm(a)
    if length < EPS:
        raise ValueError("degenerate edge")
    return mul(a, 1 / length)


def vector(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("expected 3-vector")
    if any(type(x) not in (int, float) or not math.isfinite(x) for x in value):
        raise ValueError("nonfinite vector")
    return tuple(value)


def integer(value, field):
    if type(value) is not int or value < 0:
        raise ValueError(f"invalid {field}")
    return value


def corners(box):
    return [
        add(
            box["center"],
            tuple(
                sum(
                    (1 if mask & (1 << i) else -1) * box["half"][i] * box["axes"][i][j]
                    for i in range(3)
                )
                for j in range(3)
            ),
        )
        for mask in range(8)
    ]


def validate_box(box):
    # Single loader for every consumer: a uniform model scale may live inside the
    # axes (candidate v2 world boxes carry 0.9375). Fold it into half, keep unit
    # axes; reject only non-orthogonal or non-uniformly scaled frames.
    center = vector(box["center"])
    axes = tuple(vector(a) for a in box["axes"])
    half = vector(box["half"])
    if len(axes) != 3 or min(half) <= 0:
        raise ValueError("invalid axes/extents")
    scales = tuple(norm(a) for a in axes)
    if min(scales) <= 0:
        raise ValueError("degenerate axes")
    if max(scales) - min(scales) > 1e-5:
        raise ValueError("nonuniformly scaled axes")
    units = tuple(mul(a, 1 / s) for a, s in zip(axes, scales))
    if any(abs(dot(units[i], units[j])) > 1e-5 for i in range(3) for j in range(i)):
        raise ValueError("nonorthogonal axes")
    return {
        "center": center,
        "axes": units,
        "half": tuple(h * s for h, s in zip(half, scales)),
    }


def finite_record(value):
    if isinstance(value, dict):
        for item in value.values():
            finite_record(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            finite_record(item)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("nonfinite record number")


def identity(record):
    finite_record(record)
    uuid = record["targetUuid"]
    if str(UUID(uuid)) != uuid:
        raise ValueError("noncanonical UUID")
    if not re.fullmatch(r"[a-z0-9_.-]+:[a-z0-9_./-]+", record["dimension"]):
        raise ValueError("invalid dimension")
    integer(record["sampleUnixMs"], "sampleUnixMs")
    if type(record["sampleMonoNs"]) is not int:
        raise ValueError("invalid monotonic sample (origin is process-local)")
    return uuid, record["dimension"]


def validate_server(record):
    if record["schema"] == "blockfield.bodypose.v2":
        return validate_server_v2(record)
    if record["schema"] != "blockfield.bodypose.v1":
        raise ValueError("unknown server schema")
    identity(record)
    for key in ("serverTick", "gameTime", "epoch", "resourceEpoch"):
        integer(record[key], key)
    if record["samplePoint"] != "end_server_tick" or record["partialTick"] != 0:
        raise ValueError("not a committed tick endpoint")
    vector(record["feet"])
    for key in ("fixed", "bounds"):
        values = record[key]
        if (
            len(values) != 6
            or any(type(x) not in (int, float) or not math.isfinite(x) for x in values)
            or any(values[i] > values[i + 3] for i in range(3))
        ):
            raise ValueError("invalid " + key)
    if (
        record["skinModel"] not in ("wide", "slim")
        or type(record["skinKnown"]) is not bool
    ):
        raise ValueError("invalid skin metadata")
    if (
        type(record["entityScale"]) not in (int, float)
        or not math.isfinite(record["entityScale"])
        or record["entityScale"] <= 0
    ):
        raise ValueError("invalid entity scale")
    for key in ("skinObbs", "damageObbs"):
        if [b["part"] for b in record[key]] != list(PARTS):
            raise ValueError("six ordered body parts required")
        record[key] = [dict(validate_box(b), part=b["part"]) for b in record[key]]
    if any(
        p[j] < record["bounds"][j] - BOX_EPS or p[j] > record["bounds"][j + 3] + BOX_EPS
        for b in record["damageObbs"]
        for p in corners(b)
        for j in range(3)
    ):
        raise ValueError("damage geometry outside declared bounds")
    return record


def oracle_part(part):
    """Recover only verified boxes from actual face topology, never fit a mesh."""
    if part["shape"] not in ("box", "quads"):
        raise ValueError("unknown oracle shape")
    vertices = [vector(p) for p in part["vertices"]]
    if not vertices or len(vertices) % 4 or len(vertices) > MAX_VERTICES:
        raise ValueError("incomplete/oversized quads")
    faces = [vertices[i : i + 4] for i in range(0, len(vertices), 4)]
    result = {"faces": faces, "vertices": vertices, "box": None}
    if len(vertices) == 24:
        try:
            result.update(native_cube(vertices))
            return result
        except ValueError:
            pass
    if part["shape"] != "box":
        if part["shape"] != "quads":
            raise ValueError("unknown oracle shape")
        return result
    unique, counts, topology = [], [], []
    for face in faces:
        indices = []
        for p in face:
            found = next(
                (i for i, q in enumerate(unique) if norm(sub(p, q)) <= BOX_EPS), None
            )
            if found is None:
                found = len(unique)
                unique.append(p)
                counts.append(0)
            counts[found] += 1
            indices.append(found)
        e, f = sub(face[1], face[0]), sub(face[3], face[0])
        if (
            min(norm(e), norm(f)) <= BOX_EPS
            or abs(dot(unit(e), unit(f))) > 1e-4
            or norm(sub(add(face[0], add(e, f)), face[2])) > BOX_EPS
        ):
            raise ValueError("nonrectangular face advertised as box")
        topology.append(indices)
    if (
        len(faces) != 6
        or len(unique) != 8
        or counts != [3] * 8
        or len({tuple(sorted(f)) for f in topology}) != 6
        or any(len(set(f)) != 4 for f in topology)
    ):
        raise ValueError("nonclosed box topology")
    edges = set()
    neighbors = set()
    for face in topology:
        for i, a in enumerate(face):
            b = face[(i + 1) % 4]
            edges.add(tuple(sorted((a, b))))
            if a == 0:
                neighbors.add(b)
            if b == 0:
                neighbors.add(a)
    if len(edges) != 12 or len(neighbors) != 3:
        raise ValueError("invalid box edges")
    vectors = [sub(unique[i], unique[0]) for i in sorted(neighbors)]
    box = validate_box(
        {
            "center": mul(tuple(sum(p[j] for p in unique) for j in range(3)), 1 / 8),
            "axes": [unit(e) for e in vectors],
            "half": [norm(e) / 2 for e in vectors],
        }
    )
    if any(min(norm(sub(p, q)) for q in unique) > BOX_EPS for p in corners(box)):
        raise ValueError("bent box")
    result["box"] = box
    result["corners"] = unique
    return result


def validate_observer(record):
    if record["schema"] == "blockfield.playergeometry.v2":
        return validate_receipt_v2(record)
    if record["schema"] != "blockfield.playergeometry.v1":
        raise ValueError("unknown oracle schema")
    identity(record)
    if str(UUID(record["observerUuid"])) != record["observerUuid"]:
        raise ValueError("invalid observer UUID")
    for key in ("tick", "frameSequence", "targetTickCount", "observerTick"):
        integer(record[key], key)
    if not 0 <= record["partialTick"] <= 1 or not math.isfinite(record["partialTick"]):
        raise ValueError("invalid partial tick")
    if record["capturePath"] != "native_emitted_quads":
        raise ValueError("not native geometry")
    vector(record["renderPosition"])
    if set(record["parts"]) != {p.lower() for p in PARTS}:
        raise ValueError("incomplete body")
    record["geometry"] = {p: oracle_part(record["parts"][p.lower()]) for p in PARTS}
    return record


def summary(values):
    if not values:
        return {"count": 0}
    values = sorted(values)

    def percentile(p):
        k = (len(values) - 1) * p
        return values[int(k)] * (1 - k % 1) + values[math.ceil(k)] * (k % 1)

    return {
        "count": len(values),
        "p50": percentile(0.5),
        "p95": percentile(0.95),
        "max": values[-1],
        "rms": math.sqrt(statistics.mean(x * x for x in values)),
    }


def assignment(a, b):
    """Exact minimum squared-distance bijection (8 vertices, bitmask DP)."""
    states = {0: (0, [])}
    for p in a:
        following = {}
        for mask, (cost, errors) in states.items():
            for j, q in enumerate(b):
                if mask & (1 << j):
                    continue
                distance = norm(sub(p, q))
                new = mask | (1 << j)
                candidate = (cost + distance**2, errors + [distance])
                if new not in following or candidate[0] < following[new][0]:
                    following[new] = candidate
        states = following
    return states[(1 << len(b)) - 1][1]


def metrics(server, observed, server_anchor=(0, 0, 0), observer_anchor=(0, 0, 0)):
    if observed["box"] is None:
        points = observed["vertices"]
        center = tuple(statistics.mean(p[j] for p in points) for j in range(3))
        return {
            "failure": "non_box_oracle",
            "centerVertexSummary": norm(
                sub(sub(server["center"], server_anchor), sub(center, observer_anchor))
            ),
            "containment": "ambiguous",
        }
    a = [sub(p, server_anchor) for p in corners(server)]
    b = [sub(p, observer_anchor) for p in observed["corners"]]
    oracle = observed["box"]
    permutations = list(itertools.permutations(range(3)))

    def axis_cost(order):
        angles = [
            math.degrees(
                math.acos(min(1, abs(dot(server["axes"][i], oracle["axes"][j]))))
            )
            for i, j in enumerate(order)
        ]
        return (
            round(
                sum(
                    abs(server["half"][i] - oracle["half"][j])
                    for i, j in enumerate(order)
                )
                / BOX_EPS
            ),
            sum(angles),
        )

    order = min(permutations, key=axis_cost)
    return {
        "center": norm(
            sub(
                sub(server["center"], server_anchor),
                sub(oracle["center"], observer_anchor),
            )
        ),
        "corners": summary(assignment(a, b)),
        "hausdorff": max(
            max(min(norm(sub(p, q)) for q in b) for p in a),
            max(min(norm(sub(q, p)) for p in a) for q in b),
        ),
        "axisDegrees": [
            math.degrees(
                math.acos(min(1, abs(dot(server["axes"][i], oracle["axes"][j]))))
            )
            for i, j in enumerate(order)
        ],
        "extentDifference": [
            2 * (server["half"][i] - oracle["half"][j]) for i, j in enumerate(order)
        ],
        "axisAmbiguous": any(
            abs(oracle["half"][i] - oracle["half"][j]) < BOX_EPS
            for i in range(3)
            for j in range(i)
        ),
    }


def slab_interval(start, end, box):
    cx, cy, cz = box["center"]
    ox, oy, oz = start[0] - cx, start[1] - cy, start[2] - cz
    dx, dy, dz = end[0] - start[0], end[1] - start[1], end[2] - start[2]
    low, high = 0.0, 1.0
    for (ax, ay, az), half in zip(box["axes"], box["half"]):
        origin = ox * ax + oy * ay + oz * az
        delta = dx * ax + dy * ay + dz * az
        if abs(delta) < 1e-12:
            if abs(origin) > half + EPS:
                return None
        else:
            a, b = (-half - origin) / delta, (half - origin) / delta
            low, high = max(low, min(a, b)), min(high, max(a, b))
            if low > high + 1e-12:
                return None
    return (max(0.0, min(1.0, low)), max(0.0, min(1.0, high)))


def slab(start, end, box):
    hit = slab_interval(start, end, box)
    return None if hit is None else hit[0]


def triangle(start, end, a, b, c):
    direction, ab, ac = sub(end, start), sub(b, a), sub(c, a)
    p = cross(direction, ac)
    determinant = dot(ab, p)
    if abs(determinant) < 1e-12:
        return None
    relative = sub(start, a)
    u = dot(relative, p) / determinant
    q = cross(relative, ab)
    v = dot(direction, q) / determinant
    t = dot(ac, q) / determinant
    if u >= -1e-10 and v >= -1e-10 and u + v <= 1 + 1e-10 and -1e-10 <= t <= 1 + 1e-10:
        return max(0.0, min(1.0, t))
    return None


def inside(start, geometry):
    if geometry["box"] is None:
        return None
    center = geometry["box"]["center"]
    for a, b, _, d in geometry["faces"]:
        normal = unit(cross(sub(b, a), sub(d, a)))
        if dot(normal, sub(center, a)) > 0:
            normal = mul(normal, -1)
        if dot(normal, sub(start, a)) > EPS:
            return False
    return True


def quad_ray(start, end, geometry):
    if inside(start, geometry):
        return 0.0
    hits = []
    for a, b, c, d in geometry["faces"]:
        for vertices in ((a, b, c), (a, c, d)):
            hit = triangle(start, end, *vertices)
            if hit is not None:
                hits.append(hit)
    return min(hits) if hits else None


def label(start, end, body, oracle=False):
    hits = []
    length = norm(sub(end, start))
    for i, part in enumerate(PARTS):
        hit = quad_ray(start, end, body[part]) if oracle else slab(start, end, body[i])
        if hit is not None:
            hits.append((hit * length, i))
    if not hits:
        return "MISS", None, False, False
    depth = min(t for t, _ in hits)
    tied = [i for t, i in hits if t - depth <= EPS]
    part = PARTS[min(tied)]
    point = add(start, mul(sub(end, start), depth / length if length else 0))
    box = body[part]["box"] if oracle else body[min(tied)]
    graze = False
    if box:
        distances = [
            abs(abs(dot(sub(point, box["center"]), a)) - h)
            for a, h in zip(box["axes"], box["half"])
        ]
        graze = sum(d < 0.001 for d in distances) >= 2
        # A nearly parallel entry face is also a grazing observation.
        graze |= any(
            d < 0.001 and length and abs(dot(unit(sub(end, start)), a)) < 0.01
            for d, a in zip(distances, box["axes"])
        )
    return part, depth, len(tied) > 1, bool(graze)


def grid(server, geometry, spacing=0.025, cameras=()):
    points = [p for box in server for p in corners(box)] + [
        p for g in geometry.values() for p in g["vertices"]
    ]
    lower = tuple(min(p[j] for p in points) - 0.1 for j in range(3))
    upper = tuple(max(p[j] for p in points) + 0.1 for j in range(3))
    center = mul(add(lower, upper), 0.5)
    radius = norm(sub(upper, lower)) / 2 + 1
    directions = [
        tuple(s if j == i else 0 for j in range(3)) for i in range(3) for s in (-1, 1)
    ]
    directions += [
        (
            math.cos(math.radians(e)) * math.cos(k * math.pi / 4),
            math.sin(math.radians(e)),
            math.cos(math.radians(e)) * math.sin(k * math.pi / 4),
        )
        for k in range(8)
        for e in (-30, 0, 30)
    ]
    specifications = []
    for index, direction in enumerate(directions):
        u = unit(cross(direction, (0, 1, 0) if abs(direction[1]) < 0.9 else (1, 0, 0)))
        v = cross(direction, u)
        bounds = list(itertools.product(*zip(lower, upper)))
        ranges = [
            (
                min(dot(sub(p, center), axis) for p in bounds),
                max(dot(sub(p, center), axis) for p in bounds),
            )
            for axis in (u, v)
        ]
        eyes = [None]
        if index in (0, 1, 4, 5):
            eyes += [sub(center, mul(direction, distance)) for distance in (5, 20)]
        if index == 0:
            eyes += [vector(c) for c in cameras]
        for eye in eyes:
            spec = {
                "index": len(specifications),
                "direction": direction,
                "spacing": spacing,
                "ranges": ranges,
                "eye": eye,
                "seed": 0,
                "bounds": [lower, upper],
            }
            specifications.append(spec)
            for i in range(math.ceil((ranges[0][1] - ranges[0][0]) / spacing) + 1):
                for j in range(math.ceil((ranges[1][1] - ranges[1][0]) / spacing) + 1):
                    point = add(
                        center,
                        add(
                            mul(u, ranges[0][0] + i * spacing),
                            mul(v, ranges[1][0] + j * spacing),
                        ),
                    )
                    if eye is None:
                        start, end = (
                            sub(point, mul(direction, radius)),
                            add(point, mul(direction, radius)),
                        )
                    else:
                        ray = unit(sub(point, eye))
                        start, end = eye, add(point, mul(ray, radius))
                    yield spec, start, end


def box_edges(box):
    """Twelve world-space edge segments of an OBB."""
    points = corners(box)
    return [
        (points[a], points[b])
        for a, b in (
            (0, 1),
            (0, 2),
            (0, 4),
            (1, 3),
            (1, 5),
            (2, 3),
            (2, 6),
            (3, 7),
            (4, 5),
            (4, 6),
            (5, 7),
            (6, 7),
        )
    ]


def point_segment_distance(point, seg_a, seg_b):
    axis = sub(seg_b, seg_a)
    length2 = dot(axis, axis)
    if length2 < 1e-18:
        return norm(sub(point, seg_a))
    t = min(1.0, max(0.0, dot(sub(point, seg_a), axis) / length2))
    return norm(sub(point, add(seg_a, mul(axis, t))))


def union_edge_distance(point, boxes):
    """Minimum distance from a point to any edge of the union of box faces."""
    best = math.inf
    for box in boxes:
        for seg_a, seg_b in box_edges(box):
            distance = point_segment_distance(point, seg_a, seg_b)
            if distance < best:
                best = distance
    return best


def ray_report(
    skin, damage, geometry, spacing, cameras=(), *, edge_exclude=None, excluded=()
):
    """Label rays on a grid. Compound-mode gates use edge_exclude=0.001 (§9.1(3)):

    rays whose hit point is within edge_exclude of any face edge or seam of the
    union of oracle and server faces are counted as seamExcluded, not as
    disagreements. Rays first blocked by excluded geometry are dropped+listed.
    """
    reports = {
        key: {
            "allGrid": 0,
            "unionHit": 0,
            "disagreements": 0,
            "gatedDisagreements": 0,
            "seamExcluded": 0,
            "droppedExcluded": 0,
            "dropped": [],
            "seamDisagreements": 0,
            "grazeDisagreements": 0,
            "falseHits": 0,
            "falseMisses": 0,
            "headTorso": 0,
            "leftRight": 0,
            "confusion": {a: dict.fromkeys(LABELS, 0) for a in LABELS},
            "mismatches": [],
            "depths": [],
        }
        for key in ("skin", "damage")
    }
    union_boxes = None
    if edge_exclude is not None:
        union_boxes = list(skin) + list(damage)
        for part in PARTS:
            oracle = geometry[part]
            if oracle["box"] is not None:
                union_boxes.append(oracle["box"])
    specs = {}
    for spec, start, end in grid(damage, geometry, spacing, cameras):
        specs[spec["index"]] = spec
        observed, depth, seam, graze = label(start, end, geometry, True)
        excluded_entry = None
        for box in excluded:
            hit = slab(start, end, box)
            if hit is not None and (
                excluded_entry is None or hit * norm(sub(end, start)) < excluded_entry
            ):
                excluded_entry = hit * norm(sub(end, start))
        for key, body in (("skin", skin), ("damage", damage)):
            predicted, entry, server_seam, server_graze = label(start, end, body)
            report = reports[key]
            report["allGrid"] += 1
            report["unionHit"] += predicted != "MISS" or observed != "MISS"
            first_entry = None
            for candidate in (entry, depth):
                if candidate is not None and (
                    first_entry is None or candidate < first_entry
                ):
                    first_entry = candidate
            if (
                excluded_entry is not None
                and first_entry is not None
                and excluded_entry < first_entry - 1e-9
            ):
                report["droppedExcluded"] += 1
                report["dropped"].append(
                    {"grid": spec["index"], "start": start, "end": end}
                )
                continue
            report["confusion"][predicted][observed] += 1
            if entry is not None and depth is not None:
                report["depths"].append(abs(entry - depth))
            if predicted == observed:
                continue
            report["disagreements"] += 1
            if edge_exclude is not None and first_entry is not None:
                point = add(start, mul(unit(sub(end, start)), first_entry))
                if union_edge_distance(point, union_boxes) < edge_exclude:
                    report["seamExcluded"] += 1
                    continue
            report["gatedDisagreements"] += 1
            report["seamDisagreements"] += seam or server_seam
            report["grazeDisagreements"] += graze or server_graze
            report["falseHits"] += observed == "MISS"
            report["falseMisses"] += predicted == "MISS"
            report["headTorso"] += {predicted, observed} == {"HEAD", "TORSO"}
            report["leftRight"] += (
                predicted.removeprefix("LEFT_") == observed.removeprefix("RIGHT_")
                and predicted.startswith("LEFT_")
                or predicted.removeprefix("RIGHT_") == observed.removeprefix("LEFT_")
                and predicted.startswith("RIGHT_")
            )
            report["mismatches"].append(
                {
                    "grid": spec["index"],
                    "start": start,
                    "end": end,
                    "server": predicted,
                    "observer": observed,
                    "entryDifference": None
                    if entry is None or depth is None
                    else entry - depth,
                    "seam": seam or server_seam,
                    "graze": graze or server_graze,
                }
            )
    for report in reports.values():
        report["worstRay"] = max(
            report["mismatches"],
            key=lambda r: abs(r["entryDifference"] or 0),
            default=None,
        )
        report["entryDepth"] = summary(report.pop("depths"))
        report["allGridRate"] = (
            report["disagreements"] / report["allGrid"] if report["allGrid"] else None
        )
        report["unionHitRate"] = (
            report["disagreements"] / report["unionHit"] if report["unionHit"] else None
        )
    return {"grids": list(specs.values()), **reports}


# Compound mode (§3.4, §8 U4): id normalization, bone registration, gap harness.
# Normalization runs on the receipt only; server dumps pass through untouched.
SKIN_OUTER_NAMES = (
    "hat",
    "jacket",
    "left_sleeve",
    "right_sleeve",
    "left_pants",
    "right_pants",
)
NORMALIZED_ID = re.compile(
    r"^(skin:(inner|hat|jacket|left_sleeve|right_sleeve|left_pants|right_pants)"
    r"|uniform:[a-z_]+/.+|armor:[a-z0-9_]+/.+|curio:[a-z0-9_]+/.+)$"
)
EXCLUDED_LAYERS = {
    "CapeLayer",
    "ElytraLayer",
    "ArrowLayer",
    "BeeStingerLayer",
    "CustomHeadLayer",
    "ParrotOnShoulderLayer",
    "Deadmau5EarsLayer",
    "ItemInHandLayer",
    "PlayerItemInHandLayer",
    "StuckInBodyLayer",
    "ParachuteRenderer",
    "NameTagRenderer",
    "ParticleRenderer",
}
# Runtime receipts carry the intermediary simple name (layer:class_972 for
# CapeLayer) because the game runs obfuscated. Verified against the 1.21.1 yarn
# mappings (c net/minecraft/class_972 .../CapeLayer etc.): every entry below is
# the intermediary name of the named layer above it, in the same order.
EXCLUDED_INTERMEDIARY = frozenset(
    {
        "class_972",  # CapeLayer
        "class_979",  # ElytraLayer
        "class_973",  # ArrowLayer
        "class_4506",  # BeeStingerLayer
        "class_976",  # CustomHeadLayer
        "class_983",  # ParrotOnShoulderLayer
        "class_978",  # Deadmau5EarsLayer
        "class_989",  # ItemInHandLayer
        "class_5697",  # PlayerItemInHandLayer
        "class_4507",  # StuckInBodyLayer
    }
)
# §9.1(3) seam/graze tolerances: inter-part contact within 1 mm, chord < 1 mm
# or entry-face |cos| < 0.05 is a dropped tooling ray, never a disagreement.
SEAM_TOLERANCE = 0.001
GRAZE_CHORD = 0.001
GRAZE_COS = 0.05
# Vanilla inner cubes in model pixels, bone-local (spec §3.2).
VANILLA_INNER_PX = {
    "HEAD": (-4, -8, -4, 8, 8, 8),
    "TORSO": (-4, 0, -2, 8, 12, 4),
    "LEFT_ARM": (-1, -2, -2, 4, 12, 4),
    "RIGHT_ARM": (-3, -2, -2, 4, 12, 4),
    "LEFT_LEG": (-2, 0, -2, 4, 12, 4),
    "RIGHT_LEG": (-2, 0, -2, 4, 12, 4),
}
VANILLA_INNER_SLIM_PX = {
    **VANILLA_INNER_PX,
    "LEFT_ARM": (-1, -2, -2, 3, 12, 4),
    "RIGHT_ARM": (-2, -2, -2, 3, 12, 4),
}
VANILLA_DEFORMATION_PX = {
    "HEAD": 0.5,
    "TORSO": 0.25,
    "LEFT_ARM": 0.25,
    "RIGHT_ARM": 0.25,
    "LEFT_LEG": 0.25,
    "RIGHT_LEG": 0.25,
}
GAP_CLEARANCE = 0.03  # gap rays clear every oracle box by >= 3 cm
GAP_CATEGORIES = (
    "frontal_phantom",
    "side_chest_air",
    "arm_torso_front_oblique",
    "arm_torso_side",
    "above_helmet",
    "behind_pack",
)
RAY_SET_VERSION = 1


def vanilla_inner_box(part, slim=False):
    """Hard-coded vanilla inner cube as an OBB in bone-local metres."""
    table = VANILLA_INNER_SLIM_PX if slim else VANILLA_INNER_PX
    x, y, z, w, h, d = table[part]
    return {
        "center": ((x + w / 2) * PX, (y + h / 2) * PX, (z + d / 2) * PX),
        "half": (w / 2 * PX, h / 2 * PX, d / 2 * PX),
        "axes": ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
    }


def is_excluded_box(source, raw_id):
    # Ownership identifies vanilla physics cape; uniform child names do not.
    # Runtime sources use intermediary simple names (class_972), so both match.
    owner = source.removeprefix("layer:").split(":", 1)[0].rsplit(".", 1)[-1]
    return owner in EXCLUDED_LAYERS or owner in EXCLUDED_INTERMEDIARY


def normalize_box_id(raw_id, source, bone):
    """Map one receipt box to the §3.4 id scheme. Receipt only."""
    text = str(raw_id)
    if NORMALIZED_ID.fullmatch(text):
        return text
    low_source, low_id = source.lower(), text.lower()
    if low_source == "base" or low_source.startswith("base:"):
        local = low_source.partition(":")[2] or low_id
        # The testbot registers the bone root under its bone name (head, torso,
        # left_arm, ...); that run holds the skin inner cube, i.e. skin:inner.
        if local in ("", "inner", f"{bone.lower()}_inner", bone.lower()):
            return "skin:inner"
        if local in SKIN_OUTER_NAMES:
            return "skin:" + local
        return "skin:" + local.replace(" ", "_")
    if "parachute" in low_source:
        return f"curio:back/{bone.lower()}/{text}"
    if "thermal" in low_source or "goggles" in low_source:
        return f"curio:head/{bone.lower()}/{text}"
    if "armor" in low_source:
        slot = "unknown"
        for candidate in ("head", "chest", "legs", "feet"):
            if candidate in low_source:
                slot = candidate
        return f"armor:{slot}/{bone.lower()}/{text}"
    if "accessories" in low_source or "uniform" in low_id + low_source:
        # The testbot registers the full child path (torso/vest_front); the §3.4
        # id keeps the bone once (uniform:body/vest_front). Bare leaf ids pass
        # through unchanged. Bone segments follow the shared UniformGeometry
        # root names (spec §3.1), which the server dump uses: TORSO is "body".
        path = text
        prefix = bone.lower() + "/"
        if path.lower().startswith(prefix):
            path = path[len(prefix) :]
        segment = "body" if bone == "TORSO" else bone.lower()
        return f"uniform:{segment}/{path}"
    return f"layer:{bone.lower()}/{text}"


def normalize_receipt_v2(receipt):
    """Normalize a v2 receipt's box ids; list non-whitelisted unattributed ids."""
    normalized, mapping, unattributed = {}, [], []
    for part in PARTS:
        normalized[part] = []
        for entry in receipt["parts"][part.lower()]["boxes"]:
            source = entry.get("source", "base")
            if is_excluded_box(source, entry["id"]):
                continue
            new_id = normalize_box_id(entry["id"], source, part)
            normalized[part].append(new_id)
            mapping.append({"from": entry["id"], "to": new_id})
            if not NORMALIZED_ID.fullmatch(new_id):
                unattributed.append(new_id)
    unattributed.extend(
        entry.get("source", "unknown")
        for entry in receipt.get("unattributed", [])
        if not is_excluded_box(entry.get("source", "unknown"), "")
    )
    return {"ids": normalized, "mapping": mapping, "unattributed": unattributed}


def validate_receipt_v2(record):
    if record["schema"] != "blockfield.playergeometry.v2":
        raise ValueError("unknown oracle schema")
    identity(record)
    if record.get("capturePath") != "native_emitted_quads":
        raise ValueError("not native geometry")
    if str(UUID(record["observerUuid"])) != record["observerUuid"]:
        raise ValueError("invalid observer UUID")
    for key in ("tick", "frameSequence", "targetTickCount", "observerTick"):
        integer(record[key], key)
    if not 0 <= record["partialTick"] <= 1:
        raise ValueError("invalid partial tick")
    vector(record["renderPosition"])
    if record["skinModel"] not in ("wide", "default", "slim"):
        raise ValueError("invalid skin metadata")
    if set(record["parts"]) != {p.lower() for p in PARTS}:
        raise ValueError("incomplete body")
    record["geometry"] = {}
    for part in PARTS:
        boxes = record["parts"][part.lower()]["boxes"]
        if not boxes or sum(len(b["vertices"]) for b in boxes) > MAX_VERTICES:
            raise ValueError("empty/oversized box list for " + part)
        record["geometry"][part] = [oracle_part(box) for box in boxes]
    return record


# Native ModelPart.Cube emission: signed local corners and six ordered faces.
NATIVE_SIGNS = (
    (1, -1, 1),
    (-1, -1, 1),
    (-1, -1, -1),
    (1, -1, -1),
    (1, 1, -1),
    (-1, 1, -1),
    (-1, 1, 1),
    (1, 1, 1),
)
NATIVE_FACES = (
    (0, 1, 2, 3),
    (4, 5, 6, 7),
    (2, 1, 6, 5),
    (3, 2, 5, 4),
    (0, 3, 4, 7),
    (1, 0, 7, 6),
)
NATIVE_EPS = 0.0001


def native_corners(vertices):
    vertices = [vector(p) for p in vertices]
    if len(vertices) != 24:
        raise ValueError("native cube needs six quads")
    points = vertices[:8]
    if any(
        norm(sub(p, points[i])) > NATIVE_EPS
        for p, i in zip(vertices, itertools.chain.from_iterable(NATIVE_FACES))
    ):
        raise ValueError("non-native cube topology/order")
    if min(norm(sub(p, q)) for i, p in enumerate(points) for q in points[:i]) <= 1e-5:
        raise ValueError("degenerate native cube")
    return points


def signed_axes(points):
    center = oracle_unique8_center(points)
    axes = tuple(
        tuple(
            sum((p[j] - center[j]) * sign[k] for p, sign in zip(points, NATIVE_SIGNS))
            / 8
            for j in range(3)
        )
        for k in range(3)
    )
    if any(
        norm(
            sub(
                p,
                add(
                    center,
                    tuple(
                        sum(sign[k] * axes[k][j] for k in range(3)) for j in range(3)
                    ),
                ),
            )
        )
        > NATIVE_EPS
        for p, sign in zip(points, NATIVE_SIGNS)
    ):
        raise ValueError("bent native cube")
    return center, axes


def inverse_axes(axes):
    determinant = dot(axes[0], cross(axes[1], axes[2]))
    if abs(determinant) < 1e-15:
        raise ValueError("singular bone frame")
    return tuple(
        mul(cross(axes[i], axes[j]), 1 / determinant)
        for i, j in ((1, 2), (2, 0), (0, 1))
    )


def native_cube(vertices):
    points = native_corners(vertices)
    center, axes = signed_axes(points)
    # Polar rotation removes float noise on thin gear; extent contains every vertex.
    scale = max(norm(a) for a in axes)
    rotation = tuple(mul(a, 1 / scale) for a in axes)
    for _ in range(16):
        inverse = inverse_axes(rotation)
        rotation = tuple(mul(add(a, b), 0.5) for a, b in zip(rotation, inverse))
    half = tuple(max(abs(dot(sub(p, center), a)) for p in points) for a in rotation)
    box = validate_box(dict(center=center, axes=rotation, half=half))
    if max(min(norm(sub(p, q)) for q in points) for p in corners(box)) > NATIVE_EPS:
        raise ValueError("native cube reconstruction exceeds 0.1 mm")
    return {"box": box, "corners": points, "native": True}


def register_bone(vertices, part, slim=False, vertex_order="native"):
    """Signed world←bone affine frame from native inner-skin quads + vanilla only."""
    if vertex_order != "native":
        raise ValueError("unsupported inner cube vertex order")
    points = native_corners(vertices)
    center, half_axes = signed_axes(points)
    vanilla = vanilla_inner_box(part, slim)
    axes = tuple(mul(a, 1 / h) for a, h in zip(half_axes, vanilla["half"]))
    scales = [norm(a) for a in axes]
    if (
        min(scales) <= 0
        or max(scales) - min(scales) > 0.0002
        or any(
            abs(dot(unit(axes[i]), unit(axes[j]))) > 0.0002
            for i in range(3)
            for j in range(i)
        )
    ):
        raise ValueError("inner cube does not match vanilla proportions")
    translation = sub(
        center,
        tuple(
            sum(vanilla["center"][k] * axes[k][j] for k in range(3)) for j in range(3)
        ),
    )
    return {
        "rotation": axes,
        "translation": translation,
        "permuted": False,
        "label": "native_order",
    }


def transform_box(box, registration):
    axes, translation = registration["rotation"], registration["translation"]
    transform = lambda p: tuple(
        sum(p[k] * axes[k][j] for k in range(3)) for j in range(3)
    )
    transformed = tuple(transform(a) for a in box["axes"])
    return {
        "center": add(transform(box["center"]), translation),
        "axes": tuple(unit(a) for a in transformed),
        "half": tuple(h * norm(a) for h, a in zip(box["half"], transformed)),
    }


def bone_registered_errors(local_box, oracle_unique8, registration):
    rebound = transform_box(local_box, registration)
    return {
        "corner": summary(assignment(corners(rebound), oracle_unique8)),
        "center": norm(sub(rebound["center"], oracle_unique8_center(oracle_unique8))),
    }


def oracle_unique8_center(unique8):
    return tuple(sum(p[j] for p in unique8) / 8 for j in range(3))


def point_to_box_distance(point, box):
    local = tuple(dot(sub(point, box["center"]), axis) for axis in box["axes"])
    return norm(tuple(max(0.0, abs(x) - h) for x, h in zip(local, box["half"])))


def segment_clearance(start, end, box):
    """Exact segment-to-OBB distance: quadratic between the six face crossings."""
    if slab(start, end, box) is not None:
        return 0.0
    origin = tuple(dot(sub(start, box["center"]), a) for a in box["axes"])
    direction = tuple(dot(sub(end, start), a) for a in box["axes"])
    half = box["half"]
    cuts = sorted(
        {
            0.0,
            1.0,
            *(
                min(1.0, max(0.0, (sign * h - o) / d))
                for o, d, h in zip(origin, direction, half)
                if abs(d) > 1e-15
                for sign in (-1, 1)
            ),
        }
    )
    best = math.inf
    for low, high in zip(cuts, cuts[1:]):
        mid = (low + high) / 2
        active = [
            (d, o - math.copysign(h, o + mid * d))
            for o, d, h in zip(origin, direction, half)
            if abs(o + mid * d) > h
        ]
        aa = sum(d * d for d, _ in active)
        t = min(high, max(low, -sum(d * o for d, o in active) / aa)) if aa else mid
        distance = sum(
            max(0.0, abs(o + t * d) - h) ** 2
            for o, d, h in zip(origin, direction, half)
        )
        best = min(best, distance)
    return math.sqrt(best)


def ray_enters_box(start, end, box):
    hit = slab(start, end, box)
    return None if hit is None else hit * norm(sub(end, start))


def inflated_off_box(fixed):
    """Gate-OFF baseline box: fixed.inflate(0.17,0,0.17).expandTowards(0,0.2,0)."""
    return {
        "center": (
            (fixed[0] + fixed[3]) / 2,
            (fixed[1] + fixed[4]) / 2 + 0.1,
            (fixed[2] + fixed[5]) / 2,
        ),
        "half": (
            (fixed[3] - fixed[0]) / 2 + 0.17,
            (fixed[4] - fixed[1]) / 2 + 0.1,
            (fixed[5] - fixed[2]) / 2 + 0.17,
        ),
        "axes": ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
    }


def bone_local(point, registration):
    return tuple(
        dot(sub(point, registration["translation"]), a)
        for a in (
            registration["inverse"]
            if "inverse" in registration
            else inverse_axes(registration["rotation"])
        )
    )


def compound_clearance(start, end, boxes):
    best = math.inf
    for box in boxes:
        best = min(best, segment_clearance(start, end, box))
        if best == 0:
            break
    return best


def gap_regions(boxes, bones):
    bones = {
        p: dict(bones[p], inverse=inverse_axes(bones[p]["rotation"])) for p in PARTS
    }
    bones["limits"] = (
        min(
            bone_local(v, bones["HEAD"])[1]
            for b in boxes
            if b["part"] == "HEAD"
            for v in corners(b)
        ),
        max(
            bone_local(v, bones["TORSO"])[2]
            for b in boxes
            if b["part"] == "TORSO"
            for v in corners(b)
        ),
    )
    return bones


def categorize_gap_ray(hit, start, end, oracle_boxes, bones):
    """Exclusive anatomical regions pinned by spec r1.3 / analysis §5."""
    p, head = bone_local(hit, bones["TORSO"]), bone_local(hit, bones["HEAD"])
    direction = sub(bone_local(end, bones["TORSO"]), bone_local(start, bones["TORSO"]))
    head_top, back = bones["limits"]
    if head[1] < head_top - 0.005 and abs(head[0]) <= 0.4 and abs(head[2]) <= 0.4:
        return "above_helmet"
    chest = 0 <= p[1] <= 0.75 and abs(p[0]) <= 0.45
    if not chest:
        return "other"
    if p[2] > back + 0.005:
        return "behind_pack"
    near = {
        part: min(
            (point_to_box_distance(hit, b) for b in oracle_boxes if b["part"] == part),
            default=math.inf,
        )
        for part in ("TORSO", "LEFT_ARM", "RIGHT_ARM")
    }
    side = abs(direction[0]) >= abs(direction[2])
    if near["TORSO"] <= 0.12 and min(near["LEFT_ARM"], near["RIGHT_ARM"]) <= 0.12:
        return "arm_torso_side" if side else "arm_torso_front_oblique"
    if side:
        return "side_chest_air"
    return "frontal_phantom" if p[2] < -0.125 else "other"


def legacy_off_label(hit, fixed, eye_height):
    y = hit[1] - fixed[1]
    if eye_height - 0.25 < y < eye_height + 0.25:
        return "HEAD"
    return "LEFT_LEG" if y < (fixed[4] - fixed[1]) * 0.33 else "TORSO"


def prepare_entries(entries):
    """Broad phase by owning part; native faces remain the intersection oracle."""
    groups = {}
    for entry in entries:
        geometry = entry.get("parsed")
        box = geometry["box"] if geometry else entry.get("box", entry)
        points = geometry["vertices"] if geometry else corners(box)
        groups.setdefault(entry["part"], []).extend(points)
        if geometry and box:
            entry["broad"] = dict(box, half=tuple(h + 0.00015 for h in box["half"]))
    for entry in entries:
        points = groups[entry["part"]]
        low = tuple(min(p[j] for p in points) - 0.00015 for j in range(3))
        high = tuple(max(p[j] for p in points) + 0.00015 for j in range(3))
        entry["partBound"] = {
            "center": mul(add(low, high), 0.5),
            "half": mul(sub(high, low), 0.5),
            "axes": ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
        }
    return entries


def first_entry(start, end, entries, oracle=False):
    """Return owning part, distance in metres and entry; ties use metres too."""
    length = norm(sub(end, start))
    best, chosen = math.inf, None
    part_hit = {}
    for entry in entries:
        part = entry["part"]
        if "partBound" in entry:
            if part not in part_hit:
                part_hit[part] = slab(start, end, entry["partBound"]) is not None
            if not part_hit[part]:
                continue
        geometry = entry.get("parsed")
        box = geometry["box"] if geometry else entry.get("box", entry)
        if box is not None:
            broad = entry.get("broad", box) if oracle else box
            if slab(start, end, broad) is None:
                continue
        t = (
            quad_ray(start, end, geometry)
            if oracle and geometry
            else slab(start, end, box)
        )
        if t is None:
            continue
        distance = t * length
        index = PARTS.index(entry["part"]) if entry["part"] in PARTS else len(PARTS)
        previous = (
            PARTS.index(chosen["part"])
            if chosen and chosen["part"] in PARTS
            else len(PARTS)
        )
        if distance < best - EPS or abs(distance - best) <= EPS and index < previous:
            best, chosen = distance, entry
    return (chosen["part"], best, chosen) if chosen else ("MISS", None, None)


def select_gap_rays(
    oracle_boxes,
    baseline_boxes,
    bones,
    spacing=0.025,
    clearance=GAP_CLEARANCE,
    per_category=6,
    *,
    baseline="on",
    fixed=None,
    eye_height=None,
    fireable_eyes=(),
):
    """One baseline and its own oracle; complete deterministic grid, no prefix cap."""
    prepare_entries(baseline_boxes)
    bones = gap_regions(oracle_boxes, bones)
    pseudo = {
        "ORACLE": {
            "vertices": [v for b in oracle_boxes for v in b.get("vertices", corners(b))]
        }
    }
    census = {
        c: {"count": 0, "baselineHit": 0, "largestClearanceM": None}
        for c in (*GAP_CATEGORIES, "other")
    }
    rays, seen, hits = [], 0, 0
    by_category = {c: [] for c in GAP_CATEGORIES}
    for _, start, end in grid(baseline_boxes, pseudo, spacing):
        seen += 1
        part, depth, _ = first_entry(start, end, baseline_boxes)
        if depth is None:
            continue
        hits += 1
        hit = add(start, mul(unit(sub(end, start)), depth))
        category = categorize_gap_ray(hit, start, end, oracle_boxes, bones)
        row = census[category]
        row["baselineHit"] += 1
        clear = compound_clearance(start, end, oracle_boxes)
        row["largestClearanceM"] = max(row["largestClearanceM"] or 0.0, clear)
        if clear < clearance:
            continue
        row["count"] += 1
        if category == "other" or len(by_category[category]) >= per_category:
            continue
        ray = {
            "id": f"gap-{baseline}-{seen}",
            "category": category,
            "start": list(start),
            "end": list(end),
            "oracle_clearance": clear,
            "baseline": baseline,
            "verifiedHP": False,
            "requiresBeforeDamageValidation": True,
        }
        if fireable_eyes:
            accepted = False
            for eye in fireable_eyes:
                aim = end
                fire_part, fire_depth, _ = first_entry(eye, aim, baseline_boxes)
                if fire_depth is None:
                    continue
                fire_clear = compound_clearance(eye, aim, oracle_boxes)
                fire_hit = add(eye, mul(unit(sub(aim, eye)), fire_depth))
                if (
                    fire_clear < clearance + NATIVE_EPS
                    or categorize_gap_ray(fire_hit, eye, aim, oracle_boxes, bones)
                    != category
                ):
                    continue
                ray.update(eye=list(eye), aim=list(aim), fireOracleClearance=fire_clear)
                hit, part, accepted = fire_hit, fire_part, True
                break
            if not accepted:
                continue
        if baseline == "off":
            if fixed is None or eye_height is None:
                raise ValueError(
                    "OFF attribution needs fixed bounds and actual eye height"
                )
            part = legacy_off_label(hit, fixed, eye_height)
        ray["expectedPart"] = part
        by_category[category].append(ray)
    for c in GAP_CATEGORIES:
        row = census[c]
        row["status"] = (
            "N/A"
            if row["count"] == 0
            else "ready"
            if len(by_category[c]) >= per_category
            else "insufficient"
        )
        row["selected"] = len(by_category[c])
        rays.extend(by_category[c])
    return {
        "rays": rays,
        "scanned": seen,
        "baselineHit": hits,
        "census": census,
        "by_category": {c: row["count"] for c, row in census.items()},
        "ok": all(census[c]["status"] in ("N/A", "ready") for c in GAP_CATEGORIES),
    }


def control_rays_from_receipt(norm_boxes, per_part=6, *, fireable_eyes=(), excluded=()):
    """Freeze only scene-verified eyes whose whole-compound first entry is intended."""
    compound = prepare_entries(
        [dict(entry, part=p) for p in PARTS for entry in norm_boxes[p]]
    )
    rays = []
    for part in PARTS:
        ordered = sorted(norm_boxes[part], key=lambda e: e["id"].startswith("skin:"))
        count = 0
        for entry in ordered:
            box = entry["box"]
            for axis, sign in itertools.product(range(3), (-1, 1)):
                point = add(
                    box["center"], mul(box["axes"][axis], sign * box["half"][axis])
                )
                for eye in fireable_eyes:
                    if norm(sub(point, eye)) < EPS:
                        continue
                    direction = unit(sub(point, eye))
                    # Aim beyond the face, so rounding cannot turn an endpoint into a miss.
                    aim = add(point, mul(direction, 0.001))
                    owning, depth, first = first_entry(eye, aim, compound, True)
                    _, blocked, _ = first_entry(eye, aim, excluded, True)
                    if (
                        owning != part
                        or first["id"] != entry["id"]
                        or blocked is not None
                        and blocked < depth - EPS
                    ):
                        continue
                    rays.append(
                        {
                            "id": f"control-{part.lower()}-{count}",
                            "category": "control",
                            "part": part,
                            "expectedPart": part,
                            "gear": entry["id"],
                            "eye": list(eye),
                            "aim": list(aim),
                            "start": list(eye),
                            "end": list(aim),
                            "firstEntryVerified": True,
                            "verifiedHP": False,
                        }
                    )
                    count += 1
                    break
                if count >= per_part:
                    break
            if count >= per_part:
                break
    return rays


def report_candidate_clearance(rays, candidate_boxes):
    """Measured-only clearance of candidate boxes along frozen rays."""
    for ray in rays:
        start, end = (
            tuple(ray.get("eye", ray["start"])),
            tuple(ray.get("aim", ray["end"])),
        )
        ray["candidate_clearance"] = min(
            (segment_clearance(start, end, b) for b in candidate_boxes),
            default=math.inf,
        )
        ray["candidate_hit"] = any(
            slab(start, end, b) is not None for b in candidate_boxes
        )
    return rays


def freeze_ray_set(path, rays, meta):
    payload = {"version": RAY_SET_VERSION, "meta": meta, "rays": rays}
    Path(path).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf8")
    return payload


def load_ray_set(path):
    payload = json.loads(Path(path).read_text(encoding="utf8"))
    if payload.get("version") != RAY_SET_VERSION or not isinstance(
        payload.get("rays"), list
    ):
        raise ValueError("unknown frozen ray set")
    return payload


def verify_before_attribution(ray_set, before_runs):
    """Every gap ray must show HP loss with the baseline's label before freezing."""
    by_id = {run["ray_id"]: run for run in before_runs}
    failures = []
    for ray in ray_set["rays"]:
        if ray.get("category") == "control":
            continue
        run = by_id.get(ray["id"])
        if run is None or not run.get("hp_loss", 0) > 0:
            failures.append({"ray": ray["id"], "reason": "no_hp_loss"})
        elif (
            run.get("part", "").upper()
            != ray.get("expectedPart", run.get("expected_part", "")).upper()
        ):
            failures.append({"ray": ray["id"], "reason": "wrong_baseline_part"})
    return {"failures": failures, "checked": len(ray_set["rays"])}


def compound_gate_pass(report, key="damage"):
    return report[key]["gatedDisagreements"] == 0


def load_records(path, validator):
    valid, failures = [], []
    text = Path(path).read_text(encoding="utf8")
    try:
        single = json.loads(text)
        records = [single] if isinstance(single, dict) else text.splitlines()
    except ValueError:
        records = text.splitlines()
    for line, record in enumerate(records, 1):
        try:
            if isinstance(record, str):
                record = json.loads(record)
            payload = record.get("receipt", record.get("dump", record))
            if payload.get("errorCode") or payload.get("error"):
                raise ValueError(
                    "oracle_missing:"
                    + str(
                        payload.get(
                            "reason", payload.get("errorCode", payload.get("error"))
                        )
                    )
                )
            payload["_request"] = record.get("request", {})
            valid.append(validator(payload))
        except (ValueError, TypeError, KeyError, IndexError) as error:
            failures.append({"line": line, "reason": str(error)})
    return valid, failures


def calibration(manifest):
    """Validate shared capture clocks; cached records are not request-time clock reads."""
    pairs = manifest["calibrationPairs"]
    post = manifest.get("postCalibrationPairs", [])
    if len(pairs) < 10 or manifest.get("baselineTicks", 0) < 40:
        raise ValueError("requires >=10 stable calibration pairs after >=40 ticks")
    origins = {key: [] for key in ("request", "server", "observer")}
    ages = {key: [] for key in ("server", "observer")}
    delays, scene = [], []
    missing_metadata = False
    for pair in pairs + post:
        for name in ages:
            sample = pair[name]
            send, recv = sample["sendUnixMs"], sample["recvUnixMs"]
            monotonic_width = (sample["recvMonoNs"] - sample["sendMonoNs"]) / 1_000_000
            if recv < send or monotonic_width < 0:
                raise ValueError("invalid request bracket")
            if abs(monotonic_width - (recv - send)) > 5:
                raise ValueError("wall clock changed during request")
            origins["request"].extend(
                sample[edge + "UnixMs"] - sample[edge + "MonoNs"] / 1_000_000
                for edge in ("send", "recv")
            )
            delays.append(monotonic_width)
            ages[name].append(recv - sample["sampleUnixMs"])
            if "sampleMonoNs" in sample:
                origins[name].append(
                    sample["sampleUnixMs"] - sample["sampleMonoNs"] / 1_000_000
                )
            else:
                missing_metadata = True
        scene.append(
            pair["observer"]["tick"]
            - pair["server"]["gameTime"]
            - (pair["observer"]["sampleUnixMs"] - pair["server"]["sampleUnixMs"]) / 50
        )
    drift = manifest.get("clockDriftMs", 0)
    if type(drift) not in (int, float) or not math.isfinite(drift) or drift < 0:
        raise ValueError("invalid clock drift bound")
    drift = max(
        drift, *(max(values) - min(values) for values in origins.values() if values)
    )
    shared = manifest.get("clockDomain") == "shared_local_unix" and not manifest.get(
        "remote", False
    )
    uncertainty = drift + 1
    reasons = []
    if not shared:
        reasons.append("remote_or_undeclared_clock_not_independently_calibrated")
        uncertainty = max(
            uncertainty, *(abs(age) for values in ages.values() for age in values)
        )
    if missing_metadata:
        reasons.append("missing_capture_monotonic_metadata")
    if drift > 5:
        reasons.append("wall_clock_unstable")
    if shared and any(age < -uncertainty for values in ages.values() for age in values):
        reasons.append("future_calibration_capture")
    return {
        "server": 0,
        "observer": 0,
        "uncertaintyMs": uncertainty,
        "uncertaintyBounded": shared and not missing_metadata,
        "windowTicks": max(2, math.ceil(uncertainty / 50)),
        "sceneOffsetTicks": statistics.median(scene),
        "requestDurationMs": summary(delays),
        "sampleAgeAtReceiveMs": {key: summary(values) for key, values in ages.items()},
        "inconclusive": bool(reasons),
        "reasons": reasons,
    }


def compatible(a, b):
    # LEGACY-CUT: the dump carries no animator state (schema v3); continuity is
    # epoch/resourceEpoch identity plus adjacent ticks. Geometry itself compares
    # per part at the call site.
    return (
        a["epoch"] == b["epoch"]
        and a["resourceEpoch"] == b["resourceEpoch"]
        and a["serverTick"] + 1 == b["serverTick"]
    )


def quaternion(axes):
    # Convert mirrored boxes to a proper basis; signs do not change the shape.
    a, b, _ = axes
    c = cross(a, b)
    m = [[a[i], b[i], c[i]] for i in range(3)]
    trace = sum(m[i][i] for i in range(3))
    if trace > 0:
        s = math.sqrt(trace + 1) * 2
        return (
            (m[2][1] - m[1][2]) / s,
            (m[0][2] - m[2][0]) / s,
            (m[1][0] - m[0][1]) / s,
            s / 4,
        )
    i = max(range(3), key=lambda k: m[k][k])
    j, k = (i + 1) % 3, (i + 2) % 3
    s = math.sqrt(1 + m[i][i] - m[j][j] - m[k][k]) * 2
    q = [0.0] * 4
    q[i], q[j], q[k], q[3] = (
        s / 4,
        (m[j][i] + m[i][j]) / s,
        (m[k][i] + m[i][k]) / s,
        (m[k][j] - m[j][k]) / s,
    )
    return tuple(q)


def interpolate(a, b, fraction):
    qa, qb = quaternion(a["axes"]), quaternion(b["axes"])
    cosine = dot(qa, qb)
    if cosine < 0:
        qb, cosine = mul(qb, -1), -cosine
    if cosine > 0.9995:
        q = add(mul(qa, 1 - fraction), mul(qb, fraction))
        q = mul(q, 1 / norm(q))
    else:
        angle = math.acos(min(1, cosine))
        q = add(
            mul(qa, math.sin((1 - fraction) * angle) / math.sin(angle)),
            mul(qb, math.sin(fraction * angle) / math.sin(angle)),
        )
    x, y, z, w = q
    axes = (
        (1 - 2 * (y * y + z * z), 2 * (x * y + z * w), 2 * (x * z - y * w)),
        (2 * (x * y - z * w), 1 - 2 * (x * x + z * z), 2 * (y * z + x * w)),
        (2 * (x * z + y * w), 2 * (y * z - x * w), 1 - 2 * (x * x + y * y)),
    )
    return {
        "center": add(mul(a["center"], 1 - fraction), mul(b["center"], fraction)),
        "half": add(mul(a["half"], 1 - fraction), mul(b["half"], fraction)),
        "axes": axes,
    }


def compare(servers, observers, manifest, spacing=0.025, rays=True):
    clock = calibration(manifest)
    if servers and all(s["schema"] == "blockfield.bodypose.v2" for s in servers):
        return compare_compound(servers, observers, manifest, clock, spacing, rays)
    target, dimension = manifest["targetUuid"], manifest["dimension"]
    unrelated_server = sum(identity(s) != (target, dimension) for s in servers)
    servers = sorted(
        [s for s in servers if identity(s) == (target, dimension)],
        key=lambda s: s["sampleUnixMs"],
    )
    if not servers:
        raise ValueError("no matching server frames")
    times = [s["sampleUnixMs"] + clock["server"] for s in servers]
    results, failures, seen = [], [], set()
    for receipt in observers:
        sequence = (receipt.get("observerUuid"), receipt["frameSequence"])
        if sequence in seen:
            continue
        seen.add(sequence)
        if identity(receipt) != (target, dimension):
            failures.append(
                {"frame": sequence, "reason": "unrelated_target_or_dimension"}
            )
            continue
        request = receipt["_request"]
        if request and (
            request["recvUnixMs"] - receipt["sampleUnixMs"] - clock["observer"] > 2000
            or request["recvUnixMs"] - receipt["sampleUnixMs"] - clock["observer"]
            < -clock["uncertaintyMs"]
        ):
            failures.append({"frame": sequence, "reason": "stale_or_future_receipt"})
            continue
        moment = receipt["sampleUnixMs"] + clock["observer"]
        index = min(range(len(times)), key=lambda i: abs(times[i] - moment))
        server = servers[index]
        if abs(times[index] - moment) > (clock["windowTicks"] + 0.5) * 50:
            failures.append({"frame": sequence, "reason": "server_capture_gap"})
            continue
        window = [
            s
            for s, t in zip(servers, times)
            if abs(t - moment) <= clock["windowTicks"] * 50
            and s["epoch"] == server["epoch"]
            and s["resourceEpoch"] == server["resourceEpoch"]
        ]
        parts = {}
        for i, part in enumerate(PARTS):
            observed = receipt["geometry"][part]
            parts[part] = {
                "world": metrics(server["skinObbs"][i], observed),
                "translationNormalized": metrics(
                    server["skinObbs"][i],
                    observed,
                    server["feet"],
                    receipt["renderPosition"],
                ),
                "uncertaintyWindow": [
                    {
                        "tick": s["serverTick"],
                        "metrics": metrics(s["skinObbs"][i], observed),
                    }
                    for s in window
                ],
            }
        before = max((i for i, t in enumerate(times) if t <= moment), default=None)
        after = min((i for i, t in enumerate(times) if t >= moment), default=None)
        diagnostic = {"available": False, "reason": "gap_or_discontinuity"}
        if (
            before is not None
            and after is not None
            and before != after
            and compatible(servers[before], servers[after])
        ):
            fraction = (moment - times[before]) / (times[after] - times[before])
            diagnostic = {
                "available": True,
                "ticks": [servers[before]["serverTick"], servers[after]["serverTick"]],
                "fraction": fraction,
                "parts": {
                    p: metrics(
                        interpolate(
                            servers[before]["skinObbs"][i],
                            servers[after]["skinObbs"][i],
                            fraction,
                        ),
                        receipt["geometry"][p],
                    )
                    for i, p in enumerate(PARTS)
                },
            }
        row = {
            "frameSequence": receipt["frameSequence"],
            "serverTick": server["serverTick"],
            "gameTime": server["gameTime"],
            "epoch": server["epoch"],
            "partialTick": receipt["partialTick"],
            "timeDeltaMs": moment - times[index],
            "uncertaintyMs": clock["uncertaintyMs"],
            "windowCoverage": "complete"
            if times[0] <= moment - clock["windowTicks"] * 50
            and times[-1] >= moment + clock["windowTicks"] * 50
            else "truncated",
            "windowEndpoints": [window[0]["serverTick"], window[-1]["serverTick"]]
            if window
            else [],
            "alignment": "inconclusive"
            if clock["inconclusive"] or clock["windowTicks"] > 5
            else "nominal",
            "sceneTimeDiagnosticTicks": receipt["tick"]
            + receipt["partialTick"]
            - clock["sceneOffsetTicks"]
            - server["gameTime"],
            "parts": parts,
            "interpolatedDiagnosticOnly": diagnostic,
            "diagnostics": server.get("diagnostics", []),
            "freshnessVerified": bool(request),
            "requestDurationMs": (
                request.get("recvMonoNs", 0) - request.get("sendMonoNs", 0)
            )
            / 1_000_000
            if request
            else None,
            "sampleAgeAtReceiveMs": request.get("recvUnixMs", 0) - moment
            if request
            else None,
            "commandQueueDurationMs": None,
            "captureDurationNs": receipt.get("captureDurationNs"),
            "observerTick": receipt["tick"],
            "observerSampleUnixMs": receipt["sampleUnixMs"],
            "serverSampleUnixMs": server["sampleUnixMs"],
            "cohort": receipt.get("cohort", manifest.get("cohort", {})),
        }
        if rays:
            row["rays"] = ray_report(
                server["skinObbs"],
                server["damageObbs"],
                receipt["geometry"],
                spacing,
                manifest.get("cameraPositions", []),
            )
        results.append(row)

    def aggregate(rows):
        output = {}
        for part in PARTS:
            values = [r["parts"][part]["world"] for r in rows]
            output[part] = {
                key: summary([m[key] for m in values if key in m])
                for key in ("center", "hausdorff")
            }
            output[part]["cornerRms"] = summary(
                [m["corners"]["rms"] for m in values if "corners" in m]
            )
            output[part]["cornerMax"] = summary(
                [m["corners"]["max"] for m in values if "corners" in m]
            )
            output[part]["axisMaxDegrees"] = summary(
                [max(m["axisDegrees"]) for m in values if "axisDegrees" in m]
            )
            output[part]["extentMax"] = summary(
                [
                    max(abs(e) for e in m["extentDifference"])
                    for m in values
                    if "extentDifference" in m
                ]
            )
            output[part]["nonBox"] = sum("failure" in m for m in values)
        return output

    aggregates = aggregate(results)
    cohorts = {}
    for row in results:
        key = json.dumps(row["cohort"], sort_keys=True)
        cohorts.setdefault(key, []).append(row)

    def aggregate_rays(rows):
        reports = {}
        for label in ("skin", "damage"):
            measured = [r["rays"][label] for r in rows if "rays" in r]
            if len(measured) != len(rows):
                continue
            counts = {
                key: sum(r[key] for r in measured)
                for key in (
                    "allGrid",
                    "unionHit",
                    "disagreements",
                    "falseHits",
                    "falseMisses",
                )
            }
            counts["confusion"] = {
                a: {b: sum(r["confusion"][a][b] for r in measured) for b in LABELS}
                for a in LABELS
            }
            counts["unionHitRate"] = (
                counts["disagreements"] / counts["unionHit"]
                if counts["unionHit"]
                else None
            )
            reports[label] = counts
        return reports

    cohort_reports = [
        {
            "cohort": json.loads(key),
            "frames": len(rows),
            "parts": aggregate(rows),
            "rays": aggregate_rays(rows),
        }
        for key, rows in cohorts.items()
    ]
    worst = sorted(
        (
            {
                "frameSequence": r["frameSequence"],
                "serverTick": r["serverTick"],
                "part": p,
                "cornerMax": r["parts"][p]["world"]["corners"]["max"],
            }
            for r in results
            for p in PARTS
            if "corners" in r["parts"][p]["world"]
        ),
        key=lambda r: r["cornerMax"],
        reverse=True,
    )[:10]
    nonbox = sum(
        "failure" in row["parts"][p]["world"] for row in results for p in PARTS
    )
    return {
        "schema": "blockfield.bodypose.comparison.v1",
        "manifest": manifest,
        "calibration": clock,
        "alignment": "inconclusive"
        if clock["inconclusive"] or clock["windowTicks"] > 5
        else "nominal_with_uncertainty",
        "pairedFrames": len(results),
        "serverTickGaps": [
            {"after": a["serverTick"], "before": b["serverTick"], "epoch": a["epoch"]}
            for a, b in zip(servers, servers[1:])
            if a["epoch"] == b["epoch"] and b["serverTick"] > a["serverTick"] + 1
        ],
        "receiptGapsMs": [
            b["observerSampleUnixMs"] - a["observerSampleUnixMs"]
            for a, b in zip(results, results[1:])
            if b["observerSampleUnixMs"] - a["observerSampleUnixMs"] > 100
        ],
        "duplicateReceipts": len(observers) - len(seen),
        "unrelatedServerFrames": unrelated_server,
        "nonBoxParts": nonbox,
        "failures": failures,
        "aggregates": aggregates,
        "cohorts": cohort_reports,
        "worstFrames": worst,
        "frames": results,
        "accepted": False,
        "note": "Geometry sensitivity, not combat probability. Owner decides residual acceptability.",
    }


def baseline_comparison(baseline, report):
    result = {
        "label": baseline["manifest"].get("label"),
        "regression": None,
        "inconclusive": True,
    }
    if any(
        source.get("alignment") == "inconclusive"
        or source.get("failures")
        or source.get("nonBoxParts")
        or any(source.get("inputFailures", {}).values())
        for source in (baseline, report)
    ):
        return dict(result, reason="inconclusive_measurements")
    keys = ("cohort", "declaredInputs", "settings", "targetUuid", "dimension", "phases")
    old_manifest, new_manifest = baseline["manifest"], report["manifest"]
    if any(
        k not in old_manifest
        or k not in new_manifest
        or old_manifest[k] != new_manifest[k]
        for k in keys
    ):
        return dict(result, reason="missing_or_different_declared_contract")
    # Request results and actual execution times vary; compare the scheduled native inputs.
    actions = []
    for manifest in (old_manifest, new_manifest):
        if "actions" not in manifest:
            return dict(result, reason="missing_declared_actions")
        actions.append(
            [
                {k: v for k, v in action.items() if k not in ("elapsed", "result")}
                for action in manifest["actions"]
            ]
        )
    if actions[0] != actions[1] or not new_manifest["phases"]:
        return dict(result, reason="missing_or_different_action_phase_contract")
    cohorts = []
    for source in (baseline, report):
        cohorts.append(
            {
                json.dumps(c["cohort"], sort_keys=True): c
                for c in source.get("cohorts", [])
            }
        )
    if not cohorts[0] or cohorts[0].keys() != cohorts[1].keys():
        return dict(result, reason="missing_or_different_measured_cohort_coverage")
    phases = {phase["phase"] for phase in new_manifest["phases"]}
    if not phases <= {c["cohort"].get("phase") for c in cohorts[1].values()}:
        return dict(result, reason="missing_declared_phase_coverage")
    deltas, regression = [], False
    try:
        for key, old in cohorts[0].items():
            new = cohorts[1][key]
            if old["frames"] <= 0 or old["frames"] != new["frames"]:
                return dict(result, reason="different_measured_cohort_frame_coverage")
            parts = {}
            for part in PARTS:
                parts[part] = {}
                for metric in (
                    "center",
                    "hausdorff",
                    "cornerRms",
                    "cornerMax",
                    "axisMaxDegrees",
                    "extentMax",
                ):
                    before, after = (
                        old["parts"][part][metric],
                        new["parts"][part][metric],
                    )
                    if (
                        before["count"] != old["frames"]
                        or after["count"] != new["frames"]
                    ):
                        return dict(result, reason="missing_cohort_pose_metrics")
                    parts[part][metric] = {
                        stat: after[stat] - before[stat] for stat in ("p95", "max")
                    }
                    regression |= any(
                        value > EPS for value in parts[part][metric].values()
                    )
            labels = {}
            for label in ("skin", "damage"):
                before, after = old["rays"][label], new["rays"][label]
                if before["unionHit"] <= 0 or after["unionHit"] <= 0:
                    return dict(result, reason="missing_union_hit_metrics")
                for evidence in (before, after):
                    for predicted in LABELS:
                        for observed in LABELS:
                            integer(
                                evidence["confusion"][predicted][observed],
                                "confusion count",
                            )
                counts = (
                    "allGrid",
                    "unionHit",
                    "disagreements",
                    "falseHits",
                    "falseMisses",
                    "confusion",
                )
                labels[label] = {
                    "baseline": {k: before[k] for k in counts},
                    "candidate": {k: after[k] for k in counts},
                    "unionHitRateDelta": after["unionHitRate"] - before["unionHitRate"],
                }
                labels[label]["confusionRateDeltas"] = {
                    a: {
                        b: after["confusion"][a][b] / after["unionHit"]
                        - before["confusion"][a][b] / before["unionHit"]
                        for b in LABELS
                        if b != a
                    }
                    for a in LABELS
                }
                regression |= any(
                    value > EPS
                    for row in labels[label]["confusionRateDeltas"].values()
                    for value in row.values()
                )
                regression |= labels[label]["unionHitRateDelta"] > EPS
                regression |= any(
                    after[k] / after["unionHit"] - before[k] / before["unionHit"] > EPS
                    for k in ("falseHits", "falseMisses")
                )
            deltas.append({"cohort": new["cohort"], "parts": parts, "rays": labels})
    except (KeyError, TypeError, ValueError):
        return dict(result, reason="missing_cohort_metrics")
    return dict(result, inconclusive=False, regression=regression, deltas=deltas)


def markdown(report):
    if report["schema"] == "blockfield.bodypose.comparison.v2":
        lines = [
            "# Compound geometry comparison",
            "",
            f"Accepted: {report['accepted']}",
            "",
            "World residuals are reported separately and do not gate phase 1.",
            "",
        ]
        for row in report["frames"]:
            lines.append(
                f"Frame {row['frameSequence']}: rebound corner max {row['cornerMaxM'] * 1000:.6f} mm; "
                f"gated grid disagreements {row.get('grid', {}).get('gatedDisagreements', 'unmeasured')}"
            )
        return "\n".join(lines) + "\n"
    lines = [
        "| Part | Frames | Centre p95 (m) | Corner RMS worst (m) | Axis worst (deg) |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for part in PARTS:
        metrics_ = [
            row["parts"][part]["world"]
            for row in report["frames"]
            if "corners" in row["parts"][part]["world"]
        ]
        center = report["aggregates"][part]["center"]
        lines.append(
            f"| {part} | {len(metrics_)} | {center.get('p95', 0):.6f} | {max((m['corners']['rms'] for m in metrics_), default=0):.6f} | {max((max(m['axisDegrees']) for m in metrics_), default=0):.4f} |"
        )
    lines.append(
        f"\nAlignment: {report['alignment']}; paired {report['pairedFrames']}; non-box parts {report['nonBoxParts']}; failures {len(report['failures'])}. No automatic acceptance.\n"
    )
    for key in ("skin", "damage"):
        grids = [f["rays"][key] for f in report["frames"] if "rays" in f]
        total = sum(g["allGrid"] for g in grids)
        union = sum(g["unionHit"] for g in grids)
        disagreements = sum(g["disagreements"] for g in grids)
        lines.append(
            f"{key}: {disagreements}/{union} union-hit rays; {disagreements}/{total} all-grid rays.\n"
        )
    return "\n".join(lines)


def validate_server_v2(record):
    identity(record)
    for key in ("serverTick", "gameTime", "epoch", "resourceEpoch"):
        integer(record[key], key)
    if record["samplePoint"] != "end_server_tick" or record["partialTick"] != 0:
        raise ValueError("not a committed tick endpoint")
    vector(record["feet"])
    for key in ("fixed", "bounds"):
        values = record[key]
        if (
            len(values) != 6
            or any(type(v) not in (int, float) for v in values)
            or any(values[i] >= values[i + 3] for i in range(3))
        ):
            raise ValueError("invalid " + key)
    if (
        set(record.get("visible", {})) != set(PARTS)
        or len(record.get("bones", [])) != 6
    ):
        raise ValueError("six ordered body parts/bones required")
    if record.get("shape", "").split("/", 1)[0] not in ("wide", "slim"):
        raise ValueError("invalid skin metadata")
    for part in PARTS:
        entries = record["visible"][part]
        if not entries or len({e["id"] for e in entries}) != len(entries):
            raise ValueError("empty/duplicate visible ids")
        for entry in entries:
            if not NORMALIZED_ID.fullmatch(entry["id"]):
                raise ValueError("invalid visible id")
            validate_box(entry["local"])
            validate_box(entry["world"])
            if any(
                v[j] < record["bounds"][j] - BOX_EPS
                or v[j] > record["bounds"][j + 3] + BOX_EPS
                for v in corners(entry["world"])
                for j in range(3)
            ):
                raise ValueError("visible geometry outside declared bounds")
    for bone in record["bones"]:
        if len(bone) != 12:
            raise ValueError("bone must be a 3×4 matrix")
        inverse_axes(tuple(vector(bone[k : k + 3]) for k in (0, 3, 6)))
    return record


def excluded_entries(receipt):
    return [
        dict(id=raw["id"], part=p, parsed=oracle_part(raw))
        for p in PARTS
        for raw in receipt["parts"][p.lower()]["boxes"]
        if is_excluded_box(raw.get("source", "base"), raw["id"])
    ] + [
        dict(id=raw.get("source", "excluded"), part="MISS", parsed=oracle_part(raw))
        for raw in receipt.get("unattributed", [])
        if is_excluded_box(raw.get("source", "unknown"), "")
    ]


def box_face_distance(point, box):
    """Distance from a point to the nearest face of an OBB (0 on the surface)."""
    local = tuple(dot(sub(point, box["center"]), axis) for axis in box["axes"])
    if all(abs(x) <= h for x, h in zip(local, box["half"])):
        return min(h - abs(x) for x, h in zip(local, box["half"]))
    return point_to_box_distance(point, box)


def other_part_face_distance(point, part, boxes):
    """Nearest face distance over boxes owned by a different part."""
    best = math.inf
    for box in boxes:
        if box.get("part", part) == part:
            continue
        distance = box_face_distance(point, box)
        if distance < best:
            best = distance
    return best


def entry_chord_cos(start, end, box, entry_dist):
    """Chord through an OBB and |cos| of the ray with the entry-face normal."""
    interval = slab_interval(start, end, box)
    length = norm(sub(end, start))
    if interval is None or not length:
        return None, None
    direction = unit(sub(end, start))
    point = add(start, mul(direction, entry_dist))
    local = tuple(abs(dot(sub(point, box["center"]), a)) for a in box["axes"])
    face = min(range(3), key=lambda i: box["half"][i] - local[i])
    return (interval[1] - interval[0]) * length, abs(dot(direction, box["axes"][face]))


def compound_grid(predicted, entries, world=(), spacing=0.025, cameras=(), excluded=()):
    observed = prepare_entries([e for p in PARTS for e in entries[p]])
    prepare_entries(predicted)
    prepare_entries(world)
    prepare_entries(excluded)
    pseudo = {
        "ORACLE": {"vertices": [v for e in observed for v in e["parsed"]["vertices"]]}
    }
    boxes = [b for b in predicted] + [e["box"] for e in observed if e["box"]]
    report = {
        "allGrid": 0,
        "unionHit": 0,
        "disagreements": 0,
        "gatedDisagreements": 0,
        "seamExcluded": 0,
        "droppedExcluded": 0,
        "dropped": [],
        "droppedSeam": 0,
        "droppedSeamRays": [],
        "droppedGraze": 0,
        "droppedGrazeRays": [],
        "mismatches": [],
        "worldDisagreements": 0,
        "confusion": {a: dict.fromkeys(LABELS, 0) for a in LABELS},
        "worldConfusion": {a: dict.fromkeys(LABELS, 0) for a in LABELS},
    }
    oracle_obbs = [
        dict(e["box"], part=e["part"]) for e in observed if e["box"] is not None
    ]
    for spec, start, end in grid(predicted, pseudo, spacing, cameras):
        report["allGrid"] += 1
        sp, sd, sbox = first_entry(start, end, predicted)
        op, od, obox = first_entry(start, end, observed, True)
        report["unionHit"] += sp != "MISS" or op != "MISS"
        first = min((x for x in (sd, od) if x is not None), default=None)
        _, blocked, _ = first_entry(start, end, excluded, True)
        if blocked is not None and first is not None and blocked < first - EPS:
            report["droppedExcluded"] += 1
            report["dropped"].append(
                {"grid": spec["index"], "start": start, "end": end}
            )
            continue
        report["confusion"][sp][op] += 1
        if world:
            wp, _, _ = first_entry(start, end, world)
            report["worldConfusion"][wp][op] += 1
            report["worldDisagreements"] += wp != op
        if sp == op:
            continue
        report["disagreements"] += 1
        point = add(start, mul(unit(sub(end, start)), first))
        near_native_edge = any(
            point_segment_distance(point, face[k], face[(k + 1) % 4]) < 0.001
            for e in observed
            for face in e["parsed"]["faces"]
            for k in range(4)
        )
        if near_native_edge or union_edge_distance(point, boxes) < 0.001:
            report["seamExcluded"] += 1
            continue
        # Inter-part seam: either entry point within 1 mm of a face owned by
        # a different part (coplanar garment shells tie inside rebound noise).
        direction = unit(sub(end, start))
        seam = False
        if sd is not None:
            seam = (
                other_part_face_distance(add(start, mul(direction, sd)), sp, predicted)
                < SEAM_TOLERANCE
            )
        if not seam and od is not None and obox is not None and obox.get("box"):
            seam = (
                other_part_face_distance(
                    add(start, mul(direction, od)), op, oracle_obbs
                )
                < SEAM_TOLERANCE
            )
        if seam:
            report["droppedSeam"] += 1
            report["droppedSeamRays"].append(
                {
                    "grid": spec["index"],
                    "start": start,
                    "end": end,
                    "server": sp,
                    "observer": op,
                    "serverId": sbox.get("id") if sbox else None,
                    "observerId": obox.get("id") if obox else None,
                    "serverDist": sd,
                    "observerDist": od,
                    "reason": "inter_part_seam",
                }
            )
            continue
        # Grazing ray: sub-mm chord or near-parallel entry face on either side.
        graze = False
        if sd is not None:
            chord, cos = entry_chord_cos(start, end, sbox, sd)
            graze = (chord is not None and chord < GRAZE_CHORD) or (
                cos is not None and cos < GRAZE_COS
            )
        if not graze and od is not None and obox is not None and obox.get("box"):
            chord, cos = entry_chord_cos(start, end, obox["box"], od)
            graze = (chord is not None and chord < GRAZE_CHORD) or (
                cos is not None and cos < GRAZE_COS
            )
        if graze:
            report["droppedGraze"] += 1
            report["droppedGrazeRays"].append(
                {
                    "grid": spec["index"],
                    "start": start,
                    "end": end,
                    "server": sp,
                    "observer": op,
                    "serverId": sbox.get("id") if sbox else None,
                    "observerId": obox.get("id") if obox else None,
                    "serverDist": sd,
                    "observerDist": od,
                    "reason": "graze",
                }
            )
            continue
        report["gatedDisagreements"] += 1
        if len(report["mismatches"]) < 50:
            report["mismatches"].append(
                {
                    "grid": spec["index"],
                    "start": start,
                    "end": end,
                    "server": sp,
                    "observer": op,
                    "serverId": sbox.get("id") if sbox else None,
                    "observerId": obox.get("id") if obox else None,
                    "serverDist": sd,
                    "observerDist": od,
                }
            )
    report["excludedOcclusion"] = (
        "captured" if excluded else "unverified: no excluded geometry in receipt"
    )
    return report


def compound_contract(dump, receipt, spacing=0.025, rays=True, cameras=()):
    entries = receipt_entries(receipt)
    slim = dump.get("shape", "").startswith("slim")
    skin_equal = slim == (receipt.get("skinModel") == "slim")
    bones = oracle_bones(entries, receipt.get("skinModel") == "slim")
    normalized = normalize_receipt_v2(receipt)
    parts, predicted, world = {}, [], []
    for i, part in enumerate(PARTS):
        server = dump["visible"][part]
        oracle = {e["id"]: e for e in entries[part]}
        ids = sorted(e["id"] for e in server) == sorted(e["id"] for e in entries[part])
        local_errors, world_errors = [], []
        per_box = []
        for entry in server:
            rebound = dict(
                transform_box(entry["local"], bones[part]), part=part, id=entry["id"]
            )
            native_world = dict(validate_box(entry["world"]), part=part, id=entry["id"])
            predicted.append(rebound)
            world.append(native_world)
            if (
                entry["id"] not in oracle
                or oracle[entry["id"]]["parsed"]["box"] is None
            ):
                continue
            points = oracle[entry["id"]]["parsed"]["corners"]
            errors = assignment(corners(rebound), points)
            residual = assignment(corners(native_world), points)
            local_errors.extend(errors)
            world_errors.extend(residual)
            per_box.append(
                {
                    "id": entry["id"],
                    "cornerMaxM": max(errors),
                    "worldCornerMaxM": max(residual),
                }
            )
        matrix = dump["bones"][i]
        server_axes = tuple(tuple(matrix[k : k + 3]) for k in (0, 3, 6))
        oracle_axes = bones[part]["rotation"]
        # Compare signed frames, preserving rotations even for symmetric skin cubes.
        relative = [[dot(unit(a), unit(b)) for b in oracle_axes] for a in server_axes]
        angle = math.degrees(
            math.acos(
                min(1.0, max(-1.0, (sum(relative[k][k] for k in range(3)) - 1) / 2))
            )
        )
        parts[part] = {
            "idSetEqual": ids,
            "onlyDump": sorted(set(e["id"] for e in server) - oracle.keys()),
            "onlyReceipt": sorted(oracle.keys() - set(e["id"] for e in server)),
            "boneRegistered": summary(local_errors),
            "worldCorners": summary(world_errors),
            "boneTranslationM": sub(matrix[9:12], bones[part]["translation"]),
            "boneRotationDegrees": angle,
            "boxes": per_box,
            "complete": len(per_box) == len(server),
        }
    server_yaw = dump.get("state", {}).get("bodyYaw")
    oracle_yaw = receipt.get("nativePose", {}).get("bodyYaw")
    yaw = (
        (server_yaw - oracle_yaw + 180) % 360 - 180
        if server_yaw is not None and oracle_yaw is not None
        else None
    )
    report = {
        "schema": "blockfield.bodypose.contract.v2",
        "parts": parts,
        "skinModelEqual": skin_equal,
        "unattributed": normalized["unattributed"],
        "bodyYawResidualDegrees": yaw,
        "cornerMaxM": max(
            (p["boneRegistered"].get("max", 0) for p in parts.values()), default=0
        ),
    }
    excluded = excluded_entries(receipt)
    report["excluded"] = [{"id": e["id"], "part": e["part"]} for e in excluded]
    if rays:
        report["grid"] = compound_grid(
            predicted, entries, world, spacing, cameras, excluded
        )
    report["accepted"] = (
        skin_equal
        and not report["unattributed"]
        and all(p["idSetEqual"] and p["complete"] for p in parts.values())
        and report["cornerMaxM"] <= 0.0005
        and rays
        and report["grid"]["gatedDisagreements"] == 0
    )
    return report


def compare_compound(servers, observers, manifest, clock, spacing, rays):
    target = (manifest["targetUuid"], manifest["dimension"])
    servers = [s for s in servers if identity(s) == target]
    if not servers:
        raise ValueError("no matching server frames")
    rows, failures, seen = [], [], set()
    for receipt in observers:
        seq = (receipt.get("observerUuid"), receipt["frameSequence"])
        if seq in seen:
            continue
        seen.add(seq)
        if identity(receipt) != target:
            failures.append({"frame": seq, "reason": "unrelated_target_or_dimension"})
            continue
        if receipt["schema"] != "blockfield.playergeometry.v2":
            failures.append(
                {"frame": seq, "reason": "compound mode requires v2 oracle"}
            )
            continue
        server = min(
            servers, key=lambda s: abs(s["sampleUnixMs"] - receipt["sampleUnixMs"])
        )
        if (
            abs(server["sampleUnixMs"] - receipt["sampleUnixMs"])
            > (clock["windowTicks"] + 0.5) * 50
        ):
            failures.append({"frame": seq, "reason": "server_capture_gap"})
            continue
        request = receipt.get("_request", {})
        age = (
            request.get("recvUnixMs", receipt["sampleUnixMs"]) - receipt["sampleUnixMs"]
        )
        if age > 2000 or age < -clock["uncertaintyMs"]:
            failures.append({"frame": seq, "reason": "stale_or_future_receipt"})
            continue
        row = compound_contract(
            server, receipt, spacing, rays, manifest.get("cameraPositions", [])
        )
        rows.append(
            dict(
                row,
                frameSequence=receipt["frameSequence"],
                serverTick=server["serverTick"],
                cohort=receipt.get("cohort", manifest.get("cohort", {})),
            )
        )
    return {
        "schema": "blockfield.bodypose.comparison.v2",
        "manifest": manifest,
        "calibration": clock,
        "alignment": "inconclusive"
        if clock["inconclusive"]
        else "nominal_with_uncertainty",
        "pairedFrames": len(rows),
        "frames": rows,
        "failures": failures,
        "nonBoxParts": sum(
            not p["complete"] for row in rows for p in row["parts"].values()
        ),
        "accepted": bool(rows)
        and not failures
        and not clock["inconclusive"]
        and all(r["accepted"] for r in rows),
    }


def load_dump_boxes(path):
    """Candidate world boxes; baseline selection explicitly reads damageObbs."""
    payload = read_records(path)[0]
    payload = payload.get("dump", payload)
    if isinstance(payload.get("visible"), dict):
        return [
            dict(validate_box(entry["world"]), part=part)
            for part in PARTS
            for entry in payload["visible"].get(part, [])
        ]
    return [
        dict(validate_box(entry), part=entry["part"]) for entry in payload["damageObbs"]
    ]


def receipt_entries(receipt):
    """Retain actual native faces, including complete cuboids labelled quads."""
    entries = {p: [] for p in PARTS}
    for part in PARTS:
        for raw in receipt["parts"][part.lower()]["boxes"]:
            source = raw.get("source", "base")
            if is_excluded_box(source, raw["id"]):
                continue
            parsed = oracle_part(raw)
            entries[part].append(
                {
                    "id": normalize_box_id(raw["id"], source, part),
                    "part": part,
                    "parsed": parsed,
                    "box": parsed["box"],
                }
            )
    return entries


def oracle_bones(entries, slim=False):
    return {
        p: register_bone(
            next(e for e in entries[p] if e["id"] == "skin:inner")["parsed"][
                "vertices"
            ],
            p,
            slim,
        )
        for p in PARTS
    }


def load_oracle_boxes(path):
    payload = read_records(path)[0]
    payload = payload.get("receipt", payload)
    if payload["schema"] != "blockfield.playergeometry.v2":
        raise ValueError(
            "gap anatomical regions need a v2 oracle with inner skin cubes"
        )
    validate_receipt_v2(payload)
    if normalize_receipt_v2(payload)["unattributed"]:
        raise ValueError("gap oracle contains unattributed geometry")
    entries = receipt_entries(payload)
    if any(e["box"] is None for group in entries.values() for e in group):
        raise ValueError(
            "gap clearance requires verified native cuboids, not open meshes"
        )
    boxes = [
        dict(e["box"], part=p, id=e["id"], vertices=e["parsed"]["vertices"])
        for p in PARTS
        for e in entries[p]
    ]
    return boxes, entries


def run_gap(args):
    if args.oracle_off is None:
        raise ValueError(
            "--gap needs --oracle-off: each baseline requires its own receipt"
        )
    eyes = (
        [vector(p) for p in json.loads(args.fireable_eyes.read_text())]
        if args.fireable_eyes
        else []
    )
    job_dir = Path(args.job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    results = {}
    for name, oracle_path, dump_path in (
        ("on", args.oracle, args.baseline_on),
        ("off", args.oracle_off, args.baseline_off),
    ):
        oracle, entries = load_oracle_boxes(oracle_path)
        receipt = read_records(oracle_path)[0]
        receipt = receipt.get("receipt", receipt)
        dump = read_records(dump_path)[0]
        dump = validate_server(dump.get("dump", dump))
        if identity(dump) != identity(receipt):
            raise ValueError("baseline and its oracle have different target/dimension")
        base = (
            [dict(validate_box(b), part=b["part"]) for b in dump["damageObbs"]]
            if name == "on"
            else [dict(inflated_off_box(dump["fixed"]), part="OFF")]
        )
        crouched = dump.get("state", {}).get("pose") in ("CROUCHING", "CROUCHED")
        selection = select_gap_rays(
            oracle,
            base,
            oracle_bones(entries, receipt.get("skinModel") == "slim"),
            args.spacing,
            per_category=args.per_category,
            baseline=name,
            fixed=dump["fixed"],
            eye_height=dump.get("eyeHeight", 1.27 if crouched else 1.62),
            fireable_eyes=eyes,
        )
        excluded = excluded_entries(receipt)
        controls = control_rays_from_receipt(
            entries, fireable_eyes=eyes, excluded=excluded
        )
        rays = selection["rays"] + controls
        if args.candidate:
            report_candidate_clearance(rays, load_dump_boxes(args.candidate))
        results[name] = freeze_ray_set(
            job_dir / f"frozen-{name}.json",
            rays,
            {
                "baseline": name,
                "oracle": str(oracle_path),
                "inputBaseline": str(dump_path),
                "sampleDeltaMs": receipt["sampleUnixMs"] - dump["sampleUnixMs"],
                "scanned": selection["scanned"],
                "baselineHit": selection["baselineHit"],
                "census": selection["census"],
                "hpVerified": False,
                "requiresHeldOcclusionCheck": not bool(excluded),
                "geometricOnly": not bool(eyes),
                "controls": len(controls),
                "requiresBeforeDamageValidation": True,
                "ok": selection["ok"]
                and all(sum(r["part"] == p for r in controls) >= 6 for p in PARTS),
            },
        )
    print(json.dumps({name: payload["meta"] for name, payload in results.items()}))
    return results


F3B_TOLERANCE_M = 0.0005  # §9.1(10): drawn boxes equal the dump within 0.5 mm.


def read_records(path):
    """One NDJSON record per line, or a single pretty-printed JSON record."""
    text = Path(path).read_text(encoding="utf8")
    try:
        single = json.loads(text)
        if isinstance(single, dict):
            return [single]
    except ValueError:
        pass
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def load_capture_frames(path):
    """Server capture NDJSON (bf bodypose capture) indexed by server tick.

    Records carry both ``serverTick`` and ``gameTime``; pairing uses
    ``serverTick`` (the committed frame's tick). Captures from before that
    field existed fall back to ``gameTime``. Returns the frames and the key
    field used.
    """
    frames = {}
    key_field = "gameTime"
    for line, record in enumerate(read_records(path), 1):
        payload = record.get("dump", record)
        try:
            if "serverTick" in payload:
                tick = integer(payload["serverTick"], "serverTick")
                key_field = "serverTick"
            else:
                tick = integer(payload["gameTime"], "gameTime")
        except (KeyError, ValueError, TypeError):
            raise ValueError(f"capture line {line} has no serverTick or gameTime")
        boxes = {}
        if isinstance(payload.get("visible"), dict):
            for part in PARTS:
                boxes[part] = [
                    dict(validate_box(entry.get("world", entry)), part=part)
                    for entry in payload["visible"].get(part, [])
                ]
        else:
            for entry in payload.get("damageObbs", []):
                boxes.setdefault(entry.get("part"), []).append(
                    dict(validate_box(entry), part=entry.get("part"))
                )
        frames[tick] = boxes
    if not frames:
        raise ValueError("no capture frames found")
    return frames, key_field


def debug_corner_sets(debug_boxes):
    """Part name to lists of 8 world corners from a receipt debugBoxes payload."""
    parts = {}
    for part in PARTS:
        sets = []
        for packed in debug_boxes.get("boxes", {}).get(part.lower(), []):
            if len(packed) != 24:
                raise ValueError("debug box is not 8 corners")
            sets.append(
                tuple(
                    (packed[i], packed[i + 1], packed[i + 2]) for i in range(0, 24, 3)
                )
            )
        parts[part] = sets
    return parts


def run_f3b(args):
    """Pair receipt F3+B debug boxes with the server capture by exact tick (§9.1(10)).

    Boxes pair by emission index within a part (both sides build from the same
    BodyGeometry order); corners pair by the exact bijection, so order never hides
    a swap. Reports the max corner difference in metres.

    Pairing uses the drawn frame's ``serverTick``; receipts from before that field
    existed fall back to the client draw ``tick`` and are labelled ``tickLegacy``.
    Captures are keyed the same way (``serverTick``, falling back to ``gameTime``).
    """
    frames, capture_key = load_capture_frames(args.capture)
    ticks = sorted(frames)
    paired, no_debug, max_overall = 0, 0, 0.0
    pairing = {"serverTick": 0, "tickLegacy": 0}
    part_max, unpaired, mismatches = {}, [], []
    for record in read_records(args.receipt):
        payload = record.get("receipt", record)
        debug = payload.get("debugBoxes")
        if not debug:
            no_debug += 1
            continue
        if "serverTick" in debug:
            tick = integer(debug["serverTick"], "serverTick")
            paired_by = "serverTick"
        else:
            tick = integer(debug["tick"], "tick")
            paired_by = "tickLegacy"
        server = frames.get(tick)
        if server is None:
            nearest = min(ticks, key=lambda t: abs(t - tick))
            unpaired.append(
                {
                    "tick": tick,
                    "pairedBy": paired_by,
                    "nearestCaptureTick": nearest,
                    "gap": abs(nearest - tick),
                }
            )
            continue
        drawn = debug_corner_sets(debug)
        worst = 0.0
        for part in PARTS:
            boxes, sets = server.get(part, []), drawn.get(part, [])
            if len(boxes) != len(sets):
                mismatches.append(
                    {
                        "tick": tick,
                        "part": part,
                        "capture": len(boxes),
                        "drawn": len(sets),
                    }
                )
            for server_box, drawn_set in zip(boxes, sets):
                diffs = assignment(corners(server_box), list(drawn_set))
                worst = max([worst, part_max.get(part, 0.0), *diffs])
                part_max[part] = max(part_max.get(part, 0.0), max(diffs, default=0.0))
        paired += 1
        pairing[paired_by] += 1
        max_overall = max(max_overall, worst)
    report = {
        "schema": "blockfield.f3b.comparison.v1",
        "toleranceM": F3B_TOLERANCE_M,
        "paired": paired,
        "pairing": pairing,
        "captureKey": capture_key,
        "receiptsWithoutDebugBoxes": no_debug,
        "unpaired": unpaired,
        "countMismatches": mismatches,
        "maxCornerDifferenceM": max_overall,
        "perPartMaxM": {part: part_max.get(part, 0.0) for part in PARTS},
        "accepted": paired > 0
        and not unpaired
        and not mismatches
        and max_overall <= F3B_TOLERANCE_M,
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
    print(
        json.dumps(
            {
                "paired": paired,
                "maxCornerDifferenceM": max_overall,
                "accepted": report["accepted"],
            }
        )
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gap",
        action="store_true",
        help="freeze gap/control rays instead of comparing",
    )
    parser.add_argument(
        "--f3b",
        action="store_true",
        help="pair receipt F3+B debug boxes with a server capture by exact tick",
    )
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--oracle", type=Path)
    parser.add_argument("--baseline-on", type=Path)
    parser.add_argument("--baseline-off", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--job-dir", type=Path)
    parser.add_argument("--per-category", type=int, default=6)
    parser.add_argument("--oracle-off", type=Path)
    parser.add_argument(
        "--fireable-eyes", type=Path, help="JSON list of scene-verified fireable eyes"
    )
    for flag in ("server", "observer", "manifest", "output"):
        parser.add_argument("--" + flag, type=Path, required=False)
    parser.add_argument(
        "--baseline",
        type=Path,
        help="prior comparison JSON for equivalent declared inputs/cohorts",
    )
    parser.add_argument("--spacing", type=float, default=0.025)
    args = parser.parse_args()
    if not 0.005 <= args.spacing <= 0.1:
        parser.error("spacing must be .005.. .1 m")
    if args.per_category <= 0:
        parser.error("per-category must be positive")
    if args.gap:
        if not all(
            (
                args.oracle,
                args.oracle_off,
                args.baseline_on,
                args.baseline_off,
                args.job_dir,
            )
        ):
            parser.error(
                "--gap needs --oracle, --oracle-off, --baseline-on, --baseline-off and --job-dir"
            )
        run_gap(args)
        return 0
    if args.f3b:
        if not all((args.receipt, args.capture, args.output)):
            parser.error("--f3b needs --receipt, --capture and --output")
        run_f3b(args)
        return 0
    for flag in ("server", "observer", "manifest", "output"):
        if getattr(args, flag) is None:
            parser.error(f"--{flag} is required without --gap")
    servers, server_failures = load_records(args.server, validate_server)
    observers, observer_failures = load_records(args.observer, validate_observer)
    manifest = json.loads(args.manifest.read_text(encoding="utf8"))
    try:
        report = compare(servers, observers, manifest, args.spacing)
    except (ValueError, KeyError, TypeError) as error:
        report = {
            "schema": "blockfield.bodypose.comparison.v1",
            "manifest": manifest,
            "alignment": "inconclusive",
            "pairedFrames": 0,
            "nonBoxParts": 0,
            "failures": [{"reason": str(error)}],
            "frames": [],
            "aggregates": {p: {"center": {"count": 0}} for p in PARTS},
            "accepted": False,
        }
    report["inputFailures"] = {"server": server_failures, "observer": observer_failures}
    if server_failures or observer_failures:
        report["accepted"] = False
    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf8"))
        report["baselineComparison"] = baseline_comparison(baseline, report)
    args.output.write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf8"
    )
    args.output.with_suffix(".md").write_text(markdown(report), encoding="utf8")
    return int(
        bool(
            server_failures
            or observer_failures
            or report["failures"]
            or report["nonBoxParts"]
            or not report["pairedFrames"]
            or report["alignment"] == "inconclusive"
            or report["schema"] == "blockfield.bodypose.comparison.v2"
            and not report["accepted"]
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
