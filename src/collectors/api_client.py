"""Collector for 'Free API Live Football Data' (RapidAPI).

Fetches today's matches from the free-api-live-football-data API and stores
them in the local SQLite database (data/football.db), skipping matches that
were already saved and refreshing the score/status of ones that changed.

Endpoint used: GET /football-get-matches-by-date?date=YYYYMMDD
"""

import os
import re
import sqlite3
import sys
import time
from datetime import date

import requests
from dotenv import load_dotenv

# --- Paths ------------------------------------------------------------------
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ENV_PATH = os.path.join(BASE_DIR, ".env")
DB_PATH = os.path.join(BASE_DIR, "data", "football.db")

sys.path.insert(0, os.path.join(BASE_DIR, "src", "database"))
from migrate_add_secondary_stats import migrate as ensure_secondary_stats_schema  # noqa: E402

# --- API config ----------------------------------------------------------------
API_HOST = "free-api-live-football-data.p.rapidapi.com"
MATCHES_BY_DATE_URL = f"https://{API_HOST}/football-get-matches-by-date"
MATCH_STATS_URL = f"https://{API_HOST}/football-get-match-all-stats"

FINISHED_STATUSES = ("FT", "AET", "Pen", "ET", "AP")

# How many finished matches to backfill corner/card stats for in one call
# (each needs its own request, so keep this modest on the free plan).
STATS_ENRICH_LIMIT = int(os.getenv("STATS_ENRICH_LIMIT", "20"))
STATS_REQUEST_PAUSE = float(os.getenv("STATS_REQUEST_PAUSE", "0.7"))

load_dotenv(ENV_PATH)
API_KEY = os.getenv("API_KEY")


# --------------------------------------------------------------------------- #
# Commercial filter - keep senior, top-tier men's football; drop youth /
# reserve / women's / lower-division sides that are not useful for the
# tipster content pipeline.
# --------------------------------------------------------------------------- #
_LOWTIER_PATTERNS = [
    r"\bu-?1[5-9]\b",          # U15..U19  (and "U-19")
    r"\bu-?2[0-3]\b",          # U20..U23
    r"\bsub[-\s]?1[5-9]\b",    # Spanish "Sub 19"
    r"\bsub[-\s]?2[0-3]\b",
    r"\bunder[-\s]?\d{2}\b",
    r"\breserv",              # Reserve(s) / Reserva(s)
    r"\byouth\b",
    r"\bjuvenil\b",
    r"\bprimavera\b",         # Italian youth competition
    r"\bacademy\b",
    r"\bwomen'?s?\b",
    r"\bfemenin[oa]\b",
    r"\bfeminin",             # feminin / féminine / feminino
    r"\bfrauen\b",
    r"\bdames\b",
    r"\(w\)",                 # "(W)" women marker
    r"\sB$",                  # trailing " B"  (e.g. "Athletic Club B")
    r"\sII+$",               # trailing " II" / " III"
    r"\s[23]$",               # trailing " 2" / " 3"  (Scandinavian reserves)
]
_LOWTIER_RE = re.compile("|".join(_LOWTIER_PATTERNS), re.IGNORECASE)


def contains_lowtier_keyword(text):
    """True if `text` carries a youth / reserve / women's / lower-tier marker."""
    return bool(text) and _LOWTIER_RE.search(text) is not None


def is_commercial_match(home_team, away_team, league_name=""):
    """False if either team name or the league name looks like youth, reserve,
    women's or an otherwise non-commercial competition; True otherwise.
    """
    return not any(contains_lowtier_keyword(x)
                   for x in (home_team, away_team, league_name))


