import { useMemo, useRef, useState } from "react";
import type { Position } from "./api";
import { formatScore } from "./EvalBar";
import { SEVERITY_LABEL, SEVERITY_MARK, type Severity } from "./severity";

const WIDTH = 600;
const HEIGHT = 140;
const INSET = 7; // keeps markers off the top and bottom edges

interface GraphPoint {
  ply: number;
  winPercent: number;
  score: Position["eval"];
  severity: Severity | null;
  loss: number;
  san: string | null;
}

/** Severity markers differ by shape as well as colour, so the tier is never
 *  carried by colour alone — the status palette's warning and serious steps sit
 *  close together for colour-vision deficiency. */
function Marker({ severity, x, y }: { severity: Severity; x: number; y: number }) {
  const fill = `var(--severity-${severity})`;
  const common = { fill, stroke: "var(--graph-marker-ring)", strokeWidth: 2 };

  if (severity === "inaccuracy") {
    return <circle cx={x} cy={y} r={4.5} {...common} />;
  }
  if (severity === "mistake") {
    return (
      <rect
        x={x - 4.5}
        y={y - 4.5}
        width={9}
        height={9}
        transform={`rotate(45 ${x} ${y})`}
        {...common}
      />
    );
  }
  return <polygon points={`${x},${y - 6} ${x + 5.5},${y + 4} ${x - 5.5},${y + 4}`} {...common} />;
}

/** Evaluation across the game, with errors marked at the ply they were played.
 *
 *  Two-tone around a neutral midline rather than a single-hue line: the measure
 *  has a natural zero, and it reuses the evaluation bar's white-over-dark
 *  language so the two components read as one system. */
export function EvalGraph({
  positions,
  moves,
  ply,
  onSelect,
}: {
  positions: Position[];
  moves: string[];
  ply: number;
  onSelect: (ply: number) => void;
}) {
  const container = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<number | null>(null);

  const points = useMemo<GraphPoint[]>(() => {
    if (positions.length === 0) return [];
    const last = positions[positions.length - 1];
    const result: GraphPoint[] = [];

    for (let index = 0; index <= positions.length; index += 1) {
      const before = positions[index];
      const previous = positions[index - 1];
      result.push({
        ply: index,
        winPercent: before ? (before.eval_win_percent ?? 50) : last.played_win_percent,
        score: before ? before.eval : last.played_move_eval,
        severity: (previous?.severity as Severity | null) ?? null,
        loss: previous?.win_percent_loss ?? 0,
        san: previous ? (moves[index - 1] ?? null) : null,
      });
    }
    return result;
  }, [positions, moves]);

  if (points.length < 2) return null;

  const x = (index: number) => (index / (points.length - 1)) * WIDTH;
  const y = (percent: number) =>
    INSET + (1 - percent / 100) * (HEIGHT - INSET * 2);

  const curve = points.map((point) => `${x(point.ply)},${y(point.winPercent)}`);
  const area = `M ${curve.join(" L ")} L ${WIDTH},${HEIGHT} L 0,${HEIGHT} Z`;
  const stroke = `M ${curve.join(" L ")}`;

  const present = (["blunder", "mistake", "inaccuracy"] as Severity[]).filter(
    (severity) => points.some((point) => point.severity === severity),
  );

  const active = hover ?? ply;
  const shown = points[active];

  function locate(event: React.MouseEvent<SVGRectElement>) {
    const bounds = event.currentTarget.getBoundingClientRect();
    const ratio = (event.clientX - bounds.left) / bounds.width;
    const index = Math.round(ratio * (points.length - 1));
    return Math.max(0, Math.min(points.length - 1, index));
  }

  return (
    <figure className="graph" ref={container}>
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="graph-svg"
        role="img"
        aria-label={`Evaluation across ${positions.length} plies, with ${
          points.filter((p) => p.severity).length
        } errors marked. The move list below gives the same information per move.`}
      >
        <rect width={WIDTH} height={HEIGHT} fill="var(--graph-black)" />
        <path d={area} fill="var(--graph-white)" />
        <line
          x1={0}
          x2={WIDTH}
          y1={y(50)}
          y2={y(50)}
          stroke="var(--graph-midline)"
          strokeWidth={1}
          strokeDasharray="4 4"
          vectorEffect="non-scaling-stroke"
        />
        <path
          d={stroke}
          fill="none"
          stroke="var(--graph-line)"
          strokeWidth={2}
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
        />

        {points.map((point) =>
          point.severity ? (
            <Marker
              key={point.ply}
              severity={point.severity}
              x={x(point.ply)}
              y={y(point.winPercent)}
            />
          ) : null,
        )}

        <line
          x1={x(active)}
          x2={x(active)}
          y1={0}
          y2={HEIGHT}
          stroke="var(--graph-cursor)"
          strokeWidth={2}
          vectorEffect="non-scaling-stroke"
        />

        {/* Full-height hit target: far easier to hit than the curve itself. */}
        <rect
          width={WIDTH}
          height={HEIGHT}
          fill="transparent"
          style={{ cursor: "pointer" }}
          onMouseMove={(event) => setHover(locate(event))}
          onMouseLeave={() => setHover(null)}
          onClick={(event) => onSelect(locate(event))}
        />
      </svg>

      {shown && (
        <p className="graph-readout">
          <span className="muted">
            {shown.san
              ? `${Math.ceil(shown.ply / 2)}${shown.ply % 2 ? "." : "…"} ${shown.san}`
              : "Start"}
          </span>{" "}
          <strong>{formatScore(shown.score)}</strong>
          {shown.severity && (
            <>
              {" · "}
              <span className={`severity ${shown.severity}`}>
                {SEVERITY_MARK[shown.severity]} {SEVERITY_LABEL[shown.severity]}
              </span>{" "}
              <span className="muted">−{shown.loss.toFixed(0)}%</span>
            </>
          )}
        </p>
      )}

      {present.length > 0 && (
        <figcaption className="legend">
          {present.map((severity) => (
            <span key={severity}>
              <svg width={16} height={16} viewBox="0 0 16 16" aria-hidden="true">
                <Marker severity={severity} x={8} y={8} />
              </svg>
              {SEVERITY_LABEL[severity]}
            </span>
          ))}
        </figcaption>
      )}
    </figure>
  );
}
