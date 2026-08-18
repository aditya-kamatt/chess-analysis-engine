import type { Explanation, Position } from "./api";
import { SEVERITY_LABEL, SEVERITY_MARK, type Severity } from "./severity";

/** What went wrong on the move just played, above the engine's lines.
 *
 *  The diagnosis is always visible where the toggle below it hides the engine's
 *  candidates by default (PRD 4.5). The two are not the same spoiler: being told
 *  the move dropped a piece to a knight fork is the feedback that makes a review
 *  worth doing, where being shown the move to play instead is the answer to the
 *  exercise — so that half stays behind the same toggle.
 */
export function ErrorExplanation({
  position,
  moveNumber,
  revealed,
  onReveal,
  onPlayPunishment,
  onPlayBetter,
}: {
  position: Position;
  moveNumber: string;
  revealed: boolean;
  onReveal: () => void;
  onPlayPunishment: (pv: string[]) => void;
  onPlayBetter: (pv: string[]) => void;
}) {
  const explanation = position.explanation;
  const severity = position.severity as Severity | null;
  if (!explanation || !severity) return null;

  return (
    <section className={`explanation ${severity}`}>
      <h2>
        <span className={`severity ${severity}`} aria-hidden="true">
          {SEVERITY_MARK[severity]}
        </span>
        {SEVERITY_LABEL[severity]}
        <span className="muted move">
          {moveNumber} {explanation.played_san}
        </span>
        <span className="muted loss">−{position.win_percent_loss.toFixed(0)}%</span>
      </h2>

      <p className="verdict">{explanation.summary}</p>

      {explanation.punishment.length > 0 && (
        <p className="refutation">
          <span className="muted">Punished by</span>{" "}
          <button
            className="link line"
            onClick={() => onPlayPunishment(explanation.punishment_pv)}
            title="Play the refutation out on the board"
          >
            {explanation.punishment.join(" ")}
          </button>
        </p>
      )}

      <Instead
        explanation={explanation}
        revealed={revealed}
        onReveal={onReveal}
        onPlayBetter={onPlayBetter}
      />
    </section>
  );
}

/** The half of the explanation that is the answer to the exercise. */
function Instead({
  explanation,
  revealed,
  onReveal,
  onPlayBetter,
}: {
  explanation: Explanation;
  revealed: boolean;
  onReveal: () => void;
  onPlayBetter: (pv: string[]) => void;
}) {
  if (explanation.engine_agreed) {
    return (
      <p className="muted">
        The engine picked this move too — at fixed depth the search can turn
        against a move one ply after recommending it.
      </p>
    );
  }

  const better = explanation.better;
  if (!better) return null;

  if (!revealed) {
    return (
      <p className="muted">
        <button className="link" onClick={onReveal}>
          Show the move to play instead
        </button>
      </p>
    );
  }

  return (
    <p className="instead">
      <span className="muted">Play</span>{" "}
      <button
        className="link line"
        onClick={() => onPlayBetter(better.pv)}
        title="Play this line out on the board"
      >
        {better.pv_san.join(" ")}
      </button>
      {better.simpler && (
        // The engine's own move is named rather than buried: the claim is that
        // this one is easier for the same result, not that the engine is wrong.
        <span className="muted simpler">
          {" "}
          — simpler than {better.engine_san}, and worth {better.cost.toFixed(1)}%
        </span>
      )}
    </p>
  );
}
