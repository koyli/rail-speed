"""Turn resolved services into speed tables: trains and start-to-stop runs.

Routes are grouped from trains in the page itself, so that its filters (calling
point, operator) choose which trains count towards each route's best."""
from collections import defaultdict

from .network import METRES_PER_MILE

OPERATORS = {
    "AW": "Transport for Wales", "CC": "c2c", "CH": "Chiltern Railways", "CS": "Caledonian Sleeper",
    "EM": "East Midlands Railway", "ES": "Eurostar", "GC": "Grand Central", "GN": "Great Northern",
    "GR": "LNER", "GW": "Great Western Railway", "GX": "Gatwick Express", "HC": "Heathrow Connect",
    "HT": "Hull Trains", "HX": "Heathrow Express", "IL": "Island Line", "LD": "Lumo",
    "LE": "Greater Anglia", "LM": "West Midlands Trains", "LO": "London Overground",
    "LT": "London Underground", "ME": "Merseyrail", "NT": "Northern", "SE": "Southeastern",
    "SN": "Southern", "SR": "ScotRail", "SW": "South Western Railway", "TL": "Thameslink",
    "TP": "TransPennine Express", "VT": "Avanti West Coast", "XC": "CrossCountry",
    "XR": "Elizabeth line", "GM": "Grand Union", "YG": "Hull Trains", "LF": "Grand Union", "ZZ": "Other",
}


class Stations:
    """Maps TIPLOCs to a station identity (CRS where known) and a display name."""

    def __init__(self, tiplocs, naptan):
        self.tiplocs, self.naptan = tiplocs, naptan
        self.names, self.codes, self._index = [], [], {}   # compact station table for the page

    def index(self, tiploc):
        """Position of this station in `names`, adding it if new."""
        k = self.key(tiploc)
        if k not in self._index:
            self._index[k] = len(self.names)
            self.names.append(self.name(tiploc))
            t = self.tiplocs.get(tiploc)
            self.codes.append(t.crs if t and t.crs else "")
        return self._index[k]

    def key(self, tiploc):
        t = self.tiplocs.get(tiploc)
        return (t.crs if t and t.crs else tiploc)

    def name(self, tiploc):
        if tiploc in self.naptan:
            return self.naptan[tiploc][2]
        t = self.tiplocs.get(tiploc)
        return t.name.title() if t else tiploc


def _public_stops(locs):
    return [i for i, l in enumerate(locs) if l.pub_arr is not None or l.pub_dep is not None]


def _span(net, locs, i, j):
    """Track metres from locs[i] to locs[j] via every intermediate timing point.
    Returns (metres, fraction measured on the network) or (None, 0)."""
    r = net.route_m([l.tiploc for l in locs[i:j + 1]])
    return r if r is not None else (None, 0.0)


def analyse(services, net, stations):
    trains, segs = [], {}
    for sched, dates in services:
        locs = sched.locs
        stops = _public_stops(locs)
        if len(stops) < 2:
            continue
        o = next((i for i in stops if locs[i].pub_dep is not None), None)
        d = next((i for i in reversed(stops) if locs[i].pub_arr is not None), None)
        if o is None or d is None or d <= o:
            continue
        mins = locs[d].pub_arr - locs[o].pub_dep
        if mins <= 0:
            continue
        route_m, quality = _span(net, locs, o, d)
        crow_m = net.crow_m(locs[o].tiploc, locs[d].tiploc)
        op = sched.atoc
        trains.append({
            "uid": sched.uid, "hc": sched.headcode, "op": op,
            "from": stations.name(locs[o].tiploc), "to": stations.name(locs[d].tiploc),
            "fk": stations.key(locs[o].tiploc), "tk": stations.key(locs[d].tiploc),
            "dep": _hhmm(locs[o].pub_dep), "arr": _hhmm(locs[d].pub_arr), "mins": mins,
            "stops": sum(1 for i in stops if o < i < d),
            "rmi": _mi(route_m), "cmi": _mi(crow_m), "q": round(quality, 2),
            "rmph": _mph(route_m, mins), "cmph": _mph(crow_m, mins),
            "days": _days(dates), "pw": sched.power,
            "st": [stations.index(locs[i].tiploc) for i in stops if o <= i <= d],
        })

        # Start-to-stop runs between consecutive calls, on working (half-minute) times.
        for a, b in zip(stops, stops[1:]):
            la, lb = locs[a], locs[b]
            t0 = la.dep if la.dep is not None else la.pub_dep
            t1 = lb.arr if lb.arr is not None else lb.pub_arr
            if t0 is None or t1 is None or t1 <= t0:
                continue
            ka, kb = stations.key(la.tiploc), stations.key(lb.tiploc)
            if ka == kb:
                continue
            m, q = _span(net, locs, a, b)
            if m is None:
                continue
            pair = (ka, kb)
            mins_ab = t1 - t0
            cur = segs.get(pair)
            if cur is None or mins_ab < cur["mins"]:
                segs[pair] = {
                    "from": stations.name(la.tiploc), "to": stations.name(lb.tiploc),
                    "mins": mins_ab, "rmi": _mi(m), "q": round(q, 2), "rmph": _mph(m, mins_ab),
                    "cmi": _mi(net.crow_m(la.tiploc, lb.tiploc)),
                    "cmph": _mph(net.crow_m(la.tiploc, lb.tiploc), mins_ab),
                    "uid": sched.uid, "hc": sched.headcode, "op": op, "dep": _hhmm(t0),
                    "days": _days(dates), "n": (cur["n"] if cur else 0) + 1,
                    "st": [stations.index(la.tiploc), stations.index(lb.tiploc)],
                }
            else:
                cur["n"] += 1
                if mins_ab == cur["mins"]:   # equally fast on other days too
                    cur["days"] = _union_days(cur["days"], _days(dates))
    return trains, list(segs.values())


def _mi(m):
    return None if m is None else round(m / METRES_PER_MILE, 2)


def _mph(m, mins):
    return None if m is None or not mins else round(m / METRES_PER_MILE / (mins / 60), 1)


def _hhmm(t):
    half = "½" if t % 1 else ""
    t = int(t) % 1440
    return f"{t // 60:02d}:{t % 60:02d}{half}"


def _union_days(a, b):
    return "".join(x if x != "-" else y for x, y in zip(a, b))


def _days(dates):
    names = "MTWTFSS"
    wds = {d.weekday() for d in dates}
    return "".join(names[i] if i in wds else "-" for i in range(7))
