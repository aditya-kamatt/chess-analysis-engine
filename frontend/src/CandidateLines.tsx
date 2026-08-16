import type { Line } from "./api";
import { formatScore } from "./EvalBar";

/** The engine's top moves, hidden until asked for.
 *
 *  Hidden by default is the point, not a space saving (PRD 4.5): scrolling a
 *  pre-annotated game teaches very little, so the intent is that you try to
 *  find the move first. The preference persists rather than resetting each
 *  session, so someone who wants them always on says so once.
 */
export function CandidateLines({
  lines,
  depth,
  pending,
  revealed,
  onToggle,
  onPreview,
  onPlay,
}: {
  lines: Line[];
  depth: number | null;
  pending: boolean;
  revealed: boolean;
  onToggle: (revealed: boolean) => void;
  onPreview: (line: Line | null) => void;
  onPlay: (line: Line) => void;
}) {
  if (!revealed) {
    return (
      <section className="candidates">
        <button onClick={() => onToggle(true)}>Show engine lines</button>
        <p className="muted">Hidden on purpose — try to find the move first.</p>
      </section>
    );
  }

  return (
    <section className="candidates">
      <div className="candidates-head">
        <h2>Engine lines</h2>
        <button className="link" onClick={() => onToggle(false)}>
          hide
        </button>
      </div>

      {pending && <p className="muted">Evaluating…</p>}

      {!pending && lines.length === 0 && (
        <p className="muted">No lines for this position.</p>
      )}

      <ol className="candidate-list" onMouseLeave={() => onPreview(null)}>
        {lines.map((line, index) => (
          <li key={line.move}>
            <button
              className="candidate"
              onMouseEnter={() => onPreview(line)}
              onFocus={() => onPreview(line)}
              onClick={() => onPlay(line)}
              title="Play this line out on the board"
            >
              <span className="rank">{index + 1}</span>
              <span className="san">{line.san}</span>
              <span className="score">{formatScore(line.score)}</span>
              <span className="continuation muted">
                {line.pv_san.slice(1, 6).join(" ")}
              </span>
              {line.alternatives.length > 0 && (
                <span className="muted alt">or {line.alternatives.join(", ")}</span>
              )}
            </button>
          </li>
        ))}
      </ol>

      {lines.length > 0 && depth !== null && (
        <p className="muted">
          Depth {depth}. Hover to preview, click to play the line out.
        </p>
      )}
    </section>
  );
}

/** A line the user is exploring — the engine's, or one they played themselves. */
export function SidelineBar({
  san,
  index,
  branchLabel,
  onStep,
  onExit,
}: {
  san: string[];
  index: number;
  branchLabel: string;
  onStep: (index: number) => void;
  onExit: () => void;
}) {
  return (
    <div className="variation">
      <span className="muted">Sideline from {branchLabel}:</span>
      <button
        className={index === 0 ? "move current" : "move"}
        onClick={() => onStep(0)}
        title="Back to the branch point"
      >
        ⟨
      </button>
      {san.map((move, position) => (
        <button
          key={`${move}-${position}`}
          className={position + 1 === index ? "move current" : "move"}
          onClick={() => onStep(position + 1)}
        >
          {move}
        </button>
      ))}
      <button className="link" onClick={onExit} title="Escape">
        back to game
      </button>
    </div>
  );
}
