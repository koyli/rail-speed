"""Station coordinates (crow-flies) and track-network distances (route miles).

Coordinates are OSGB36 eastings/northings in metres; straight-line distance on
the National Grid is within ~0.05% of true great-circle distance across GB.

Route distance comes from a track network model, either
- TPS (Network Rail's Train Planning System export): a graph of track nodes and
  edges with lengths in metres; each station/timing point (TIPLOC) owns the
  nodes of its tracks; or
- BPLAN: NWK records are links between adjacent TIPLOCs with lengths in metres.
A schedule lists only timing points, so consecutive points are joined by a
shortest path over that graph.
"""
import csv
import heapq
import math
import os
import pickle
import statistics
import tarfile
import xml.etree.ElementTree as ET
from collections import defaultdict

METRES_PER_MILE = 1609.344


def load_naptan(path):
    """TIPLOC -> (easting, northing, name) from a NaPTAN stops CSV (rail, area 910)."""
    coords = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            atco = row.get("ATCOCode", "")
            if not atco.startswith("9100") or row.get("Status", "active") not in ("active", ""):
                continue
            try:
                e, n = float(row["Easting"]), float(row["Northing"])
            except (KeyError, ValueError):
                continue
            name = row.get("CommonName", "")
            for suffix in (" Rail Station", " Railway Station", " Station"):
                if name.endswith(suffix):
                    name = name[: -len(suffix)]
                    break
            coords[atco[4:]] = (e, n, name)
    return coords


