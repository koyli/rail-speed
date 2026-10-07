"""Generate a tiny SCHEDULE extract and BPLAN file for tests (real TIPLOCs, made-up trains)."""
import json
import os

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def loc(kind, tiploc, arr=None, dep=None, pas=None, parr=None, pdep=None):
    return {"location_type": kind, "record_identity": kind, "tiploc_code": tiploc,
            "arrival": arr, "departure": dep, "pass": pas,
            "public_arrival": parr, "public_departure": pdep}


def sched(uid, stp, start, end, days, locs, cat="XX", status="P", atoc="GW", hc="1A00"):
    seg = {"signalling_id": hc, "CIF_train_category": cat, "CIF_power_type": "IET",
           "schedule_location": locs} if locs is not None else {}
    return {"JsonScheduleV1": {"CIF_train_uid": uid, "CIF_stp_indicator": stp,
            "schedule_start_date": start, "schedule_end_date": end, "schedule_days_runs": days,
            "train_status": status, "atoc_code": atoc, "transaction_type": "Create",
            "schedule_segment": seg}}


GW_FAST = [
    loc("LO", "PADTON", dep="1000", pdep="1000"),
    loc("LI", "RDNGSTN", arr="1023", dep="1025", parr="1023", pdep="1025"),
    loc("LI", "DIDCOTP", pas="1036H", parr="0000", pdep="0000"),
    loc("LI", "SDON", arr="1053", dep="1054H", parr="1053", pdep="1054"),
    loc("LT", "BRSTLTM", arr="1130", parr="1130"),
]
GW_OVERLAY = [
    loc("LO", "PADTON", dep="1000", pdep="1000"),
    loc("LI", "RDNGSTN", arr="1030", dep="1032", parr="1030", pdep="1032"),
    loc("LI", "DIDCOTP", arr="1045", dep="1046", parr="1045", pdep="1046"),
    loc("LI", "SDON", arr="1105", dep="1106", parr="1105", pdep="1106"),
    loc("LT", "BRSTLTM", arr="1150", parr="1150"),
]
SLEEPER = [
    loc("LO", "EUSTON", dep="2115", pdep="2115"),
    loc("LT", "IVRNESS", arr="0845", parr="0845"),
]
# Public departure rounds down to 2359 after a working departure of 0001.
LATE = [
    loc("LO", "BHAMNWS", dep="2340", pdep="2340"),
    loc("LI", "HROW", arr="2359H", dep="0001", parr="2359", pdep="2359"),
    loc("LT", "EUSTON", arr="0015", parr="0015"),
]
LNER = [
    loc("LO", "KNGX", dep="0800", pdep="0800"),
    loc("LT", "YORK", arr="0952", parr="0952"),
]

RECORDS = [
    {"JsonTimetableV1": {"classification": "public", "timestamp": 0}},
    {"TiplocV1": {"tiploc_code": "PADTON", "crs_code": "PAD", "tps_description": "LONDON PADDINGTON"}},
    {"TiplocV1": {"tiploc_code": "BRSTLTM", "crs_code": "BRI", "tps_description": "BRISTOL TEMPLE MEADS"}},
    {"TiplocV1": {"tiploc_code": "KIDDSVR", "crs_code": None, "tps_description": "KIDDERMINSTER S.V.R."}},
    sched("G00001", "P", "2026-01-01", "2026-12-31", "1111100", GW_FAST),
    sched("G00001", "O", "2026-10-07", "2026-10-07", "0010000", GW_OVERLAY),  # Wednesday
    sched("G00001", "C", "2026-10-08", "2026-10-08", "0001000", None),        # Thursday
    sched("S00001", "P", "2026-01-01", "2026-12-31", "1111000", SLEEPER, cat="XZ", atoc="CS", hc="1S25"),
    sched("M00001", "P", "2026-01-01", "2026-12-31", "0000010", LATE, atoc="LM", hc="1Y99"),
    sched("L00001", "P", "2026-01-01", "2026-12-31", "1111111", LNER, atoc="GR", hc="1N01"),
    sched("B00001", "P", "2026-01-01", "2026-12-31", "1111111", LNER, cat="BR", status="B", atoc="GR"),
    sched("E00001", "P", "2026-01-01", "2026-12-31", "1111111", LNER, cat="EE", atoc="GR"),
    sched("H00001", "P", "2026-01-01", "2026-12-31", "1111111", LNER, atoc="NY"),  # heritage
    sched("V00001", "P", "2026-01-01", "2026-12-31", "1111111",
          [loc("LO", "KIDDSVR", dep="1000", pdep="1000"), loc("LT", "YORK", arr="1100", parr="1100")],
          atoc="LM"),  # heritage line (Severn Valley), mainline operator
    sched("X00001", "P", "2025-01-01", "2025-12-31", "1111111", LNER, atoc="GR"),  # expired
    {"EOF": True},
]

# Track lengths in metres, roughly right for the GWML.
BPLAN_LINKS = [("PADTON", "RDNGSTN", 57_900), ("RDNGSTN", "DIDCOTP", 27_400),
               ("DIDCOTP", "SDON", 38_300), ("SDON", "BRSTLTM", 61_200)]


def main():
    os.makedirs(HERE, exist_ok=True)
    with open(os.path.join(HERE, "schedule.json"), "w") as f:
        for r in RECORDS:
            f.write(json.dumps(r) + "\n")
    with open(os.path.join(HERE, "bplan.txt"), "w", newline="") as f:
        f.write("PIF\t1\r\n")
        f.write("LOC\tA\tPADTON\tLONDON PADDINGTON\t01-01-2000 00:00:00\t\t999999\t999999\tT\t\t\r\n")
        for a, b, m in BPLAN_LINKS:
            for x, y in ((a, b), (b, a)):
                f.write(f"NWK\tA\t{x}\t{y}\tML\tMain\t01-01-2000 00:00:00\t\tU\tU\t{m}\t\t\t\t\t\t\t\t\r\n")


if __name__ == "__main__":
    main()
