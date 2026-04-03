"""Tests for Book C: live event signals."""

from domain.oracle.books.book_c import (
    detect_foul_trouble,
    detect_ot_likely,
    detect_blowout,
    detect_clutch_comeback,
    live_signal_to_signal,
)
from domain.oracle.models import Book


def test_foul_trouble_q3():
    """4 fouls in Q3, below line -> SELL over."""
    sig = detect_foul_trouble(
        fouls=4, period="Q3", clock_seconds=300,
        current_stat=15, line=27,
        kalshi_price_cents=55,
        ticker="T1", player_name="Player X",
        min_edge=0.10,
    )
    assert sig is not None
    assert sig.signal_type == "foul_trouble"
    assert sig.side == "no"  # sell over
    assert sig.edge > 0.10


def test_foul_trouble_q4_no_trigger():
    """4 fouls in Q4 -> not triggered (already in Q4)."""
    sig = detect_foul_trouble(
        fouls=4, period="Q4", clock_seconds=300,
        current_stat=15, line=27,
        kalshi_price_cents=55,
    )
    assert sig is None


def test_foul_trouble_above_line():
    """Player above line -> no signal."""
    sig = detect_foul_trouble(
        fouls=5, period="Q3", clock_seconds=300,
        current_stat=30, line=27,
        kalshi_price_cents=55,
    )
    assert sig is None


def test_foul_trouble_3_fouls():
    """Only 3 fouls -> no signal."""
    sig = detect_foul_trouble(
        fouls=3, period="Q3", clock_seconds=300,
        current_stat=15, line=27,
        kalshi_price_cents=55,
    )
    assert sig is None


def test_ot_likely():
    """Tied game, 30s left in Q4, player close to line -> BUY over.

    Dynamic OT model: margin=0/clock=30s → ot_prob ~68%, combined with
    high over_prob_with_ot from strong per-min rate → model_prob well
    above the 25c Kalshi price, generating edge.
    """
    sig = detect_ot_likely(
        margin=0, period="Q4", clock_seconds=30,
        current_stat=24, per_min_rate=1.0, line=27,
        kalshi_price_cents=25,  # Kalshi underpriced at 25c
        ticker="T1", player_name="Player Y",
        min_edge=0.10,
    )
    assert sig is not None
    assert sig.signal_type == "ot_likely"
    assert sig.side == "yes"  # buy over
    assert sig.edge > 0.10
    # Dynamic model should produce varying probabilities
    assert 0.20 < sig.model_prob < 0.85


def test_ot_dynamic_prob_varies_with_clock():
    """OT probability should increase as clock decreases (closer to end)."""
    # 90 seconds left, margin=0: lower OT probability
    sig_90 = detect_ot_likely(
        margin=0, period="Q4", clock_seconds=90,
        current_stat=24, per_min_rate=1.0, line=27,
        kalshi_price_cents=20,
        min_edge=0.01,  # low threshold to let both through
    )
    # 15 seconds left, margin=0: higher OT probability
    sig_15 = detect_ot_likely(
        margin=0, period="Q4", clock_seconds=15,
        current_stat=24, per_min_rate=1.0, line=27,
        kalshi_price_cents=20,
        min_edge=0.01,
    )
    assert sig_90 is not None
    assert sig_15 is not None
    # The later scenario should have higher model_prob (more OT likely)
    assert sig_15.model_prob > sig_90.model_prob


def test_ot_likely_not_close():
    """Game not close -> no OT signal."""
    sig = detect_ot_likely(
        margin=10, period="Q4", clock_seconds=90,
        current_stat=24, per_min_rate=0.8, line=27,
        kalshi_price_cents=40,
    )
    assert sig is None


def test_ot_likely_too_far_below():
    """Player too far below line -> no signal."""
    sig = detect_ot_likely(
        margin=0, period="Q4", clock_seconds=90,
        current_stat=5, per_min_rate=0.5, line=27,
        kalshi_price_cents=40,
    )
    assert sig is None


def test_blowout_q3():
    """20+ pt blowout in Q3, player below line -> SELL over."""
    sig = detect_blowout(
        margin=25, period="Q3", clock_seconds=300,
        current_stat=15, line=27,
        kalshi_price_cents=50,
        ticker="T1", player_name="Player Z",
        min_edge=0.10,
    )
    assert sig is not None
    assert sig.signal_type == "blowout"
    assert sig.side == "no"  # sell over
    assert sig.edge > 0.10


def test_blowout_small_margin():
    """10 pt lead -> not a blowout."""
    sig = detect_blowout(
        margin=10, period="Q3", clock_seconds=300,
        current_stat=15, line=27,
        kalshi_price_cents=50,
    )
    assert sig is None


def test_blowout_player_above_line():
    """Player already over -> no signal."""
    sig = detect_blowout(
        margin=25, period="Q3", clock_seconds=300,
        current_stat=30, line=27,
        kalshi_price_cents=50,
    )
    assert sig is None


def test_blowout_q2_not_triggered():
    """Blowout detection only in Q3+."""
    sig = detect_blowout(
        margin=25, period="Q2", clock_seconds=300,
        current_stat=10, line=27,
        kalshi_price_cents=50,
    )
    assert sig is None