def get_todays_matches(target_date=None):
    """Call the API and return the list of match objects for a date.

    `target_date` is a datetime.date (defaults to today). The API expects the
    date as a YYYYMMDD string.
    """
    if not API_KEY:
        raise RuntimeError(f"API_KEY not found. Checked {ENV_PATH}")

    target_date = target_date or date.today()
    headers = {
        "X-RapidAPI-Key": API_KEY,
        "X-RapidAPI-Host": API_HOST,
    }
    params = {"date": target_date.strftime("%Y%m%d")}

    response = requests.get(
        MATCHES_BY_DATE_URL, headers=headers, params=params, timeout=30
    )
    response.raise_for_status()
    payload = response.json()

    if payload.get("status") != "success":
        raise RuntimeError(f"API did not return success: {payload}")

    matches = payload.get("response", {}).get("matches", [])
    print(f"API returned {len(matches)} matches for {params['date']}")
    return matches


def _status_str(status):
    """Reduce the API's `status` object to a short code for the DB."""
    reason = status.get("reason") or {}
    if reason.get("short"):
        return reason["short"]          # e.g. FT, AET, Pen, PP, Canc.
    if status.get("cancelled"):
        return "CANC"
    if status.get("finished"):
        return "FT"
    if status.get("started"):
        return "LIVE"
    return "NS"


def _parse_match(item):
    """Flatten one API match object into a row for the `matches` table."""
    home = item.get("home", {})
    away = item.get("away", {})
    status = item.get("status", {})

    return {
        "match_id": item.get("id"),
        # Prefer the ISO UTC kickoff; fall back to the local "time" string.
        "date": status.get("utcTime") or item.get("time"),
        # This API only exposes a numeric league id, not a name.
        "league": str(item["leagueId"]) if item.get("leagueId") is not None else None,
        "home_team": home.get("longName") or home.get("name"),
        "away_team": away.get("longName") or away.get("name"),
        "home_score": home.get("score"),
        "away_score": away.get("score"),
        "status": _status_str(status),
    }


def save_rows(rows, db_path=DB_PATH):
    """Upsert already-parsed match rows into the DB.

    `rows` is an iterable of dicts with the same keys as `_parse_match` returns.
    New match_ids are inserted; existing ones only have score/status refreshed.
    Returns a tuple (inserted, updated).
    """
    ensure_secondary_stats_schema(db_path)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    inserted = 0
    updated = 0
    for row in rows:
        if row.get("match_id") is None:
            continue

        cursor.execute(
            "SELECT home_score, away_score, status FROM matches WHERE match_id = ?",
            (row["match_id"],),
        )
        existing = cursor.fetchone()

        if existing is None:
            cursor.execute(
                """
                INSERT INTO matches
                    (match_id, date, league, home_team, away_team,
                     home_score, away_score, status)
                VALUES
                    (:match_id, :date, :league, :home_team, :away_team,
                     :home_score, :away_score, :status)
                """,
                row,
            )
            inserted += 1
        elif tuple(existing) != (row["home_score"], row["away_score"], row["status"]):
            # Same match already stored: refresh live score / status only.
            cursor.execute(
                """
                UPDATE matches
                   SET home_score = :home_score,
                       away_score = :away_score,
                       status = :status
                 WHERE match_id = :match_id
                """,
                row,
            )
            updated += 1

    conn.commit()
    conn.close()
    return inserted, updated


def save_matches(matches, db_path=DB_PATH):
    """Parse raw API match objects and upsert them. Returns (inserted, updated)."""
    return save_rows((_parse_match(m) for m in matches), db_path=db_path)


# --------------------------------------------------------------------------- #
# Secondary stats: corners + cards (from the match's own statistics endpoint)
# --------------------------------------------------------------------------- #
def _stat_pair(groups, key):
    """Find the first {home, away} pair for a stat `key` inside the nested
    /football-get-match-all-stats groups. Returns (home, away) ints or
    (None, None) if the stat isn't present or isn't numeric.
    """
    for group in groups or []:
        for stat in group.get("stats", []) or []:
            if stat.get("key") == key:
                values = stat.get("stats") or [None, None]
                out = []
                for v in values[:2]:
                    try:
                        out.append(int(v))
                    except (TypeError, ValueError):
                        out.append(None)
                if out[0] is not None or out[1] is not None:
                    return out[0], out[1]
    return None, None


