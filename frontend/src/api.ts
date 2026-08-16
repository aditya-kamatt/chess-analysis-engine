export interface Settings {
  chesscom_enabled: boolean;
  chesscom_username: string | null;
  chesscom_last_synced_at: string | null;
  chesscom_backfill_cursor: string | null;
  reveal_lines_by_default: boolean;
  analysis_depth: number;
  background_analysis: boolean;
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
}

export interface GameDetail extends GameSummary {
  pgn: string;
}

export interface GameList {
  games: GameSummary[];
  total: number;
  history_back_to: string | null;
}

export interface SyncResponse {
  inserted: number;
  entries_seen: number;
  archives_read: number;
  archives_unchanged: number;
  first_sync: boolean;
  last_synced_at: string;
  backfill_cursor: string | null;
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

  saveSettings: (settings: {
    chesscom_enabled: boolean;
    chesscom_username: string | null;
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

  games: (limit = 50, offset = 0) =>
    request<GameList>(`/api/games?limit=${limit}&offset=${offset}`),

  game: (id: number) => request<GameDetail>(`/api/games/${id}`),

  analysis: (id: number) =>
    request<{ positions: Position[] }>(`/api/games/${id}/analysis`),

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
