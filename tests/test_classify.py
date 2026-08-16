import pytest

from chess_analysis.classify import DEFAULT_THRESHOLDS, Severity, Thresholds


@pytest.mark.parametrize(
    ("loss", "expected"),
    [
        (0.0, None),
        (9.9, None),
        (10.0, Severity.INACCURACY),
        (19.9, Severity.INACCURACY),
        (20.0, Severity.MISTAKE),
        (29.9, Severity.MISTAKE),
        (30.0, Severity.BLUNDER),
        (100.0, Severity.BLUNDER),
    ],
)
def test_default_tier_boundaries(loss, expected):
    assert DEFAULT_THRESHOLDS.classify(loss) == expected


def test_thresholds_are_tunable():
    strict = Thresholds(inaccuracy=5.0, mistake=10.0, blunder=15.0)
    assert strict.classify(6.0) == Severity.INACCURACY
    assert strict.classify(16.0) == Severity.BLUNDER
    assert DEFAULT_THRESHOLDS.classify(6.0) is None


def test_severity_serialises_as_its_label():
    """The value stored in `positions.severity`."""
    assert str(Severity.BLUNDER) == "blunder"
