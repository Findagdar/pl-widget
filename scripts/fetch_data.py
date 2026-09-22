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
  2. Top scorers: pulled from the same API — the single top scorer for
     each club, used to auto-fill the "Key men" card whenever a fixture
     doesn't have a hand-curated keyPlayer entry.
  3. Fixtures: pulled from the same API — the next full unplayed
     matchday's fixtures (home/away/date/venue) for all 20 clubs, one
     entry per team, in the same shape the widget already expects. If no
     key is configured (or the request fails), the existing fixtures in
     data.json are left untouched rather than wiped.
  4. Head-to-head: for each fixture in that matchday, pulled from the
     API's per-match head2head endpoint and boiled down into the numbers
     the widget's "bookie angle" cards use — H2H record, average goals,
     BTTS rate, and the most recent meeting (a built-in "fun fact").
  5. Leaves "lineups" and "keyPlayer" alone. Confirmed lineups usually
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
import time
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
FOOTBALL_DATA_TIMEOUT = 20

# football-data.org free tier: 10 requests/minute. A full run now makes
# standings(1) + scorers(1) + fixtures(1) + head-to-head(1 per fixture,
# ~10) calls, so we throttle every call through this instead of trusting
# each function to stay under the limit on its own.
_MIN_CALL_INTERVAL = 6.5  # seconds
_last_call_at = 0.0


def _throttled_get(url: str, params: dict | None = None) -> requests.Response:
    """requests.get(), but never firing more than ~9/minute at football-data.org."""
    global _last_call_at
    wait = _MIN_CALL_INTERVAL - (time.monotonic() - _last_call_at)
    if wait > 0:
        time.sleep(wait)
    resp = requests.get(
        url,
        headers={"X-Auth-Token": FOOTBALL_DATA_API_KEY},
        params=params,
        timeout=FOOTBALL_DATA_TIMEOUT,
    )
    _last_call_at = time.monotonic()
    return resp


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
        resp = _throttled_get(f"{FOOTBALL_DATA_BASE}/competitions/{FOOTBALL_DATA_COMPETITION}/standings")
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


def fetch_scorers_api() -> dict:
    """
    Pull the competition's top scorers from football-data.org and keep
    just the single top scorer for each club (the scorers list is ranked
    goals-descending, so the first entry seen per team is that team's top
    scorer). Used to auto-fill the widget's "Key men" card when there's no
    hand-curated keyPlayer entry for a team. Returns {} on no key/failure.
    """
    if not FOOTBALL_DATA_API_KEY:
        return {}
    try:
        resp = _throttled_get(
            f"{FOOTBALL_DATA_BASE}/competitions/{FOOTBALL_DATA_COMPETITION}/scorers",
            params={"limit": 100},
        )
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as e:
        print(f"NOTE: football-data.org scorers request failed ({e}); skipping auto top-scorers.")
        return {}

    top_scorers = {}
    for entry in payload.get("scorers", []):
        team_name = (entry.get("team") or {}).get("name", "")
        slug = slugify_club(team_name)
        if not slug or slug in top_scorers:
            continue  # already have this team's top scorer
        player = entry.get("player") or {}
        top_scorers[slug] = {
            "name": player.get("name", "Unknown"),
            "goals": entry.get("goals") or 0,
            "assists": entry.get("assists") or 0,
        }
    return top_scorers


