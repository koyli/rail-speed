import datetime as dt
import json
import os
import tempfile
import unittest

from railspeed import analyse, cli, network, schedule
from tests import make_fixtures

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = make_fixtures.HERE
WEEK = [dt.date(2026, 10, 5) + dt.timedelta(days=i) for i in range(7)]  # Mon 5 Oct


class Pipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        make_fixtures.main()
        cls.by_uid, cls.tiplocs = schedule.load(os.path.join(FIX, "schedule.json"), WEEK[0], WEEK[-1])

    def test_filters_non_passenger_and_expired(self):
        self.assertEqual(set(self.by_uid), {"G00001", "S00001", "L00001", "M00001"})

    def test_stp_resolution(self):
        g = self.by_uid["G00001"]
        self.assertEqual(schedule.applicable(g, WEEK[0]).stp, "P")
        self.assertEqual(schedule.applicable(g, WEEK[2]).stp, "O")
        self.assertIsNone(schedule.applicable(g, WEEK[3]))   # cancelled Thursday
        self.assertIsNone(schedule.applicable(g, WEEK[5]))   # doesn't run Saturday

    def test_services_merge_identical_days(self):
        svcs = {(s.uid, s.stp): d for s, d in schedule.services(self.by_uid, WEEK)}
        self.assertEqual([d.weekday() for d in svcs[("G00001", "P")]], [0, 1, 4])
        self.assertEqual([d.weekday() for d in svcs[("G00001", "O")]], [2])

    def test_midnight_rollover(self):
        s = self.by_uid["S00001"][0]
        self.assertEqual(s.locs[-1].pub_arr - s.locs[0].pub_dep, 11 * 60 + 30)
        late = self.by_uid["M00001"][0].locs
        self.assertEqual(late[1].dep, 1441)
        self.assertEqual(late[1].pub_dep, 1439)      # rounded public time stays before midnight
        self.assertEqual(late[-1].pub_arr - late[0].pub_dep, 35)

    def test_half_minutes_and_zero_public_times(self):
        didcot = self.by_uid["G00001"][0].locs[2]
        self.assertEqual(didcot.pas, 10 * 60 + 36.5)
        self.assertIsNone(didcot.pub_arr)

    def test_speeds(self):
        naptan = network.load_naptan(os.path.join(ROOT, "data", "naptan_rail.csv"))
        net = network.Network()
        net.load_bplan(os.path.join(FIX, "bplan.txt"), WEEK[0], {k: v[:2] for k, v in naptan.items()})
        net.add_coords(naptan)
        stns = analyse.Stations(self.tiplocs, naptan)
        trains, segs = analyse.analyse(schedule.services(self.by_uid, WEEK), net,
                                               stns)
        gw = next(t for t in trains if t["uid"] == "G00001" and t["mins"] == 90)
        self.assertAlmostEqual(gw["rmi"], 184_800 / network.METRES_PER_MILE, places=1)
        self.assertEqual(gw["q"], 1.0)
        self.assertAlmostEqual(gw["rmph"], 76.6, places=1)
        self.assertEqual(gw["stops"], 2)              # Didcot is a pass, not a stop
        self.assertEqual(gw["days"], "MT--F--")
        names = [stns.names[i] for i in gw["st"]]
        self.assertEqual(names, ["London Paddington", "Reading", "Swindon", "Bristol Temple Meads"])
        self.assertEqual(gw["t"], [0, 0, 23, 25, 53, 54, 90, 90])
        self.assertEqual(gw["lw"], [23, 28, 35.5])   # working times: 1054H dep Swindon
        self.assertEqual(len(gw["lr"]), 3)
        self.assertAlmostEqual(sum(gw["lr"]), gw["rmi"], places=1)
        self.assertTrue(103 < gw["cmi"] < 105)        # Paddington-Bristol TM ~104 mi crow
        # KX-York has no network links: falls back to straight line, flagged.
        lner = next(t for t in trains if t["uid"] == "L00001")
        self.assertEqual(lner["q"], 0.0)
        # Start-to-stop uses working times: Reading 1025 -> Swindon 1053 over 65.7 km.
        rs = next(s for s in segs if s["from"] == "Reading" and s["to"] == "Swindon")
        self.assertEqual(rs["mins"], 28)
        self.assertEqual(rs["days"], "MT--F--")
        self.assertEqual(trains[rs["ti"]]["uid"], "G00001")

    def test_tps_bus_and_ship_tracks_are_not_rail(self):
        import xml.etree.ElementTree as ET
        track = lambda **a: ET.Element("track", {k: str(v) for k, v in a.items()})
        self.assertFalse(network._is_rail_track(track(name="BUS", description="BUS", trackcategory=3)))
        self.assertFalse(network._is_rail_track(track(name="", description="Bus", trackcategory=3)))
        self.assertFalse(network._is_rail_track(track(name="SHP", description="SHIP", trackcategory=3)))
        # Category 3 also covers real track.
        self.assertTrue(network._is_rail_track(track(name="DM", description="Down Main", trackcategory=3)))

    def test_route_rejects_shortcut_shorter_than_crow_flies(self):
        net = network.Network()
        net.add_coords({"A": (0, 0), "B": (20_000, 0)})
        net.add_link("A", "B", 100)   # bogus 100 m link between stations 20 km apart
        self.assertEqual(net.route_m(["A", "B"]), (20_000, 0.0))
        self.assertEqual(len(net.suspect), 1)

    def test_route_never_shorter_than_crow_and_impossible_runs(self):
        self.assertEqual(analyse._at_least_crow((1000, 1.0), 1200), (1200, 0.0))
        self.assertEqual(analyse._at_least_crow((1300, 1.0), 1200), (1300, 1.0))
        # 1.6 km start-to-stop needs at least 80 s at 1 m/s^2; 30 s is an artefact.
        self.assertAlmostEqual(analyse._min_run_seconds(1600), 80)

    def test_borrow_coords_from_same_station(self):
        T = schedule.Tiploc
        tiplocs = {"ASHFKY": T("ASHFKY", "AFK", "ASHFORD INTERNATIONAL", "86531"),
                   "ASHFKI": T("ASHFKI", "ASI", "ASHFORD INT (PLATS 3-4)", "86531"),
                   "STFODOM": T("STFODOM", "SFA", "STRATFORD INTL (DOMESTIC)", "51416"),
                   "STFOX": T("STFOX", "SFA", "STRATFORD INTL OTHER", None)}
        got = network.borrow_coords({"ASHFKY": (601282, 142204, "Ashford"), "STFODOM": (538175, 184760, "Stratford")}, tiplocs)
        self.assertEqual(got["ASHFKI"][:2], (601282, 142204))      # same STANOX
        self.assertEqual(got["STFOX"][:2], (538175, 184760))       # same CRS
        got = network.borrow_coords({"HIGHBYI": (531500, 184900, "Highbury & Islington")},
                                    {"HIGHBYE": T("HIGHBYE", None, "HIGHBURY AND ISLINGTON ELL", None)})
        self.assertEqual(got["HIGHBYE"][:2], (531500, 184900))     # same name, platform qualifier dropped

    def test_misplaced_station_detected(self):
        names = ["Aberdeen", "Stonehaven", "Ok A", "Ok B"]
        trains = [{"st": [0, 1], "lc": [80.0], "lw": [15]},    # 80 mi in 15 min: 320 mph
                  {"st": [2, 3], "lc": [10.0], "lw": [10]}]    # 60 mph: fine
        self.assertEqual(set(analyse.misplaced_stations(trains, names)), {"Aberdeen", "Stonehaven"})

    def test_build_writes_site(self):
        with tempfile.TemporaryDirectory() as out:
            cli.main(["build", "--date", "2026-10-05", "--schedule", os.path.join(FIX, "schedule.json"),
                      "--bplan", os.path.join(FIX, "bplan.txt"), "--out", out])
            with open(os.path.join(out, "data.js")) as f:
                js = f.read()
            data = json.loads(js[len("window.RAIL="):-2])
            self.assertEqual(len(data["trains"]), 5)
            self.assertTrue(os.path.exists(os.path.join(out, "index.html")))


if __name__ == "__main__":
    unittest.main()
