import type { DrawShape } from "chessground/draw";
import type { Key } from "chessground/types";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  api,
  platformName,
  type AnalysisStatus,
  type AnalysisSummary,
  type Evaluation,
  type GameDetail,
  type Line,
  type Position,
} from "./api";
import { Board } from "./Board";
import { CandidateLines, SidelineBar } from "./CandidateLines";
import { EvalBar } from "./EvalBar";
import { EvalGraph } from "./EvalGraph";
import { SummaryStats } from "./Summary";
import { shortDate, timeControl } from "./format";
import {
  PgnError,
  checkedColor,
  isPromotion,
  legalDests,
  positionsAfter,
  replayPgn,
  sanFor,
  turnOf,
} from "./replay";
import { SEVERITY_MARK, type Severity, severityBadge } from "./severity";

/** A line being explored off the game: the engine's, or one the user played.
 *  `index` is how many of `moves` are currently on the board, so stepping back
 *  and playing something else replaces the tail. */
interface Sideline {
  fromPly: number;
  moves: string[];
  index: number;
}

export function GameView({ gameId, onBack }: { gameId: number; onBack: () => void }) {
  const [game, setGame] = useState<GameDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [positions, setPositions] = useState<Position[]>([]);
  const [summary, setSummary] = useState<AnalysisSummary | null>(null);
  const [status, setStatus] = useState<AnalysisStatus | null>(null);
  const [ply, setPly] = useState(0);
  const [flipped, setFlipped] = useState(false);
  const [revealed, setRevealed] = useState(false);
  const [preview, setPreview] = useState<Line | null>(null);
  const [sideline, setSideline] = useState<Sideline | null>(null);
  const [sidelineEval, setSidelineEval] = useState<Evaluation | null>(null);
  const [evaluating, setEvaluating] = useState(false);
  /** A move waiting on a promotion piece, held between the drop and the pick. */
  const [promotion, setPromotion] = useState<{ from: string; to: string } | null>(null);
  const [announcement, setAnnouncement] = useState("");
  // Drawn arrows and circles, kept per position so they survive navigation.
  const [drawings, setDrawings] = useState<Record<string, DrawShape[]>>({});

  useEffect(() => {
    setGame(null);
    setPositions([]);
    setSummary(null);
    setStatus(null);
    setPromotion(null);
    setAnnouncement("");
    setPly(0);
    setSideline(null);
    setDrawings({});
    api
      .game(gameId)
      .then(setGame)
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)));
    // Whether lines start revealed is a preference, not per-session state.
    api
      .settings()
      .then((settings) => setRevealed(settings.reveal_lines_by_default))
      .catch(() => undefined);
  }, [gameId]);

  // Opening a game asks for it to be analysed next, ahead of any background
  // work, so you never wait on the rest of the archive to see this one.
  useEffect(() => {
    if (!game) return;
    let live = true;

    (async () => {
      const existing = await api.analysis(gameId).catch(() => null);
      if (!live) return;
      if (existing && existing.positions.length > 0) {
        setPositions(existing.positions);
        setSummary(existing.summary);
        return;
      }
      if (game.analysis_status === "unanalysable") return;

      await api.analyseGame(gameId).catch(() => null);

      while (live) {
        await new Promise((resolve) => setTimeout(resolve, 1200));
        if (!live) return;
        const [result, queue] = await Promise.all([
          api.analysis(gameId).catch(() => null),
          api.analysisStatus().catch(() => null),
        ]);
        if (!live) return;
        if (queue) setStatus(queue);
        if (result && result.positions.length > 0) {
          setPositions(result.positions);
          setSummary(result.summary);
          return;
        }
        if (queue?.error) return;
      }
    })();

    return () => {
      live = false;
    };
  }, [game, gameId]);

  const replay = useMemo(() => {
    if (!game) return null;
    try {
      return replayPgn(game.pgn);
    } catch (err) {
      setError(err instanceof PgnError ? err.message : String(err));
      return null;
    }
  }, [game]);

  const lastPly = replay ? replay.moves.length : 0;

  const goTo = useCallback((next: number | ((current: number) => number)) => {
    setSideline(null);
    setPreview(null);
    setPly(next);
  }, []);

  const toggleLines = useCallback((next: boolean) => {
    setRevealed(next);
    api.savePreferences({ reveal_lines_by_default: next }).catch(() => undefined);
  }, []);

  /* Reviewing a game is a hunt for the moments it went wrong, and stepping one
     ply at a time to find them is the slowest way to do it. `positions[k]`
     describes the move played *from* position k, which lands on ply k + 1 — the
     ply to stop at. */
  const errorPlies = useMemo(
    () =>
      positions.flatMap((position, index) => (position.severity ? [index + 1] : [])),
    [positions],
  );

  // The graph replacing the progress bar is a silent change. Announced only if
  // there was actually a wait, so opening an already-analysed game says nothing.
  const waited = useRef(false);
  useEffect(() => {
    if (positions.length === 0) {
      waited.current = true;
      return;
    }
    if (waited.current) {
      waited.current = false;
      setAnnouncement("Analysis complete.");
    }
  }, [positions]);
  const nextError = errorPlies.find((candidate) => candidate > ply) ?? null;
  const previousError =
    errorPlies.filter((candidate) => candidate < ply).pop() ?? null;

  // The position on the board: the game's, or wherever the sideline has got to.
  const branchFen = replay ? replay.fens[sideline?.fromPly ?? ply] : null;
  const walked = useMemo(
    () =>
      sideline && branchFen
        ? positionsAfter(branchFen, sideline.moves.slice(0, sideline.index))
        : null,
    [sideline, branchFen],
  );
  const boardFen = walked
    ? walked.fens[walked.fens.length - 1]
    : (replay?.fens[ply] ?? null);

  // A sideline has no stored analysis, so its position is evaluated on demand.
  useEffect(() => {
    if (!sideline || !boardFen) {
      setSidelineEval(null);
      setEvaluating(false);
      return;
    }
    let live = true;
    setEvaluating(true);
    api
      .evaluate(boardFen)
      .then((result) => {
        if (live) setSidelineEval(result);
      })
      .catch((err) => {
        if (live) {
          setSidelineEval(null);
          setError(err instanceof ApiError ? err.message : String(err));
        }
      })
      .finally(() => {
        if (live) setEvaluating(false);
      });
    return () => {
      live = false;
    };
  }, [sideline, boardFen]);

  const applyMove = useCallback(
    (uci: string) => {
      setPreview(null);
      setSideline((current) => {
        if (!current) return { fromPly: ply, moves: [uci], index: 1 };
        const kept = current.moves.slice(0, current.index);
        return { ...current, moves: [...kept, uci], index: kept.length + 1 };
      });
    },
    [ply],
  );

  const playMove = useCallback(
    (from: string, to: string) => {
      if (!boardFen) return;
      // Promoting is a two-part move, so the second part gets asked for rather
      // than assumed: auto-queening silently discards the under-promotion, and
      // a sideline is exactly where someone is checking whether it mattered.
      if (isPromotion(boardFen, from, to)) {
        setPromotion({ from, to });
        return;
      }
      applyMove(from + to);
    },
    [boardFen, applyMove],
  );

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      // Letter shortcuts below would otherwise swallow Ctrl+P and friends.
      if (event.ctrlKey || event.metaKey || event.altKey) return;

      // A move half-made owns the keyboard: navigating away from the position
      // it belongs to would leave it pointing at a different board.
      if (promotion) {
        if (event.key !== "Escape") return;
        event.preventDefault();
        setPromotion(null);
        return;
      }

      if (sideline) {
        const depth = sideline.moves.length;
        const handlers: Record<string, () => void> = {
          ArrowLeft: () =>
            setSideline((s) => (s ? { ...s, index: Math.max(0, s.index - 1) } : s)),
          ArrowRight: () =>
            setSideline((s) => (s ? { ...s, index: Math.min(depth, s.index + 1) } : s)),
          Escape: () => setSideline(null),
        };
        const handler = handlers[event.key];
        if (!handler) return;
        event.preventDefault();
        handler();
        return;
      }

      const handlers: Record<string, () => void> = {
        ArrowLeft: () => goTo((p) => Math.max(0, p - 1)),
        ArrowRight: () => goTo((p) => Math.min(lastPly, p + 1)),
        Home: () => goTo(0),
        End: () => goTo(lastPly),
        // Letters rather than the vertical arrows: those still have to scroll
        // the page, which on a short screen is the only way to reach the lines.
        n: () => nextError !== null && goTo(nextError),
        p: () => previousError !== null && goTo(previousError),
      };
      const handler = handlers[event.key];
      if (!handler) return;
      event.preventDefault();
      handler();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [lastPly, sideline, goTo, nextError, previousError, promotion]);

  const dests = useMemo(() => (boardFen ? legalDests(boardFen) : undefined), [boardFen]);
  const shapes = useMemo(
    () => (boardFen ? (drawings[boardFen] ?? []) : []),
    [drawings, boardFen],
  );
  const autoShapes = useMemo<DrawShape[]>(() => {
    const overlay: DrawShape[] = [];

    if (preview) {
      overlay.push({
        orig: preview.move.slice(0, 2) as Key,
        dest: preview.move.slice(2, 4) as Key,
        brush: "paleBlue",
      });
    }

    // Chessground has already slid the pawn onto the last rank by the time it
    // tells us about the move. Rebuilding this array is what makes the board
    // re-read the FEN and put it back, so the position stays true while the
    // piece is still being chosen — and the arrow says which pawn is waiting.
    if (promotion) {
      overlay.push({
        orig: promotion.from as Key,
        dest: promotion.to as Key,
        brush: "paleBlue",
      });
    }

    // Badge the square the move landed on, so an error is visible on the board
    // and not only in the move list. A sideline has no severity to show.
    if (!sideline) {
      const played = replay?.moves[ply - 1];
      const severity = positions[ply - 1]?.severity as Severity | null | undefined;
      if (played && severity) {
        overlay.push(severityBadge(played.to as Key, severity));
      }
    }

    return overlay;
  }, [preview, promotion, sideline, replay, ply, positions]);
  const onShapesChange = useCallback(
    (next: DrawShape[]) => {
      if (!boardFen) return;
      setDrawings((current) => ({ ...current, [boardFen]: next }));
    },
    [boardFen],
  );

  if (error && !game) {
    return (
      <main>
        <button onClick={onBack}>← Back</button>
        <div className="banner" role="alert">
          {error}
        </div>
      </main>
    );
  }

  if (!game || !replay || !boardFen) {
    // The board's shape, not a line of text: the PGN and the analysis arrive
    // separately, and a page that grows twice under the pointer is worse than
    // one that starts the size it will end up.
    return (
      <main className="wide">
        <button onClick={onBack}>← Back</button>
        <p className="muted" role="status">
          Loading game…
        </p>
        <div className="analysis" aria-hidden="true">
          <div className="board-column panel">
            <div className="board-row">
              <div className="skeleton bar-shape" />
              <div className="skeleton board-shape" />
            </div>
          </div>
          <div className="side-panel panel" />
        </div>
      </main>
    );
  }

  const orientation = flipped
    ? game.player_color === "black"
      ? "white"
      : "black"
    : game.player_color === "black"
      ? "black"
      : "white";

  const stored = evaluationAt(positions, ply);
  const evaluation = sideline
    ? { winPercent: sidelineEval?.win_percent ?? null, score: sidelineEval?.eval ?? null }
    : stored;

  const here = positions[ply] ?? null;
  const lines = sideline ? (sidelineEval?.lines ?? []) : (here?.lines ?? []);
  const linesDepth = sideline ? (sidelineEval?.depth ?? null) : (here?.depth ?? null);

  const gameMove = replay.moves[ply - 1];
  const sidelineMove = walked?.squares[walked.squares.length - 1];
  const lastMove: [Key, Key] | undefined = sideline
    ? (sidelineMove as [Key, Key] | undefined)
    : gameMove
      ? [gameMove.from as Key, gameMove.to as Key]
      : undefined;

  return (
    <main className="wide">
      <button className="back" onClick={onBack}>
        ← Back
      </button>

      <header className="game-head panel">
        <div className="game-title">
          <h1>
            {game.player_color === "white" ? "You" : game.opponent} vs{" "}
            {game.player_color === "white" ? game.opponent : "You"}
          </h1>
          <p className="muted meta">
            {shortDate(game.played_at)} · {timeControl(game.time_control)} ·{" "}
            {game.eco_code ?? "—"} · {game.result ?? "—"}
            {game.url && (
              <>
                {" · "}
                <a href={game.url} target="_blank" rel="noreferrer">
                  on {platformName(game.platform)}
                </a>
              </>
            )}
          </p>
        </div>

        {summary && <SummaryStats summary={summary} />}
      </header>

      {error && game && (
        <div className="banner" role="alert">
          <span>{error}</span>
          <button onClick={() => setError(null)} aria-label="Dismiss">
            ×
          </button>
        </div>
      )}

      <div className="analysis">
        <div className="board-column panel">
          <div className="board-row">
            <EvalBar
              winPercent={evaluation.winPercent}
              score={evaluation.score}
              orientation={orientation}
            />
            <div className="board-wrap">
              <Board
                fen={boardFen}
                orientation={orientation}
                lastMove={lastMove}
                dests={dests}
                turnColor={turnOf(boardFen)}
                check={checkedColor(boardFen)}
                onMove={playMove}
                autoShapes={autoShapes}
                shapes={shapes}
                onShapesChange={onShapesChange}
              />
              {promotion && (
                <PromotionPicker
                  square={promotion.to}
                  orientation={orientation}
                  color={turnOf(boardFen)}
                  onChoose={(piece) => {
                    applyMove(promotion.from + promotion.to + piece);
                    setPromotion(null);
                  }}
                  onCancel={() => setPromotion(null)}
                />
              )}
            </div>
          </div>
          {/* These read as their glyph or as nothing at all without a name;
              `title` is a tooltip, and a tooltip is not an accessible name a
              touch user or a screen reader can rely on. */}
          <div className="controls">
            <button
              onClick={() => goTo(0)}
              disabled={ply === 0}
              title="Start (Home)"
              aria-label="Go to start"
            >
              |◀
            </button>
            <button
              onClick={() => goTo(ply - 1)}
              disabled={ply === 0}
              title="Previous (←)"
              aria-label="Previous move"
            >
              ◀
            </button>
            <button
              onClick={() => goTo(ply + 1)}
              disabled={ply === lastPly}
              title="Next (→)"
              aria-label="Next move"
            >
              ▶
            </button>
            <button
              onClick={() => goTo(lastPly)}
              disabled={ply === lastPly}
              title="End (End)"
              aria-label="Go to end"
            >
              ▶|
            </button>
            <button
              onClick={() => setFlipped(!flipped)}
              title="Flip board"
              aria-label="Flip board"
            >
              ⇅
            </button>
            <span className="muted">
              {ply} / {lastPly}
              {sideline && ` +${sideline.index}`}
            </span>
            <div className="jump">
              <button
                onClick={() => previousError !== null && goTo(previousError)}
                disabled={previousError === null}
                title="Previous error (p)"
              >
                ◀ Error
              </button>
              <button
                onClick={() => nextError !== null && goTo(nextError)}
                disabled={nextError === null}
                title="Next error (n)"
              >
                Error ▶
              </button>
            </div>
          </div>

          {/* One slot: the graph once there is analysis to draw, and why there
              is not until then. Both belong under the board — the graph is a
              scrubber for it, and progress is what you watch while waiting. */}
          {positions.length > 0 ? (
            <EvalGraph
              positions={positions}
              moves={replay.moves.map((m) => m.san)}
              ply={ply}
              onSelect={goTo}
            />
          ) : (
            <AnalysisPending game={game} gameId={gameId} status={status} />
          )}

          <p className="muted hint">
            Drag a piece to explore a sideline. Right-drag to draw an arrow,
            right-click a square to ring it. <kbd>n</kbd> and <kbd>p</kbd> jump
            between errors.
          </p>

          <p className="sr-only" role="status">
            {announcement}
          </p>
        </div>

        {/* Beside the board rather than stacked beneath it. All three of these
            are read against the position on the board, and putting them in the
            column the board was leaving empty means no scrolling to consult
            the engine about the move you are looking at. */}
        <div className="side-panel panel">
          <CandidateLines
            lines={lines}
            depth={linesDepth}
            pending={evaluating}
            revealed={revealed}
            onToggle={toggleLines}
            onPreview={setPreview}
            onPlay={(line) =>
              setSideline({ fromPly: sideline?.fromPly ?? ply, moves: line.pv, index: 1 })
            }
          />

          {sideline && branchFen && (
            <SidelineBar
              san={sanFor(branchFen, sideline.moves)}
              index={sideline.index}
              branchLabel={
                sideline.fromPly === 0
                  ? "the start"
                  : `${Math.ceil(sideline.fromPly / 2)}${
                      sideline.fromPly % 2 ? "." : "…"
                    } ${replay.moves[sideline.fromPly - 1]?.san ?? ""}`
              }
              onStep={(index) => setSideline({ ...sideline, index })}
              onExit={() => setSideline(null)}
            />
          )}

          {sidelineEval?.over && (
            <p className="muted">This sideline ends in {sidelineEval.over}.</p>
          )}

          <MoveList
            moves={replay.moves.map((m) => m.san)}
            positions={positions}
            ply={ply}
            dimmed={sideline !== null}
            onSelect={goTo}
          />
        </div>
      </div>
    </main>
  );
}