def fetch_head_to_head_api(match_id: int) -> dict:
    """
    Pull head-to-head history for one fixture (by its football-data.org
    match id) and boil it down into the numbers the widget's "bookie
    angle" cards use: H2H record, average goals per meeting, BTTS rate,
    and the most recent meeting (used as a built-in "fun fact"). The
    aggregates' "homeTeam"/"awayTeam" refer to the current fixture's
    home/away side, which is exactly how the widget keys everything else.
    Returns {} on any failure or if the two sides have no history.
    """
    if not FOOTBALL_DATA_API_KEY:
        return {}
    try:
        resp = _throttled_get(f"{FOOTBALL_DATA_BASE}/matches/{match_id}/head2head", params={"limit": 10})
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as e:
        print(f"NOTE: football-data.org head2head request failed for match {match_id} ({e}); skipping.")
        return {}

    agg = payload.get("aggregates") or {}
    played = agg.get("numberOfMatches") or 0
    if not played:
        return {}

    home_agg = agg.get("homeTeam") or {}
    away_agg = agg.get("awayTeam") or {}
    total_goals = agg.get("totalGoals") or 0

    both_scored = 0
    counted = 0
    last_meeting = None
    last_date = ""
    for m in payload.get("matches") or []:
        score = (m.get("score") or {}).get("fullTime") or {}
        h, a = score.get("home"), score.get("away")
        if h is None or a is None:
            continue
        counted += 1
        if h > 0 and a > 0:
            both_scored += 1
        m_date = m.get("utcDate") or ""
        if m_date > last_date:
            last_date = m_date
            last_meeting = {
                "date": m_date,
                "home": (m.get("homeTeam") or {}).get("name", ""),
                "away": (m.get("awayTeam") or {}).get("name", ""),
                "homeScore": h,
                "awayScore": a,
                "competition": (m.get("competition") or {}).get("name", ""),
            }

    return {
        "played": played,
        "homeWins": home_agg.get("wins", 0),
        "draws": home_agg.get("draws", 0),
        "awayWins": away_agg.get("wins", 0),
        "avgGoals": round(total_goals / played, 1) if played else None,
        "bttsPct": round(both_scored / counted * 100) if counted else None,
        "lastMeeting": last_meeting,
    }


def fetch_fixtures_api(teams: dict) -> dict:
    """
    Pull the next full unplayed matchday's fixtures from football-data.org
    and shape them into the same {slug: {opponent, home, date, venue,
    competition, matchweek, matchId}} structure the widget already reads —
    one entry per team, two entries per match. Returns {} (leaving
    whatever fixtures data.json already has untouched) if no API key is
    set or the request fails/returns nothing.
    """
    if not FOOTBALL_DATA_API_KEY:
        return {}
    try:
        resp = _throttled_get(
            f"{FOOTBALL_DATA_BASE}/competitions/{FOOTBALL_DATA_COMPETITION}/matches",
            params={"status": "SCHEDULED"},
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
            "matchId": m.get("id"),
        }
        fixtures[home_slug] = {"opponent": away_slug, "home": True, **common}
        fixtures[away_slug] = {"opponent": home_slug, "home": False, **common}

    return fixtures


def fetch_all_head_to_head(fixtures: dict) -> dict:
    """
    Call fetch_head_to_head_api() once per fixture in `fixtures` (not once
    per team — fixtures has two entries per match) and key the results by
    the home team's slug, same as `fixtures` itself.
    """
    if not FOOTBALL_DATA_API_KEY:
        return {}
    head_to_head = {}
    seen_match_ids = set()
    for slug, fx in fixtures.items():
        if not fx.get("home"):
            continue
        match_id = fx.get("matchId")
        if not match_id or match_id in seen_match_ids:
            continue
        seen_match_ids.add(match_id)
        h2h = fetch_head_to_head_api(match_id)
        if h2h:
            head_to_head[slug] = h2h
    return head_to_head


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

    print("Fetching top scorers…")
    top_scorers = fetch_scorers_api()
    if top_scorers:
        print(f"Fetched top scorers for {len(top_scorers)} clubs from football-data.org.")
    else:
        top_scorers = existing.get("topScorers", {})

    print("Fetching fixtures…")
    api_fixtures = fetch_fixtures_api(teams)
    if api_fixtures:
        sample_mw = next(iter(api_fixtures.values()))["matchweek"]
        print(f"Fetched {len(api_fixtures) // 2} fixtures from football-data.org (matchday {sample_mw}).")
        fixtures = api_fixtures

        print("Fetching head-to-head history for each fixture (paced to respect the API's rate limit)…")
        head_to_head = fetch_all_head_to_head(api_fixtures)
        if head_to_head:
            print(f"Fetched head-to-head history for {len(head_to_head)} fixture(s).")
    else:
        if not FOOTBALL_DATA_API_KEY:
            print("No FOOTBALL_DATA_API_KEY configured — leaving fixtures unchanged. See README to set one up.")
        fixtures = existing.get("fixtures", {})
        head_to_head = existing.get("headToHead", {})

    result = {
        "_readme": existing.get("_readme", "Auto-refreshed by scripts/fetch_data.py."),
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "standings": standings,
        "fixtures": fixtures,
        "headToHead": head_to_head,
        "topScorers": top_scorers,
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
