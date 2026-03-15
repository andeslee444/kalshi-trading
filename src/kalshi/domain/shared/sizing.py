"""Shared sizing, fee, and liquidity helpers extracted from probability."""

from __future__ import annotations

import logging
import math


_log = logging.getLogger("probability")

KALSHI_FEE_RATE = 0.07  # Kalshi fee formula: 0.07 * P * (1-P)

# Minimum spread to consider a market liquid enough to trade
MIN_LIQUIDITY_VOLUME = 10
MAX_SPREAD_FOR_ENTRY = 20  # cents


def kalshi_fee_cents(price_cents):
    """Kalshi per-contract fee in cents. Formula: 0.07 * P * (1-P) * 100."""
    p = price_cents / 100.0
    return KALSHI_FEE_RATE * p * (1 - p) * 100


def half_kelly(edge, price_cents, max_cost_cents, bankroll_cents=None, fee_cents=0,
               return_details=False):
    """Generalized half-Kelly position sizing for binary contracts (buy side).

    Returns (contracts, risk_cents).
    If return_details=True, returns (contracts, risk_cents, details_dict) where
    details_dict contains {"kelly_fraction": float, "bankroll_used": int}.

    edge: our_prob - market_implied_prob (positive = trade, negative = skip)
    price_cents: price we'd pay (1-99)
    max_cost_cents: max total spend per trade (from config)
    bankroll_cents: total available balance for Kelly fraction calculation.
                    IMPORTANT: should always be passed for proper sizing.
                    Without it, sizing is purely cost-cap limited (not Kelly).
    fee_cents: per-contract fee in cents (from kalshi_fee_cents()). When > 0,
               reduces the payout (100 -> 100-fee) rather than the probability,
               which is the mathematically correct fee treatment for Kelly.

    When buying YES at price p:
      win = (100 - fee) - p cents with prob our_prob
      lose = p cents with prob (1 - our_prob)

    When buying NO at price (100-p):
      This is equivalent - just pass the NO price as price_cents.
    """
    _zero = (0, 0, {"kelly_fraction": 0.0, "bankroll_used": bankroll_cents or 0}) if return_details else (0, 0)
    if edge <= 0 or price_cents <= 0 or price_cents >= 100:
        return _zero

    implied_prob = price_cents / 100.0
    our_prob = implied_prob + edge

    # Clamp to valid probability range
    original_prob = our_prob
    our_prob = max(0.001, min(0.999, our_prob))
    if original_prob <= 0.001 or original_prob >= 0.999:
        _log.debug("Probability clamped: %.6f -> [0.001, 0.999]", original_prob)

    # Kelly fraction: f = (b*p - q) / b
    # where b = win/loss ratio, p = win prob, q = 1 - p
    # Fee reduces the payout, not the probability
    win_amount = (100 - fee_cents) - price_cents
    if win_amount <= 0:
        return _zero
    loss_amount = price_cents
    b = win_amount / loss_amount
    kelly_f = (b * our_prob - (1 - our_prob)) / b
    half_f = kelly_f / 2

    if half_f <= 0:
        return _zero

    # Max contracts from Kelly fraction (if bankroll provided)
    if bankroll_cents is not None and bankroll_cents > 0:
        max_kelly = int((half_f * bankroll_cents) / price_cents)
    else:
        max_kelly = 999999

    # Max contracts from cost cap
    max_cost = max_cost_cents // price_cents

    contracts = min(max_kelly, max_cost)
    contracts = max(0, contracts)

    risk = contracts * price_cents
    if return_details:
        return (contracts, risk, {"kelly_fraction": round(half_f, 6), "bankroll_used": bankroll_cents or 0})
    return (contracts, risk)