function AnalysisPending({
  game,
  gameId,
  status,
}: {
  game: GameDetail;
  gameId: number;
  status: AnalysisStatus | null;
}) {
  if (game.analysis_status === "unanalysable") {
    return <p className="muted">This game has no moves to analyse.</p>;
  }
  if (status?.error) {
    return (
      <div className="banner" role="alert">
        Analysis is not running: {status.error}
      </div>
    );
  }
  const here = status?.current_game_id === gameId && status.current_total > 0;

  return (
    <div className="progress">
      {/* Constant text, so it is announced once when the wait starts rather
          than on every tick of the bar beside it. */}
      <span className="muted" role="status">
        Analysing this game…
      </span>
      {here && (
        <progress
          value={status.current_ply}
          max={status.current_total}
          aria-label="Positions analysed in this game"
        />
      )}
      {here && (
        <span className="muted">
          {status.current_ply} / {status.current_total}
        </span>
      )}
    </div>
  );
}

const PROMOTION_PIECES = [
  { piece: "q", white: "♕", black: "♛", label: "Queen" },
  { piece: "r", white: "♖", black: "♜", label: "Rook" },
  { piece: "b", white: "♗", black: "♝", label: "Bishop" },
  { piece: "n", white: "♘", black: "♞", label: "Knight" },
];

