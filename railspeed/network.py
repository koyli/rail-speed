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
import re
import statistics
import tarfile
import xml.etree.ElementTree as ET
from collections import defaultdict

METRES_PER_MILE = 1609.344
END_M = 300           # half of this from where a route starts or ends to the station's centre
MAX_SPREAD_M = 5000   # a timing point's nodes more than this beyond its nearest are ignored


def load_naptan(path):
    """TIPLOC -> (easting, northing, name) from a NaPTAN stops CSV (rail, area 910).

    Inactive records are used too, where a TIPLOC has no active one: the
    timetable still calls at some of them (St Pancras, Stratford and Ebbsfleet
    Internationals' main codes) and their coordinates are still good."""
    coords, inactive = {}, {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            atco = row.get("ATCOCode", "")
            if not atco.startswith("9100"):
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
            active = row.get("Status", "active") in ("active", "")
            (coords if active else inactive)[atco[4:]] = (e, n, name)
    for tiploc, v in inactive.items():
        coords.setdefault(tiploc, v)
    return coords


# Trailing words in timetable names that pick out platforms, not a station.
_QUALIFIERS = {"ELL", "HL", "LL", "LT", "METRO", "SVR", "PLATFORM", "PLATFORMS", "PLAT", "PLATS"}


def _station_name_key(name):
    """'HIGHBURY AND ISLINGTON ELL' and 'Highbury & Islington' -> same key."""
    words = re.sub(r"[^A-Z0-9 ]", " ", name.upper().replace("&", " AND ")).split()
    while words and (words[-1] in _QUALIFIERS or words[-1].isdigit()):
        words.pop()
    return " ".join(words)


def load_extra_stations(path):
    """TIPLOC -> (easting, northing, name) from data/extra_stations.csv: stations
    the timetable calls at that NaPTAN doesn't list (Severn Valley Railway)."""
    coords = {}
    with open(path, newline="", encoding="utf-8") as f:
        rows = csv.DictReader(line for line in f if not line.startswith("#"))
        for row in rows:
            coords[row["tiploc"]] = (float(row["easting"]), float(row["northing"]), row["name"])
    return coords


def borrow_coords(coords, tiplocs):
    """Coordinates for timetable TIPLOCs NaPTAN lacks, from another TIPLOC at
    the same place: same STANOX first, then same CRS (e.g. Ashford
    International's HS1 platforms, ASHFKI), then the same name once platform
    qualifiers are dropped, if that's unambiguous (Highbury & Islington's East
    London line platforms, HIGHBYE). Returns the additions."""
    by_stanox, by_crs = {}, {}
    for code, t in tiplocs.items():
        if code in coords:
            if t.stanox:
                by_stanox.setdefault(t.stanox, coords[code])
            if t.crs:
                by_crs.setdefault(t.crs, coords[code])
    by_name = {}
    for v in coords.values():
        by_name.setdefault(_station_name_key(v[2]), []).append(v)
    extra = {}
    for code, t in tiplocs.items():
        if code not in coords:
            v = by_stanox.get(t.stanox) if t.stanox else None
            if v is None and t.crs:
                v = by_crs.get(t.crs)
            if v is None:
                same = by_name.get(_station_name_key(t.name), [])
                # Unambiguous: one station, or several records all at one spot
                # (Highbury & Islington has two, 10 m apart).
                if same and all(crow(same[0], o) <= 500 for o in same):
                    v = same[0]
            if v is not None:
                extra[code] = v
    return extra


def crow(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


class Network:
    def __init__(self):
        self.adj = defaultdict(dict)   # node -> {node: metres}
        self.tiploc_nodes = {}         # tiploc -> nodes it owns (TPS); BPLAN nodes are tiplocs
        self.coords = {}               # tiploc -> (e, n)
        self._cache = {}
        self.chainage = {}             # station node -> (line reference id, metres along it)
        self._routes = {}
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
        """Distance along a train's route of each of its timing points (metres,
        to each point's centre).

        The route is the single shortest path through the track network that
        passes through at least one node of every timing point, in order. Each
        point's position is the middle of the stretch of that path inside it,
        so station lengths are counted exactly as crossed, nothing depends on
        the direction of travel (reversed, it's the same path), and the path
        never takes a needless out-and-back. The first and last points get half
        a platform (END_M / 2) beyond where the path starts and ends. A point
        off the network is bridged over, or joined by a straight line if it has
        coordinates.

        Returns (positions, legs): positions[k] is metres or None for a point
        that couldn't be placed; legs is [(k_from, k_to, metres, on_network)].
        Returns (None, None) if the start can't be placed (e.g. Paris for
        Eurostar)."""
        key = tuple(tiplocs)
        if key not in self._routes:
            self._routes[key] = self._positions(tiplocs)
        return self._routes[key]

    def _positions(self, tiplocs):
        n = len(tiplocs)
        if not self.nodes_of(tiplocs[0]) and tiplocs[0] not in self.coords:
            return None, None
        pos = [None] * n
        legs = []
        pos[0] = 0.0
        start = 0
        while True:
            run = self._path_run(tiplocs, start)
            for k, p in run.items():
                pos[k] = pos[start] + p
            ks = sorted(run)
            for k0, k1 in zip(ks, ks[1:]):
                legs.append((k0, k1, pos[k1] - pos[k0], True))
            last = ks[-1]
            # The run stopped short: bridge to the next point with a straight line.
            nxt = next((k for k in range(last + 1, n)
                        if self.crow_m(tiplocs[last], tiplocs[k]) is not None), None)
            if nxt is None:
                break
            straight = self.crow_m(tiplocs[last], tiplocs[nxt])
            pos[nxt] = pos[last] + straight
            legs.append((last, nxt, straight, False))
            start = nxt
        return pos, legs

    def _path_run(self, tiplocs, start):
        """Shortest path from tiplocs[start] through each following point
        reachable on the network, until one isn't. Returns {k: metres from the
        start point's centre to point k's centre}."""
        n = len(tiplocs)
        first = self.nodes_of(tiplocs[start])
        if not first:
            return {start: 0.0}
        layers = [(start, {u: 0.0 for u in first}, {}, {u: u for u in first})]
        for k in range(start + 1, n):
            t = tiplocs[k]
            if t == tiplocs[layers[-1][0]]:
                continue
            targets = set(self.nodes_of(t))
            if not targets:
                if t in self.coords:
                    break        # off the network but placeable: end the run here
                continue         # can't place it at all: bridge over it
            prev_k, prev_dp = layers[-1][0], layers[-1][1]
            base = min(prev_dp.values())
            straight = self.crow_m(tiplocs[prev_k], t)
            reach = 3 * straight + 10_000 if straight is not None else 150_000
            r = _layer_dijkstra(self.adj, prev_dp, targets, base + reach)
            if r is None:
                break
            dp, pred, origin = r
            if straight is not None and min(dp.values()) - base < straight - 1500:
                self.suspect.append((tiplocs[prev_k], t, min(dp.values()) - base, straight))
                break
            layers.append((k, dp, pred, origin))
        # Backtrack from the nearest node of the last point: for each point, the
        # node the path leaves it from (exit) and first reaches it at (entry).
        out = {}
        exit_node = min(layers[-1][1], key=layers[-1][1].get)
        for i in range(len(layers) - 1, -1, -1):
            k, dp, pred, origin = layers[i]
            entry = exit_node
            if i > 0:
                u = exit_node
                while u in pred and origin[u] != u:
                    u = pred[u]
                    if u in dp:
                        entry = u
            if i == len(layers) - 1 and i > 0:
                out[k] = dp[entry] + self._end(tiplocs[k])
            elif i == 0:
                out[k] = dp[exit_node] - self._end(tiplocs[k])
            else:
                out[k] = (dp[entry] + dp[exit_node]) / 2
            if i > 0:
                exit_node = origin[entry]
        base = out[start]
        return {k: v - base for k, v in out.items()}

    def _end(self, tiploc):
        return END_M / 2 if len(self.nodes_of(tiploc)) > 1 else 0.0

    def route_m(self, tiplocs):
        """(metres, fraction measured on the network) from first to last point, or None."""
        pos, legs = self.positions(tiplocs)
        if pos is None or pos[-1] is None:
            return None
        return span(pos, legs, 0, len(tiplocs) - 1)

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


def _layer_dijkstra(adj, init, targets, limit):
    """Shortest paths from a weighted set of start nodes ({node: distance so
    far}) to the nodes of `targets` within MAX_SPREAD_M of the nearest.
    Returns ({target: distance}, pred, origin), or None if none is reached."""
    dist = dict(init)
    pred, origin = {}, {u: u for u in init}
    heap = [(d, u) for u, d in init.items()]
    heapq.heapify(heap)
    found, first = {}, None
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist[u]:
            continue
        if d > limit or (first is not None and d > first + MAX_SPREAD_M):
            break
        if u in targets and u not in found:
            found[u] = d
            if first is None:
                first = d
            if len(found) == len(targets):
                break
        for v, w in adj[u].items():
            nd = d + w
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                pred[v] = u
                origin[v] = origin[u]
                heapq.heappush(heap, (nd, v))
    if not found:
        return None
    return found, pred, origin


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
