"""Backfill historical results so the Poisson model has more than one game/team.

The free API has no per-team history endpoint, but it does expose a full
season of results per competition:

    GET /football-get-all-matches-by-league?leagueid=<id>

Strategy:
  1. Look at today's fixtures -> collect the leagues and the team names playing.
  2. For the busiest of those leagues (capped, to respect the free quota),
     download the league's results.
  3. Keep finished matches that involve a team playing today AND are either
     within the last LOOKBACK_DAYS (default 90 = "last 3 months") or among
     that team's last LAST_N_GAMES (default 10).
  4. Upsert them into data/football.db via api_client.save_rows().
"""

import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_client import (  # noqa: E402  (local module, sys.path set above)
    API_HOST, DB_PATH, ENV_PATH, _status_str, get_todays_matches, save_rows,
    enrich_secondary_stats, matches_missing_secondary_stats,
    matches_missing_secondary_stats_for_teams,
)

load_dotenv(ENV_PATH, override=True)
API_KEY = os.getenv("API_KEY")

LEAGUE_MATCHES_URL = f"https://{API_HOST}/football-get-all-matches-by-league"

LOOKBACK_DAYS = int(os.getenv("HISTORY_LOOKBACK_DAYS", "90"))
LAST_N_GAMES = int(os.getenv("HISTORY_LAST_N_GAMES", "10"))
# Many of "today's" competitions are obscure and carry no season history; sweep
# a generous number so the established leagues (often only 1-2 games today) are
# covered too.
MAX_LEAGUES = int(os.getenv("HISTORY_MAX_LEAGUES", "40"))
REQUEST_PAUSE = float(os.getenv("HISTORY_REQUEST_PAUSE", "0.8"))  # seconds
# Corners/cards need one extra request per match; cap how many we backfill
# per run so a big history pull doesn't blow the free-plan quota.
HISTORY_STATS_LIMIT = int(os.getenv("HISTORY_STATS_LIMIT", "40"))


def _headers():
    return {"X-RapidAPI-Key": API_KEY, "X-RapidAPI-Host": API_HOST}


def fetch_league_matches(league_id):
    """Return the raw list of match objects for a league (season schedule)."""
    for attempt in range(2):
        resp = requests.get(
            LEAGUE_MATCHES_URL,
            headers=_headers(),
            params={"leagueid": league_id},
            timeout=30,
        )
        if resp.status_code == 429:  # rate limited - back off once
            time.sleep(5 * (attempt + 1))
            continue
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("status") != "success":
            raise RuntimeError(f"league {league_id}: {payload}")
        return payload.get("response", {}).get("matches", [])
    raise RuntimeError(f"league {league_id}: rate limited")


def _parse_league_match(item, league_id):
    """Flatten a /football-get-all-matches-by-league item into a DB row."""
    home = item.get("home", {})
    away = item.get("away", {})
    status = item.get("status", {})
    try:
        match_id = int(item.get("id"))
    except (TypeError, ValueError):
        return None

    return {
        "match_id": match_id,
        "date": status.get("utcTime"),
        "league": str(league_id),
        "home_team": home.get("name"),
        "away_team": away.get("name"),
        "home_score": home.get("score"),
        "away_score": away.get("score"),
        "status": _status_str(status),
    }


def _match_dt(row):
    raw = (row.get("date") or "").replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def collect_history(today_matches):
    """Return a list of DB rows: recent finished games for today's teams."""
    # Leagues + team names in action today.
    league_counts = defaultdict(int)
    todays_teams = set()
    for m in today_matches:
        league_counts[m["leagueId"]] += 1
        todays_teams.add(m["home"]["name"])
        todays_teams.add(m["away"]["name"])

    leagues = [lid for lid, _ in sorted(
        league_counts.items(), key=lambda kv: kv[1], reverse=True
    )][:MAX_LEAGUES]

    print(f"{len(todays_teams)} teams playing today across "
          f"{len(league_counts)} leagues; pulling history for the top "
          f"{len(leagues)} leagues.")

    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    kept = {}

    for i, league_id in enumerate(leagues, 1):
        try:
            raw = fetch_league_matches(league_id)
        except Exception as exc:
            print(f"  [{i}/{len(leagues)}] league {league_id}: FAILED ({exc})")
            continue

        rows = []
        for item in raw:
            if not item.get("status", {}).get("finished"):
                continue
            row = _parse_league_match(item, league_id)
            if not row or row["home_score"] is None or row["away_score"] is None:
                continue
            if row["home_team"] not in todays_teams and \
               row["away_team"] not in todays_teams:
                continue
            rows.append(row)

        # Per team: keep last-N games OR anything newer than the cutoff.
        per_team = defaultdict(list)
        for row in rows:
            dt = _match_dt(row)
            if dt is None:
                continue
            for side in ("home_team", "away_team"):
                if row[side] in todays_teams:
                    per_team[row[side]].append((dt, row))

        added = 0
        for team, entries in per_team.items():
            entries.sort(key=lambda t: t[0], reverse=True)
            for idx, (dt, row) in enumerate(entries):
                if idx < LAST_N_GAMES or dt >= cutoff:
                    if row["match_id"] not in kept:
                        kept[row["match_id"]] = row
                        added += 1
        print(f"  [{i}/{len(leagues)}] league {league_id}: "
              f"{len(raw)} season games -> +{added} kept")

        if i < len(leagues):
            time.sleep(REQUEST_PAUSE)

    return list(kept.values())


def main():
    if not API_KEY:
        raise SystemExit(f"API_KEY not found. Checked {ENV_PATH}")

    print("Fetching today's fixtures to know which teams/leagues to backfill...")
    today_matches = get_todays_matches()

    rows = collect_history(today_matches)
    print(f"\nCollected {len(rows)} historical finished matches.")

    inserted, updated = save_rows(rows, db_path=DB_PATH)
    print(f"Saved to {DB_PATH}: {inserted} new, {updated} updated, "
          f"{len(rows) - inserted - updated} already current.")

    # Prioritise corner/card backfill for the exact teams playing today, so the
    # secondary-markets model has real data for the fixtures we care about;
    # then top up with whatever else is missing, up to the cap.
    todays_teams = {t for m in today_matches
                    for t in (m["home"]["name"], m["away"]["name"])}
    priority = matches_missing_secondary_stats_for_teams(
        todays_teams, DB_PATH, limit=HISTORY_STATS_LIMIT)
    extra = [mid for mid in matches_missing_secondary_stats(DB_PATH, limit=HISTORY_STATS_LIMIT)
             if mid not in set(priority)]
    to_enrich = (priority + extra)[:HISTORY_STATS_LIMIT]
    if to_enrich:
        print(f"\nBackfilling corners/cards for {len(to_enrich)} match(es) "
              f"({len(priority)} involving today's teams, capped at "
              f"{HISTORY_STATS_LIMIT})...")
        ok, failed = enrich_secondary_stats(to_enrich)
        print(f"  stats: {ok} ok, {failed} failed.")


if __name__ == "__main__":
    main()
