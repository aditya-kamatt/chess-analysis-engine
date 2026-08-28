import type { DrawShape } from "chessground/draw";
import type { Key } from "chessground/types";

export type Severity = "inaccuracy" | "mistake" | "blunder";

export const SEVERITY_MARK: Record<Severity, string> = {
  inaccuracy: "?!",
  mistake: "?",
  blunder: "??",
};

export const SEVERITY_LABEL: Record<Severity, string> = {
  inaccuracy: "Inaccuracy",
  mistake: "Mistake",
  blunder: "Blunder",
};

/** Worst first: a tally is read to find the damage, not to enumerate tiers. */
export const SEVERITY_ORDER: Severity[] = ["blunder", "mistake", "inaccuracy"];

const SEVERITY_PLURAL: Record<Severity, string> = {
  inaccuracy: "inaccuracies",
  mistake: "mistakes",
  blunder: "blunders",
};

/** "1 blunder", "3 inaccuracies" — the spoken form of a tally, for labels the
 *  `??` glyphs would leave a screen reader with nothing to say. */
export function severityCount(count: number, severity: Severity): string {
  const noun =
    count === 1 ? SEVERITY_LABEL[severity].toLowerCase() : SEVERITY_PLURAL[severity];
  return `${count} ${noun}`;
}

/** Glyph ink per badge, picked for contrast against that badge's own fill.
 *  Dark on the yellow and orange steps (9.5:1 and 6.6:1), white on the red
 *  one (4.8:1) — a single ink would be illegible on one of them. */
const BADGE_INK: Record<Severity, string> = {
  inaccuracy: "#1a1a19",
  mistake: "#1a1a19",
  blunder: "#ffffff",
};

/** A badge on the square a move landed on, the way the big sites mark errors.
 *
 *  Chessground renders `customSvg` into a `viewBox="0 0 100 100"` covering one
 *  square, so these coordinates are square-relative: the badge sits on the
 *  top-right corner and scales with the board. */
export function severityBadge(square: Key, severity: Severity): DrawShape {
  const glyph = SEVERITY_MARK[severity];
  const size = glyph.length > 1 ? 30 : 40;

  return {
    orig: square,
    customSvg: {
      // Colours go through `style`, not presentation attributes: `var()` is a
      // CSS declaration value and does not reliably substitute in attributes.
      html: `
        <circle cx="74" cy="26" r="24"
                style="fill: var(--severity-${severity});
                       stroke: var(--badge-ring); stroke-width: 4" />
        <text x="74" y="26" text-anchor="middle" dominant-baseline="central"
              font-family="system-ui, sans-serif" font-weight="700"
              font-size="${size}" style="fill: ${BADGE_INK[severity]}">${glyph}</text>
      `,
    },
  };
}
