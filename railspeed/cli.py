"""Command line: fetch data, build the speed tables and web page, inspect files."""
import argparse
import datetime as dt
import json
import os
import shutil
import sys
import time

from . import analyse, fetch, network, schedule

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def cmd_fetch(args):
    os.makedirs(args.data, exist_ok=True)
    for name in args.what:
        fetch.download(name, args.data)


def cmd_build(args):
    t0 = time.time()
    start = dt.date.fromisoformat(args.date) if args.date else _next_monday()
    dates = [start + dt.timedelta(days=i) for i in range(args.days)]
    print(f"Analysing {dates[0]} .. {dates[-1]}")

    naptan_path = os.path.join(args.data, "naptan_rail.csv")
    naptan = network.load_naptan(naptan_path) if os.path.exists(naptan_path) else {}
    print(f"  NaPTAN: {len(naptan)} stations with coordinates")

    net = network.Network()
    tps = args.tps or (None if args.bplan else _first_existing(args.data, ["tps.tar.bz2"]))
    bplan = args.bplan or _first_existing(args.data, ["bplan.txt", "BPLAN.txt"])
    if tps:
        print(f"Loading network model {tps}")
        net.load_tps(tps, on_date=start)
    elif bplan:
        print(f"Loading network model {bplan}")
        net.load_bplan(bplan, on_date=start, naptan={k: v[:2] for k, v in naptan.items()})
    else:
        print("  No network model: route miles fall back to summed straight lines "
              "between timing points (an underestimate, flagged in the output).")
    net.add_coords(naptan)

    sched_path = args.schedule or os.path.join(args.data, "schedule.json.gz")
    print(f"Loading timetable {sched_path}")
    by_uid, tiplocs = schedule.load(sched_path, dates[0], dates[-1])
    print(f"  {sum(len(v) for v in by_uid.values())} candidate schedules, {len(by_uid)} train UIDs")
    services = schedule.services(by_uid, dates)
    print(f"  {len(services)} distinct services run in the window")

    trains, segs, routes = analyse.analyse(services, net, analyse.Stations(tiplocs, naptan))
    if net.suspect:
        print(f"  {len(net.suspect)} network legs rejected as shorter than the straight line, e.g. "
              + ", ".join(f"{a}-{b} {m:.0f}m vs {c:.0f}m" for a, b, m, c in net.suspect[:5]))
    measured = sum(1 for t in trains if t["q"] >= 0.99)
    print(f"  {len(trains)} trains, {len(routes)} routes, {len(segs)} start-to-stop pairs; "
          f"{measured} trains fully measured on the network")

    meta = {
        "from": dates[0].isoformat(), "to": dates[-1].isoformat(),
        "built": dt.datetime.now().isoformat(timespec="minutes"),
        "network": net.has_links, "operators": analyse.OPERATORS,
    }
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "data.js"), "w") as f:
        f.write("window.RAIL=")
        json.dump({"meta": meta, "trains": trains, "routes": routes, "segs": segs},
                  f, separators=(",", ":"), ensure_ascii=False)
        f.write(";\n")
    shutil.copy(os.path.join(ROOT, "railspeed", "index.html"), os.path.join(args.out, "index.html"))
    print(f"Wrote {args.out}/index.html in {time.time() - t0:.0f}s")


def cmd_inspect_tps(args):
    """Summarise the structure of the TPS network model XML, to wire up a loader."""
    import tarfile
    import xml.etree.ElementTree as ET
    from collections import Counter

    counts, samples = Counter(), {}
    with tarfile.open(args.path, "r:*") as tar:
        member = next(m for m in tar if m.name.lower().endswith(".xml"))
        print(f"{member.name}: {member.size / 1e6:.0f} MB")
        for _, el in ET.iterparse(tar.extractfile(member), events=("end",)):
            tag = el.tag.split("}")[-1]
            counts[tag] += 1
            if counts[tag] <= 2:
                samples.setdefault(tag, []).append(dict(el.attrib))
            el.clear()
            if sum(counts.values()) >= args.limit:
                break
    for tag, n in counts.most_common():
        print(f"{tag:30} {n:>9}  {samples[tag][0]}")


def _next_monday():
    today = dt.date.today()
    return today + dt.timedelta(days=(7 - today.weekday()) % 7 or 7)


def _first_existing(d, names):
    for n in names:
        p = os.path.join(d, n)
        if os.path.exists(p):
            return p
    return None


def main(argv=None):
    p = argparse.ArgumentParser(prog="railspeed")
    p.add_argument("--data", default=os.path.join(ROOT, "data"))
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="download data sets")
    f.add_argument("what", nargs="*", default=["schedule", "naptan"], choices=sorted(fetch.SOURCES))
    f.set_defaults(func=cmd_fetch)

    b = sub.add_parser("build", help="compute speeds and write the web page")
    b.add_argument("--date", help="first day to analyse (default: next Monday)")
    b.add_argument("--days", type=int, default=7)
    b.add_argument("--schedule", help="SCHEDULE extract (default: data/schedule.json.gz)")
    b.add_argument("--tps", help="TPS network model (default: data/tps.tar.bz2 if present)")
    b.add_argument("--bplan", help="BPLAN network model file (default: data/bplan.txt if present)")
    b.add_argument("--out", default=os.path.join(ROOT, "site"))
    b.set_defaults(func=cmd_build)

    i = sub.add_parser("inspect-tps", help="summarise the TPS network model XML")
    i.add_argument("path")
    i.add_argument("--limit", type=int, default=2_000_000)
    i.set_defaults(func=cmd_inspect_tps)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
