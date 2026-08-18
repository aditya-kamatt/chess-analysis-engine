import chess
import pytest

from chess_analysis.explain import explain_error

# Black has just played a losing queen sortie; White wins it with Nxh4.
HANGING_QUEEN = "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 0 1"

# White's knight on d5 hits c7, where the king and the a8 rook line up behind it.
FORKABLE = "r3k2r/ppp2ppp/8/3N4/8/8/PPP2PPP/R3K2R b KQkq - 0 1"

# Black's rook is the only thing covering the back rank.
BACK_RANK = "3r2k1/5ppp/8/8/8/8/5PPP/R5K1 b - - 0 1"

# Black's knight stands between the bishop's diagonal and the queen.
PINNABLE = "4k3/3q1ppp/2n5/8/8/8/5PPP/4KB2 b - - 0 1"

# White's knight can take the undefended bishop on d5.
FREE_BISHOP = "4k3/8/8/3b4/5N2/8/4P3/4K3 w - - 0 1"


def line(*uci: str, cp: int | None = None, mate: int | None = None) -> dict:
    score = {"cp": cp} if mate is None else {"mate": mate}
    return {"move": uci[0], "score": score, "pv": list(uci)}


def test_a_hung_queen_is_named_as_one():
    explanation = explain_error(
        HANGING_QUEEN,
        "d8h4",
        [line("g8f6", "b1c3", cp=20)],
        refutation=[line("f3h4", "a7a6", cp=900)],
    )

    assert explanation["material_loss"] == 9.0
    assert explanation["material_kind"] == "lost"
    assert explanation["summary"] == (
        "Qh4 drops a queen — Nxh4 takes the undefended queen on h4."
    )


def test_the_refutation_is_shown_in_san():
    explanation = explain_error(
        HANGING_QUEEN,
        "d8h4",
        [line("g8f6", "b1c3", cp=20)],
        refutation=[line("f3h4", "a7a6", cp=900)],
    )

    assert explanation["punishment"] == ["Nxh4", "a6"]
    assert explanation["played_san"] == "Qh4"


def test_a_fork_is_recognised():
    explanation = explain_error(
        FORKABLE,
        "h7h6",
        [line("e8c8", "e1c1", cp=0)],
        refutation=[line("d5c7", "e8d7", "c7a8", cp=300)],
    )

    assert explanation["motifs"] == ["Nxc7+ forks the king and the rook"]
    assert "forks the king and the rook" in explanation["summary"]


def test_back_rank_mate_is_named():
    explanation = explain_error(
        BACK_RANK,
        "d8d7",
        [line("f7f6", "f2f3", cp=0)],
        refutation=[line("a1a8", "d7d8", "a8d8", mate=2)],
    )

    assert explanation["mate_in"] == 2
    assert explanation["summary"] == "Rd7 allows mate in 2 — Rxd8# is back-rank mate."
    # A mate is not also billed as material: the game is over either way.
    assert explanation["material_loss"] is None


def test_a_pin_names_both_ends_of_it():
    explanation = explain_error(
        PINNABLE,
        "h7h6",
        [line("c6d4", "e1e2", cp=0)],
        refutation=[line("f1b5", cp=150)],
    )

    assert explanation["motifs"] == ["Bb5 pins the knight against the queen"]


def test_material_that_was_never_taken_is_passed_up_not_dropped():
    """Failing to win a piece and giving one away are different mistakes."""
    explanation = explain_error(
        FREE_BISHOP,
        "e2e4",
        [line("f4d5", "e8f7", cp=400), line("e2e4", "e8f7", cp=100)],
        refutation=[line("d5a8", cp=100)],
    )

    assert explanation["material_kind"] == "missed"
    assert explanation["summary"].startswith("e4 passes up a piece")


def test_material_the_evaluation_does_not_support_is_not_reported():
    """A line that stands a piece down and wins it back past the horizon reads
    as material; if the engine does not agree the position got worse, it is the
    horizon that moved."""
    explanation = explain_error(
        FREE_BISHOP,
        "e2e4",
        [line("f4d5", "e8f7", cp=400), line("e2e4", "e8f7", cp=100)],
        refutation=[line("d5a8", cp=380)],
    )

    assert explanation["material_loss"] is None
    assert "win chance" in explanation["summary"]


def test_a_simpler_move_displaces_the_engines_first_choice():
    """Taking a free bishop beats a quiet move worth the same (PRD 4.5)."""
    explanation = explain_error(
        FREE_BISHOP,
        "e1d1",
        [line("e2e4", "e8d7", cp=100), line("f4d5", cp=95)],
        refutation=[line("d5f3", cp=90)],
    )

    better = explanation["better"]
    assert better["san"] == "Nxd5"
    assert better["simpler"] is True
    assert better["engine_san"] == "e4"
    assert better["cost"] < 1.0


