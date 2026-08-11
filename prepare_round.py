#!/usr/bin/env python3
"""Make a round reproducible after re-extraction (B1).

Re-running extract_network.py regenerates edges.csv and silently wipes
every hand-edited `service` value and every edge split (E8).  This tool
keeps the annotation in two small declarative files inside the
gitignored round directory and replays them onto a fresh extraction:

    round.local/service_overrides.csv   edge_id,service,name
        -- the complete annotation snapshot (name is informational)
    round.local/splits.csv              at_lat,at_lon,edge_id
        -- every split point, replayed via split_edge.py in order
           (parents before children, so nested splits compose)

Workflow:

    python prepare_round.py --data data --round round.local --export
        snapshot the CURRENT data/edges.csv (run this BEFORE
        re-extracting -- e.g. before enlarging the polygon to cover a
        depot / handover office)

    python extract_network.py --polygon round.local/my_round.geojson \
        --default-service 0 --out data
        re-extract; with default 0, streets that are new to the
        enlarged area arrive as connectors, not as service

    python prepare_round.py --data data --round round.local
        (default mode) replay splits, then apply the overrides;
        edges without an override keep the extraction default --
        review them in the editor (they are the newly added streets)

Backups: edges.csv.bak / nodes.csv.bak are written before applying.
Caveat: edge_ids come from OSM way/node ids, which are stable unless
the underlying OSM data is re-mapped; stale override rows are reported,
never silently dropped.
"""

import argparse
import csv
import re
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).parent
NOTE_RE = re.compile(r"split from (\S+) at (-?[0-9.]+),(-?[0-9.]+)")


def read_edges(data_dir: Path):
    with open(data_dir / "edges.csv", newline="",
              encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames), list(reader)


def export(data_dir: Path, round_dir: Path):
    _, rows = read_edges(data_dir)
    round_dir.mkdir(parents=True, exist_ok=True)

    ov_path = round_dir / "service_overrides.csv"
    with open(ov_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["edge_id", "service", "name"])
        for r in rows:
            w.writerow([r["edge_id"], (r.get("service") or "").strip(),
                        r.get("name") or ""])

    splits = {}
    for r in rows:
        m = NOTE_RE.search(r.get("note") or "")
        if m and r["edge_id"].startswith(m.group(1) + "#"):
            splits[(m.group(1), m.group(2), m.group(3))] = None
    ordered = sorted(splits, key=lambda k: (k[0].count("#"), k[0]))
    sp_path = round_dir / "splits.csv"
    with open(sp_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["at_lat", "at_lon", "edge_id"])
        for src, lat, lon in ordered:
            w.writerow([lat, lon, src])

    print(f"Exported {len(rows)} service values -> {ov_path}")
    print(f"Exported {len(ordered)} split(s)      -> {sp_path}")
    print("Safe to re-extract now; apply with:  "
          f"python prepare_round.py --data {data_dir} --round {round_dir}")


def apply(data_dir: Path, round_dir: Path):
    ov_path = round_dir / "service_overrides.csv"
    sp_path = round_dir / "splits.csv"
    if not ov_path.exists():
        sys.exit(f"{ov_path} not found -- run --export first "
                 f"(before re-extracting).")

    shutil.copy2(data_dir / "edges.csv", data_dir / "edges.csv.bak")
    shutil.copy2(data_dir / "nodes.csv", data_dir / "nodes.csv.bak")

    n_split = n_split_failed = 0
    if sp_path.exists():
        with open(sp_path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                res = subprocess.run(
                    [sys.executable, str(BASE / "split_edge.py"),
                     "--data", str(data_dir),
                     f"--at={row['at_lat']},{row['at_lon']}",
                     "--edge", row["edge_id"]],
                    capture_output=True, text=True)
                if res.returncode == 0:
                    n_split += 1
                else:
                    n_split_failed += 1
                    print(f"  warning: could not replay split of "
                          f"{row['edge_id']} at {row['at_lat']},"
                          f"{row['at_lon']}:\n    "
                          f"{(res.stderr or res.stdout).strip()}")

    overrides = {}
    with open(ov_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            overrides[row["edge_id"]] = (row.get("service") or "").strip()

    header, rows = read_edges(data_dir)
    applied = 0
    for r in rows:
        if r["edge_id"] in overrides:
            r["service"] = overrides.pop(r["edge_id"])
            applied += 1
    fresh = len(rows) - applied
    with open(data_dir / "edges.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)

    print(f"\nApplied {applied} service overrides"
          + (f", replayed {n_split} split(s)" if n_split else "")
          + (f", {n_split_failed} split(s) FAILED" if n_split_failed
             else "") + ".")
    if fresh:
        print(f"{fresh} edges had no override (new streets from the "
              f"enlarged area) -- they keep the extraction default. "
              f"Review them in the editor (make_editor.py --serve).")
    if overrides:
        sample = ", ".join(list(overrides)[:5])
        print(f"warning: {len(overrides)} stale override(s) match no "
              f"edge in edges.csv (OSM re-mapped? renamed ids?): "
              f"{sample}{' ...' if len(overrides) > 5 else ''}")
    print("Backups: edges.csv.bak, nodes.csv.bak")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default="data",
                    help="directory with edges.csv + nodes.csv")
    ap.add_argument("--round", default="round.local", dest="round_dir",
                    help="directory holding the declarative round "
                         "config (gitignored)")
    ap.add_argument("--export", action="store_true",
                    help="snapshot the current annotation instead of "
                         "applying the stored one")
    args = ap.parse_args()
    if args.export:
        export(Path(args.data), Path(args.round_dir))
    else:
        apply(Path(args.data), Path(args.round_dir))


if __name__ == "__main__":
    main()
