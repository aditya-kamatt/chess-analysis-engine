# Chess Analysis Engine

Self-hosted analysis for your own Chess.com and Lichess games.

## Setup

```bash
uv sync                      # Python dependencies
./scripts/fetch-stockfish.sh # pinned Stockfish 18 into vendor/
cd frontend && npm install
```

Stockfish is pinned rather than installed from a package manager: fixed depth
only yields reproducible evaluations if the engine producing them also stays
fixed. Override with `STOCKFISH_VERSION` / `STOCKFISH_BUILD`, or point at your
own binary with `STOCKFISH_PATH`.

## Running

Two processes in development — Vite serves the UI and proxies `/api` to uvicorn:

```bash
uv run uvicorn chess_analysis.api:app --reload --port 8000
cd frontend && npm run dev          # http://localhost:5173
```

Or build the frontend once and let FastAPI serve everything on one port:

```bash
cd frontend && npm run build
uv run uvicorn chess_analysis.api:app --port 8000   # http://localhost:8000
```

The database lands at `data/chess.db`; set `CHESS_ANALYSIS_DB` to move it.

## Analysing a game from the terminal

```bash
uv run python -m chess_analysis.cli games/opera-game.pgn --lines
```

## Tests

```bash
uv run pytest
```

Tests in `test_stockfish.py` need the engine and skip without it. Nothing in the
suite touches the network — the Chess.com and Lichess clients are exercised
through a mock transport.

## Layout

| Path | Role |
|------|------|
| `src/chess_analysis/evaluation.py` | Win% model, POV conversion, score serialisation |
| `src/chess_analysis/classify.py` | Inaccuracy / mistake / blunder thresholds |
| `src/chess_analysis/engine.py` | Stockfish over UCI, fixed depth, MultiPV |
| `src/chess_analysis/analyzer.py` | PGN → per-ply evaluations and severities |
| `src/chess_analysis/platforms/` | Game sources: Chess.com and Lichess |
| `src/chess_analysis/sync.py` | Per-platform pulls, cursors, conditional requests |
| `src/chess_analysis/store.py` | All SQL |
| `src/chess_analysis/api.py` | HTTP API and static hosting |
| `frontend/` | React UI |

## Status

Working: engine analysis, move classification, Chess.com and Lichess sync,
settings, game list.

Both platforms are independent — connect either or both. Lichess takes an
optional personal API token (Preferences → API access tokens on lichess.org),
which raises its rate limits; it is stored in `data/chess.db` and never
returned by the API or written to a log.

Not built yet: background analysis worker (games list as `pending`), the
analysis board, lazy backfill, Docker.
