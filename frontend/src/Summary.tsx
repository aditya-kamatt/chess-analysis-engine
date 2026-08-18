import type { AnalysisSummary } from "./api";
import {
  SEVERITY_MARK,
  SEVERITY_ORDER,
  type Severity,
  severityCount,
} from "./severity";

const COUNT: Record<Severity, (summary: AnalysisSummary) => number> = {
  blunder: (summary) => summary.blunders,
  mistake: (summary) => summary.mistakes,
  inaccuracy: (summary) => summary.inaccuracies,
};

function tally(summary: AnalysisSummary): [Severity, number][] {
  return SEVERITY_ORDER.map(
    (severity) => [severity, COUNT[severity](summary)] as [Severity, number],
  ).filter(([, count]) => count > 0);
}

/** Accuracy as a whole number. The curve tops out a hair under 100, so a game
 *  played without a single error still reads 100%. */
export function accuracyPercent(summary: AnalysisSummary): number {
  return Math.round(summary.accuracy);
}

function accuracyTitle(summary: AnalysisSummary): string {
  return (
    `${summary.average_loss.toFixed(1)}% win chance given up per move, ` +
    `across ${summary.moves} of your moves`
  );
}

/** Compact tally for the game list — `1?? 2? 3?!`, worst first.
 *
 *  The glyphs carry the tier, as everywhere else in the app, so the column
 *  stays narrow enough to sit beside six others. Colour is never the only
 *  signal, and the spoken form is on the wrapper for anyone not reading it. */
export function ErrorCounts({ summary }: { summary: AnalysisSummary }) {
  const counts = tally(summary);

  if (counts.length === 0) {
    return <span className="muted">clean</span>;
  }

  return (
    <span
      className="error-counts"
      aria-label={counts
        .map(([severity, count]) => severityCount(count, severity))
        .join(", ")}
    >
      {counts.map(([severity, count]) => (
        <span key={severity} className={`severity ${severity}`} aria-hidden="true">
          {count}
          {SEVERITY_MARK[severity]}
        </span>
      ))}
    </span>
  );
}

const TIERS: [Severity, string][] = [
  ["blunder", "Blunders"],
  ["mistake", "Mistakes"],
  ["inaccuracy", "Inaccuracies"],
];

/** The same tally as figures, for the game header. Every tier is shown even at
 *  zero: a row of four numbers is read at a glance, and "0 blunders" is itself
 *  worth seeing. */
export function SummaryStats({ summary }: { summary: AnalysisSummary }) {
  return (
    <div className="stats">
      <div className="stat accuracy" title={accuracyTitle(summary)}>
        <span className="value">{accuracyPercent(summary)}%</span>
        <span className="label">Accuracy</span>
      </div>

      {TIERS.map(([severity, label]) => {
        const count = COUNT[severity](summary);
        return (
          <div
            key={severity}
            className={`stat ${severity}${count === 0 ? " zero" : ""}`}
          >
            <span className="value">
              {count}
              <span className="sr-only"> {SEVERITY_MARK[severity]}</span>
            </span>
            <span className="label">{label}</span>
          </div>
        );
      })}
    </div>
  );
}
