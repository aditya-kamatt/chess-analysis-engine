import { Chessground } from "chessground";
import type { Api } from "chessground/api";
import type { DrawShape } from "chessground/draw";
import type { Color, Key } from "chessground/types";
import { useEffect, useRef } from "react";

interface BoardProps {
  fen: string;
  orientation: Color;
  lastMove?: [Key, Key];
  /** Legal targets per square. Omit to make the board read-only. */
  dests?: Map<Key, Key[]>;
  turnColor?: Color;
  /** Colour of the king in check; chessground rings that square in red. */
  check?: Color | boolean;
  onMove?: (from: string, to: string) => void;
  /** Ghost preview of a candidate move — the app's own overlay. */
  autoShapes?: DrawShape[];
  /** Arrows and circles the user drew with the right mouse button. */
  shapes?: DrawShape[];
  onShapesChange?: (shapes: DrawShape[]) => void;
}

/** Chessground is a vanilla DOM library, so it owns its subtree: React creates
 *  the element once and every later change goes through the chessground API
 *  rather than a re-render. */
export function Board({
  fen,
  orientation,
  lastMove,
  dests,
  turnColor,
  check,
  onMove,
  autoShapes,
  shapes,
  onShapesChange,
}: BoardProps) {
  const container = useRef<HTMLDivElement>(null);
  const board = useRef<Api | null>(null);

  // Chessground takes its callbacks once, at construction, so they are read
  // through a ref rather than captured — otherwise every handler would keep
  // firing against the first render's state.
  const handlers = useRef({ onMove, onShapesChange });
  handlers.current = { onMove, onShapesChange };

  useEffect(() => {
    if (!container.current) return;
    board.current = Chessground(container.current, {
      coordinates: true,
      addPieceZIndex: true,
      movable: {
        free: false,
        showDests: true,
        events: {
          after: (from, to) => handlers.current.onMove?.(from, to),
        },
      },
      // Right-click drag draws an arrow, right-click a square rings it, with
      // modifier keys switching colour — the convention from the big sites.
      drawable: {
        enabled: true,
        onChange: (drawn) => handlers.current.onShapesChange?.(drawn),
      },
    });
    return () => {
      board.current?.destroy();
      board.current = null;
    };
  }, []);

  // One effect, not several: `set` clears both shape layers, so they must be
  // reapplied after it whenever any input changes.
  useEffect(() => {
    board.current?.set({
      fen,
      orientation,
      lastMove,
      turnColor,
      check,
      movable: { free: false, color: dests ? turnColor : undefined, dests },
    });
    board.current?.setShapes(shapes ?? []);
    board.current?.setAutoShapes(autoShapes ?? []);
  }, [fen, orientation, lastMove, dests, turnColor, check, autoShapes, shapes]);

  return <div className="cg-wrap board" ref={container} />;
}
