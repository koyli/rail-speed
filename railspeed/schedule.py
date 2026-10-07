"""Parse the NROD SCHEDULE JSON extract and resolve which trains run on given dates.

The extract is newline-delimited JSON. Each JsonScheduleV1 record is one
schedule: a train UID valid over a date range on certain weekdays, with an STP
indicator. For any date, the schedule that applies to a UID is the valid one
with the highest-priority STP indicator (C cancel > N new > O overlay > P permanent).
"""
import datetime as dt
import gzip
import json
from collections import defaultdict, namedtuple

# Advertised passenger categories. Excludes buses (BR/BS), ships (SS),
# empty stock (EE), staff trains (OS), unadvertised (OU/XU) and London
# Underground trains running over Network Rail (OL).
PASSENGER_CATEGORIES = {"OO", "OW", "XX", "XZ", "XC", "XD", "XI", "XR"}
PASSENGER_STATUS = {"P", "1"}
# Operators excluded as not mainline railway: heritage and charter (North
# Yorkshire Moors Railway, West Coast Railways' Jacobite, Vintage Trains,
# Locomotive Services, Belmond), the Sheffield Supertram tram-train, and
# Eurostar, whose schedules only model the GB section ("Paris Nord" is a
# boundary stub by the tunnel, so its times and distances are meaningless).
EXCLUDED_OPERATORS = {"NY", "WR", "TY", "LS", "PO", "SJ", "ES"}
# Heritage lines reached by mainline operators' trains, by a marker in their
# stations' timetable names (Severn Valley Railway: "KIDDERMINSTER S.V.R.").
HERITAGE_STATION_MARKERS = ("S.V.R",)
STP_RANK = {"C": 0, "N": 1, "O": 2, "P": 3}

# One timing point: minutes after midnight (float, half-minutes kept) with the
# day rollover already applied, so times increase monotonically along the train.
Loc = namedtuple("Loc", "tiploc arr dep pas pub_arr pub_dep")
Schedule = namedtuple("Schedule", "uid stp start end days atoc headcode category power locs")
Tiploc = namedtuple("Tiploc", "code crs name stanox")


def _minutes(t):
    """'0812H' -> 492.5; None/'' -> None."""
    if not t:
        return None
    t = t.strip()
    if len(t) < 4 or not t[:4].isdigit():
        return None
    m = int(t[:2]) * 60 + int(t[2:4])
    return m + 0.5 if t.endswith("H") else float(m)


def _locations(raw):
    locs, last, offset = [], None, 0.0
    for r in raw:
        arr, dep, pas = _minutes(r.get("arrival")), _minutes(r.get("departure")), _minutes(r.get("pass"))
        pa, pd = _minutes(r.get("public_arrival")), _minutes(r.get("public_departure"))
        kind = r.get("location_type") or r.get("record_identity")
        # CIF uses 0000 for "no public time" at intermediate points; only trust it
        # when the working time is also around midnight.
        if kind == "LI":
            if pa == 0 and not (arr is not None and (arr <= 2 or arr >= 1438)):
                pa = None
            if pd == 0 and not (dep is not None and (dep <= 2 or dep >= 1438)):
                pd = None
        # Chronological order within the record, so a 2359/0001 arr/dep rolls over correctly.
        out = {}
        for name, v in (("arr", arr), ("pub_arr", pa), ("dep", dep), ("pub_dep", pd), ("pas", pas)):
            if v is not None:
                v += offset
                if last is not None:
                    # Snap to the day nearest the previous time: no train goes 12h
                    # between consecutive points, but a rounded public 2359 can
                    # follow a working 0001.
                    while v < last - 720:  # crossed midnight
                        offset += 1440
                        v += 1440
                    if v > last + 720:
                        v -= 1440
                last = v if last is None else max(last, v)
            out[name] = v
        locs.append(Loc(r["tiploc_code"].strip(), **out))
    return tuple(locs)


def _parse_date(s):
    return dt.date.fromisoformat(s) if s else None


def load(path, window_start, window_end):
    """Stream the extract; keep passenger schedules (and cancellations) that
    could apply within [window_start, window_end]. Returns (schedules_by_uid, tiplocs)."""
    by_uid = defaultdict(list)
    tiplocs = {}
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            if '"JsonScheduleV1"' in line[:30]:
                s = json.loads(line)["JsonScheduleV1"]
                if s.get("transaction_type", "Create") != "Create":
                    continue
                start, end = _parse_date(s["schedule_start_date"]), _parse_date(s["schedule_end_date"])
                if start > window_end or end < window_start:
                    continue
                stp = s["CIF_stp_indicator"]
                seg = s.get("schedule_segment") or {}
                if stp != "C":
                    if s.get("train_status") not in PASSENGER_STATUS:
                        continue
                    if seg.get("CIF_train_category") not in PASSENGER_CATEGORIES:
                        continue
                    if s.get("atoc_code") in EXCLUDED_OPERATORS:
                        continue
                    if not seg.get("schedule_location"):
                        continue
                    ends = (seg["schedule_location"][0], seg["schedule_location"][-1])
                    if any(m in tiplocs[e["tiploc_code"].strip()].name
                           for e in ends if e["tiploc_code"].strip() in tiplocs
                           for m in HERITAGE_STATION_MARKERS):
                        continue
                by_uid[s["CIF_train_uid"]].append(Schedule(
                    uid=s["CIF_train_uid"], stp=stp, start=start, end=end,
                    days=s["schedule_days_runs"], atoc=s.get("atoc_code") or "",
                    headcode=seg.get("signalling_id") or "",
                    category=seg.get("CIF_train_category") or "",
                    power=seg.get("CIF_power_type") or "",
                    locs=_locations(seg.get("schedule_location") or []) if stp != "C" else (),
                ))
            elif '"TiplocV1"' in line[:20]:
                t = json.loads(line)["TiplocV1"]
                code = t["tiploc_code"].strip()
                tiplocs[code] = Tiploc(code, (t.get("crs_code") or "").strip() or None,
                                       (t.get("tps_description") or t.get("description") or code).strip(),
                                       t.get("stanox"))
    return by_uid, tiplocs


def applicable(schedules, date):
    """The schedule for one UID that runs on `date`, or None (incl. cancelled)."""
    wd = date.weekday()
    valid = [s for s in schedules if s.start <= date <= s.end and s.days[wd] == "1"]
    if not valid:
        return None
    best = min(valid, key=lambda s: STP_RANK.get(s.stp, 9))
    return None if best.stp == "C" else best


def services(by_uid, dates):
    """Distinct services running on any of `dates`.

    Yields (schedule, [dates it runs]) with identical timing patterns merged, so a
    train that runs Mon-Fri appears once, but a Saturday variant is separate.
    """
    out = {}
    for uid, scheds in by_uid.items():
        for d in dates:
            s = applicable(scheds, d)
            if s is None:
                continue
            key = (uid, s.locs)
            if key in out:
                out[key][1].append(d)
            else:
                out[key] = (s, [d])
    return list(out.values())
