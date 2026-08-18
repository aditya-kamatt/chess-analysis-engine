export interface Settings {
  chesscom_enabled: boolean;
  chesscom_username: string | null;
  chesscom_last_synced_at: string | null;
  chesscom_backfill_cursor: string | null;
  lichess_enabled: boolean;
  lichess_username: string | null;
  /** Whether a token is stored. The token itself is never sent back — the form
   *  needs to know it is there, not what it is. */
  lichess_token_set: boolean;
  lichess_last_synced_at: string | null;
  lichess_backfill_cursor: string | null;
  reveal_lines_by_default: boolean;
  analysis_depth: number;
  background_analysis: boolean;
}

export const PLATFORM_NAMES: Record<string, string> = {
  chesscom: "Chess.com",
  lichess: "Lichess",
};

export function platformName(platform: string): string {
  return PLATFORM_NAMES[platform] ?? platform;
}

/** What a game's analysis adds up to, over the player's own moves.
 *
 *  Computed server-side with the same win% model that assigns severity, rather
 *  than summed in the browser: an accuracy figure that disagreed with the
 *  blunder count beside it would be worse than no figure at all. */
export interface AnalysisSummary {
  moves: number;
  inaccuracies: number;
  mistakes: number;
  blunders: number;
  /** Mean win percentage given up per move. */
  average_loss: number;
  accuracy: number;
}

export interface GameSummary {
  id: number;
  platform: string;
  played_at: string;
  time_control: string | null;
  player_color: string | null;
  opponent: string | null;
  result: string | null;
  eco_code: string | null;
  url: string | null;
  analysis_status: string;
  /** Null until the game has been analysed. */
  analysis: AnalysisSummary | null;
}

export interface GameDetail extends GameSummary {
  pgn: string;
}

export interface GameList {
  games: GameSummary[];
  /** Matching the current filters, not the whole archive. */
  total: number;
  /** Oldest game synced, regardless of filters. */
  history_back_to: string | null;
}

/** Narrowing applied server-side, so counts and paging stay honest. */
export interface GameFilters {
  color?: "white" | "black";
  result?: "win" | "loss" | "draw";
  timeClass?: "bullet" | "blitz" | "rapid" | "daily";
  withErrors?: boolean;
}

/** One platform's share of a sync. Freshness is per-platform and stays that
 *  way: an account synced a minute ago and one that failed have no average. */
export interface PlatformSync {
  platform: string;
  inserted: number;
  entries_seen: number;
  archives_read: number;
  archives_unchanged: number;
  first_sync: boolean;
  last_synced_at: string;
  backfill_cursor: string | null;
}

/** A platform that could not be synced while another one could. Reported
 *  rather than thrown, so one failing account does not discard the other's
 *  games. */
export interface SyncFailure {
  platform: string;
  message: string;
}

export interface SyncResponse {
  platforms: PlatformSync[];
  failures: SyncFailure[];
  inserted: number;
  entries_seen: number;
  first_sync: boolean;
  total_games: number;
}

/** A score as stored: exactly one of `cp` or `mate` is set. `mate: 0` means the
 *  side to move is mated; `mate_given` marks its negation. */
export interface Score {
  cp?: number;
  mate?: number;
  mate_given?: boolean;
}

export interface Line {
  /** UCI, for the board. */
  move: string;
  /** SAN, for people. */
  san: string;
  score: Score;
  pv: string[];
  pv_san: string[];
  /** Move orders that transpose into this same line (PRD 4.5). */
  alternatives: string[];
}

export interface Position {
  ply: number;
  fen: string;
  side_to_move: string;
  played_move: string;
  played_move_eval: Score;
  /** Best available evaluation of the position *before* the played move. */
  eval: Score | null;
  eval_win_percent: number | null;
  played_win_percent: number;
  lines: Line[];
  depth: number;
  win_percent_loss: number;
  severity: "inaccuracy" | "mistake" | "blunder" | null;
}

export interface Evaluation {
  fen: string;
  /** Set when the position is final: "checkmate", "stalemate", … */
  over: string | null;
  eval: Score;
  win_percent: number;
  lines: Line[];
  depth: number | null;
}

export interface AnalysisStatus {
  running: boolean;
  queued: number;
  current_game_id: number | null;
  current_ply: number;
  current_total: number;
  completed: number;
  failed: number;
  /** Set when the engine could not start — a setup problem, not a game one. */
  error: string | null;
}

/** An API failure carrying the message the backend chose to show. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...init,
    });
  } catch {
    throw new ApiError("Could not reach the server. Is it running?", 0);
  }

  if (!response.ok) {
    throw new ApiError(await errorMessage(response), response.status);
  }
  return (await response.json()) as T;
}

/** FastAPI returns `detail` as a string for our errors and as a list for
 *  validation failures; flatten both to one line. */
async function errorMessage(response: Response): Promise<string> {
  try {
    const body = await response.json();
    const detail = body?.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail.map((d: { msg?: string }) => d.msg ?? "invalid").join("; ");
    }
  } catch {
    /* fall through to the generic message */
  }
  return `Request failed (${response.status})`;
}

export const api = {
  settings: () => request<Settings>("/api/settings"),

  /** Both platform sections every time: each field has a default server-side,
   *  so sending half the form would quietly disconnect the other account.
   *  `lichess_token` omitted means "leave the stored one alone"; an empty
   *  string is the explicit "forget it". */
  saveSettings: (settings: {
    chesscom_enabled: boolean;
    chesscom_username: string | null;
    lichess_enabled: boolean;
    lichess_username: string | null;
    lichess_token?: string;
    analysis_depth: number;
    background_analysis: boolean;
  }) =>
    request<Settings>("/api/settings", {
      method: "PUT",
      body: JSON.stringify(settings),
    }),

  /** Partial update — preferences only, so it cannot disturb account fields. */
  savePreferences: (preferences: {
    reveal_lines_by_default?: boolean;
    analysis_depth?: number;
    background_analysis?: boolean;
  }) =>
    request<Settings>("/api/settings", {
      method: "PATCH",
      body: JSON.stringify(preferences),
    }),

  sync: () => request<SyncResponse>("/api/sync", { method: "POST" }),

  games: (limit = 50, offset = 0, filters: GameFilters = {}) => {
    const query = new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
    });
    if (filters.color) query.set("color", filters.color);
    if (filters.result) query.set("result", filters.result);
    if (filters.timeClass) query.set("time_class", filters.timeClass);
    if (filters.withErrors) query.set("with_errors", "true");
    return request<GameList>(`/api/games?${query}`);
  },

  game: (id: number) => request<GameDetail>(`/api/games/${id}`),

  /** Positions and their summary together: the game view polls this while
   *  analysis runs, so the header fills in with the board. */
  analysis: (id: number) =>
    request<{ positions: Position[]; summary: AnalysisSummary | null }>(
      `/api/games/${id}/analysis`,
    ),

  /** Evaluate an arbitrary position — a sideline the user just played. */
  evaluate: (fen: string) =>
    request<Evaluation>("/api/evaluate", {
      method: "POST",
      body: JSON.stringify({ fen }),
    }),

  analysisStatus: () => request<AnalysisStatus>("/api/analysis/status"),

  /** Analyse this game next, ahead of background work. Idempotent unless
   *  `force` — reopening a finished game does not re-analyse it. */
  analyseGame: (id: number, force = false) =>
    request<AnalysisStatus>(`/api/games/${id}/analyse?force=${force}`, {
      method: "POST",
    }),
};
