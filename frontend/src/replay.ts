import { Chess } from "chess.js";
import type { Key } from "chessground/types";

export interface ReplayMove {
  san: string;
  from: string;
  to: string;
}

export interface Replay {
  /** Position at every ply: `fens[0]` is the start, `fens[n]` is after move n. */
  fens: string[];
  moves: ReplayMove[];
}

export class PgnError extends Error {}

/** Expand a PGN into the position after every ply.
 *
 *  Done once per game rather than stepping a mutable board, so jumping to an
 *  arbitrary ply is a lookup instead of a replay from the start. */
export function replayPgn(pgn: string): Replay {
  const parsed = new Chess();
  try {
    parsed.loadPgn(pgn);
  } catch (err) {
    throw new PgnError(err instanceof Error ? err.message : "Could not read this PGN");
  }

  const board = new Chess();
  const fens = [board.fen()];
  const moves: ReplayMove[] = [];

  for (const move of parsed.history({ verbose: true })) {
    board.move(move.san);
    fens.push(board.fen());
    moves.push({ san: move.san, from: move.from, to: move.to });
  }

  return { fens, moves };
}


/** Positions after each move of a UCI line, for playing a variation out.
 *
 *  Returns one more entry than there are moves: index 0 is `fen` itself. */
export function positionsAfter(fen: string, uciMoves: string[]) {
  const board = new Chess(fen);
  const fens = [board.fen()];
  const squares: [string, string][] = [];

  for (const uci of uciMoves) {
    try {
      board.move({
        from: uci.slice(0, 2),
        to: uci.slice(2, 4),
        promotion: uci.slice(4) || undefined,
      });
    } catch {
      break; // a stored line that no longer replays stops here
    }
    fens.push(board.fen());
    squares.push([uci.slice(0, 2), uci.slice(2, 4)]);
  }

  return { fens, squares };
}


/** Legal targets per origin square, in the shape chessground wants. */
export function legalDests(fen: string): Map<Key, Key[]> {
  const board = new Chess(fen);
  const dests = new Map<Key, Key[]>();
  for (const move of board.moves({ verbose: true })) {
    const from = move.from as Key;
    const existing = dests.get(from);
    if (existing) existing.push(move.to as Key);
    else dests.set(from, [move.to as Key]);
  }
  return dests;
}

export function turnOf(fen: string): "white" | "black" {
  return fen.split(" ")[1] === "b" ? "black" : "white";
}

/** SAN for each move of a UCI line, for showing the sideline as notation. */
export function sanFor(fen: string, uciMoves: string[]): string[] {
  const board = new Chess(fen);
  const san: string[] = [];
  for (const uci of uciMoves) {
    try {
      const move = board.move({
        from: uci.slice(0, 2),
        to: uci.slice(2, 4),
        promotion: uci.slice(4) || undefined,
      });
      san.push(move.san);
    } catch {
      break;
    }
  }
  return san;
}

/** Does this move need a promotion piece chosen? */
export function isPromotion(fen: string, from: string, to: string): boolean {
  const board = new Chess(fen);
  const piece = board.get(from as never);
  if (!piece || piece.type !== "p") return false;
  const rank = to[1];
  return (piece.color === "w" && rank === "8") || (piece.color === "b" && rank === "1");
}


/** The colour in check, or false — the shape chessground's `check` config
 *  wants, which highlights that king's square. */
export function checkedColor(fen: string): "white" | "black" | false {
  const board = new Chess(fen);
  return board.inCheck() ? turnOf(fen) : false;
}
