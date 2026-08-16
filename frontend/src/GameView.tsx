import type { DrawShape } from "chessground/draw";
import type { Key } from "chessground/types";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  api,
  type AnalysisStatus,
  type Evaluation,
  type GameDetail,
  type Line,
  type Position,
} from "./api";
import { Board } from "./Board";
import { CandidateLines, SidelineBar } from "./CandidateLines";
import { EvalBar } from "./EvalBar";
import { EvalGraph } from "./EvalGraph";
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
  const [status, setStatus] = useState<AnalysisStatus | null>(null);
  const [ply, setPly] = useState(0);
  const [flipped, setFlipped] = useState(false);
  const [revealed, setRevealed] = useState(false);
  const [preview, setPreview] = useState<Line | null>(null);
  const [sideline, setSideline] = useState<Sideline | null>(null);
  const [sidelineEval, setSidelineEval] = useState<Evaluation | null>(null);
  const [evaluating, setEvaluating] = useState(false);
  // Drawn arrows and circles, kept per position so they survive navigation.
  const [drawings, setDrawings] = useState<Record<string, DrawShape[]>>({});

  useEffect(() => {
    setGame(null);
    setPositions([]);
    setStatus(null);
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

  const playMove = useCallback(
    (from: string, to: string) => {
      if (!boardFen) return;
      // Under-promotion is rare enough that auto-queening beats a modal here.
      const uci = from + to + (isPromotion(boardFen, from, to) ? "q" : "");
      setPreview(null);
      setSideline((current) => {
        if (!current) return { fromPly: ply, moves: [uci], index: 1 };
        const kept = current.moves.slice(0, current.index);
        return { ...current, moves: [...kept, uci], index: kept.length + 1 };
      });
    },
    [boardFen, ply],
  );

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
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
      };
      const handler = handlers[event.key];
      if (!handler) return;
      event.preventDefault();
      handler();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [lastPly, sideline, goTo]);

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
  }, [preview, sideline, replay, ply, positions]);
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
    return (
      <main>
        <button onClick={onBack}>← Back</button>
        <p className="muted">Loading game…</p>
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
    <main>
      <button onClick={onBack}>← Back</button>

      <h1>
        {game.player_color === "white" ? "You" : game.opponent} vs{" "}
        {game.player_color === "white" ? game.opponent : "You"}
      </h1>
      <p className="muted">
        {shortDate(game.played_at)} · {timeControl(game.time_control)} ·{" "}
        {game.eco_code ?? "—"} · {game.result ?? "—"}
        {game.url && (
          <>
            {" · "}
            <a href={game.url} target="_blank" rel="noreferrer">
              on Chess.com
            </a>
          </>
        )}
      </p>

      {error && game && (
        <div className="banner" role="alert">
          <span>{error}</span>
          <button onClick={() => setError(null)} aria-label="Dismiss">
            ×
          </button>
        </div>
      )}

      <div className="analysis">
        <div className="board-column">
          <div className="board-row">
            <EvalBar
              winPercent={evaluation.winPercent}
              score={evaluation.score}
              orientation={orientation}
            />
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
          </div>
          <div className="controls">
            <button onClick={() => goTo(0)} disabled={ply === 0} title="Start (Home)">
              |◀
            </button>
            <button
              onClick={() => goTo(ply - 1)}
              disabled={ply === 0}
              title="Previous (←)"
            >
              ◀
            </button>
            <button
              onClick={() => goTo(ply + 1)}
              disabled={ply === lastPly}
              title="Next (→)"
            >
              ▶
            </button>
            <button
              onClick={() => goTo(lastPly)}
              disabled={ply === lastPly}
              title="End (End)"
            >
              ▶|
            </button>
            <button onClick={() => setFlipped(!flipped)} title="Flip board">
              ⇅
            </button>
            <span className="muted">
              {ply} / {lastPly}
              {sideline && ` +${sideline.index}`}
            </span>
          </div>
          <p className="muted hint">
            Drag a piece to explore a sideline. Right-drag to draw an arrow,
            right-click a square to ring it.
          </p>
        </div>

        <MoveList
          moves={replay.moves.map((m) => m.san)}
          positions={positions}
          ply={ply}
          dimmed={sideline !== null}
          onSelect={goTo}
        />
      </div>

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

      {positions.length === 0 && (
        <AnalysisPending game={game} gameId={gameId} status={status} />
      )}

      {positions.length > 0 && (
        <EvalGraph
          positions={positions}
          moves={replay.moves.map((m) => m.san)}
          ply={ply}
          onSelect={goTo}
        />
      )}
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
  if (status?.current_game_id === gameId && status.current_total > 0) {
    const percent = Math.round((status.current_ply / status.current_total) * 100);
    return (
      <p className="muted">
        Analysing this game — position {status.current_ply} of {status.current_total} (
        {percent}%)…
      </p>
    );
  }
  return <p className="muted">Analysing this game…</p>;
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
  );
}
