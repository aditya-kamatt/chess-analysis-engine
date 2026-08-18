"""Why a move was bad, said in words (PRD 4.5, "Explanation quality").

Knowing the engine prefers Nxe5 is not useful without knowing it wins a pinned
knight, so three things are derived here for each move the classifier flagged:

1. **The punishment.** The refutation is already stored — the opponent's best
   line out of the position the error left behind is the *next* ply's first
   candidate line — so saying what the move actually allowed costs no engine
   time at all.
2. **The damage.** Material at the end of that refutation, against material at
   the end of the line the player should have played, both measured against
   what is on the board now — which separates material a move gives away from
   material it merely fails to win, and keeps a piece that was already dropping
   off the bill of the move that failed to save it.
3. **The motif.** Fork, pin, skewer, discovered attack, back-rank mate, or a
   piece taken for free — read off the refutation's own moves.

A fourth concern sits alongside them: Stockfish's top move is regularly one no
human would find or benefit from finding, so where a *simpler* move stands
within a small margin of it, that one is put forward instead.

Like `lines`, all of this is display rather than analysis, and runs when a game
is read rather than when it is analysed: retuning the wording, the margin or the
simplicity heuristic costs nothing, where changing what is stored would mean
re-analysing every game already in the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import chess

from chess_analysis.evaluation import pov, score_from_dict, win_percent

# How much win percentage a simpler move may give up and still be preferred to
# the engine's first choice. Small on purpose: this is "the human move is just
# as good", not "the human move is good enough".
SIMPLER_MARGIN = 3.0

# How much clearer that move has to be before it displaces the engine's. Without
# a gap, near-identical moves would swap places on heuristic noise.
SIMPLICITY_EDGE = 1.5

# Plies of the refutation to read motifs out of. The punishing idea shows up in
# the first move or two; past that the line is consolidation.
MOTIF_HORIZON = 4

# Plies of the refutation to show. Enough to see the idea through, short enough
# to read.
PUNISHMENT_PLIES = 6

# Below a pawn, a material "loss" is line-length noise rather than a fact about
# the move, and the position is what actually changed.
MATERIAL_FLOOR = 1.0

# How far into each line material is counted. A tactic collects within a move or
# two; past that the two lines are describing different games.
MATERIAL_PLIES = 6

# How far the exchanges left hanging at the end of a line are played out before
# its material is counted. Captures on one square run out long before this.
_QUIESCE_PLIES = 8

# How much of a claimed material loss the evaluation has to agree with before it
# is reported. A line that stands a pawn down for two moves and wins it back
# reads as material at a fixed horizon; if the engine does not think the
# position got that much worse, the horizon is what moved, not the material.
_CORROBORATION = 0.6

# Stand-in centipawns for a mate score, only ever used to compare a mate
# against a number: any real evaluation is far below it.
_MATE_CENTIPAWNS = 100_000

PIECE_VALUES = {
    chess.PAWN: 1.0,
    chess.KNIGHT: 3.0,
    chess.BISHOP: 3.0,
    chess.ROOK: 5.0,
    chess.QUEEN: 9.0,
    # Kings are never captured, so they carry no material weight; the fork and
    # pin tests treat the king separately, where it outranks everything.
    chess.KING: 0.0,
}

_ROOK_STEPS = ((1, 0), (-1, 0), (0, 1), (0, -1))
_BISHOP_STEPS = ((1, 1), (1, -1), (-1, 1), (-1, -1))
_SLIDER_STEPS = {
    chess.ROOK: _ROOK_STEPS,
    chess.BISHOP: _BISHOP_STEPS,
    chess.QUEEN: _ROOK_STEPS + _BISHOP_STEPS,
}

# Material differences a player already has a word for. Anything else is
# reported as a plain count of pawns.
_MATERIAL_NAMES = {
    1.0: "a pawn",
    2.0: "the exchange",
    3.0: "a piece",
    5.0: "a rook",
    9.0: "a queen",
}


def explain_error(
    fen: str,
    played_move: str,
    lines: list[dict[str, Any]],
    *,
    refutation: list[dict[str, Any]] | None = None,
    win_percent_loss: float = 0.0,
    last_move: str | None = None,
    margin: float = SIMPLER_MARGIN,
) -> dict[str, Any] | None:
    """Explain one flagged move.

    `lines` are the stored candidates for `fen`, best first; `refutation` the
    stored candidates for the position `played_move` leads to, whose first entry
    is what the opponent gets to do about it. `last_move` is the move that was
    played into `fen`, used only to recognise a recapture as an easy move.

    None when the position or the move will not replay — a malformed row is
    worth showing without an explanation, not worth a 500.
    """
    board = chess.Board(fen)
    move = _parse(board, played_move)
    if move is None:
        return None

    mover = board.turn
    played_san = board.san(move)
    after = board.copy(stack=False)
    after.push(move)

    replayed = (_candidate(board, line, last_move) for line in lines)
    candidates = [candidate for candidate in replayed if candidate]
    if not candidates:
        return None

    better = _preferred(candidates, margin)
    # Fixed depth means the search can pick a move and then think better of it
    # one ply later, which lands as a "loss" on a move it recommended itself.
    # There is nothing to play instead of it, and saying so is the explanation.
    engine_agreed = candidates[0].pv[0] == move

    # Only a trade counts as answered: a piece moved to a square and taken there
    # was hung, where one that captured and was taken back was traded.
    punishing = _punishment(
        after,
        refutation,
        answered=move.to_square if board.is_capture(move) else None,
    )
    mate_in = punishing["mate_in"] if punishing else None
    missed_mate = _mate_in(lines[0]["score"], mover) if mate_in is None else None
    damage = (
        _material_loss(
            board,
            after,
            candidates[0].pv,
            punishing["pv"],
            mover,
            swing=_eval_swing(lines[0]["score"], refutation[0]["score"], mover),
        )
        if punishing and refutation and mate_in is None and missed_mate is None
        else None
    )
    material, material_kind = damage if damage else (None, None)

    return {
        "played_san": played_san,
        "summary": _summary(
            played_san,
            material=material,
            material_kind=material_kind,
            mate_in=mate_in,
            missed_mate=missed_mate,
            motif=punishing["motifs"][0] if punishing and punishing["motifs"] else None,
            win_percent_loss=win_percent_loss,
        ),
        "material_loss": material,
        "material_kind": material_kind,
        "mate_in": mate_in,
        "missed_mate_in": missed_mate,
        "motifs": punishing["motifs"] if punishing else [],
        "punishment": punishing["san"] if punishing else [],
        "punishment_pv": punishing["uci"] if punishing else [],
        # `better` is empty when the flagged move is the engine's own first
        # choice: there is nothing to suggest playing instead of it.
        "engine_agreed": engine_agreed,
        "better": None if engine_agreed else better,
    }


@dataclass(frozen=True)
class _Candidate:
    """One stored line, replayed far enough to judge and to display."""

    san: str
    pv: list[chess.Move]
    pv_san: list[str]
    win_percent: float
    simplicity: float


def _candidate(
    board: chess.Board,
    line: dict[str, Any],
    last_move: str | None,
) -> _Candidate | None:
    moves, san = _replay_line(board, line.get("pv") or [line["move"]])
    if not moves:
        return None

    return _Candidate(
        san=san[0],
        pv=moves,
        pv_san=san,
        win_percent=win_percent(pov(score_from_dict(line["score"]), board.turn)),
        simplicity=_simplicity(board, moves, last_move),
    )


def _preferred(candidates: list[_Candidate], margin: float) -> dict[str, Any]:
    """The move to actually recommend: the engine's, or an easier one beside it.

    A move only displaces the engine's first choice by being clearly simpler
    *and* nearly as strong, so the recommendation stays honest — `cost` says
    what preferring it gives up.
    """
    best = candidates[0]
    within = [c for c in candidates if best.win_percent - c.win_percent <= margin]
    # Ties keep the engine's ranking: `max` returns the first of equal keys.
    simplest = max(within, key=lambda c: c.simplicity)

    chosen = best
    clearer = simplest.simplicity >= best.simplicity + SIMPLICITY_EDGE
    if simplest is not best and clearer:
        chosen = simplest

    return {
        "san": chosen.san,
        "pv_san": chosen.pv_san[:PUNISHMENT_PLIES],
        # UCI as well as SAN, so the line can be played out on the board rather
        # than parsed from algebraic notation by the reader (PRD 4.5).
        "pv": [move.uci() for move in chosen.pv[:PUNISHMENT_PLIES]],
        "move": chosen.pv[0].uci(),
        "simpler": chosen is not best,
        "cost": round(best.win_percent - chosen.win_percent, 1),
        "engine_san": best.san,
    }


def _punishment(
    after: chess.Board,
    refutation: list[dict[str, Any]] | None,
    *,
    answered: int | None,
) -> dict[str, Any] | None:
    """What the opponent gets to play, in SAN, with the motifs it turns on.

    `answered` is the square the flagged move traded on, if it traded, which the
    motifs need in order not to call the recapture there a free piece.
    """
    if not refutation:
        return None

    moves, san = _replay_line(after, refutation[0].get("pv") or [refutation[0]["move"]])
    if not moves:
        return None

    return {
        "san": san[:PUNISHMENT_PLIES],
        "uci": [move.uci() for move in moves[:PUNISHMENT_PLIES]],
        "pv": moves,
        "motifs": _motifs(after, moves, answered),
        "mate_in": _mate_in(refutation[0]["score"], after.turn),
    }


def _replay_line(
    board: chess.Board,
    ucis: list[str],
) -> tuple[list[chess.Move], list[str]]:
    """A stored line as moves and as SAN, cut where it stops replaying.

    Truncated rather than dropped, the way `lines` does it: a variation that no
    longer replays in full is still a variation, and its first move is still
    what the engine said about this position.
    """
    replay = board.copy(stack=False)
    moves: list[chess.Move] = []
    san: list[str] = []
    for uci in ucis:
        move = _parse(replay, uci)
        if move is None:
            break
        san.append(replay.san(move))
        moves.append(move)
        replay.push(move)
    return moves, san


def _mate_in(score: dict[str, Any], color: chess.Color) -> int | None:
    """Moves until `color` mates, if that is what the score says.

    Only mates in `color`'s favour: a score saying the side to move is the one
    getting mated is not something they are about to do.
    """
    point_of_view = pov(score_from_dict(score), color)
    mate = point_of_view.mate() if point_of_view.is_mate() else None
    return mate if mate is not None and mate > 0 else None


def _motifs(
    board: chess.Board,
    moves: list[chess.Move],
    answered: int | None,
) -> list[str]:
    """Tactical features of the punishing side's moves, the two clearest first.

    Only even plies are read: those are the moves of the side doing the
    punishing, and the answers between them are the player's, which explain
    nothing about why the position collapsed.

    Ordered by how much each one explains rather than by when it happens — a
    mate two moves down the line is the reason the position is lost, whatever
    geometry the move before it also happened to create.
    """
    found: list[tuple[int, str]] = []
    replay = board.copy(stack=False)
    for index, move in enumerate(moves[:MOTIF_HORIZON]):
        if index % 2 == 0:
            motif = _motif_for(replay, move, answered)
            if motif and motif not in found:
                found.append(motif)
        replay.push(move)
        if replay.is_checkmate():
            break

    found.sort(key=lambda motif: motif[0])
    return [text for _, text in found[:2]]


def _motif_for(
    board: chess.Board,
    move: chess.Move,
    answered: int | None,
) -> tuple[int, str] | None:
    """The one thing a move is doing, with how much it explains, or nothing.

    One per move rather than everything that happens to be true of it: a knight
    that forks the king and a rook also takes a pawn on the way, and saying both
    buries the half that explains the position.
    """
    san = board.san(move)
    mover = board.turn
    after = board.copy(stack=False)
    after.push(move)

    if after.is_checkmate():
        # "Rd8# is mate" says nothing the mate-in-N does not already say. The
        # back-rank pattern has a name, and the name is the lesson.
        return (0, f"{san} is back-rank mate") if _is_back_rank_mate(after) else None

    fork = _fork(after, move.to_square)
    if fork:
        return 1, f"{san} forks {fork}"

    tied = _pin_or_skewer(after, move.to_square)
    if tied:
        return 2, f"{san} {tied}"

    uncovered = _discovered(board, after, move, mover)
    if uncovered:
        return 3, f"{san} uncovers an attack on the {uncovered}"

    # A recapture on the square the flagged move traded on is not a free piece,
    # whatever the square's defenders now look like: the piece standing there
    # was traded for, not left hanging.
    if (
        board.is_capture(move)
        and move.to_square != answered
        and not board.attackers(not mover, move.to_square)
    ):
        victim = (
            chess.PAWN
            if board.is_en_passant(move)
            else _type_at(board, move.to_square)
        )
        if victim is not None:
            name = chess.piece_name(victim)
            square = chess.square_name(move.to_square)
            return 4, f"{san} takes the undefended {name} on {square}"

    return None


def _fork(after: chess.Board, square: int) -> str | None:
    """Two things worth taking, hit by one piece that just moved.

    A target counts if it outvalues the piece attacking it, if nothing defends
    it, or if it is the king — the three cases where the attack cannot simply be
    ignored.
    """
    piece = after.piece_at(square)
    if piece is None:
        return None

    targets: list[tuple[float, str]] = []
    for target in after.attacks(square):
        victim = after.piece_at(target)
        if victim is None or victim.color == piece.color:
            continue
        if victim.piece_type == chess.KING:
            targets.append((float("inf"), "king"))
            continue
        # Hitting a pawn and something else is not a fork worth the word, even
        # when the pawn is free.
        if PIECE_VALUES[victim.piece_type] < PIECE_VALUES[chess.KNIGHT]:
            continue
        value = PIECE_VALUES[victim.piece_type]
        defended = bool(after.attackers(victim.color, target))
        if not defended or value > PIECE_VALUES[piece.piece_type]:
            targets.append((value, chess.piece_name(victim.piece_type)))

    if len(targets) < 2:
        return None
    targets.sort(key=lambda target: -target[0])
    return f"the {targets[0][1]} and the {targets[1][1]}"


def _pin_or_skewer(after: chess.Board, square: int) -> str | None:
    """Two enemy pieces lined up on one of the moved piece's rays.

    Which of the two is worth more decides the name: the cheaper one in front is
    a pin (it cannot move), the dearer one in front is a skewer (it must).
    """
    piece = after.piece_at(square)
    if piece is None:
        return None

    for step in _SLIDER_STEPS.get(piece.piece_type, ()):
        occupied = _scan(after, square, step)
        if len(occupied) < 2:
            continue
        front, back = (after.piece_at(sq) for sq in occupied)
        if front is None or back is None:
            continue
        if front.color == piece.color or back.color == piece.color:
            continue

        front_name = chess.piece_name(front.piece_type)
        back_name = chess.piece_name(back.piece_type)
        knight = PIECE_VALUES[chess.KNIGHT]

        # Both ends have to be worth something, or every bishop on a diagonal
        # "pins" a pawn to a king. Away from the king this is a motif about
        # winning material, so both pieces have to be material worth winning.
        if front.piece_type == chess.KING:
            if PIECE_VALUES[back.piece_type] >= knight:
                return f"skewers the king to the {back_name}"
            continue
        if PIECE_VALUES[front.piece_type] < knight:
            continue
        if back.piece_type == chess.KING:
            return f"pins the {front_name} against the king"
        if PIECE_VALUES[back.piece_type] < knight:
            continue
        if PIECE_VALUES[back.piece_type] > PIECE_VALUES[front.piece_type]:
            return f"pins the {front_name} against the {back_name}"
        if PIECE_VALUES[front.piece_type] > PIECE_VALUES[back.piece_type]:
            return f"skewers the {front_name} and the {back_name}"
    return None


def _discovered(
    board: chess.Board,
    after: chess.Board,
    move: chess.Move,
    mover: chess.Color,
) -> str | None:
    """A piece the move stopped blocking a friendly slider's line to.

    The square vacated has to lie between the slider and its new target, which
    is exactly what makes the attack a discovered one rather than one that was
    already there.
    """
    sliders = (
        after.pieces(chess.QUEEN, mover)
        | after.pieces(chess.ROOK, mover)
        | after.pieces(chess.BISHOP, mover)
    )
    for square in sliders:
        if square == move.to_square:
            continue
        for target in after.attacks(square):
            victim = after.piece_at(target)
            if victim is None or victim.color == mover:
                continue
            worth_it = (
                victim.piece_type == chess.KING
                or PIECE_VALUES[victim.piece_type] >= PIECE_VALUES[chess.KNIGHT]
            )
            if not worth_it:
                continue
            if chess.between(square, target) & chess.BB_SQUARES[move.from_square]:
                return chess.piece_name(victim.piece_type)
    return None


def _is_back_rank_mate(after: chess.Board) -> bool:
    """Mate delivered along the mated king's own first rank, by a rook or queen,
    with the king walled in — the pattern people recognise by name."""
    mated = after.turn
    king = after.king(mated)
    if king is None:
        return False

    back_rank = 0 if mated == chess.WHITE else 7
    if chess.square_rank(king) != back_rank:
        return False

    checkers = after.checkers()
    if not any(
        _type_at(after, square) in (chess.ROOK, chess.QUEEN)
        and chess.square_rank(square) == back_rank
        for square in checkers
    ):
        return False

    # The escape squares that matter are the ones off the back rank: a king with
    # air in front of it was mated some other way.
    forward = 1 if mated == chess.WHITE else -1
    file = chess.square_file(king)
    for offset in (-1, 0, 1):
        if not 0 <= file + offset < 8:
            continue
        square = chess.square(file + offset, back_rank + forward)
        blocker = after.piece_at(square)
        if blocker is None or blocker.color != mated:
            return False
    return True


def _simplicity(
    board: chess.Board,
    moves: list[chess.Move],
    last_move: str | None,
) -> float:
    """How readily a human would both find this move and trust it.

    A rough score, deliberately: the point is not to rank moves precisely but to
    notice the case the PRD calls out, where the engine's first choice is a
    quiet move whose justification is four plies deep and a capture beside it is
    worth nearly the same. Forcing, material-winning moves score high; moves
    that hang a piece for compensation, or that need a series of quiet moves to
    make sense, score low.
    """
    move = moves[0]
    mover = board.turn
    after = board.copy(stack=False)
    after.push(move)

    score = 0.0

    if board.is_capture(move):
        exchange = _see(board, move)
        if exchange > 0:
            score += 3.0
        elif exchange == 0:
            score += 1.5
        if not board.attackers(not mover, move.to_square):
            score += 2.0
        if last_move:
            answered = _parse_uci(last_move)
            if answered is not None and answered.to_square == move.to_square:
                score += 1.5  # a recapture: barely a decision at all

    if after.is_check():
        score += 1.0

    # A piece of one's own that was hanging and no longer is.
    if _is_en_prise(board, move.from_square, mover) and not _is_en_prise(
        after, move.to_square, mover
    ):
        score += 1.0

    # Leaving a piece en prise for compensation is the engine move a human
    # neither finds nor believes.
    if _is_en_prise(after, move.to_square, mover):
        score -= 2.0

    replay = board.copy(stack=False)
    mates = False
    for index, step in enumerate(moves[:MOTIF_HORIZON]):
        quiet = not replay.is_capture(step) and not replay.gives_check(step)
        replay.push(step)
        mates = mates or replay.is_checkmate()
        if quiet and index % 2 == 0:
            score -= 0.75

    if mates:
        score += 2.0
    return score


def _material_loss(
    board: chess.Board,
    after: chess.Board,
    best_pv: list[chess.Move],
    punishment: list[chess.Move],
    mover: chess.Color,
    swing: float,
) -> tuple[float, str] | None:
    """Material the move costs, in pawns, and whether it was lost or passed up.

    Two different things get called "losing material": ending up with less than
    you have now, and failing to win what was on offer. They are told apart by
    measuring both lines against the material actually on the board — a piece
    that was already dropping is not billed to the move that failed to save it,
    and a missed win is not reported as a loss.

    Three things keep the figure honest. Both lines are walked the same number
    of plies from the same position, so a pawn grabbed on the twentieth ply of
    the longer one cannot masquerade as a loss in the shorter. Each is settled
    before it is counted, because a principal variation stops wherever the
    search stopped — frequently mid-exchange, where the material on the board is
    not the material either side is going to keep. And nothing is reported that
    the evaluation does not support: `swing` is what the engine says the move
    cost, in pawns, so a material claim it disagrees with is treated as an
    artefact of where the lines were cut off rather than as a fact.

    Short horizons on purpose. A tactic collects within a move or two; past that
    the two lines are describing different games, not the same one differently.
    """
    horizon = min(len(best_pv), len(punishment) + 1, MATERIAL_PLIES)
    if horizon < 2:
        return None

    now = _balance(board, mover)
    kept = _settled_balance(_walk(board, best_pv[:horizon]), mover)
    left = _settled_balance(_walk(after, punishment[: horizon - 1]), mover)

    for amount, kind in ((now - left, "lost"), (kept - now, "missed")):
        if amount >= MATERIAL_FLOOR and swing >= amount * _CORROBORATION:
            return round(amount, 1), kind
    return None


def _eval_swing(
    before: dict[str, Any],
    after: dict[str, Any],
    mover: chess.Color,
) -> float:
    """What the engine says the move cost, in pawns."""
    best = pov(score_from_dict(before), mover).score(mate_score=_MATE_CENTIPAWNS)
    played = pov(score_from_dict(after), mover).score(mate_score=_MATE_CENTIPAWNS)
    assert best is not None and played is not None  # mate_score fills both in
    return (best - played) / 100


def _walk(board: chess.Board, moves: list[chess.Move]) -> chess.Board:
    """The position a line reaches, stopping early if it no longer replays."""
    replay = board.copy(stack=False)
    for move in moves:
        if move not in replay.legal_moves:
            break
        replay.push(move)
    return replay


def _settled_balance(board: chess.Board, color: chess.Color) -> float:
    """Material once the exchanges hanging over the position are played out.

    A miniature quiescence search: whoever is to move takes whatever wins
    material, until neither side has such a capture left. Without it a line
    that ends one ply after a queen was taken reads as a queen won.
    """
    replay = board.copy(stack=False)
    for _ in range(_QUIESCE_PLIES):
        captures = [
            (_see(replay, move), move)
            for move in replay.legal_moves
            if replay.is_capture(move)
        ]
        if not captures:
            break
        won, best = max(captures, key=lambda capture: capture[0])
        if won <= 0:
            break
        replay.push(best)
    return _balance(replay, color)


def _balance(board: chess.Board, color: chess.Color) -> float:
    """Material on the board from `color`'s point of view, in pawns."""
    total = 0.0
    for piece_type, value in PIECE_VALUES.items():
        total += value * len(board.pieces(piece_type, color))
        total -= value * len(board.pieces(piece_type, not color))
    return total