def test_clutch_comeback_signal_triggers_fade():
    """Trailing team is overpriced late in a one-possession game (fade direction)."""
    # Empirical: margin=2, 90s -> trailing wins 35.2%
    # Kalshi trailing price 50c (implied 50%) -> overpricing = 50% - 35.2% = 14.8%
    sig = detect_clutch_comeback(
        margin=2,
        period="Q4",
        clock_seconds=90,
        trailing_team_price_cents=50,
        ticker="KXNBAGAME-TEST-TRAIL",
        trailing_team="Pacers",
        leading_team="Bucks",
        game_id="G1",
        min_edge=0.08,
    )
    assert sig is not None
    assert sig.signal_type == "clutch_comeback"
    assert sig.side == "no"
    assert sig.edge > 0.08
    assert sig.metadata["trailing_team"] == "Pacers"
    assert sig.metadata["direction"] == "fade_trailing"


def test_clutch_comeback_signal_triggers_buy_trailing():
    """Trailing team is underpriced — buy YES on trailing."""
    # Empirical: margin=1, 20s -> trailing wins 40.2%
    # Kalshi trailing price 25c (implied 25%) -> underpricing = 40.2% - 25% = 15.2%
    sig = detect_clutch_comeback(
        margin=1,
        period="Q4",
        clock_seconds=20,
        trailing_team_price_cents=25,
        ticker="KXNBAGAME-TEST-TRAIL",
        trailing_team="Heat",
        leading_team="Celtics",
        game_id="G2",
        min_edge=0.08,
    )
    assert sig is not None
    assert sig.signal_type == "clutch_comeback"
    assert sig.side == "yes"
    assert sig.edge > 0.08
    assert sig.metadata["direction"] == "buy_trailing"


def test_clutch_comeback_rejects_tied_game():
    sig = detect_clutch_comeback(
        margin=0,
        period="Q4",
        clock_seconds=45,
        trailing_team_price_cents=40,
    )
    assert sig is None


def test_clutch_comeback_rejects_low_edge():
    # Empirical: margin=2, 90s -> trailing wins 35.2%
    # Kalshi trailing price 35c (implied 35%) -> both edges < 0.08
    sig = detect_clutch_comeback(
        margin=2,
        period="Q4",
        clock_seconds=90,
        trailing_team_price_cents=35,
        min_edge=0.08,
    )
    assert sig is None


def test_live_signal_to_signal():
    from domain.oracle.books.book_c import LiveSignal
    ls = LiveSignal(
        signal_type="foul_trouble",
        ticker="T1",
        side="no",
        edge=0.20,
        model_prob=0.75,
        kalshi_price_cents=45,
        player_name="Player X",
        game_id="G1",
        player_id=42,
    )
    signal = live_signal_to_signal(ls)
    assert signal.book == Book.C
    assert signal.side == "no"
    assert signal.edge == 0.20
    assert signal.metadata["signal_type"] == "foul_trouble"


def test_extreme_prices_rejected():
    """Extreme prices (near 0 or 100) should be rejected."""
    assert detect_foul_trouble(4, "Q3", 300, 15, 27, 3) is None
    assert detect_foul_trouble(4, "Q3", 300, 15, 27, 97) is None
    assert detect_ot_likely(0, "Q4", 90, 24, 0.8, 27, 3) is None
    assert detect_blowout(25, "Q3", 300, 15, 27, 3) is None


# ── Signal cancellation tests (spec Section 5) ──

def test_cancel_blowout_on_margin_narrowing():
    """Blowout reversal: margin < 15 → cancel."""
    from domain.oracle.books.book_c import should_cancel_signal
    assert should_cancel_signal("blowout", margin=12, period="Q3", clock_seconds=300)
    assert not should_cancel_signal("blowout", margin=20, period="Q3", clock_seconds=300)


def test_cancel_ot_on_margin_widening():
    """OT reversal: margin > 4 → cancel."""
    from domain.oracle.books.book_c import should_cancel_signal
    assert should_cancel_signal("ot_likely", margin=6, period="Q4", clock_seconds=60)
    assert not should_cancel_signal("ot_likely", margin=1, period="Q4", clock_seconds=60)


def test_cancel_foul_trouble_late_q4():
    """Foul trouble: deep Q4 with no 5th foul → cancel (less impact)."""
    from domain.oracle.books.book_c import should_cancel_signal
    # Q4, 2 min left, still only 4 fouls → cancel
    assert should_cancel_signal("foul_trouble", margin=5, period="Q4",
                                 clock_seconds=120, fouls=4)
    # Q3, 4 fouls → don't cancel (still pre-Q4)
    assert not should_cancel_signal("foul_trouble", margin=5, period="Q3",
                                     clock_seconds=300, fouls=4)


def test_cancel_clutch_comeback_on_tie():
    from domain.oracle.books.book_c import should_cancel_signal
    assert should_cancel_signal("clutch_comeback", margin=0, period="Q4", clock_seconds=45)
    assert not should_cancel_signal("clutch_comeback", margin=3, period="Q4", clock_seconds=45)