/** Which piece the pawn becomes.
 *
 *  Stacked down the promoting file from the promotion square itself, the way
 *  every board people already use does it, rather than centred in a dialog: the
 *  choice depends on the position, so the position has to stay readable. The
 *  scrim behind is light for the same reason — it marks the board as waiting
 *  and gives the click that cancels somewhere to land, without hiding it. */
function PromotionPicker({
  square,
  orientation,
  color,
  onChoose,
  onCancel,
}: {
  square: string;
  orientation: "white" | "black";
  color: "white" | "black";
  onChoose: (piece: string) => void;
  onCancel: () => void;
}) {
  const file = square.charCodeAt(0) - 97;
  const column = orientation === "white" ? file : 7 - file;
  // Whether that square is drawn at the top or the bottom of the board as it is
  // currently turned, which is where the stack has to hang from.
  const atTop = (square[1] === "8") === (orientation === "white");

  const style: React.CSSProperties = {
    left: `${column * 12.5}%`,
    flexDirection: atTop ? "column" : "column-reverse",
    ...(atTop ? { top: 0 } : { bottom: 0 }),
  };

  return (
    <div
      className="promotion"
      role="dialog"
      aria-label="Promote to"
      // A click on the scrim is the other half of Escape.
      onClick={(event) => {
        if (event.target === event.currentTarget) onCancel();
      }}
    >
      <div className="promotion-choices" style={style}>
        {PROMOTION_PIECES.map(({ piece, white, black, label }, index) => (
          <button
            key={piece}
            autoFocus={index === 0}
            onClick={() => onChoose(piece)}
            aria-label={label}
            title={label}
          >
            {color === "white" ? white : black}
          </button>
        ))}
      </div>
    </div>
  );
}

