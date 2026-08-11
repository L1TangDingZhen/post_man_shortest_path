#!/usr/bin/env python3
"""Split a street segment at an arbitrary point (SPEC-1).

A delivery boundary that falls mid-block (a house-number cutoff between
two intersections) cannot be expressed with whole-segment service
values.  Splitting the edge at the boundary creates two child edges
that can carry different values (typically the outer child gets 0).

Usage:
    python split_edge.py --data data --at=-37.8450,144.9505
    python split_edge.py --data data --at=<lat,lon> --edge EDGE_ID

--at is typically obtained by right-clicking the boundary property on
an online map and copying the coordinates (note the equals form --
southern latitudes are negative).  --edge restricts the search when
two streets run close together (e.g. divided arterials).

Behaviour:
  * nearest candidate edge farther than ~30 m -> abort with a non-zero
    exit (probably a mis-click)
  * projection within ~8 m of either endpoint -> print that the
    boundary coincides with that intersection, change nothing, exit 0
  * otherwise the parent row is replaced by two children  <id>#a
    (u side) and <id>#b (v side)  joined at a new synthetic node s<n>;
    child lengths are recomputed by haversine along their coordinate
    chains; name / highway / service and all other columns are
    inherited; `note` records the split.

Geometry is projected in a local equirectangular frame (metres), so
the maths is not skewed by the lon/lat aspect ratio; no shapely
needed.  Repeated invocations compose -- a child is just another
splittable edge.
"""

import argparse
import csv
import math
import re
import sys
from pathlib import Path

MAX_DIST_M = 30.0   # farther than this from every edge = mis-click
SNAP_M = 8.0        # closer than this to an endpoint = no split needed
R_EARTH = 6371000.0


def haversine(a, b):
    """Metres between two (lat, lon) points."""
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((la2 - la1) / 2) ** 2 +
         math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * R_EARTH * math.asin(math.sqrt(h))


def chain_length(coords):
    return sum(haversine(a, b) for a, b in zip(coords, coords[1:]))


def parse_wkt_linestring(wkt):
    """'LINESTRING (x y, x y, ...)' -> [(lat, lon), ...] or None."""
    m = re.match(r"\s*LINESTRING\s*\((.+)\)\s*$", wkt, re.IGNORECASE)
    if not m:
        return None
    pts = []
    for pair in m.group(1).split(","):
        xy = pair.split()
        if len(xy) != 2:
            return None
        try:
            pts.append((float(xy[1]), float(xy[0])))  # WKT is lon lat
        except ValueError:
            return None
    return pts if len(pts) >= 2 else None


def oriented_coords(row, nodes):
    """Edge geometry as [(lat, lon), ...] running u -> v (E9)."""
    coords = parse_wkt_linestring(row.get("geometry_wkt") or "")
    if coords is None:
        return [nodes[row["u"]], nodes[row["v"]]]
    u = nodes[row["u"]]

    def sqd(p, q):
        return (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2

    if sqd(coords[-1], u) < sqd(coords[0], u):
        coords = list(reversed(coords))
    return coords


def project_onto_chain(coords, pt):
    """Project pt onto the polyline in a local metre frame.

    Returns (dist_m, d_along_m, total_m, seg_index, cut_latlon)."""
    mlat = 111132.0
    mlon = 111320.0 * math.cos(math.radians(pt[0]))

    def xy(c):
        return ((c[1] - pt[1]) * mlon, (c[0] - pt[0]) * mlat)

    seg_len = [haversine(a, b) for a, b in zip(coords, coords[1:])]
    cum = [0.0]
    for L in seg_len:
        cum.append(cum[-1] + L)

    best = None
    for i, (a, b) in enumerate(zip(coords, coords[1:])):
        (ax, ay), (bx, by) = xy(a), xy(b)
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        t = 0.0 if l2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / l2))
        px, py = ax + t * dx, ay + t * dy
        dist = math.hypot(px, py)          # pt is the local origin
        if best is None or dist < best[0]:
            cut = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
            best = (dist, cum[i] + t * seg_len[i], i, cut)
    dist, d_along, i, cut = best
    return dist, d_along, cum[-1], i, cut


def next_synthetic_id(nodes):
    n = 0
    for node_id in nodes:
        m = re.fullmatch(r"s(\d+)", node_id)
        if m:
            n = max(n, int(m.group(1)))
    return f"s{n + 1}"


