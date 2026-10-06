# Rail Speed League

Speed rankings computed from the GB passenger timetable: fastest and slowest
trains end to end, routes ranked by their fastest service, and fastest
start-to-stop runs. Each ranking can use route miles or crow-flies miles.

Plain Python 3.9+, standard library only. Output is a static page in `site/`.

```sh
export NROD_USER=you@example.com NROD_PASS=...   # Network Rail Open Data login
python3 -m railspeed fetch schedule tps naptan   # -> data/
python3 -m railspeed build                # analyse next Mon-Sun -> site/index.html
python3 -m railspeed build --date 2026-10-12 --days 1
python3 -m unittest                       # tests on a synthetic fixture
```

## Data and method

| Need | Source |
|---|---|
| Trains | NROD SCHEDULE, `CIF_ALL_FULL_DAILY` / `toc-full` (JSON lines) |
| Crow-flies | NaPTAN rail stations; `ATCOCode` is `9100` + TIPLOC; OSGB eastings/northings |
| Route miles | TPS network model (`SupportingFileAuthenticate?type=TPS`): track graph, edge lengths in metres; BPLAN also supported |

- **Which trains run**: for each date, each train UID uses its valid schedule
  with the highest-priority STP indicator (C cancel > N > O overlay > P). Trains
  with identical timings on different days are merged, and their days are listed.
- **Passenger only**: status P/1 and advertised categories (OO, XX, XZ, ...);
  buses, ships, empty stock, unadvertised trains, London Underground trains and
  heritage/charter operators (NYMR, West Coast Railways' Jacobite, etc.) and the
  Sheffield tram-train are excluded. So is Eurostar: its schedules only model
  the GB section, with "Paris Nord" as a stub at the tunnel boundary.
- **End-to-end**: public departure at the first public call to public arrival at
  the last one, including dwell time at stops.
- **Route miles**: the length of the shortest path through the track network
  that passes through every timing point the train passes, in order (passing
  points included, so it follows the train's actual route). Each stop sits at
  the middle of the stretch of that path inside it; the first and last get
  half a platform. It's the same path either way, so distances match in both
  directions. Rail-replacement bus and ship links in the model are excluded.
  Track can't be shorter than the straight line, so where the measurement
  comes out shorter (local quirks in the model) the straight line is used and
  the figure is marked `≈`. Checked against published mileages, both ways:
  King's Cross–Edinburgh 392.7 (computed 392.5–392.9), King's Cross–Newcastle
  268.3 (268.5), King's Cross–York 188.3 (188.2), Euston–Glasgow 401.2
  (400.3–401.5). Sunday East Coast trains diverted via Lincoln show about 12
  miles more. The first build parses the 650 MB TPS XML once and caches the
  graph.
- **Routes**: unordered pairs of origin and destination stations, grouped by CRS.
  A route's speed is the speed of its fastest train.
- **Start-to-stop**: the fastest working-timetable run between each pair of
  consecutive calls, kept per operator so the operator filter works. Runs
  timed faster than a train could start and stop over that distance (1 m/s²
  accelerating and braking) are dropped as too short to time reliably: the
  working timetable is in half-minutes, too coarse for very short hops (e.g.
  1 minute for 0.6 miles from Highbury & Islington to Canonbury). In the train
  details panel such legs say so instead of showing a speed.

## Publishing

GitHub Pages serves the page at https://koyli.github.io/rail-speed/.
`.github/workflows/pages.yml` fetches fresh data, runs the tests, builds and
deploys every Saturday at 06:00 UTC, on every push to `main`, and on demand
(Actions tab → Pages → Run workflow). It needs repository secrets `NROD_USER`
and `NROD_PASS`. Nothing generated is committed: `data/` and `site/` are gitignored.

## Licence

Copyright (C) 2026 koyli. Licensed under the GNU General Public License v3.0
or later; see [LICENSE](LICENSE).