/** The evaluation of the position currently on the board.
 *
 *  Stored rows describe played moves, so there is one per ply: position `k`
 *  before the move is `positions[k].eval`, and the final position is the last
 *  row's `played` evaluation. */
function evaluationAt(positions: Position[], ply: number) {
  if (positions.length === 0) return { winPercent: null, score: null };
  if (ply < positions.length) {
    return {
      winPercent: positions[ply].eval_win_percent,
      score: positions[ply].eval,
    };
  }
  const last = positions[positions.length - 1];
  return { winPercent: last.played_win_percent, score: last.played_move_eval };
}

function MoveList({
  moves,
  positions,
  ply,
  dimmed,
  onSelect,
}: {
  moves: string[];
  positions: Position[];
  ply: number;
  dimmed: boolean;
  onSelect: (ply: number) => void;
}) {
  const active = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    active.current?.scrollIntoView({ block: "nearest" });
  }, [ply]);

  const rows = [];
  for (let index = 0; index < moves.length; index += 2) {
    rows.push(index);
  }

  return (
    // The wrapper is what takes the panel's leftover height; the list fills it
    // absolutely, so a long game contributes nothing to how tall the row wants
    // to be and cannot drag the board's column down with it.
    <div className="moves-wrap">
      <ol className={dimmed ? "moves dimmed" : "moves"}>
        {rows.map((index) => (
          <li key={index}>
            <span className="number">{index / 2 + 1}.</span>
            {[index, index + 1].map((offset) =>
              moves[offset] === undefined ? null : (
                <button
                  key={offset}
                  ref={ply === offset + 1 ? active : null}
                  className={ply === offset + 1 ? "move current" : "move"}
                  onClick={() => onSelect(offset + 1)}
                >
                  {moves[offset]}
                  {positions[offset]?.severity && (
                    <span className={`severity ${positions[offset].severity}`}>
                      {SEVERITY_MARK[positions[offset].severity!]}
                    </span>
                  )}
                </button>
              ),
            )}
          </li>
        ))}
      </ol>
    </div>
  );
}
