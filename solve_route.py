#!/usr/bin/env python3
"""Solve a postal delivery round as a Rural Postman Problem.

Input:  a data directory containing edges.csv and nodes.csv produced by
        extract_network.py, AFTER you have edited the `service` column:

            2 = I deliver both sides  -> street must be traversed twice
            1 = I deliver one side only (or I zigzag both sides in a
                single pass)          -> street traversed once
            0 = not my street, but I may ride along it (connector)
            x = never use this edge   -> removed from the network
                entirely (not even as a deadhead shortcut)

Output: route.csv       ordered traversal list -- the basis for the new
                        sort sequence
        route_map.html  interactive route viewer: step through the walk
                        with a slider/play button, current segment
                        highlighted with a direction arrow
        console summary service km vs total km vs extra (deadhead) km

Algorithm (4 phases -- exact optimum, not a heuristic, whenever the
service streets form one connected piece):

  Phase 1  Build the full network F (all edges, including service=0)
           and the required multigraph R (each edge repeated `service`
           times).  F is the world you can ride through; R is the work
           you must do.

  Phase 2  Parity repair.  A single continuous route can only exist if
           every node of R has even degree (Euler, 1736).  Nodes with
           odd degree are exactly where a route is forced to break.
           Compute shortest paths between all odd nodes over F, then a
           minimum-weight perfect matching (blossom algorithm, via
           networkx) picks the cheapest set of extra connections.
           These connections are the ONLY extra distance in the final
           route -- everything else is mandatory work.

  Phase 3  R + connectors is now Eulerian: extract an Euler circuit
           (or an open Euler path with --open).

  Phase 4  Expand connectors back into real street segments and emit
           the ordered route.

Usage:
    python solve_route.py --data data --out result
    python solve_route.py --data data --out result --open
    python solve_route.py --data data --out result --start=-37.8406,144.9541
    python solve_route.py --data data --out result \
        --start=-37.8406,144.9541 --end=-37.8380,144.9520

--start + --end (SPEC-2): a zero-length virtual REQUIRED edge
end -> start is added, the ordinary closed circuit is solved, then the
circuit is rotated so the virtual edge is last and dropped -- what
remains is the jointly optimal open route start -> ... -> end.  The
endpoints are snapped to the full network F, so they may lie on
non-service streets (e.g. a depot or a parcel-handover office reached
via service-0 corridor streets); if they sit off the service
component, the auto-bridge connects them and the near-optimal warning
applies as usual.
"""

import argparse
import csv
import itertools
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import networkx as nx

try:
    from shapely import wkt as shapely_wkt
except ImportError:  # geometry is optional; straight lines still work
    shapely_wkt = None


# ----------------------------------------------------------------------
# Input
# ----------------------------------------------------------------------

