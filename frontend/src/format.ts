/** "4 minutes ago" — the freshness indicator is required, not decorative: the
 *  data is explicitly not live and the user must be able to tell at a glance
 *  whether a just-finished game is included (PRD 4.2). */
export function relativeTime(iso: string | null): string {
  if (!iso) return "never";

  const seconds = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";

  const units: [number, Intl.RelativeTimeFormatUnit][] = [
    [60, "minute"],
    [3600, "hour"],
    [86400, "day"],
    [2592000, "month"],
  ];

  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  let chosen: [number, Intl.RelativeTimeFormatUnit] = units[0];
  for (const unit of units) {
    if (seconds >= unit[0]) chosen = unit;
  }
  return formatter.format(-Math.floor(seconds / chosen[0]), chosen[1]);
}

export function monthAndYear(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString(undefined, {
    month: "long",
    year: "numeric",
  });
}

export function shortDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

/** Chess.com encodes time control as seconds, "seconds+increment", or
 *  "1/seconds" for daily games. */
export function timeControl(raw: string | null): string {
  if (!raw) return "—";
  if (raw.startsWith("1/")) {
    const days = Math.round(Number(raw.slice(2)) / 86400);
    return `${days}d/move`;
  }
  const [base, increment] = raw.split("+");
  const minutes = Number(base) / 60;
  const label = Number.isInteger(minutes) ? `${minutes}` : minutes.toFixed(1);
  return increment ? `${label}+${increment}` : `${label} min`;
}
