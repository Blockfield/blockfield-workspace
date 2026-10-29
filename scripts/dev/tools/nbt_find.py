#!/usr/bin/env python3
"""Поиск блоков по id в мире Anvil без зависимостей (для карт-эталонов и стенда).
   just --command python scripts/dev/tools/nbt_find.py <worlddir> <regex-по-id> [макс=50] -> "x y z id props"
   BE=<regex> just --command python scripts/dev/tools/nbt_find.py <worlddir> be          -> блок-энтити с их NBT
   just --command python scripts/dev/tools/nbt_find.py <worlddir> palette [regex]       -> сводка id по region/entities
   just --command python scripts/dev/tools/nbt_find.py <worlddir> near x y z [r=4]       -> все блоки в кубе вокруг точки
Пример: just --command python scripts/dev/tools/nbt_find.py ~/.cache/bf-test-server/maps/konstaxa 'create:(train_door|framed_glass_pane)'"""

import sys
import os
import zlib
import struct
import re
import collections


def rd(b, o, t):
    if t == 1:
        return b[o], o + 1
    if t == 2:
        return struct.unpack_from(">h", b, o)[0], o + 2
    if t == 3:
        return struct.unpack_from(">i", b, o)[0], o + 4
    if t == 4:
        return struct.unpack_from(">q", b, o)[0], o + 8
    if t == 5:
        return struct.unpack_from(">f", b, o)[0], o + 4
    if t == 6:
        return struct.unpack_from(">d", b, o)[0], o + 8
    if t == 7:
        n = struct.unpack_from(">i", b, o)[0]
        return b[o + 4 : o + 4 + n], o + 4 + n
    if t == 8:
        n = struct.unpack_from(">H", b, o)[0]
        return b[o + 2 : o + 2 + n].decode("utf8", "replace"), o + 2 + n
    if t == 9:
        lt = b[o]
        n = struct.unpack_from(">i", b, o + 1)[0]
        o += 5
        out = []
        for _ in range(n):
            v, o = rd(b, o, lt)
            out.append(v)
        return out, o
    if t == 10:
        d = {}
        while True:
            t2 = b[o]
            o += 1
            if t2 == 0:
                return d, o
            n = struct.unpack_from(">H", b, o)[0]
            k = b[o + 2 : o + 2 + n].decode("utf8", "replace")
            o += 2 + n
            v, o = rd(b, o, t2)
            d[k] = v
    if t == 11:
        n = struct.unpack_from(">i", b, o)[0]
        return struct.unpack_from(">%di" % n, b, o + 4), o + 4 + 4 * n
    if t == 12:
        n = struct.unpack_from(">i", b, o)[0]
        return struct.unpack_from(">%dq" % n, b, o + 4), o + 4 + 8 * n
    raise ValueError(t)


def chunks(path):
    data = open(path, "rb").read()
    for i in range(1024):
        off = int.from_bytes(data[i * 4 : i * 4 + 3], "big") * 4096
        if not off:
            continue
        ln = int.from_bytes(data[off : off + 4], "big")
        ct = data[off + 4]
        raw = data[off + 5 : off + 4 + ln]
        try:
            yield rd(zlib.decompress(raw) if ct == 2 else raw, 3, 10)[0]
        except Exception:
            pass


def region_chunks(root, sub="region"):
    d = os.path.join(root, sub)
    if not os.path.isdir(d):
        return
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".mca"):
            yield from chunks(os.path.join(d, fn))


