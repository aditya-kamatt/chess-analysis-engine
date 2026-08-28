import type { DrawShape } from "chessground/draw";

import type { Line } from "./api";

/** State transitions for reviewing one game. Rendering stays in GameView. */
export interface Sideline {
  fromPly: number;
  moves: string[];
  index: number;
}

export interface Promotion {
  from: string;
  to: string;
}

export interface ReviewSession {
  ply: number;
  revealed: boolean;
  preview: Line | null;
  sideline: Sideline | null;
  promotion: Promotion | null;
  drawings: Record<string, DrawShape[]>;
}

export const initialReviewSession: ReviewSession = {
  ply: 0,
  revealed: false,
  preview: null,
  sideline: null,
  promotion: null,
  drawings: {},
};

export type ReviewAction =
  | { type: "reset" }
  | { type: "goTo"; ply: number }
  | { type: "reveal"; revealed: boolean }
  | { type: "preview"; line: Line | null }
  | { type: "startSideline"; sideline: Sideline }
  | { type: "appendMove"; fromPly: number; move: string }
  | { type: "stepSideline"; index: number }
  | { type: "exitSideline" }
  | { type: "promotion"; promotion: Promotion | null }
  | { type: "drawings"; fen: string; shapes: DrawShape[] };

/** The interface is the test surface for navigation and sideline precedence. */
export function reviewReducer(
  state: ReviewSession,
  action: ReviewAction,
): ReviewSession {
  switch (action.type) {
    case "reset":
      return { ...initialReviewSession, revealed: state.revealed };
    case "goTo":
      return { ...state, ply: action.ply, sideline: null, preview: null };
    case "reveal":
      return { ...state, revealed: action.revealed };
    case "preview":
      return { ...state, preview: action.line };
    case "startSideline":
      return { ...state, preview: null, sideline: action.sideline };
    case "appendMove": {
      const current = state.sideline;
      if (!current) {
        return {
          ...state,
          preview: null,
          sideline: { fromPly: action.fromPly, moves: [action.move], index: 1 },
        };
      }
      const moves = [...current.moves.slice(0, current.index), action.move];
      return {
        ...state,
        preview: null,
        sideline: { ...current, moves, index: moves.length },
      };
    }
    case "stepSideline":
      return state.sideline
        ? { ...state, sideline: { ...state.sideline, index: action.index } }
        : state;
    case "exitSideline":
      return { ...state, sideline: null };
    case "promotion":
      return { ...state, promotion: action.promotion };
    case "drawings":
      return { ...state, drawings: { ...state.drawings, [action.fen]: action.shapes } };
  }
}
