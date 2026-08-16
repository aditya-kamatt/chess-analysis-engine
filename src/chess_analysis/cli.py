"""Run a PGN through the analyzer from the terminal.

A development harness for checking engine output against real games before any
of it is wired to a UI:

    uv run python -m chess_analysis.cli game.pgn --username aditya
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import chess
import chess.pgn

from chess_analysis.analyzer import analyse_game, player_color_for
from chess_analysis.cache import InMemoryEvalCache
from chess_analysis.engine import DEFAULT_DEPTH, DEFAULT_MULTIPV, StockfishEvaluator
from chess_analysis.evaluation import pov

_SEVERITY_MARK = {"inaccuracy": "?!", "mistake": "?", "blunder": "??"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyse a PGN file.")
    parser.add_argument("pgn", type=Path)
    parser.add_argument("--username", help="whose moves to label; default both")
    parser.add_argument("--depth", type=int, default=DEFAULT_DEPTH)
    parser.add_argument("--multipv", type=int, default=DEFAULT_MULTIPV)
    parser.add_argument("--threads", type=int, default=None)
    parser.add_argument("--hash-mb", type=int, default=None)
    parser.add_argument("--engine", default=None, help="path to Stockfish")
    parser.add_argument("--lines", action="store_true", help="print candidate lines")
    args = parser.parse_args(argv)

    with args.pgn.open() as handle:
        game = chess.pgn.read_game(handle)
    if game is None:
        print(f"no game found in {args.pgn}", file=sys.stderr)
        return 1

    player_color = player_color_for(game, args.username) if args.username else None
    if args.username and player_color is None:
        print(f"{args.username} did not play in this game", file=sys.stderr)
        return 1

    cache = InMemoryEvalCache()
    started = time.monotonic()

    def progress(done: int, total: int) -> None:
        print(f"\r  analysing {done}/{total} positions", end="", file=sys.stderr)

    with StockfishEvaluator(
        args.engine,
        depth=args.depth,
        multipv=args.multipv,
        threads=args.threads,
        hash_mb=args.hash_mb,
        cache=cache,
    ) as evaluator:
        result = analyse_game(
            game,
            evaluator,
            player_color=player_color,
            progress=progress,
        )
    elapsed = time.monotonic() - started
    print(f"\r{' ' * 40}\r", end="", file=sys.stderr)

    _print_game(result, game, show_lines=args.lines)
    print(
        f"\n{len(result.plies)} plies in {elapsed:.1f}s "
        f"at depth {result.depth} — cache {cache.hits} hit / {cache.misses} miss"
    )
    return 0


def _print_game(result, game: chess.pgn.Game, *, show_lines: bool) -> None:
    board = game.board()
    for ply in result.plies:
        san = board.san(ply.played_move)
        board.push(ply.played_move)

        number = ply.ply // 2 + 1
        prefix = f"{number:3}." if ply.side_to_move == chess.WHITE else "    ..."
        mark = _SEVERITY_MARK.get(ply.severity or "", "")
        after = pov(ply.played_move_score, chess.WHITE)

        line = f"{prefix} {san + mark:<8} {_format(after):>7}"
        if ply.severity is not None:
            line += f"  {ply.severity:<10} -{ply.win_percent_loss:.1f}%"
        print(line)

        if show_lines:
            for candidate in ply.lines:
                pv_board = chess.Board(ply.fen)
                pv_san = pv_board.variation_san(candidate.pv)
                print(f"          {_format(candidate.score):>7}  {pv_san}")


def _format(score) -> str:
    """White-relative score as a human reads it: +0.42, #4, -#2."""
    if score.is_mate():
        # mate() is 0 both for "white is mated" and "black is mated"; the signed
        # score is what separates them.
        sign = "" if score.score(mate_score=1_000_000) > 0 else "-"
        return f"{sign}#{abs(score.mate())}"
    return f"{score.score() / 100:+.2f}"


if __name__ == "__main__":
    raise SystemExit(main())