def half_kelly_sell(edge, sell_price_cents, max_cost_cents, bankroll_cents=None, fee_cents=0,
                    return_details=False):
    """Half-Kelly for selling YES (buying NO). Returns (contracts, risk_cents).
    If return_details=True, returns (contracts, risk_cents, details_dict).

    edge: additive overpricing (implied_prob - true_prob, positive = sell signal).
          IMPORTANT: this must be an additive probability difference, NOT a
          multiplicative ratio. Use longshot_edge() to get the correct value.
    sell_price_cents: YES price we're selling at (1-99)
    max_cost_cents: max total risk (contracts * (100 - sell_price))
    bankroll_cents: total available balance for Kelly fraction calculation.
                    IMPORTANT: should always be passed for proper sizing.
    fee_cents: per-contract fee in cents. When > 0, reduces the win amount
               (sell proceeds) to reflect fee drag on the payout.

    When selling YES at price p:
      - We receive (p - fee) cents now
      - We lose (100 - p) cents if the event occurs
      - True prob of event = implied_prob - edge
    """
    _zero = (0, 0, {"kelly_fraction": 0.0, "bankroll_used": bankroll_cents or 0}) if return_details else (0, 0)
    if edge <= 0 or sell_price_cents <= 0 or sell_price_cents >= 100:
        return _zero

    implied_prob = sell_price_cents / 100.0
    raw_p_true = implied_prob - edge
    p_true = max(0.001, min(0.999, raw_p_true))
    if raw_p_true <= 0.001 or raw_p_true >= 0.999:
        _log.debug("Probability clamped (sell): %.6f -> [0.001, 0.999]", raw_p_true)

    win_prob = 1 - p_true
    win_amount = sell_price_cents - fee_cents
    if win_amount <= 0:
        return _zero
    loss_amount = 100 - sell_price_cents

    b = win_amount / loss_amount
    kelly_f = (b * win_prob - (1 - win_prob)) / b
    half_f = kelly_f / 2

    if half_f <= 0:
        return _zero

    risk_per = 100 - int(sell_price_cents)
    if risk_per <= 1:
        if return_details:
            return (0, 0, {"kelly_fraction": 0.0, "bankroll_used": bankroll_cents or 0})
        return _zero

    if bankroll_cents is not None and bankroll_cents > 0:
        max_kelly = int((half_f * bankroll_cents) / risk_per)
    else:
        max_kelly = 999999

    max_cap = max_cost_cents // risk_per

    contracts = min(max_kelly, max_cap)
    contracts = max(0, contracts)

    risk_cents = contracts * risk_per
    if return_details:
        return (contracts, risk_cents, {"kelly_fraction": round(half_f, 6), "bankroll_used": bankroll_cents or 0})
    return (contracts, risk_cents)


def quarter_kelly(edge, price_cents, max_cost_cents, bankroll_cents=None,
                  max_exposure_cents=None, fee_cents=0, return_details=False):
    """Quarter-Kelly for bracket markets (higher model uncertainty).

    Bracket markets (1-degree windows) have much higher forecast sensitivity
    than threshold markets. A 1F error can move bracket prob from 16% to 3%.
    Uses quarter-Kelly (half of half-Kelly) and hard-caps exposure.

    max_exposure_cents: hard cap on total position cost.
                        Default scales with bankroll: max($5, 5% of bankroll).
    fee_cents: per-contract fee in cents, passed through to half_kelly.
    If return_details=True, returns (contracts, risk_cents, details_dict).
    """
    if max_exposure_cents is None:
        max_exposure_cents = max(500, int((bankroll_cents or 10000) * 0.05))
    result = half_kelly(edge, price_cents, max_cost_cents, bankroll_cents,
                        fee_cents=fee_cents, return_details=True)
    contracts, _risk, details = result
    contracts = max(1, round(contracts / 2)) if contracts >= 1 else 0
    details["kelly_fraction"] = details["kelly_fraction"] / 2
    if contracts * price_cents > max_exposure_cents:
        contracts = max_exposure_cents // price_cents
    risk = contracts * price_cents
    if return_details:
        return (contracts, risk, details)
    return (contracts, risk)


def uncertainty_kelly(edge, price_cents, max_cost_cents, bankroll_cents,
                      scenario_agreement, posterior_sigma, fee_cents=0):
    """Kelly sizing scaled by model confidence."""
    base_count, risk, details = quarter_kelly(
        edge, price_cents, max_cost_cents, bankroll_cents,
        fee_cents=fee_cents, return_details=True,
    )

    if base_count <= 0:
        return 0, 0, {**details, "confidence": 0, "agreement_mult": 0, "sigma_mult": 0}

    agreement_mult = 0.1 + 0.9 * max(0, min(1, scenario_agreement)) ** 2
    sigma_mult = min(1.0, 0.10 / max(posterior_sigma, 0.01))

    confidence = math.sqrt(agreement_mult * sigma_mult)
    confidence = max(0.0, min(1.0, confidence))

    adjusted_count = max(1, int(base_count * confidence))
    adjusted_risk = risk * confidence

    return adjusted_count, adjusted_risk, {
        **details,
        "confidence": round(confidence, 4),
        "agreement_mult": round(agreement_mult, 4),
        "sigma_mult": round(sigma_mult, 4),
    }