def _see(board: chess.Board, move: chess.Move) -> float:
    """Static exchange evaluation: material the mover nets by capturing here.

    Positive means the capture wins material even after every recapture; zero is
    an even trade; negative loses material.
    """
    replay = board.copy(stack=False)
    won = _captured_value(board, move)
    replay.push(move)
    return won - _see_gain(replay, move.to_square)


def _see_gain(board: chess.Board, square: int) -> float:
    """Material the side to move nets by continuing the exchange on `square`.

    Always with the cheapest attacker, and never below zero: nobody is obliged
    to recapture into a loss. Legal moves are used rather than an attacker
    bitboard, so a pinned defender correctly counts for nothing.
    """
    captures = [
        move
        for move in board.legal_moves
        if move.to_square == square and board.is_capture(move)
    ]
    if not captures:
        return 0.0

    def attacker_value(move: chess.Move) -> float:
        return PIECE_VALUES[_type_at(board, move.from_square)]

    cheapest = min(captures, key=attacker_value)
    won = _captured_value(board, cheapest)
    replay = board.copy(stack=False)
    replay.push(cheapest)
    return max(0.0, won - _see_gain(replay, square))


def _captured_value(board: chess.Board, move: chess.Move) -> float:
    if board.is_en_passant(move):
        return PIECE_VALUES[chess.PAWN]
    captured = _type_at(board, move.to_square)
    return PIECE_VALUES[captured] if captured is not None else 0.0