def crow(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


class Network:
    def __init__(self):
        self.adj = defaultdict(dict)   # node -> {node: metres}
        self.tiploc_nodes = {}         # tiploc -> nodes it owns (TPS); BPLAN nodes are tiplocs
        self.coords = {}               # tiploc -> (e, n)
        self._cache = {}
        self.suspect = []              # (a, b, network m, straight m) legs rejected as impossible

    @property
    def has_links(self):
        return bool(self.adj)

    def add_link(self, a, b, metres):
        if a == b or metres < 0:
            return
        # Treat links as bidirectional, keeping the shortest line between a pair.
        for x, y in ((a, b), (b, a)):
            if metres < self.adj[x].get(y, math.inf):
                self.adj[x][y] = metres

    def load_bplan(self, path, on_date=None, naptan=None):
        """Parse a BPLAN extract (tab-separated, CRLF). Uses LOC and NWK records.

        LOC: LOC A tiploc name start end easting northing tp_type zone stanox ...
        NWK: NWK A from to line_code line_desc start end init_dir final_dir distance_m ...
        """
        loc_coords = {}
        with open(path, encoding="latin-1") as f:
            for line in f:
                p = line.rstrip("\r\n").split("\t")
                if p[0] == "NWK" and len(p) > 10:
                    if on_date and _expired(p[7], on_date):
                        continue
                    try:
                        self.add_link(p[2].strip(), p[3].strip(), float(p[10]))
                    except ValueError:
                        pass
                elif p[0] == "LOC" and len(p) > 7:
                    if on_date and _expired(p[5], on_date):
                        continue
                    try:
                        e, n = float(p[6]), float(p[7])
                    except ValueError:
                        continue
                    if 0 < e < 700_000 and 0 < n < 1_300_000:
                        loc_coords[p[2].strip()] = (e, n)
        # BPLAN coordinates are patchy; only trust them if they agree with NaPTAN.
        if naptan and loc_coords:
            shared = [k for k in loc_coords if k in naptan]
            err = statistics.median(crow(loc_coords[k], naptan[k]) for k in shared) if shared else math.inf
            print(f"  BPLAN vs NaPTAN coordinates: median offset {err:.0f} m over {len(shared)} stations")
            if err > 1000:
                print("  -> ignoring BPLAN coordinates")
                loc_coords = {}
        for k, v in loc_coords.items():
            self.coords.setdefault(k, v)
        print(f"  network: {len(self.adj)} nodes, {sum(len(v) for v in self.adj.values()) // 2} links")

    def load_tps(self, path, on_date):
        """Load the TPS network model (tar.bz2 of XML_p.xml), via a pickle cache."""
        cache = path + ".graph.v3.pickle"  # bump when the parse changes
        if os.path.exists(cache) and os.path.getmtime(cache) >= os.path.getmtime(path):
            with open(cache, "rb") as f:
                edges, stations = pickle.load(f)
        else:
            print("  parsing TPS XML (first run only, a few minutes)...")
            edges, stations = _parse_tps(path)
            with open(cache, "wb") as f:
                pickle.dump((edges, stations), f, pickle.HIGHEST_PROTOCOL)
        day = on_date.isoformat()
        for a, b, metres, valid_from, valid_to in edges:
            if valid_from <= day <= valid_to:
                self.add_link(a, b, metres)
        for tiploc, nodes in stations.items():
            nodes = [n for n in nodes if n in self.adj]
            if nodes:
                self.tiploc_nodes[tiploc] = nodes
        print(f"  network: {len(self.adj)} nodes, {sum(len(v) for v in self.adj.values()) // 2} links, "
              f"{len(self.tiploc_nodes)} TIPLOCs on the graph")

    def nodes_of(self, tiploc):
        if tiploc in self.tiploc_nodes:
            return self.tiploc_nodes[tiploc]
        return [tiploc] if tiploc in self.adj else []

    def add_coords(self, coords):
        for k, v in coords.items():
            self.coords[k] = (v[0], v[1])

    def crow_m(self, a, b):
        if a in self.coords and b in self.coords:
            return crow(self.coords[a], self.coords[b])
        return None

    def route_m(self, tiplocs):
        """Track metres along a train's timing points, in order.

        A TIPLOC owns a spread of track nodes (often 1-3 km of a station's
        approaches), so each leg starts from the exact node the previous leg
        reached rather than anywhere in the TIPLOC; otherwise every timing
        point would shave its own length off the total. A point that can't be
        reached is skipped, bridging from the last good one; an unplaceable
        start or end gives None rather than a silently truncated distance.
        Returns (metres, fraction measured on the network), or None."""
        total = measured = 0.0
        prev = tiplocs[0]
        at = tuple(self.nodes_of(prev))   # where on the graph we are; () if off it
        if not at and prev not in self.coords:
            return None   # can't place the start (e.g. Paris for Eurostar)
        for k, b in enumerate(tiplocs[1:], 1):
            if b == prev:
                continue
            r = self._leg(prev, at, b)
            if r is None:
                if k == len(tiplocs) - 1:
                    return None
                continue
            metres, end_node = r
            total += metres
            if end_node is not None:
                measured += metres
                at = (end_node,)
            else:
                at = tuple(self.nodes_of(b))
            prev = b
        return total, (measured / total if total else 1.0)

    def _leg(self, a, sources, b):
        """(metres, node reached) over the network, or (straight-line metres,
        None) as a fallback, or None."""
        key = (sources, b)
        if key in self._cache:
            return self._cache[key]
        straight = self.crow_m(a, b)
        dst = self.nodes_of(b)
        r = None
        if sources and dst:
            # Bound the search: a real route is rarely > 3x the straight line, and
            # consecutive timing points are seldom more than ~100 km apart.
            limit = 3 * straight + 10_000 if straight is not None else 150_000
            r = _dijkstra(self.adj, sources, set(dst), limit)
            # Track can't be shorter than the straight line (allowing for the
            # spread of a station's nodes): if it is, the graph has a bogus link.
            if r is not None and straight is not None and r[0] < straight - 1500:
                self.suspect.append((a, b, r[0], straight))
                r = None
        if r is None and straight is not None:
            r = (straight, None)
        self._cache[key] = r
        return r


def _expired(end_field, on_date):
    end = end_field.strip()
    if not end:
        return False
    try:
        d, m, y = end[:10].split("-")
        return (int(y), int(m), int(d)) < (on_date.year, on_date.month, on_date.day)
    except ValueError:
        return False


NON_RAIL_TRACKS = {"BUS", "SHIP", "SHP"}


def _is_rail_track(el):
    """The TPS model also holds a schematic network for rail-replacement buses
    and ships: tracks named/described BUS or SHIP, joining stations with nominal
    100 m edges. Routing over them would let a train "take the bus" from
    Chelmsford to Shenfield. (Their category, 3 "Non-Conventional", also covers
    some real track, so it can't be used.)"""
    return (el.get("name", "").strip().upper() not in NON_RAIL_TRACKS
            and el.get("description", "").strip().upper() not in NON_RAIL_TRACKS)


def _parse_tps(path):
    """Returns ([(node_a, node_b, metres, valid_from, valid_to)], {tiploc: [node]}),
    rail track only."""
    edges, stations = [], defaultdict(set)
    rail_nodes, other_nodes = set(), set()
    station = in_edge = None
    track_is_rail = True
    pts, edge_attrs = [], None
    with tarfile.open(path, "r:*") as tar:
        member = next(m for m in tar if m.name.lower().endswith(".xml"))
        for event, el in ET.iterparse(tar.extractfile(member), events=("start", "end")):
            tag = el.tag
            if event == "start":
                if tag == "station":
                    station = el.get("abbrev", "").strip()
                elif tag == "track":
                    track_is_rail = _is_rail_track(el)
                elif tag == "edge":
                    in_edge, pts, edge_attrs = True, [], dict(el.attrib)
                continue
            if tag == "point":
                node = int(el.get("nodeid"))
                if in_edge:
                    pts.append(node)
                elif station:
                    if track_is_rail:
                        rail_nodes.add(node)
                        stations[station].add(node)
                    else:
                        other_nodes.add(node)
            elif tag == "edge":
                if len(pts) >= 2:
                    try:
                        metres = float(edge_attrs.get("length", ""))
                    except ValueError:
                        metres = -1
                    if metres >= 0:
                        edges.append((pts[0], pts[-1], metres, edge_attrs.get("validfrom", "0000"),
                                      edge_attrs.get("validTo", "9999")))
                in_edge = None
                el.clear()
            elif tag == "station":
                station = None
                el.clear()
            elif tag == "track":
                track_is_rail = True
                el.clear()
            elif tag == "node":
                el.clear()
    non_rail = other_nodes - rail_nodes
    edges = [e for e in edges if e[0] not in non_rail and e[1] not in non_rail]
    print(f"  dropped {len(non_rail)} bus/ship nodes")
    return edges, {k: sorted(v) for k, v in stations.items()}


def _dijkstra(adj, sources, targets, limit):
    dist = {s: 0.0 for s in sources}
    heap = [(0.0, s) for s in sources]
    heapq.heapify(heap)
    while heap:
        d, u = heapq.heappop(heap)
        if u in targets:
            return d, u
        if d > limit:
            return None
        if d > dist[u]:
            continue
        for v, w in adj[u].items():
            nd = d + w
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                heapq.heappush(heap, (nd, v))
    return None