def load_data(data_dir: Path):
    """Read nodes.csv and edges.csv. Node ids are kept as strings."""
    nodes = {}
    with open(data_dir / "nodes.csv", newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            nodes[row["node_id"]] = (float(row["lat"]), float(row["lon"]))

    edges, blank_service, excluded = [], 0, 0
    with open(data_dir / "edges.csv", newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            raw = (row.get("service") or "").strip()
            if raw == "":
                blank_service += 1
                service = 0
            elif raw.lower() == "x":
                excluded += 1
                continue          # excluded: not part of the graph at all
            else:
                try:
                    service = int(raw)
                except ValueError:
                    sys.exit(f"edges.csv: bad service value {raw!r} "
                             f"on edge {row['edge_id']} "
                             f"(must be 0, 1, 2 or x)")
            if service not in (0, 1, 2):
                sys.exit(f"edges.csv: service={service} on edge "
                         f"{row['edge_id']} (must be 0, 1, 2 or x)")
            edges.append({
                "edge_id": row["edge_id"],
                "u": row["u"],
                "v": row["v"],
                "name": row.get("name") or "(unnamed)",
                "length": float(row["length_m"]),
                "service": service,
                "wkt": row.get("geometry_wkt") or "",
            })
    if blank_service:
        print(f"  note: {blank_service} edges had a blank service value "
              f"-> treated as 0 (connector only)")
    if excluded:
        print(f"  note: {excluded} edges excluded (service=x) -> the "
              f"route will never use them, not even as shortcuts")
    return nodes, edges


# ----------------------------------------------------------------------
# Phase 1 -- graphs
# ----------------------------------------------------------------------

def build_graphs(edges):
    F = nx.MultiGraph()   # full rideable network
    R = nx.MultiGraph()   # required work (with multiplicities)
    for e in edges:
        F.add_edge(e["u"], e["v"], key=e["edge_id"],
                   length=e["length"], name=e["name"],
                   service=e["service"], wkt=e["wkt"])
        for p in range(1, e["service"] + 1):
            R.add_edge(e["u"], e["v"], key=f'{e["edge_id"]}|p{p}',
                       base=e["edge_id"], length=e["length"])
    return F, R


def ensure_required_connected(F, R, pinned_endpoints=False):
    """If the required work falls into several islands, bridge them with
    the cheapest paths through the full network. Prints a warning, since
    the result is then near-optimal rather than provably optimal.

    Growing from one component and always attaching the nearest other
    one is Prim's algorithm, so the bridges form a minimum spanning
    tree over component-to-component shortest paths -- optimal among
    tree-shaped connections, though not necessarily the RPP optimum
    (a Steiner-style branch off a bridge, or connectivity gained for
    free from the parity matching, can beat it)."""
    comps = list(nx.connected_components(R))
    bridges = 0
    bridge_len = 0.0
    while len(comps) > 1:
        base = comps[0]
        others = set().union(*comps[1:])
        dist, paths = nx.multi_source_dijkstra(F, sources=set(base),
                                               weight="length")
        reachable = [(d, n) for n, d in dist.items() if n in others]
        if not reachable:
            sys.exit("Service streets are disconnected and no rideable "
                     "edges link them. Extract a larger area, or check "
                     "that a linking street was not excluded (service=x).")
        d, target = min(reachable)
        path = paths[target]
        R.add_edge(path[0], path[-1], key=f"bridge|{bridges}",
                   nodepath=path, length=d)
        bridges += 1
        bridge_len += d
        comps = list(nx.connected_components(R))
    if bridges:
        islands = bridges + 1 - (1 if pinned_endpoints else 0)
        extra = " + the pinned start/end" if pinned_endpoints else ""
        print(f"  warning: required work formed {bridges + 1} components "
              f"({islands} service island(s){extra}); linked by "
              f"{bridges} bridge(s) totalling {bridge_len / 1000:.2f} km."
              f"\n           Bridging is a minimum spanning tree over the "
              f"components, so the route is near-optimal: only that "
              f"{bridge_len / 1000:.2f} km is not provably minimal."
              f"\n           Separate delivery areas are fine -- that "
              f"bridge is riding you would do anyway. Only fix islands "
              f"caused by annotation gaps (a short service=0 sliver "
              f"inside a street you do deliver); never mark a street "
              f"you do not deliver just to merge islands.")


# ----------------------------------------------------------------------
# Phase 2 -- parity repair
# ----------------------------------------------------------------------

def odd_nodes(R):
    return sorted(n for n in R.nodes if R.degree(n) % 2 == 1)


def pairwise_shortest(F, targets):
    """Shortest path lengths and node paths between all pairs of targets,
    measured over the FULL network (service-0 edges are fair game)."""
    dist, path = {}, {}
    tset = set(targets)
    for s in targets:
        d, p = nx.single_source_dijkstra(F, s, weight="length")
        for t in tset:
            if t != s:
                if t not in d:
                    sys.exit("Network is disconnected: some service "
                             "streets cannot reach each other at all. "
                             "Extract a larger area, or check service=x "
                             "exclusions.")
                dist[(s, t)] = d[t]
                path[(s, t)] = p[t]
    return dist, path


def min_matching(nodes_subset, dist):
    """Minimum-weight perfect matching on a complete graph over the odd
    nodes. This is the step that guarantees optimality."""
    if not nodes_subset:
        return [], 0.0
    K = nx.Graph()
    for a, b in itertools.combinations(nodes_subset, 2):
        K.add_edge(a, b, weight=dist[(a, b)])
    M = nx.min_weight_matching(K)
    cost = sum(dist[(a, b)] for a, b in M)
    return list(M), cost


def parity_repair(F, R, open_route, start_node):
    """Add matching connectors to R. Returns (endpoints, extra_length).
    endpoints is (s, t) for an open route, else None."""
    odd = odd_nodes(R)
    if not odd:
        return None, 0.0

    dist, path = pairwise_shortest(F, odd)

    def add_connectors(pairs):
        for i, (a, b) in enumerate(pairs):
            R.add_edge(a, b, key=f"match|{i}",
                       nodepath=path[(a, b)], length=dist[(a, b)])

    if not open_route:
        pairs, cost = min_matching(odd, dist)
        add_connectors(pairs)
        return None, cost

    # Open route: leave the best pair of odd nodes unmatched -- they
    # become the start and end. If a pin was given, fix one endpoint
    # to the odd node nearest to it (network distance over F).
    if len(odd) == 2:
        return tuple(odd), 0.0
    candidates = []
    if start_node is not None:
        if start_node in odd:
            s_fixed = start_node
        else:
            d, _ = nx.single_source_dijkstra(F, start_node,
                                             weight="length")
            s_fixed = min((n for n in odd if n in d),
                          key=lambda n: d[n], default=odd[0])
        pairs_to_try = [(s_fixed, t) for t in odd if t != s_fixed]
    else:
        pairs_to_try = list(itertools.combinations(odd, 2))
    for s, t in pairs_to_try:
        rest = [n for n in odd if n not in (s, t)]
        pairs, cost = min_matching(rest, dist)
        candidates.append((cost, (s, t), pairs))
    cost, endpoints, pairs = min(candidates, key=lambda c: c[0])
    add_connectors(pairs)
    return endpoints, cost


# ----------------------------------------------------------------------
# Phase 3 + 4 -- Euler traversal and expansion
# ----------------------------------------------------------------------

VIRTUAL_KEY = "virtual|endpin"


def deadhead_segments(F, node_path):
    """Expand a node path into deadhead segments, taking the cheapest
    real edge between each pair of adjacent nodes."""
    segs = []
    for x, y in zip(node_path, node_path[1:]):
        key, data = min(F[x][y].items(), key=lambda kv: kv[1]["length"])
        segs.append(dict(kind="deadhead", edge_id=key, u=x, v=y,
                         name=data["name"], length=data["length"],
                         wkt=data["wkt"], pass_label="-"))
    return segs


def traverse(F, R, nodes, endpoints, start_node, pin_start=None):
    """Walk the Euler circuit/path and expand connectors into real
    street segments. Returns a list of segment dicts.

    pin_start: set when a virtual end->start edge is in R (SPEC-2).
    The circuit is rotated so the virtual edge would be last, the edge
    is dropped, and the walk is oriented to begin at pin_start -- what
    remains is the open route start -> ... -> end."""
    if endpoints:
        source = endpoints[0]
        assert nx.has_eulerian_path(R, source=source)
        euler = list(nx.eulerian_path(R, source=source, keys=True))
    else:
        source = start_node if (start_node in R) else None
        assert nx.is_eulerian(R)
        euler = list(nx.eulerian_circuit(R, source=source, keys=True))

    if pin_start is not None:
        i = next(idx for idx, (u, v, k) in enumerate(euler)
                 if k == VIRTUAL_KEY)
        walk = euler[i + 1:] + euler[:i]
        if walk and walk[0][0] != pin_start:   # circuit hit the virtual
            walk = [(v, u, k) for u, v, k in reversed(walk)]  # edge the
        euler = walk                           # other way round: flip

    segments = []
    passes_seen = defaultdict(int)

    for u, v, key in euler:
        data = R[u][v][key]
        if "nodepath" in data:                       # connector -> expand
            np_ = data["nodepath"]
            if np_[0] != u:
                np_ = list(reversed(np_))
            segments.extend(deadhead_segments(F, np_))
        else:                                        # service pass
            base = data["base"]
            edata = F[u][v][base]
            passes_seen[base] += 1
            total = edata["service"]
            segments.append(dict(kind="service", edge_id=base,
                                 u=u, v=v, name=edata["name"],
                                 length=edata["length"], wkt=edata["wkt"],
                                 pass_label=f"{passes_seen[base]}/{total}"))
    return segments


# ----------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------

WINDS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def bearing_label(a, b):
    (lat1, lon1), (lat2, lon2) = a, b
    dy = lat2 - lat1
    dx = (lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    ang = math.degrees(math.atan2(dx, dy)) % 360
    return WINDS[round(ang / 45) % 8]


def cross_streets(F, node, current_name):
    names = {d["name"] for _, _, d in F.edges(node, data=True)}
    names.discard(current_name)
    if not names:
        return "(dead end)"
    return " / ".join(sorted(names)[:2])


def segment_coords(seg, nodes):
    """[(lat, lon), ...] for drawing, honouring curved geometry."""
    if seg["wkt"] and shapely_wkt is not None:
        try:
            line = shapely_wkt.loads(seg["wkt"])
            pts = [(y, x) for x, y in line.coords]
            # orient to travel direction
            if nodes[seg["u"]] and _closer(pts[-1], nodes[seg["u"]], pts[0]):
                pts = list(reversed(pts))
            return pts
        except Exception:
            pass
    return [nodes[seg["u"]], nodes[seg["v"]]]


def _closer(p, target, other):
    d1 = (p[0] - target[0]) ** 2 + (p[1] - target[1]) ** 2
    d2 = (other[0] - target[0]) ** 2 + (other[1] - target[1]) ** 2
    return d1 < d2


def write_route_csv(segments, F, nodes, out_dir: Path):
    path = out_dir / "route.csv"
    cum = 0.0
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["seq", "type", "street", "pass", "direction",
                    "from_cross", "to_cross", "length_m", "cum_km",
                    "edge_id"])
        for i, s in enumerate(segments, 1):
            cum += s["length"]
            w.writerow([
                i, s["kind"], s["name"], s["pass_label"],
                bearing_label(nodes[s["u"]], nodes[s["v"]]),
                cross_streets(F, s["u"], s["name"]),
                cross_streets(F, s["v"], s["name"]),
                round(s["length"], 1), round(cum / 1000, 3), s["edge_id"],
            ])
    return path


ROUTE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>route viewer</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body { height: 100%; margin: 0; }
  #map { height: 100%; }
  #panel {
    position: absolute; top: 10px; left: 50px; z-index: 1000;
    background: rgba(255,255,255,.96); border: 1px solid #bbb;
    border-radius: 6px; padding: 10px 12px; width: 340px;
    font: 13px/1.5 system-ui, sans-serif; box-shadow: 0 1px 4px rgba(0,0,0,.2);
  }
  #panel b { font-size: 14px; }
  .row { margin-top: 6px; }
  #slider { width: 100%; }
  #info { min-height: 3em; }
  button { cursor: pointer; }
  .sw { display: inline-block; width: 16px; height: 4px;
        vertical-align: middle; margin-right: 3px; }
  #hint { color: #666; margin-top: 6px; }
</style>
</head>
<body>
<div id="map"></div>
<div id="panel">
  <b>route viewer</b>
  <div id="totals"></div>
  <div class="row">
    <button id="prev">&#9664;</button>
    <button id="play">play</button>
    <button id="next">&#9654;</button>
    <label style="margin-left:8px"><input type="checkbox" id="follow">
      follow</label>
  </div>
  <input type="range" id="slider" min="1" value="1">
  <div class="row" id="info"></div>
  <div class="row">
    <span class="sw" style="background:#2b6cb0"></span>service
    <span class="sw" style="background:#dd6b20;margin-left:8px"></span>deadhead
    <span class="sw" style="background:#e53e3e;margin-left:8px"></span>current
    <span class="sw" style="background:#b8c2cc;margin-left:8px"></span>not yet
  </div>
  <div id="hint">drag the slider or press &larr;/&rarr; to walk the
    route step by step; hover any line for its step number</div>
</div>
<script id="route-data" type="application/json">__PAYLOAD__</script>
<script>
"use strict";
const P = JSON.parse(document.getElementById("route-data").textContent);
const S = P.segs;
const N = S.length;

const renderer = L.canvas({tolerance: 5});
const map = L.map("map", {renderer: renderer, maxZoom: 22});
L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
  {maxZoom: 22, maxNativeZoom: 20,
   attribution: "&copy; OpenStreetMap contributors &copy; CARTO"}).addTo(map);

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
                  .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