def _is_en_prise(board: chess.Board, square: int, color: chess.Color) -> bool:
    """Is the piece on `square` attacked in a way that costs material?

    Undefended and attacked, or attacked by something cheaper than itself. Kings
    are excluded: a king in check is a different problem with its own name.
    """
    piece = board.piece_at(square)
    if piece is None or piece.color != color or piece.piece_type == chess.KING:
        return False
    attackers = board.attackers(not color, square)
    if not attackers:
        return False
    if not board.attackers(color, square):
        return True
    return any(
        PIECE_VALUES[_type_at(board, attacker)] < PIECE_VALUES[piece.piece_type]
        for attacker in attackers
    )


def _scan(board: chess.Board, origin: int, step: tuple[int, int]) -> list[int]:
    """The first two occupied squares from `origin` along `step`."""
    file, rank = chess.square_file(origin), chess.square_rank(origin)
    found: list[int] = []
    while len(found) < 2:
        file += step[0]
        rank += step[1]
        if not (0 <= file < 8 and 0 <= rank < 8):
            break
        square = chess.square(file, rank)
        if board.piece_at(square) is not None:
            found.append(square)
    return found


def _summary(
    played_san: str,
    *,
    material: float | None,
    material_kind: str | None,
    mate_in: int | None,
    missed_mate: int | None,
    motif: str | None,
    win_percent_loss: float,
) -> str:
    """One sentence: what the move cost, and the tactic that collects it."""
    if mate_in is not None:
        core = f"{played_san} allows mate in {mate_in}"
    elif missed_mate is not None:
        core = f"{played_san} passes up mate in {missed_mate}"
    elif material is not None:
        verb = "drops" if material_kind == "lost" else "passes up"
        core = f"{played_san} {verb} {_material_phrase(material)}"
    else:
        core = f"{played_san} gives up {win_percent_loss:.0f}% of the win chance"
        if motif is None:
            return f"{core} without losing material."
    return f"{core} — {motif}." if motif else f"{core}."


def _material_phrase(loss: float) -> str:
    rounded = round(loss)
    if abs(loss - rounded) < 0.25 and rounded in _MATERIAL_NAMES:
        return _MATERIAL_NAMES[float(rounded)]
    return f"{loss:g} pawns of material"


def _type_at(board: chess.Board, square: int) -> int | None:
    piece = board.piece_at(square)
    return piece.piece_type if piece else None


def _parse(board: chess.Board, uci: str) -> chess.Move | None:
    """A stored UCI move, if it is legal here. Stored analysis is replayed
    rather than trusted: a row from an older schema must not raise."""
    move = _parse_uci(uci)
    return move if move is not None and move in board.legal_moves else None


def _parse_uci(uci: str) -> chess.Move | None:
    try:
        return chess.Move.from_uci(uci)
    except ValueError:
        return None