def find(root, pat, mx):
    found = 0
    for c in region_chunks(root):
        cx, cz = c.get("xPos", 0), c.get("zPos", 0)
        for sec in c.get("sections", []):
            bs = sec.get("block_states")
            if not bs:
                continue
            pal = bs["palette"]
            hits = [i for i, p in enumerate(pal) if pat.search(p["Name"])]
            if not hits:
                continue
            if "data" not in bs:
                print(
                    f"whole-section {pal[0]['Name']} chunk {cx},{cz} y{sec['Y'] * 16}"
                )
                continue
            bits = max(4, (len(pal) - 1).bit_length())
            per = 64 // bits
            mask = (1 << bits) - 1
            data = bs["data"]
            for idx in range(4096):
                w = data[idx // per] & 0xFFFFFFFFFFFFFFFF
                v = (w >> ((idx % per) * bits)) & mask
                if v in hits:
                    y = idx >> 8
                    z = (idx >> 4) & 15
                    x = idx & 15
                    p = pal[v]
                    props = ",".join(
                        f"{k}={v2}" for k, v2 in p.get("Properties", {}).items()
                    )
                    print(
                        f"{cx * 16 + x} {sec['Y'] * 16 + y} {cz * 16 + z} {p['Name']} {props}"
                    )
                    found += 1
                    if found >= mx:
                        return


def block_entities(root, pat):
    for c in region_chunks(root):
        for be in c.get("block_entities", []):
            if pat.search(be.get("id", "")):
                print(
                    be.get("x"),
                    be.get("y"),
                    be.get("z"),
                    be.get("id"),
                    {k: v for k, v in be.items() if k not in ("x", "y", "z", "id")},
                )


def palette(root, pat):
    idre = re.compile(rb"([a-z0-9_.-]+:[a-z0-9_./-]+)")
    for sub in ("region", "entities"):
        cnt = collections.Counter()
        d = os.path.join(root, sub)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if not fn.endswith(".mca"):
                continue
            data = open(os.path.join(d, fn), "rb").read()
            for i in range(1024):
                off = int.from_bytes(data[i * 4 : i * 4 + 3], "big") * 4096
                if not off:
                    continue
                ln = int.from_bytes(data[off : off + 4], "big")
                raw = data[off + 5 : off + 4 + ln]
                try:
                    raw = zlib.decompress(raw)
                except Exception:
                    continue
                for m in {x.decode() for x in idre.findall(raw)}:
                    if pat is None or pat.search(m):
                        cnt[m] += 1
        print(f"## {root} / {sub}")
        for k, v in sorted(cnt.items()):
            print(f"  {v:5d} {k}")


def near(root, cx, cy, cz, r):
    for c in region_chunks(root):
        bx, bz = c.get("xPos", 0) * 16, c.get("zPos", 0) * 16
        if bx + 15 < cx - r or bx > cx + r or bz + 15 < cz - r or bz > cz + r:
            continue
        for sec in c.get("sections", []):
            bs = sec.get("block_states")
            by = sec["Y"] * 16
            if not bs or by + 15 < cy - r or by > cy + r or "data" not in bs:
                continue
            pal = bs["palette"]
            bits = max(4, (len(pal) - 1).bit_length())
            per = 64 // bits
            mask = (1 << bits) - 1
            data = bs["data"]
            for idx in range(4096):
                x = bx + (idx & 15)
                y = by + (idx >> 8)
                z = bz + ((idx >> 4) & 15)
                if abs(x - cx) > r or abs(y - cy) > r or abs(z - cz) > r:
                    continue
                v = (data[idx // per] >> ((idx % per) * bits)) & mask
                p = pal[v]
                if p["Name"] != "minecraft:air":
                    print(
                        x,
                        y,
                        z,
                        p["Name"],
                        ",".join(
                            f"{k}={v2}" for k, v2 in p.get("Properties", {}).items()
                        ),
                    )


if __name__ == "__main__":
    root, mode = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "palette"
    if mode == "be":
        block_entities(root, re.compile(os.environ.get("BE", ".")))
    elif mode == "near":
        near(
            root, *map(int, sys.argv[3:6]), int(sys.argv[6]) if len(sys.argv) > 6 else 4
        )
    elif mode == "palette":
        palette(root, re.compile(sys.argv[3]) if len(sys.argv) > 3 else None)
    else:
        find(root, re.compile(mode), int(sys.argv[3]) if len(sys.argv) > 3 else 50)
