import { describe, expect, it } from "vitest";

import { initialReviewSession, reviewReducer } from "./review-session";

describe("reviewReducer", () => {
  it("drops a sideline and preview when navigating the game", () => {
    const withSideline = reviewReducer(initialReviewSession, {
      type: "startSideline",
      sideline: { fromPly: 4, moves: ["e2e4"], index: 1 },
    });
    const next = reviewReducer(withSideline, { type: "goTo", ply: 3 });

    expect(next.ply).toBe(3);
    expect(next.sideline).toBeNull();
    expect(next.preview).toBeNull();
  });

  it("replaces the sideline tail when a move is played after stepping back", () => {
    const started = reviewReducer(initialReviewSession, {
      type: "startSideline",
      sideline: { fromPly: 2, moves: ["e2e4", "e7e5"], index: 1 },
    });
    const next = reviewReducer(started, {
      type: "appendMove",
      fromPly: 2,
      move: "c7c5",
    });

    expect(next.sideline).toEqual({ fromPly: 2, moves: ["e2e4", "c7c5"], index: 2 });
  });
});
