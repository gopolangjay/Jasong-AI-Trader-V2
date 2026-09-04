from backend.weekend_market_engine import structure_signal


def _c(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def test_buy_requires_break_retest_and_m1_trigger():
    prior = [_c(100, 101, 99, 100) for _ in range(20)]
    # Breakout closes above prior high=101; final bar is in-progress/ignored.
    m5 = prior + [_c(100.5, 103, 100.4, 102), _c(102, 102.2, 101.8, 102)]
    m1 = [
        _c(101.5, 101.8, 101.2, 101.6),
        _c(101.6, 101.7, 100.95, 101.1),
        _c(101.1, 101.4, 101.0, 101.3),
        _c(101.3, 101.5, 101.1, 101.4),
        _c(101.4, 101.6, 101.2, 101.5),
        _c(101.5, 101.7, 101.3, 101.55),
        _c(101.55, 102.0, 101.5, 101.9),
        _c(101.9, 102.1, 101.8, 102.0),
    ]
    out = structure_signal(m5, m1, 0.05)
    assert out["eligible"] is True
    assert out["direction"] == "BUY"
    assert out["stop"] < out["entry_reference"] < out["target"]
    assert out["target_r"] == 1.5


def test_no_m5_break_is_rejected():
    m5 = [_c(100, 101, 99, 100) for _ in range(24)]
    m1 = [_c(100, 100.2, 99.8, 100) for _ in range(8)]
    out = structure_signal(m5, m1, 0.05)
    assert out["eligible"] is False
    assert out["reason"] == "NO_M5_STRUCTURE_CLOSE"


def test_missing_retest_is_rejected():
    prior = [_c(100, 101, 99, 100) for _ in range(20)]
    m5 = prior + [_c(101, 103, 101.5, 102), _c(102, 102.2, 101.8, 102)]
    m1 = [_c(103, 103.3, 102.7, 103.1) for _ in range(8)]
    out = structure_signal(m5, m1, 0.05)
    assert out["eligible"] is False
    assert out["reason"] == "NO_RETEST"
