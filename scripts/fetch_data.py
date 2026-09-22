#!/usr/bin/env python3
"""
Refresh data.json for the PL matchday widget.

Run this on a schedule (every 2 days is what it was built for — see the
GitHub Actions workflow in .github/workflows/refresh.yml for a hands-off
way to do that if you host on GitHub Pages / Netlify / Vercel with git
deploys).

What it does, in order:
  1. Standings: pulled from football-data.org's free API when a
     FOOTBALL_DATA_API_KEY is configured (see README — takes under a
     minute to get one, free, no card required). Falls back to scraping
     Wikipedia's season table if no key is set or the API request fails,
     so the widget still works out of the box with zero setup.
  2. Fixtures: pulled from the same API — the next full unplayed
     matchday's fixtures (home/away/date/venue) for all 20 clubs, one
     entry per team, in the same shape the widget already expects. If no
     key is configured (or the request fails), the existing fixtures in
     data.json are left untouched rather than wiped.
  3. Leaves "lineups" and "keyPlayer" alone. Confirmed lineups usually
     only appear ~1 hour before kickoff, so scraping them reliably every
     2 days isn't realistic — curate those two blocks by hand, or wire
     up a lineups-capable API if you want them automated too.

Requirements:
    pip install requests beautifulsoup4 pandas lxml

Environment:
    FOOTBALL_DATA_API_KEY   optional but recommended — a free API token
                             from https://www.football-data.org/client/register
                             In GitHub Actions, set it as a repository
                             secret of the same name (see README).

Usage:
    python scripts/fetch_data.py            # writes ../data.json
    python scripts/fetch_data.py --dry-run  # prints instead of writing
"""

import json
import os
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

FOOTBALL_DATA_BASE = "https://api.football-data.org/v4"
FOOTBALL_DATA_COMPETITION = "PL"
FOOTBALL_DATA_API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY", "").strip()

# football-data.org free tier: 10 requests/minute once you have a token.
# We make at most 2 calls per run (standings + matches), so this is never
# close to the limit.
FOOTBALL_DATA_TIMEOUT = 20


def slugify(name: str) -> str:
    """Turn a club name into the same slug style used in teams.json."""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = name.lower()
    name = name.replace("&", "and")
    name = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    # A few sources' names don't match our slugs 1:1 — patch the known ones.
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


def slugify_club(name: str) -> str:
    """
    Like slugify(), but first strips the "FC"/"AFC"/"CF" club-name
    decoration that football-data.org's API uses ("Arsenal FC",
    "AFC Bournemouth", "Nottingham Forest FC") so both that API and the
    plainer Wikipedia names ("Arsenal", "Bournemouth") collapse to the
    same slug.
    """
    base = name.strip()
    if base.startswith("AFC "):
        base = base[4:]
    for suffix in (" FC", " AFC", " CF"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return slugify(base)


def fetch_standings_wikipedia() -> dict:
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
                "form": [],
            }
        except (ValueError, TypeError):
            # A row that doesn't parse cleanly (mid-table merge artefacts,
            # a qualification-note row, etc.) — skip it rather than guess.
            continue

    return standings