def quarter_kelly_sell(edge, sell_price_cents, max_cost_cents, bankroll_cents=None,
                       max_exposure_cents=None, fee_cents=0, return_details=False):
    """Quarter-Kelly for sell-side trades (higher model uncertainty)."""
    if max_exposure_cents is None:
        max_exposure_cents = max(500, int((bankroll_cents or 10000) * 0.05))
    result = half_kelly_sell(edge, sell_price_cents, max_cost_cents, bankroll_cents,
                             fee_cents=fee_cents, return_details=True)
    contracts, _risk, details = result
    contracts = max(1, round(contracts / 2)) if contracts >= 1 else 0
    details["kelly_fraction"] = details["kelly_fraction"] / 2
    risk_per = 100 - int(sell_price_cents)
    if risk_per > 0 and contracts * risk_per > max_exposure_cents:
        contracts = max_exposure_cents // risk_per
    risk = contracts * risk_per if risk_per > 0 else 0
    if return_details:
        return (contracts, risk, details)
    return (contracts, risk)


def high_conviction_kelly(edge, price_cents, max_cost_cents, bankroll_cents=None,
                          fee_cents=0, return_details=False):
    """60% Kelly for high-conviction threshold-NO trades (>80% model prob)."""
    _zero = (0, 0, {"kelly_fraction": 0.0, "bankroll_used": bankroll_cents or 0}) if return_details else (0, 0)
    if edge <= 0 or price_cents <= 0 or price_cents >= 100:
        return _zero

    implied_prob = price_cents / 100.0
    our_prob = max(0.001, min(0.999, implied_prob + edge))

    win_amount = (100 - fee_cents) - price_cents
    if win_amount <= 0:
        return _zero
    loss_amount = price_cents
    b = win_amount / loss_amount
    kelly_f = (b * our_prob - (1 - our_prob)) / b
    if bankroll_cents and bankroll_cents < 50000:
        scaled_f = kelly_f * 0.5
    else:
        scaled_f = kelly_f * 0.6

    if scaled_f <= 0:
        return _zero

    if bankroll_cents is not None and bankroll_cents > 0:
        max_kelly = int((scaled_f * bankroll_cents) / price_cents)
    else:
        max_kelly = 999999

    max_cost = max_cost_cents // price_cents
    contracts = max(0, min(max_kelly, max_cost))
    risk = contracts * price_cents
    if return_details:
        return (contracts, risk, {"kelly_fraction": round(scaled_f, 6), "bankroll_used": bankroll_cents or 0})
    return (contracts, risk)


def apply_kelly_multipliers(base_kelly_contracts, multipliers, floor_pct=0.25):
    """Apply multiple Kelly reduction multipliers with a floor."""
    if base_kelly_contracts <= 0:
        return 0
    adjusted = base_kelly_contracts
    for multiplier in multipliers:
        adjusted *= multiplier
    floor = base_kelly_contracts * floor_pct
    return max(adjusted, floor)


def is_market_liquid(market, min_volume=None, max_spread=None):
    """Check if a market has sufficient liquidity for entry."""
    vol_threshold = min_volume if min_volume is not None else MIN_LIQUIDITY_VOLUME
    spread_threshold = max_spread if max_spread is not None else MAX_SPREAD_FOR_ENTRY

    yes_bid = market.get("yes_bid", 0)
    yes_ask = market.get("yes_ask", 0)
    volume = market.get("volume", 0) or 0

    if not yes_bid or not yes_ask:
        return False
    if yes_ask - yes_bid > spread_threshold:
        return False
    if volume < vol_threshold:
        return False
    return True


def compute_limit_price(yes_bid, yes_ask, side, edge=None):
    """Compute a limit price within the spread, adapted to edge strength."""
    if side == "yes":
        if yes_bid and yes_ask and yes_ask > yes_bid:
            if edge is not None and edge < 0.08:
                return min((yes_bid + yes_ask) // 2 + 1, yes_ask)
            if edge is not None and edge < 0.15:
                return max(yes_ask - 1, yes_bid + 1)
            return yes_ask
        return yes_ask or 0

    no_bid = 100 - yes_ask if yes_ask else 0
    no_ask = 100 - yes_bid if yes_bid else 0
    if no_bid and no_ask and no_ask > no_bid:
        spread = no_ask - no_bid
        if edge is not None and edge < 0.08:
            return no_bid + max(1, spread // 3)
        if edge is not None and edge < 0.15:
            return no_bid + max(1, spread * 2 // 3)
        return no_ask
    return no_ask or 0


__all__ = [
    "KALSHI_FEE_RATE",
    "MAX_SPREAD_FOR_ENTRY",
    "MIN_LIQUIDITY_VOLUME",
    "apply_kelly_multipliers",
    "compute_limit_price",
    "half_kelly",
    "half_kelly_sell",
    "high_conviction_kelly",
    "is_market_liquid",
    "kalshi_fee_cents",
    "quarter_kelly",
    "quarter_kelly_sell",
    "uncertainty_kelly",
]
