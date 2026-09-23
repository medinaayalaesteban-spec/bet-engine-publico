import sqlite3
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DB_PATH = os.path.join(os.path.dirname(__file__), '../../data/football.db')

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Tabla de partidos
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS matches (
        match_id INTEGER PRIMARY KEY,
        date TEXT,
        league TEXT,
        home_team TEXT,
        away_team TEXT,
        home_score INTEGER,
        away_score INTEGER,
        status TEXT,
        home_corners INTEGER,
        away_corners INTEGER,
        home_yellow_cards INTEGER,
        away_yellow_cards INTEGER,
        home_red_cards INTEGER,
        away_red_cards INTEGER
    )
    ''')

    conn.commit()
    conn.close()

    # In case this ran against a pre-existing DB created before the secondary
    # stats (corners/cards) columns existed.
    from migrate_add_secondary_stats import migrate
    migrate(DB_PATH)

    print("Base de datos inicializada correctamente.")

if __name__ == "__main__":
    init_db()
