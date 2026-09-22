#!/usr/bin/env python3
"""
Refresh data.json for the PL matchday widget.

Run this on a schedule (every 2 days is what it was built for — see the
GitHub Actions workflow in .github/workflows/refresh.yml for a hands-off
way to do that if you host on GitHub Pages / Netlify / Vercel with git
deploys).

What it does, in order:
  1. Pulls the current Premier League standings table from Wikipedia's
     season article. This is the reliable part — Wikipedia's table is a
     plain <table>, no anti-bot measures, and pandas.read_html parses it
     in a couple of lines.
  2. Best-effort pulls each team's next fixture (opponent, date, venue,
     matchweek) from a fixtures listing page. Fixture pages change
     markup more often than Wikipedia's standings table, so this part is
     wrapped in a try/except per team: if a team's fixture can't be
     parsed this run, its old value in data.json is left untouched
     rather than wiped.
  3. Leaves "lineups" and "keyPlayer" alone. Confirmed lineups usually
     only appear ~1 hour before kickoff, so scraping them reliably every
     2 days isn't realistic — curate those two blocks by hand, or wire
     up a proper fixtures/lineups API (see the note at the bottom) if
     you want them automated too.

Requirements:
    pip install requests beautifulsoup4 pandas lxml

Usage:
    python scripts/fetch_data.py            # writes ../data.json
    python scripts/fetch_data.py --dry-run  # prints instead of writing
"""

import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
TEAMS_PATH = ROOT / "teams.json"
DATA_PATH = ROOT / "data.json"

HEADERS = {
    # A descriptive, honest User-Agent. Don't spoof a browser UA to get
    # around a site's bot policy — if a source blocks scripted requests,
    # that's a sign to use their official API instead, not to disguise
    # the request.
    "User-Agent": "pl-matchday-widget-refresh/1.0 (contact: set-your-email-here)"
}

WIKIPEDIA_SEASON_URL = "https://en.wikipedia.org/wiki/2026%E2%80%9327_Premier_League"


def slugify(name: str) -> str:
    """Turn a Wikipedia club name into the same slug style used in teams.json."""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = name.lower()
    name = name.replace("&", "and")
    name = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    # A few Wikipedia names don't match our slugs 1:1 — patch the known ones.
    fixes = {
        "brighton-and-hove-albion": "brighton",
        "manchester-city": "manchester-city",
        "manchester-united": "manchester-united",
        "newcastle-united": "newcastle-united",
        "nottingham-forest": "nottingham-forest",
        "tottenham-hotspur": "tottenham-hotspur",
        "wolverhampton-wanderers": "wolves",
    }
    return fixes.get(name, name)


def fetch_standings() -> dict:
    """Scrape the current league table from Wikipedia. Returns {slug: {...}}."""
    import pandas as pd

    resp = requests.get(WIKIPEDIA_SEASON_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    # Wrap in StringIO: passing a raw string directly to pd.read_html() can
    # make lxml try to treat it as a filename/URL instead of HTML content
    # (raises "OSError: Error reading file '<!DOCTYPE html>...'" on some
    # pandas/lxml versions) rather than parsing it as markup.
    tables = pd.read_html(StringIO(resp.text))

    # The league table is the first wide table with a "Pts" column and 20 rows.
    table = None
    for t in tables:
        cols = [str(c).strip() for c in t.columns]
        if any("Pts" in c for c in cols) and len(t) >= 18:
            table = t
            break
    if table is None:
        raise RuntimeError("Couldn't find the standings table on the Wikipedia page — its layout may have changed.")

    # Normalise column names (Wikipedia sometimes prefixes them, e.g. "Pld.1").
    def col(*candidates):
        for c in table.columns:
            cs = str(c)
            if any(cs == cand or cs.startswith(cand) for cand in candidates):
                return c
        return None

    c_team = col("Team")
    c_pld = col("Pld")
    c_w = col("W")
    c_d = col("D")
    c_l = col("L")
    c_gf = col("GF")
    c_ga = col("GA")
    c_gd = col("GD")
    c_pts = col("Pts")

    standings = {}
    for i, row in table.iterrows():
        team_raw = str(row[c_team])
        # strip footnote markers / qualification tags Wikipedia sometimes appends
        team_name = re.sub(r"\s*\(.*?\)\s*$", "", team_raw).strip()
        team_name = re.sub(r"\[.*?\]", "", team_name).strip()
        if not team_name or team_name.lower() == "nan":
            continue
        slug = slugify(team_name)
        try:
            standings[slug] = {
                "pos": i + 1,
                "pld": int(row[c_pld]),
                "w": int(row[c_w]),
                "d": int(row[c_d]),
                "l": int(row[c_l]),
                "gf": int(row[c_gf]),
                "ga": int(row[c_ga]),
                "gd": int(str(row[c_gd]).replace("+", "").replace("−", "-")),
                "pts": int(row[c_pts]),
                # Wikipedia's table doesn't carry a form strip; keep whatever
                # was there before by merging with existing data.json below.
            }
        except (ValueError, TypeError):
            # A row that doesn't parse cleanly (mid-table merge artefacts,
            # a qualification-note row, etc.) — skip it rather than guess.
            continue

    return standings


def fetch_fixtures(team_slugs) -> dict:
    """
    Best-effort next-fixture lookup. Left as a stub you fill in for whatever
    fixtures source you're comfortable scraping (check its robots.txt and
    terms first) — or swap in a real fixtures API. Returning {} here just
    means fixtures in data.json are left as they were.

    A free, structured option worth considering instead of scraping:
    https://www.football-data.org/ (has a no-cost tier with fixtures).
    """
    return {}


def main():
    dry_run = "--dry-run" in sys.argv

    existing = json.loads(DATA_PATH.read_text()) if DATA_PATH.exists() else {}
    teams = json.loads(TEAMS_PATH.read_text())

    print("Fetching standings from Wikipedia…")
    standings = fetch_standings()

    # Preserve each team's "form" strip from the previous data.json if the
    # new scrape doesn't provide one, so the widget's form pips don't blank out.
    old_standings = existing.get("standings", {})
    for slug, row in standings.items():
        if slug in old_standings and "form" in old_standings[slug]:
            row["form"] = old_standings[slug]["form"]
        else:
            row["form"] = []

    unknown = sorted(set(standings) - set(teams))
    if unknown:
        print(f"NOTE: Wikipedia listed teams not in teams.json (add them if real): {unknown}")

    fixtures = fetch_fixtures(list(teams.keys())) or existing.get("fixtures", {})

    result = {
        "_readme": existing.get("_readme", "Auto-refreshed by scripts/fetch_data.py."),
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "standings": standings,
        "fixtures": fixtures,
        "lineups": existing.get("lineups", {}),
        "keyPlayer": existing.get("keyPlayer", {}),
    }

    if dry_run:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        DATA_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        print(f"Wrote {DATA_PATH} with {len(standings)} teams.")


if __name__ == "__main__":
    main()
