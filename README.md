# bet-engine-publico

Public copy of the data-collection and video-rendering scripts from `bet-engine`.
Everything secret or machine-specific has been factored out to environment
variables and relative paths, so this folder is safe to publish / share.

## Contents

```
src/collectors/api_client.py       - pulls today's matches + corner/card stats
src/collectors/history_loader.py   - backfills historical results for context
src/database/db_setup.py           - creates data/football.db and its schema
src/database/migrate_add_secondary_stats.py
src/bot/video_maker.py             - edge-tts narration + moviepy video render
```

## Setup

```bash
python -m venv venv
venv\Scripts\activate        # or: source venv/bin/activate
pip install -r requirements.txt

copy .env.example .env       # or: cp .env.example .env
# then edit .env and set your own API_KEY
```

## Run

```bash
python src/database/db_setup.py        # creates data/football.db
python src/collectors/api_client.py    # pulls today's matches + stats
python src/collectors/history_loader.py
```

`src/bot/video_maker.py` is a library (`generate_audio`, `render_tiktok_video`,
`make_tiktok_video`) - import it from your own script, or run it directly for
a demo (needs an `assets/background.jpg` at 1080x1920 next to `output/`).

No API keys or local file-system paths are hardcoded anywhere in this folder -
everything comes from `.env` (via `os.getenv()`) or is computed relative to
each script's own location.