def fetch_standings_api() -> dict:
    """
    Pull the current table from football-data.org's free API. Returns {}
    (never raises) if no API key is configured or the request fails, so
    the caller can fall back to the Wikipedia scrape.
    """
    if not FOOTBALL_DATA_API_KEY:
        return {}
    try:
        resp = requests.get(
            f"{FOOTBALL_DATA_BASE}/competitions/{FOOTBALL_DATA_COMPETITION}/standings",
            headers={"X-Auth-Token": FOOTBALL_DATA_API_KEY},
            timeout=FOOTBALL_DATA_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as e:
        print(f"NOTE: football-data.org standings request failed ({e}); falling back to Wikipedia.")
        return {}

    table = None
    for block in payload.get("standings", []):
        if block.get("type") == "TOTAL":
            table = block.get("table")
            break
    if not table:
        return {}

    standings = {}
    for row in table:
        team_name = row.get("team", {}).get("name", "")
        slug = slugify_club(team_name)
        # football-data's "form" is a comma-separated string of the most
        # recent results; take the last 3 for the widget's pips. Order is
        # oldest-to-newest per their docs, so the tail is the most recent.
        form_raw = row.get("form") or ""
        form = [f.strip() for f in form_raw.split(",") if f.strip()][-3:]
        standings[slug] = {
            "pos": row["position"],
            "pld": row["playedGames"],
            "w": row["won"],
            "d": row["draw"],
            "l": row["lost"],
            "gf": row["goalsFor"],
            "ga": row["goalsAgainst"],
            "gd": row["goalDifference"],
            "pts": row["points"],
            "form": form,
        }
    return standings


def fetch_standings() -> dict:
    """Try the football-data.org API first, fall back to Wikipedia."""
    api_result = fetch_standings_api()
    if api_result:
        print(f"Fetched standings from football-data.org ({len(api_result)} teams).")
        return api_result
    print("Falling back to Wikipedia standings scrape (no FOOTBALL_DATA_API_KEY set, or the API request failed)...")
    return fetch_standings_wikipedia()


def fetch_fixtures_api(teams: dict) -> dict:
    """
    Pull the next full unplayed matchday's fixtures from football-data.org
    and shape them into the same {slug: {opponent, home, date, venue,
    competition, matchweek}} structure the widget already reads — one
    entry per team, two entries per match. Returns {} (leaving whatever
    fixtures data.json already has untouched) if no API key is set or the
    request fails/returns nothing.
    """
    if not FOOTBALL_DATA_API_KEY:
        return {}
    try:
        resp = requests.get(
            f"{FOOTBALL_DATA_BASE}/competitions/{FOOTBALL_DATA_COMPETITION}/matches",
            headers={"X-Auth-Token": FOOTBALL_DATA_API_KEY},
            params={"status": "SCHEDULED"},
            timeout=FOOTBALL_DATA_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as e:
        print(f"NOTE: football-data.org fixtures request failed ({e}); leaving fixtures unchanged.")
        return {}

    matches = [m for m in payload.get("matches", []) if m.get("matchday") is not None]
    if not matches:
        return {}

    # Only the next unplayed round, not the whole rest of the season.
    next_matchday = min(m["matchday"] for m in matches)
    round_matches = [m for m in matches if m["matchday"] == next_matchday]

    fixtures = {}
    for m in round_matches:
        home_slug = slugify_club(m["homeTeam"].get("name", ""))
        away_slug = slugify_club(m["awayTeam"].get("name", ""))
        if home_slug not in teams or away_slug not in teams:
            print(
                f"NOTE: football-data.org match references a team not in teams.json "
                f"({m['homeTeam'].get('name')} vs {m['awayTeam'].get('name')}) — skipping."
            )
            continue
        date = m.get("utcDate")
        venue = m.get("venue") or teams.get(home_slug, {}).get("venue") or "Venue TBC"
        common = {
            "date": date,
            "venue": venue,
            "competition": "Premier League",
            "matchweek": next_matchday,
        }
        fixtures[home_slug] = {"opponent": away_slug, "home": True, **common}
        fixtures[away_slug] = {"opponent": home_slug, "home": False, **common}

    return fixtures


def main():
    dry_run = "--dry-run" in sys.argv

    existing = json.loads(DATA_PATH.read_text()) if DATA_PATH.exists() else {}
    teams = json.loads(TEAMS_PATH.read_text())

    print("Fetching standings…")
    standings = fetch_standings()

    # Preserve each team's "form" strip from the previous data.json only if
    # this run's source didn't already supply one (the API does; Wikipedia
    # doesn't), so the widget's form pips don't blank out.
    old_standings = existing.get("standings", {})
    for slug, row in standings.items():
        if not row.get("form"):
            if slug in old_standings and "form" in old_standings[slug]:
                row["form"] = old_standings[slug]["form"]
            else:
                row["form"] = []

    unknown = sorted(set(standings) - set(teams))
    if unknown:
        print(f"NOTE: standings source listed teams not in teams.json (add them if real): {unknown}")

    print("Fetching fixtures…")
    api_fixtures = fetch_fixtures_api(teams)
    if api_fixtures:
        sample_mw = next(iter(api_fixtures.values()))["matchweek"]
        print(f"Fetched {len(api_fixtures) // 2} fixtures from football-data.org (matchday {sample_mw}).")
        fixtures = api_fixtures
    else:
        if not FOOTBALL_DATA_API_KEY:
            print("No FOOTBALL_DATA_API_KEY configured — leaving fixtures unchanged. See README to set one up.")
        fixtures = existing.get("fixtures", {})

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
