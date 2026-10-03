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
        trains, segs, routes = analyse.analyse(schedule.services(self.by_uid, WEEK), net,
                                               analyse.Stations(self.tiplocs, naptan))
        gw = next(t for t in trains if t["uid"] == "G00001" and t["mins"] == 90)
        self.assertAlmostEqual(gw["rmi"], 184_800 / network.METRES_PER_MILE, places=1)
        self.assertEqual(gw["q"], 1.0)
        self.assertAlmostEqual(gw["rmph"], 76.6, places=1)
        self.assertEqual(gw["stops"], 2)              # Didcot is a pass, not a stop
        self.assertEqual(gw["days"], "MT--F--")
        self.assertTrue(103 < gw["cmi"] < 105)        # Paddington-Bristol TM ~104 mi crow
        # KX-York has no network links: falls back to straight line, flagged.
        lner = next(t for t in trains if t["uid"] == "L00001")
        self.assertEqual(lner["q"], 0.0)
        # Route ranking keeps the faster (permanent) GW service.
        r = next(r for r in routes if "Bristol" in r["a"] + r["b"])
        self.assertEqual(r["n"], 2)
        self.assertEqual(r["mins"], 90)
        # Start-to-stop uses working times: Reading 1025 -> Swindon 1053 over 65.7 km.
        rs = next(s for s in segs if s["from"] == "Reading" and s["to"] == "Swindon")
        self.assertEqual(rs["mins"], 28)

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
