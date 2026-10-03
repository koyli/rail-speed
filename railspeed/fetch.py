"""Download raw data sets.

Network Rail Open Data (NROD) feeds need an account; credentials are read from
the NROD_USER / NROD_PASS environment variables. NaPTAN is public.
"""
import base64
import os
import shutil
import sys
import urllib.request

NROD = "https://publicdatafeeds.networkrail.co.uk/ntrod"

SOURCES = {
    # Full weekly-refreshed timetable, all operators, JSON lines, gzipped.
    "schedule": (NROD + "/CifFileAuthenticate?type=CIF_ALL_FULL_DAILY&day=toc-full",
                 "schedule.json.gz", True),
    # Train Planning System network model (nodes + links with distances), tar.bz2 of XML.
    "tps": (NROD + "/SupportingFileAuthenticate?type=TPS", "tps.tar.bz2", True),
    "corpus": (NROD + "/SupportingFileAuthenticate?type=CORPUS", "corpus.json.gz", True),
    # Rail stations only (ATCO area 910): ATCOCode is "9100" + TIPLOC.
    "naptan": ("https://naptan.api.dft.gov.uk/v1/access-nodes?dataFormat=csv&atcoAreaCodes=910",
               "naptan_rail.csv", False),
}


def _auth_header():
    user, pw = os.environ.get("NROD_USER"), os.environ.get("NROD_PASS")
    if not user or not pw:
        sys.exit("Set NROD_USER and NROD_PASS (your Network Rail Open Data login).")
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def download(name, data_dir):
    url, filename, needs_auth = SOURCES[name]
    dest = os.path.join(data_dir, filename)
    req = urllib.request.Request(url, headers={"User-Agent": "rail-speed/0.1"})
    if needs_auth:
        # Unredirected: NROD 302s to a pre-signed S3 URL, which rejects a second auth scheme.
        req.add_unredirected_header("Authorization", _auth_header())
    print(f"Fetching {name} -> {dest}")
    tmp = dest + ".part"
    with urllib.request.urlopen(req, timeout=600) as resp, open(tmp, "wb") as out:
        shutil.copyfileobj(resp, out, 1 << 20)
    os.replace(tmp, dest)
    print(f"  {os.path.getsize(dest) / 1e6:.1f} MB")
    return dest