def dedupe(coords):
    out = [coords[0]]
    for c in coords[1:]:
        if c != out[-1]:
            out.append(c)
    return out


def to_wkt(coords):
    return ("LINESTRING (" +
            ", ".join(f"{lon} {lat}" for lat, lon in coords) + ")")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default="data",
                    help="directory with edges.csv + nodes.csv")
    ap.add_argument("--at", required=True, metavar="LAT,LON",
                    help="boundary point; use the equals form for "
                         "southern latitudes: --at=-37.845,144.9505")
    ap.add_argument("--edge", help="restrict the search to this edge_id "
                                   "(useful for divided arterials)")
    args = ap.parse_args()
    data_dir = Path(args.data)

    try:
        lat, lon = (float(x) for x in args.at.split(","))
    except ValueError:
        sys.exit(f"--at: cannot parse {args.at!r} as lat,lon")
    pt = (lat, lon)

    nodes = {}
    with open(data_dir / "nodes.csv", newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            nodes[row["node_id"]] = (float(row["lat"]), float(row["lon"]))
    with open(data_dir / "edges.csv", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = list(reader.fieldnames)
        rows = list(reader)

    if args.edge:
        candidates = [r for r in rows if r["edge_id"] == args.edge]
        if not candidates:
            sys.exit(f"edge id {args.edge!r} not found in edges.csv")
    else:
        candidates = rows

    best = None
    for r in candidates:
        coords = oriented_coords(r, nodes)
        dist, d_along, total, i, cut = project_onto_chain(coords, pt)
        if best is None or dist < best[0]:
            best = (dist, d_along, total, i, cut, r, coords)
    dist, d_along, total, seg_i, cut, row, coords = best

    if dist > MAX_DIST_M:
        sys.exit(f"nearest edge ({row['edge_id']}, {row.get('name', '?')}) "
                 f"is {dist:.0f} m from --at (> {MAX_DIST_M:.0f} m) -- "
                 f"probably a mis-click. Nothing changed.")

    if d_along < SNAP_M or total - d_along < SNAP_M:
        node = row["u"] if d_along < SNAP_M else row["v"]
        print(f"Boundary coincides with intersection node {node} "
              f"({d_along if d_along < SNAP_M else total - d_along:.1f} m "
              f"from it) -- no split needed. Whole-segment service "
              f"values already express this cutoff.")
        return

    s_id = next_synthetic_id(nodes)
    cut = (round(cut[0], 7), round(cut[1], 7))
    chain_a = dedupe(coords[:seg_i + 1] + [cut])
    chain_b = dedupe([cut] + coords[seg_i + 1:])

    child_a = dict(row)
    child_b = dict(row)
    note = f"split from {row['edge_id']} at {lat},{lon}"
    child_a.update(edge_id=row["edge_id"] + "#a", u=row["u"], v=s_id,
                   length_m=round(chain_length(chain_a), 1),
                   geometry_wkt=to_wkt(chain_a), note=note)
    child_b.update(edge_id=row["edge_id"] + "#b", u=s_id, v=row["v"],
                   length_m=round(chain_length(chain_b), 1),
                   geometry_wkt=to_wkt(chain_b), note=note)

    new_rows = []
    for r in rows:
        if r is row:
            new_rows.extend([child_a, child_b])
        else:
            new_rows.append(r)
    with open(data_dir / "edges.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(new_rows)
    # append (no BOM in append mode -- the file already starts with one)
    with open(data_dir / "nodes.csv", "a", newline="",
              encoding="utf-8") as f:
        csv.writer(f).writerow([s_id, cut[0], cut[1]])

    print(f"""Split {row['edge_id']} ({row.get('name', '?')}, {total:.1f} m) \
at {d_along:.1f} m from node {row['u']}:

  {child_a['edge_id']}   {row['u']} -> {s_id}   {child_a['length_m']} m
  {child_b['edge_id']}   {s_id} -> {row['v']}   {child_b['length_m']} m

New node {s_id} at {cut[0]},{cut[1]} appended to nodes.csv.
Both children inherited service={row.get('service', '')!s}. Now set the
child OUTSIDE your territory to 0 (or x) -- e.g. in the editor
(make_editor.py --serve) or directly in edges.csv.""")


if __name__ == "__main__":
    main()