const UNVISITED = {color: "#b8c2cc", weight: 2, opacity: 0.55};
function visitedStyle(s) {
  return s.k === "s"
    ? {color: "#2b6cb0", weight: 4, opacity: 0.9, dashArray: null}
    : {color: "#dd6b20", weight: 3, opacity: 0.9, dashArray: "5 7"};
}

const lines = S.map(function (s, i) {
  const pl = L.polyline(s.c, UNVISITED).addTo(map);
  pl.bindTooltip("#" + (i + 1) + " " + esc(s.n) + " &middot; " +
    (s.k === "s" ? "pass " + esc(s.p) : "deadhead") +
    " &middot; " + s.l + " m", {sticky: true});
  return pl;
});
map.fitBounds(L.featureGroup(lines).getBounds());

const hl = L.polyline(S[0].c,
  {color: "#e53e3e", weight: 7, opacity: 1}).addTo(map);
const arrow = L.marker(S[0].c[S[0].c.length - 1],
  {interactive: false, icon: L.divIcon({className: "", html: ""})})
  .addTo(map);

const first = S[0].c[0];
const last = S[N - 1].c[S[N - 1].c.length - 1];
const closed = Math.abs(first[0] - last[0]) < 1e-7 &&
               Math.abs(first[1] - last[1]) < 1e-7;
