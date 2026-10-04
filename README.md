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
- **Route miles**: shortest path over the network through every timing
  point the train passes, passing points included, so the path follows the
  train's actual route. Rail-replacement bus and ship links in the model are
  excluded. Each TIPLOC covers 1-3 km of track nodes, so every stop is placed at
  its centre (the median chainage of its nodes) and distances run centre to
  centre. The path normally stays continuous from leg to leg, but a leg that
  that would make an out-and-back detour to a crossover (3 km or more longer
  than measuring it from scratch) is re-measured from scratch.
  Track can't be shorter than the straight line, so where the measurement comes
  out shorter (local quirks in the model) the straight line is used and the
  figure is marked `≈`. Checked against published mileages: King's
  Cross–Edinburgh 392.7 (computed 392.8–393.2), Stevenage–Grantham 77.9 (77.9),
  Paddington–Bristol 118.3 (117.9–118.6). The first build parses the 650 MB TPS
  XML once and caches the graph.
- **Routes**: unordered pairs of origin and destination stations, grouped by CRS.
  A route's speed is the speed of its fastest train.
- **Start-to-stop**: the fastest working-timetable run between each pair of
  consecutive calls, kept per operator so the operator filter works. Runs
  timed faster than a train could start and stop over that distance (1 m/s²
  accelerating and braking) are timetable artefacts and are dropped, e.g. 30
  seconds for the mile from Southend Central to Southend East.

## Publishing

GitHub Pages serves the page at https://koyli.github.io/rail-speed/.
`.github/workflows/pages.yml` fetches fresh data, runs the tests, builds and
deploys every Saturday at 06:00 UTC, on every push to `main`, and on demand
(Actions tab → Pages → Run workflow). It needs repository secrets `NROD_USER`
and `NROD_PASS`. Nothing generated is committed: `data/` and `site/` are gitignored.

## Licence

Copyright (C) 2026 koyli. Licensed under the GNU General Public License v3.0
or later; see [LICENSE](LICENSE).
