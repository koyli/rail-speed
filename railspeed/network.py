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
DETOUR_M = 3000   # see Network.positions


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
        self._centres = {}
        self.chainage = {}             # station node -> (line reference id, metres along it)
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
        cache = path + ".graph.v4.pickle"  # bump when the parse changes
        if os.path.exists(cache) and os.path.getmtime(cache) >= os.path.getmtime(path):
            with open(cache, "rb") as f:
                edges, stations, self.chainage = pickle.load(f)
        else:
            print("  parsing TPS XML (first run only, a few minutes)...")
            edges, stations, self.chainage = _parse_tps(path)
            with open(cache, "wb") as f:
                pickle.dump((edges, stations, self.chainage), f, pickle.HIGHEST_PROTOCOL)
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

    def positions(self, tiplocs):
        """Distance along a train's path of each of its timing points, in metres.

        A TIPLOC owns a spread of track nodes (often 1-3 km of a station and its
        approaches). Each point is placed at its centre (see _offset), so
        distances run centre to centre; measuring node-to-node would drop a
        station length at each stop, enough to make a 0.8 mile hop shorter than
        the straight line.

        Normally each leg starts from the exact node the previous one reached,
        keeping the path continuous (accurate over long distances, where many
        junction timing points have no usable chainage). But that can strand
        the path on the wrong running line and send it on an out-and-back to a
        crossover (20 km near Whitchurch), so each leg is also measured starting
        from anywhere in the previous point's spread, and that is used when the
        continuous one is more than DETOUR_M longer.

        Returns (positions, legs): positions[k] is metres or None for a point
        that couldn't be reached (bridged over); legs is [(k_from, k_to, metres,
        on_network)]. Returns (None, None) if the start can't be placed (e.g.
        Paris for Eurostar)."""
        n = len(tiplocs)
        pos = [None] * n
        legs = []
        first = tiplocs[0]
        at = tuple(self.nodes_of(first))   # where on the graph we are; () if off it
        if not at and first not in self.coords:
            return None, None
        pos[0] = 0.0
        cursor = None   # distance along the path of the node `at`, while continuous
        prev_k = 0
        for k in range(1, n):
            a, b = tiplocs[prev_k], tiplocs[k]
            if b == a:
                pos[k] = pos[prev_k]
                continue
            fresh = self._leg(a, tuple(self.nodes_of(a)), b)
            if fresh is None:
                continue
            if fresh[1] is None:   # straight-line fallback: off the network
                pos[k] = pos[prev_k] + fresh[0]
                legs.append((prev_k, k, fresh[0], False))
                at, cursor, prev_k = tuple(self.nodes_of(b)), None, k
                continue
            fresh_m = self._offset(a, fresh[2]) + fresh[0] + self._offset(b, fresh[1])
            length, end_node = fresh_m, fresh[1]
            if cursor is not None:
                cont = self._leg(a, at, b)
                if cont is not None and cont[1] is not None:
                    cont_m = cursor + cont[0] + self._offset(b, cont[1]) - pos[prev_k]
                    if cont_m <= fresh_m + DETOUR_M:
                        length, end_node = cont_m, cont[1]
            pos[k] = pos[prev_k] + length
            legs.append((prev_k, k, length, True))
            # Continue from the first node reached in b's spread.
            at, cursor = (end_node,), pos[k] - self._offset(b, end_node)
            prev_k = k
        return pos, legs

    def route_m(self, tiplocs):
        """(metres, fraction measured on the network) from first to last point, or None."""
        pos, legs = self.positions(tiplocs)
        if pos is None or pos[-1] is None:
            return None
        return span(pos, legs, 0, len(tiplocs) - 1)

    def _offset(self, tiploc, node):
        """Metres from `node` to the centre of its TIPLOC, along the line.

        A TIPLOC's nodes can sprawl (Winsford's run 263.6-266.4 km along the
        line, in clusters, with other nodes between), so the centre is taken as
        the median chainage of its nodes on their main line reference, which
        sits in the main cluster. 0 where chainage isn't comparable."""
        if tiploc not in self._centres:
            marks = [self.chainage[n] for n in self.tiploc_nodes.get(tiploc, ()) if n in self.chainage]
            centre = None
            if marks:
                regions = [r for r, _ in marks]
                region = max(set(regions), key=regions.count)
                km = sorted(k for r, k in marks if r == region)
                centre = (region, km[len(km) // 2])
            self._centres[tiploc] = centre
        centre, mark = self._centres[tiploc], self.chainage.get(node)
        if centre is None or mark is None or mark[0] != centre[0]:
            return 0.0
        return min(abs(mark[1] - centre[1]), 5000.0)

    def _leg(self, a, sources, b):
        """(metres, node reached, node started from) over the network, or
        (straight-line metres, None, None) as a fallback, or None."""
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
            r = (straight, None, None)
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
    """Returns ([(node_a, node_b, metres, valid_from, valid_to)], {tiploc: [node]},
    {station node: (line reference id, metres along it)}), rail track only."""
    edges, stations = [], defaultdict(set)
    rail_nodes, other_nodes = set(), set()
    chainage = {}
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
                pt = el.find("point")
                if pt is not None:
                    n = int(pt.get("nodeid"))
                    if n in rail_nodes:
                        try:
                            chainage[n] = (int(el.get("kmregionid")), int(el.get("kmvalue")))
                        except (TypeError, ValueError):
                            pass
                el.clear()
    non_rail = other_nodes - rail_nodes
    edges = [e for e in edges if e[0] not in non_rail and e[1] not in non_rail]
    print(f"  dropped {len(non_rail)} bus/ship nodes")
    return edges, {k: sorted(v) for k, v in stations.items()}, chainage


def span(pos, legs, i, j):
    """(metres, fraction measured on the network) between points i and j of a
    `positions` result, or None if either wasn't reached."""
    if pos[i] is None or pos[j] is None:
        return None
    total = pos[j] - pos[i]
    measured = sum(m for a, b, m, on in legs if on and i <= a and b <= j)
    straight = sum(m for a, b, m, on in legs if not on and i <= a and b <= j)
    return total, (measured / (measured + straight) if measured + straight else 1.0)


def _dijkstra(adj, sources, targets, limit):
    """Shortest path from any source to any target: (metres, target, source) or None."""
    dist = {s: 0.0 for s in sources}
    origin = {s: s for s in sources}
    heap = [(0.0, s) for s in sources]
    heapq.heapify(heap)
    while heap:
        d, u = heapq.heappop(heap)
        if u in targets:
            return d, u, origin[u]
        if d > limit:
            return None
        if d > dist[u]:
            continue
        for v, w in adj[u].items():
            nd = d + w
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                origin[v] = origin[u]
                heapq.heappush(heap, (nd, v))
    return None