if (closed) {
  L.circleMarker(first, {radius: 8, color: "#2f855a", fillColor: "#48bb78",
    fillOpacity: 1}).addTo(map).bindTooltip("START = END (circuit)");
} else {
  L.circleMarker(first, {radius: 8, color: "#2f855a", fillColor: "#48bb78",
    fillOpacity: 1}).addTo(map).bindTooltip("START");
  L.circleMarker(last, {radius: 8, color: "#9b2c2c", fillColor: "#f56565",
    fillOpacity: 1}).addTo(map).bindTooltip("END");
}

document.getElementById("totals").innerHTML =
  "service <b>" + P.service_km.toFixed(2) + " km</b> &middot; total <b>" +
  P.total_km.toFixed(2) + " km</b> &middot; deadhead <b>" +
  (P.total_km - P.service_km).toFixed(2) + " km</b>";

function arrowAngle(c) {
  const a = c[c.length - 2], b = c[c.length - 1];
  const dy = b[0] - a[0];
  const dx = (b[1] - a[1]) *
    Math.cos((a[0] + b[0]) / 2 * Math.PI / 180);
  return Math.atan2(dx, dy) * 180 / Math.PI;
}

let cur = 1;
const slider = document.getElementById("slider");
slider.max = N;

function setStep(k) {
  cur = Math.max(1, Math.min(N, k));
  for (let i = 0; i < N; i++) {
    lines[i].setStyle(i < cur ? visitedStyle(S[i]) : UNVISITED);
  }
  const s = S[cur - 1];
  hl.setLatLngs(s.c);
  const end = s.c[s.c.length - 1];
  arrow.setLatLng(end);
  arrow.setIcon(L.divIcon({className: "", iconSize: [24, 24],
    iconAnchor: [12, 12],
    html: '<div style="transform:rotate(' + arrowAngle(s.c).toFixed(0) +
          'deg);font-size:20px;line-height:24px;text-align:center;' +
          'color:#e53e3e;text-shadow:0 0 3px #fff">&#9650;</div>'}));
  document.getElementById("info").innerHTML =
    "<b>#" + cur + "/" + N + "</b> &middot; " + esc(s.n) + "<br>" +
    (s.k === "s" ? "service pass " + esc(s.p) : "deadhead (connector)") +
    " &middot; " + s.l + " m &middot; heading " + s.dir +
    " &middot; cum " + s.cum.toFixed(3) + " km";
  slider.value = cur;
  if (document.getElementById("follow").checked) map.panTo(end);
}

