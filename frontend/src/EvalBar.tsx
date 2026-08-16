import type { Score } from "./api";

/** Format a white-relative score the way a player reads it: +0.42, #4, -#2. */
export function formatScore(score: Score | null): string {
  if (!score) return "—";
  if (score.mate !== undefined && score.mate !== null) {
    // mate 0 is ambiguous on its own — it means "someone has just been mated"
    // — so `mate_given` is what says which side came out ahead.
    if (score.mate === 0) return score.mate_given ? "#" : "-#";
    return score.mate > 0 ? `#${score.mate}` : `-#${Math.abs(score.mate)}`;
  }
  const pawns = (score.cp ?? 0) / 100;
  return `${pawns >= 0 ? "+" : ""}${pawns.toFixed(2)}`;
}

/** The bar fills by win percentage rather than clamped centipawns, so it moves
 *  smoothly near equality and pins on a forced mate (PRD 4.5). The percentage
 *  is computed server-side with the same model that assigns severity. */
export function EvalBar({
  winPercent,
  score,
  orientation,
}: {
  winPercent: number | null;
  score: Score | null;
  orientation: "white" | "black";
}) {
  if (winPercent === null) {
    return <div className="evalbar empty" aria-hidden="true" />;
  }

  const flipped = orientation === "black";
  const label = formatScore(score);
  // The label sits on whichever end belongs to the side that is better off.
  const labelAtTop = flipped ? winPercent >= 50 : winPercent < 50;

  return (
    <div
      className="evalbar"
      role="img"
      aria-label={`Evaluation ${label}, white win chance ${winPercent.toFixed(0)}%`}
    >
      <div
        className="white"
        style={{ height: `${winPercent}%`, [flipped ? "top" : "bottom"]: 0 }}
      />
      <span className={labelAtTop ? "label top" : "label bottom"}>{label}</span>
    </div>
  );
}