def fetch_match_secondary_stats(match_id):
    """Return {home_corners, away_corners, ...cards} for one match, or None
    fields where the data isn't available (e.g. match hasn't been played).
    """
    resp = requests.get(
        MATCH_STATS_URL,
        headers={"X-RapidAPI-Key": API_KEY, "X-RapidAPI-Host": API_HOST},
        params={"eventid": match_id},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != "success":
        raise RuntimeError(f"match {match_id}: {payload}")

    groups = payload.get("response", {}).get("stats", [])
    hc, ac = _stat_pair(groups, "corners")
    hy, ay = _stat_pair(groups, "yellow_cards")
    hr, ar = _stat_pair(groups, "red_cards")
    return {
        "home_corners": hc, "away_corners": ac,
        "home_yellow_cards": hy, "away_yellow_cards": ay,
        "home_red_cards": hr, "away_red_cards": ar,
    }


def matches_missing_secondary_stats(db_path=DB_PATH, limit=STATS_ENRICH_LIMIT):
    """Match ids for finished games that still have no corner/card data."""
    conn = sqlite3.connect(db_path)
    try:
        placeholders = ",".join("?" for _ in FINISHED_STATUSES)
        rows = conn.execute(
            f"""
            SELECT match_id FROM matches
             WHERE status IN ({placeholders})
               AND home_corners IS NULL
             ORDER BY date DESC
             LIMIT ?
            """,
            (*FINISHED_STATUSES, limit),
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def matches_missing_secondary_stats_for_teams(team_names, db_path=DB_PATH,
                                              limit=STATS_ENRICH_LIMIT):
    """Match ids for finished games involving any of `team_names` that still
    have no corner/card data. Used to aggressively fill in the exact teams we
    are about to run projections for.
    """
    teams = [t for t in {t for t in team_names if t}]
    if not teams:
        return []
    conn = sqlite3.connect(db_path)
    try:
        st_ph = ",".join("?" for _ in FINISHED_STATUSES)
        tm_ph = ",".join("?" for _ in teams)
        rows = conn.execute(
            f"""
            SELECT match_id FROM matches
             WHERE status IN ({st_ph})
               AND home_corners IS NULL
               AND (home_team IN ({tm_ph}) OR away_team IN ({tm_ph}))
             ORDER BY date DESC
             LIMIT ?
            """,
            (*FINISHED_STATUSES, *teams, *teams, limit),
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def enrich_secondary_stats(match_ids, db_path=DB_PATH, pause=STATS_REQUEST_PAUSE):
    """Fetch + store corners/cards for each match id. Returns (ok, failed) counts."""
    ensure_secondary_stats_schema(db_path)
    conn = sqlite3.connect(db_path)
    ok = failed = 0
    try:
        for i, match_id in enumerate(match_ids):
            try:
                stats = fetch_match_secondary_stats(match_id)
                stats["match_id"] = match_id
                conn.execute(
                    """
                    UPDATE matches
                       SET home_corners = :home_corners,
                           away_corners = :away_corners,
                           home_yellow_cards = :home_yellow_cards,
                           away_yellow_cards = :away_yellow_cards,
                           home_red_cards = :home_red_cards,
                           away_red_cards = :away_red_cards
                     WHERE match_id = :match_id
                    """,
                    stats,
                )
                ok += 1
            except Exception as exc:
                failed += 1
                print(f"  [stats] match {match_id} failed: {exc}")
            if i < len(match_ids) - 1:
                time.sleep(pause)
        conn.commit()
    finally:
        conn.close()
    return ok, failed


def main():
    matches = get_todays_matches()
    inserted, updated = save_matches(matches)
    print(f"Saved to {DB_PATH}: {inserted} new, {updated} updated, "
          f"{len(matches) - inserted - updated} unchanged/skipped.")

    to_enrich = matches_missing_secondary_stats(DB_PATH)
    if to_enrich:
        print(f"Backfilling corners/cards for {len(to_enrich)} finished "
              f"match(es) missing them...")
        ok, failed = enrich_secondary_stats(to_enrich)
        print(f"  stats: {ok} ok, {failed} failed.")


if __name__ == "__main__":
    main()