slider.addEventListener("input", function () { setStep(+this.value); });
document.getElementById("prev").onclick = function () { setStep(cur - 1); };
document.getElementById("next").onclick = function () { setStep(cur + 1); };

let timer = null;
document.getElementById("play").onclick = function () {
  if (timer) {
    clearInterval(timer);
    timer = null;
    this.textContent = "play";
    return;
  }
  if (cur >= N) cur = 0;
  this.textContent = "pause";
  timer = setInterval(function () {
    if (cur >= N) {
      clearInterval(timer);
      timer = null;
      document.getElementById("play").textContent = "play";
      return;
    }
    setStep(cur + 1);
  }, 350);
};
document.addEventListener("keydown", function (ev) {
  if (ev.target.tagName === "INPUT") return;
  if (ev.key === "ArrowLeft") { setStep(cur - 1); ev.preventDefault(); }
  if (ev.key === "ArrowRight") { setStep(cur + 1); ev.preventDefault(); }
});

setStep(1);
</script>
</body>
</html>
"""


def write_map(segments, nodes, out_dir: Path):
    """Self-contained interactive viewer: slider/play steps through the
    walk, the current segment is highlighted with a direction arrow."""
    segs, cum = [], 0.0
    for s in segments:
        coords = segment_coords(s, nodes)
        cum += s["length"]
        segs.append({
            "n": s["name"],
            "k": "s" if s["kind"] == "service" else "d",
            "p": s["pass_label"],
            "l": round(s["length"], 1),
            "cum": round(cum / 1000, 3),
            "dir": bearing_label(nodes[s["u"]], nodes[s["v"]]),
            "c": [[round(lat, 6), round(lon, 6)] for lat, lon in coords],
        })
    service = sum(s["length"] for s in segments if s["kind"] == "service")
    payload = json.dumps(
        {"segs": segs,
         "service_km": round(service / 1000, 2),
         "total_km": round(cum / 1000, 2)},
        ensure_ascii=False).replace("</", "<\\/")
    path = out_dir / "route_map.html"
    path.write_text(ROUTE_TEMPLATE.replace("__PAYLOAD__", payload),
                    encoding="utf-8")
    return path


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def snap_to_node(latlon_str, candidates, nodes):
    lat, lon = (float(x) for x in latlon_str.split(","))
    return min(candidates,
               key=lambda n: (nodes[n][0] - lat) ** 2 +
                             (nodes[n][1] - lon) ** 2)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default="data",
                    help="directory with edges.csv + nodes.csv")
    ap.add_argument("--out", default="result", help="output directory")
    ap.add_argument("--open", action="store_true",
                    help="allow start != end (open Euler path); the solver "
                         "picks the endpoint pair that minimises total "
                         "distance")
    ap.add_argument("--start", metavar="LAT,LON",
                    help="preferred start point, snapped to the nearest "
                         "service-street node. Southern-hemisphere "
                         "latitudes are negative, so use the equals "
                         "form: --start=-37.8406,144.9541")
    ap.add_argument("--end", metavar="LAT,LON",
                    help="pin the finish point (e.g. a parcel-handover "
                         "office). With --start: jointly optimal open "
                         "route start -> ... -> end, both snapped to "
                         "the full network (they may lie on service-0 "
                         "streets). Without --start: open route whose "
                         "end is pinned, start chosen optimally. "
                         "Mutually exclusive with --open. Equals form "
                         "for southern latitudes: --end=-37.84,144.95")
    ap.add_argument("--return-to-start", action="store_true",
                    help="after the pinned end, ride the shortest path "
                         "back to the start and include it in the "
                         "route -- a closed tour start -> service -> "
                         "end -> start (e.g. depot -> round -> "
                         "handover office -> depot). Requires --end.")
    args = ap.parse_args()
    if args.end and args.open:
        ap.error("--end is mutually exclusive with --open "
                 "(an --end route is already open)")
    if args.return_to_start and not args.end:
        ap.error("--return-to-start requires --end")

    data_dir, out_dir = Path(args.data), Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Endpoints picked in the editor (data/endpoints.json) are used when
    # no --start/--end was given, so the browser flow needs no flags.
    if not args.start and not args.end:
        ep_file = data_dir / "endpoints.json"
        if ep_file.exists():
            try:
                ep = json.loads(ep_file.read_text(encoding="utf-8"))
            except ValueError:
                ep = {}
            if ep.get("start"):
                args.start = "{},{}".format(*ep["start"])
            if ep.get("end"):
                args.end = "{},{}".format(*ep["end"])
                args.return_to_start = (args.return_to_start or
                                        bool(ep.get("return_to_start")))
            if args.start or args.end:
                print(f"  using endpoints from {ep_file} "
                      f"(set in the editor)")

    print("Loading network ...")
    nodes, edges = load_data(data_dir)
    F, R = build_graphs(edges)
    n_service_edges = sum(1 for e in edges if e["service"] > 0)
    if n_service_edges == 0:
        sys.exit("No edges have service 1 or 2 -- nothing to route. "
                 "Edit the service column in edges.csv first.")
    service_len = sum(e["length"] * e["service"] for e in edges)
    print(f"  {len(edges)} edges total | {n_service_edges} service streets "
          f"| mandatory riding: {service_len / 1000:.2f} km")

    pin_start = pin_end = None
    if args.end and args.start:
        # SPEC-2: zero-length virtual required edge end -> start; the
        # Euler circuit is later rotated around it (see traverse).
        pin_end = snap_to_node(args.end, list(F.nodes), nodes)
        pin_start = snap_to_node(args.start, list(F.nodes), nodes)
        print(f"  start snapped to node {pin_start} at {nodes[pin_start]}")
        print(f"  end snapped to node {pin_end} at {nodes[pin_end]}")
        R.add_edge(pin_end, pin_start, key=VIRTUAL_KEY, length=0.0)

    ensure_required_connected(F, R, pinned_endpoints=pin_start is not None)

    start_node = None
    if args.start and pin_start is None:
        start_node = snap_to_node(args.start, list(R.nodes), nodes)
        print(f"  start snapped to node {start_node} "
              f"at {nodes[start_node]}")
    if args.end and pin_end is None:
        pin_end = snap_to_node(args.end, list(R.nodes), nodes)
        print(f"  end snapped to node {pin_end} at {nodes[pin_end]}")

    n_odd = len(odd_nodes(R))
    print(f"Parity repair: {n_odd} odd-degree nodes to match ...")
    print("Extracting Euler traversal ...")
    if pin_start is not None:                        # --start + --end
        endpoints, extra = parity_repair(F, R, False, None)
        segments = traverse(F, R, nodes, None, None, pin_start=pin_start)
        kind = "open path, start & end pinned"
    elif pin_end is not None:                        # --end only
        endpoints, extra = parity_repair(F, R, True, pin_end)
        if endpoints:
            # orient the walk so it ENDS at the pin (or, if parity
            # forces both endpoints, at the forced endpoint nearest it)
            e_pin = snap_to_node(args.end, list(endpoints), nodes)
            endpoints = tuple(n for n in endpoints if n != e_pin) \
                + (e_pin,)
            segments = traverse(F, R, nodes, endpoints, None)
            kind = "open path, end pinned"
        else:  # no odd nodes: the route is a circuit; close it there
            segments = traverse(F, R, nodes, None, pin_end)
            kind = "closed circuit (starts and ends at --end)"
    else:
        endpoints, extra = parity_repair(F, R, args.open, start_node)
        segments = traverse(F, R, nodes, endpoints, start_node)
        kind = "open path" if endpoints else "closed circuit"

    if args.return_to_start:
        last, first = segments[-1]["v"], segments[0]["u"]
        if last != first:
            try:
                path = nx.shortest_path(F, last, first, weight="length")
            except nx.NetworkXNoPath:
                sys.exit("--return-to-start: no path from the end back "
                         "to the start exists in the network.")
            tail = deadhead_segments(F, path)
            segments.extend(tail)
            print(f"  return leg after the end: "
                  f"{sum(s['length'] for s in tail) / 1000:.2f} km "
                  f"back to the start")
            kind += " + return to start"

    total = sum(s["length"] for s in segments)
    deadhead = total - service_len
    route_csv = write_route_csv(segments, F, nodes, out_dir)
    route_map = write_map(segments, nodes, out_dir)
    print(f"""
================ RESULT ({kind}) ================
Mandatory service riding : {service_len / 1000:7.2f} km   (theoretical lower bound)
Route total              : {total / 1000:7.2f} km
Extra / deadhead         : {deadhead / 1000:7.2f} km   ({100 * deadhead / max(total, 1):.1f}% of route)
Segments                 : {sum(1 for s in segments if s['kind'] == 'service')} service + \
{sum(1 for s in segments if s['kind'] == 'deadhead')} deadhead
Outputs                  : {route_csv}
                           {route_map}
=================================================
The 'extra' number is the only part optimisation can influence --
the mandatory part is your job itself.""")


if __name__ == "__main__":
    main()
