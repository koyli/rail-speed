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
- **Route miles**: shortest path over the network between each pair of
  consecutive timing points, passing points included, so the path follows the
  train's actual route. Each TIPLOC covers 1-3 km of track nodes, so every leg
  starts from the exact node the previous one reached; otherwise each timing
  point would lose its own length from the total. A leg the network can't
  resolve falls back to a straight line, and the train is shown with `≈`.
  Checked against published mileages: King's Cross–Edinburgh 392.7 (computed
  392.6–393.0), King's Cross–York 188.3 (188.2), Paddington–Bristol 118.3 (117.5–118.2).
  The first build parses the 650 MB TPS XML once and caches the graph.
- **Routes**: unordered pairs of origin and destination stations, grouped by CRS.
  A route's speed is the speed of its fastest train.
- **Start-to-stop**: the fastest working-timetable run between each pair of
  consecutive calls.

## Publishing

GitHub Pages serves the page at https://koyli.github.io/rail-speed/.
`.github/workflows/pages.yml` fetches fresh data, runs the tests, builds and
deploys every Saturday at 06:00 UTC, on every push to `main`, and on demand
(Actions tab → Pages → Run workflow). It needs repository secrets `NROD_USER`
and `NROD_PASS`. Nothing generated is committed: `data/` and `site/` are gitignored.