def test_a_simpler_move_that_costs_too_much_is_left_alone():
    explanation = explain_error(
        FREE_BISHOP,
        "e1d1",
        [line("e2e4", "e8d7", cp=100), line("f4d5", cp=-300)],
        refutation=[line("d5f3", cp=90)],
    )

    better = explanation["better"]
    assert better["san"] == "e4"
    assert better["simpler"] is False
    assert better["cost"] == 0.0


def test_the_engines_own_move_is_not_offered_as_the_alternative():
    """At fixed depth the search can turn against a move one ply after
    recommending it. There is nothing to play instead of it."""
    explanation = explain_error(
        FREE_BISHOP,
        "e2e4",
        [line("e2e4", "e8d7", cp=100)],
        refutation=[line("d5f3", cp=-200)],
    )

    assert explanation["engine_agreed"] is True
    assert explanation["better"] is None


def test_a_positional_error_says_so_rather_than_inventing_material():
    explanation = explain_error(
        FREE_BISHOP,
        "e1d1",
        [line("e2e4", "e8d7", cp=100), line("f4d5", cp=95)],
        refutation=[line("d5f3", "d1e1", cp=90)],
        win_percent_loss=11.4,
    )

    assert explanation["material_loss"] is None
    assert explanation["mate_in"] is None
    assert explanation["summary"] == (
        "Kd1 gives up 11% of the win chance without losing material."
    )


def test_the_final_move_of_a_game_has_no_refutation_to_show():
    explanation = explain_error(
        HANGING_QUEEN,
        "d8h4",
        [line("g8f6", "b1c3", cp=20)],
    )

    assert explanation["punishment"] == []
    assert explanation["motifs"] == []
    assert explanation["material_loss"] is None
    assert explanation["better"]["san"] == "Nf6"


def test_a_recapture_counts_as_an_easy_move():
    """`last_move` is the move played into this position, so a candidate landing
    on the same square is barely a decision at all."""
    board = chess.Board(FREE_BISHOP)
    quiet = line("e2e4", "e8d7", cp=100)
    capture = line("f4d5", cp=95)

    without = explain_error(board.fen(), "e1d1", [quiet, capture])
    with_recapture = explain_error(
        board.fen(), "e1d1", [quiet, capture], last_move="c6d5"
    )

    assert without["better"]["san"] == "Nxd5"
    assert with_recapture["better"]["san"] == "Nxd5"
    assert with_recapture["better"]["cost"] == without["better"]["cost"]


@pytest.mark.parametrize("played", ["not-a-move", "e7e5", ""])
def test_a_move_that_does_not_replay_is_declined_rather_than_raised(played):
    assert explain_error(FREE_BISHOP, played, [line("f4d5", cp=95)]) is None


def test_lines_that_do_not_replay_are_skipped():
    explanation = explain_error(
        FREE_BISHOP,
        "e1d1",
        [line("d5f3", cp=100), line("f4d5", cp=95)],
    )

    assert explanation["better"]["san"] == "Nxd5"


def test_no_usable_lines_at_all():
    assert explain_error(FREE_BISHOP, "e1d1", []) is None
    assert explain_error(FREE_BISHOP, "e1d1", [line("d5f3", cp=10)]) is None


def test_a_refutation_that_does_not_replay_is_ignored():
    explanation = explain_error(
        HANGING_QUEEN,
        "d8h4",
        [line("g8f6", "b1c3", cp=20)],
        refutation=[line("a1a8", cp=900)],
    )

    assert explanation["punishment"] == []
    assert explanation["summary"].startswith("Qh4")


def test_a_recapture_is_not_called_a_free_piece():
    """Taking back on the square a trade happened is not winning material, even
    though the piece standing there is undefended by the time it is taken."""
    board = chess.Board(HANGING_QUEEN)
    explanation = explain_error(
        board.fen(),
        "c6d4",  # Nd4, offering the trade
        [line("g8f6", "b1c3", cp=20)],
        refutation=[line("f3d4", "e5d4", cp=120)],
    )

    assert explanation["motifs"] == []


def test_a_piece_moved_onto_a_defended_square_still_hangs():
    """The same square, but nothing was traded for it."""
    explanation = explain_error(
        HANGING_QUEEN,
        "d8h4",
        [line("g8f6", "b1c3", cp=20)],
        refutation=[line("f3h4", "a7a6", cp=900)],
    )

    assert explanation["motifs"] == ["Nxh4 takes the undefended queen on h4"]


def test_a_missed_forced_mate_is_not_measured_in_pawns():
    explanation = explain_error(
        FREE_BISHOP,
        "e1d1",
        [line("f4d5", "e8f7", mate=4), line("e2e4", "e8f7", cp=100)],
        refutation=[line("d5f3", cp=90)],
    )

    assert explanation["missed_mate_in"] == 4
    assert explanation["material_loss"] is None
    assert explanation["summary"].startswith("Kd1 passes up mate in 4")
