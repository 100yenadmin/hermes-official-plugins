#!/usr/bin/env python3
"""Print what one or more GLB files actually hold (stdlib only, no Blender).

    python3 glb_stats.py model.glb [more.glb ...]

One JSON line per file: {"path", "triangles", "vertices", "meshes", "primitives", "textures", "skins",
"joints", "clips", "bytes"}. Counts every triangle primitive (mode 4) of every mesh, once per mesh (not per
instance). ``skins``/``joints`` > 0 means the mesh is bound to a skeleton; ``clips`` names every animation.
"""
from __future__ import annotations

import json
import os
import struct
import sys


def stats(path: str) -> dict:
    with open(path, "rb") as fh:
        head = fh.read(20)
        if len(head) < 20 or head[:4] != b"glTF":
            raise ValueError("not a binary glTF (.glb) file")
        (json_len,) = struct.unpack("<I", head[12:16])
        doc = json.loads(fh.read(json_len))
    accessors = doc.get("accessors", [])
    tris = verts = prims = 0
    for mesh in doc.get("meshes", []):
        for prim in mesh.get("primitives", []):
            prims += 1
            n_pos = accessors[prim["attributes"]["POSITION"]]["count"]
            verts += n_pos
            mode = prim.get("mode", 4)
            n_idx = accessors[prim["indices"]]["count"] if "indices" in prim else n_pos
            if mode == 4:
                tris += n_idx // 3
            elif mode in (5, 6):  # strip / fan
                tris += max(n_idx - 2, 0)
    skins = doc.get("skins", [])
    clips = [a.get("name") or f"clip {i + 1}" for i, a in enumerate(doc.get("animations", []))]
    return {"path": path, "triangles": tris, "vertices": verts, "meshes": len(doc.get("meshes", [])),
            "primitives": prims, "textures": len(doc.get("images", [])), "skins": len(skins),
            "joints": len({j for s in skins for j in s.get("joints", [])}), "clips": clips,
            "bytes": os.path.getsize(path)}


def main(argv: list[str]) -> int:
    if not argv:
        print((__doc__ or "").strip(), file=sys.stderr)
        return 2
    rc = 0
    for p in argv:
        try:
            print(json.dumps(stats(p)))
        except (OSError, ValueError, KeyError, IndexError) as exc:
            print(json.dumps({"path": p, "error": str(exc)}))
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
