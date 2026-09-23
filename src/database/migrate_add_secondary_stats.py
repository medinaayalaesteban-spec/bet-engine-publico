"""Migration: add corner-kick and card columns to the matches table.

Safe to run any number of times - only adds a column if it isn't there yet.
SQLite has no "ADD COLUMN IF NOT EXISTS", so we check PRAGMA table_info first.
"""

import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "../../data/football.db")

NEW_COLUMNS = (
    ("home_corners", "INTEGER"),
    ("away_corners", "INTEGER"),
    ("home_yellow_cards", "INTEGER"),
    ("away_yellow_cards", "INTEGER"),
    ("home_red_cards", "INTEGER"),
    ("away_red_cards", "INTEGER"),
)


def migrate(db_path=DB_PATH):
    """Add any missing secondary-stats columns to `matches`. Returns the list added."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        existing = {row[1] for row in cur.execute("PRAGMA table_info(matches)")}
        added = []
        for name, sqltype in NEW_COLUMNS:
            if name not in existing:
                cur.execute(f"ALTER TABLE matches ADD COLUMN {name} {sqltype}")
                added.append(name)
        conn.commit()
    finally:
        conn.close()
    return added


if __name__ == "__main__":
    added = migrate()
    if added:
        print(f"Migration applied. Added columns: {', '.join(added)}")
    else:
        print("Nothing to do - all secondary-stats columns already exist.")
