"""Tests for reconciliation logic (Plan 1 Task 6).

Tests the _annotate_trade function from reconcile-trades.py.

The script has heavy module-level imports (KalshiClient, trade_files)
that trigger auth initialization and file I/O, making direct import
impractical without extensive stubbing. We keep a local copy of the
pure function and validate it matches production source via
TestAnnotateTradeSourceSync.
"""

import inspect
import textwrap
from pathlib import Path

from settlement_utils import (
    allocate_integer_total,
    realized_edge_for_trade,
    settlement_payout_cents,
    settlement_result_for_trade,
    trade_has_filled_exposure,
)

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _annotate_trade(trade, settlements, fills):
    """Local copy of reconcile-trades.py::_annotate_trade for testing.

    Validated against production source by TestAnnotateTradeSourceSync.
    """
    # Skip sell (exit) records — legacy records without action are assumed buys
    if trade.get("action", "buy") != "buy":
        return False

    ticker = trade.get("ticker", "")
    order_id = trade.get("order_id", "")
    modified = False
    has_exposure, contract_count, fill_price_cents = trade_has_filled_exposure(trade, fills=fills)

    fill = fills.get(order_id) if order_id else None
    fill_count = fill.get("fill_count") if fill else None
    fill_cost_cents = fill.get("fill_cost_cents") if fill else None
    if fill_price_cents is not None and trade.get("fill_price_cents") != fill_price_cents:
        trade["fill_price_cents"] = fill_price_cents
        modified = True
    if fill_price_cents is None and not has_exposure and trade.get("fill_price_cents") is not None:
        trade["fill_price_cents"] = None
        modified = True
    if fill_count is not None and trade.get("fill_count") != fill_count:
        trade["fill_count"] = fill_count
        modified = True
    if fill_count is None and not has_exposure and trade.get("fill_count") is not None:
        trade["fill_count"] = None
        modified = True
    if fill_cost_cents is not None and trade.get("cost_cents") != fill_cost_cents:
        trade["cost_cents"] = fill_cost_cents
        modified = True

    settlement_result = trade.get("settlement_result")
    settlement_revenue_cents = trade.get("settlement_revenue_cents")
    realized_edge = trade.get("realized_edge")

    if ticker in settlements and has_exposure:
        yes_won = settlements[ticker]["yes_won"]
        settlement_result = settlement_result_for_trade(trade.get("side", "yes"), yes_won)
        settlement_revenue_cents = settlement_payout_cents(settlement_result, contract_count)
        realized_edge = realized_edge_for_trade(
            trade,
            settlement_result,
            fill_price_cents=trade.get("fill_price_cents"),
        )
    elif not has_exposure:
        settlement_result = None
        settlement_revenue_cents = None
        realized_edge = None

    if trade.get("settlement_result") != settlement_result:
        trade["settlement_result"] = settlement_result
        modified = True
    if trade.get("settlement_revenue_cents") != settlement_revenue_cents:
        trade["settlement_revenue_cents"] = settlement_revenue_cents
        modified = True
    if trade.get("realized_edge") != realized_edge:
        trade["realized_edge"] = realized_edge
        modified = True

    return modified


def _extract_function_source(filepath, func_name):
    """Extract a function's source from a Python file without importing it.

    Reads the raw file, finds the function definition, and returns its
    normalized body (dedented, stripped of docstrings' wording differences).
    """
    source_lines = filepath.read_text().splitlines()
    # Find the function start
    start_idx = None
    for i, line in enumerate(source_lines):
        if line.startswith(f"def {func_name}("):
            start_idx = i
            break
    if start_idx is None:
        raise ValueError(f"Function {func_name} not found in {filepath}")

    # Collect all lines of the function (until next top-level def/class or EOF)
    func_lines = [source_lines[start_idx]]
    for line in source_lines[start_idx + 1:]:
        # Stop at next top-level definition (not indented)
        if line and not line[0].isspace() and (line.startswith("def ") or line.startswith("class ")):
            break
        func_lines.append(line)

    return "\n".join(func_lines).rstrip()


class TestAnnotateTradeSourceSync:
    """Validate that the local _annotate_trade copy matches production source."""

    def test_local_copy_matches_production(self):
        """Local _annotate_trade body must match reconcile-trades.py source.

        If this test fails, the local copy above is out of sync with
        production code in scripts/reconcile-trades.py. Update the local
        copy to match.
        """
        prod_source = _extract_function_source(
            _SCRIPTS_DIR / "reconcile-trades.py", "_annotate_trade"
        )
        local_source = inspect.getsource(_annotate_trade)

        # Normalize: dedent, strip docstrings, collapse whitespace for comparison
        # We compare the actual logic lines (skip def line and docstring)
        def _logic_lines(source_text):
            lines = textwrap.dedent(source_text).strip().splitlines()
            # Skip def line
            body = lines[1:]
            # Skip docstring (triple-quoted block)
            result = []
            in_docstring = False
            for line in body:
                stripped = line.strip()
                if stripped.startswith('"""') or stripped.startswith("'''"):
                    if in_docstring:
                        in_docstring = False
                        continue
                    # Single-line docstring
                    if stripped.count('"""') >= 2 or stripped.count("'''") >= 2:
                        continue
                    in_docstring = True
                    continue
                if in_docstring:
                    continue
                # Skip pure comment lines for comparison
                if stripped.startswith("#"):
                    continue
                if stripped:
                    result.append(stripped)
            return result

        prod_logic = _logic_lines(prod_source)
        local_logic = _logic_lines(local_source)

        assert prod_logic == local_logic, (
            "Local _annotate_trade copy is OUT OF SYNC with production.\n"
            f"Production lines ({len(prod_logic)}):\n"
            + "\n".join(f"  {l}" for l in prod_logic)
            + f"\nLocal lines ({len(local_logic)}):\n"
            + "\n".join(f"  {l}" for l in local_logic)
        )


class TestAnnotateTrade:
    def test_sell_records_are_skipped(self):
        """Sell (exit) records should not receive settlement annotations."""
        trade_sell = {
            "ticker": "TEST-TICKER",
            "action": "sell",
            "side": "yes",
            "price_cents": 70,
            "order_id": "order123",
        }
        settlements = {"TEST-TICKER": {"yes_won": True, "revenue_cents": 300}}
        result = _annotate_trade(trade_sell, settlements, {})
        assert result is False
        assert "settlement_result" not in trade_sell

    def test_buy_records_are_annotated(self):
        """Buy records should receive settlement annotations normally."""
        trade_buy = {
            "ticker": "TEST-TICKER",
            "action": "buy",
            "side": "yes",
            "price_cents": 30,
            "order_id": "order456",
            "status": "filled",
            "count": 2,
        }
        settlements = {"TEST-TICKER": {"yes_won": True, "revenue_cents": 300}}
        result = _annotate_trade(trade_buy, settlements, {})
        assert result is True
        assert trade_buy["settlement_result"] == "won"
        assert trade_buy["settlement_revenue_cents"] == 200

    def test_legacy_records_without_action_are_annotated(self):
        """Old records without action field should still be annotated."""
        trade_old = {
            "ticker": "TEST-TICKER",
            "side": "yes",
            "price_cents": 30,
            "count": 3,
        }
        settlements = {"TEST-TICKER": {"yes_won": False, "revenue_cents": -30}}
        result = _annotate_trade(trade_old, settlements, {})
        assert result is True
        assert trade_old["settlement_result"] == "lost"
        assert trade_old["settlement_revenue_cents"] == 0

    def test_already_normalized_record_is_idempotent(self):
        """Correctly annotated records should be left untouched."""
        trade = {
            "ticker": "TEST-TICKER",
            "action": "buy",
            "side": "yes",
            "price_cents": 30,
            "count": 1,
            "order_id": "o1",
            "status": "filled",
            "settlement_result": "won",
            "settlement_revenue_cents": 100,
            "fill_price_cents": 29,
            "fill_count": 1,
        }
        fills = {"o1": {"fill_price_cents": 29, "fill_count": 1}}
        result = _annotate_trade(trade, {"TEST-TICKER": {"yes_won": True, "revenue_cents": 300}}, fills)
        assert result is False

    def test_executed_record_without_current_api_match_keeps_existing_settlement(self):
        trade = {
            "ticker": "TEST-TICKER",
            "action": "buy",
            "side": "yes",
            "price_cents": 30,
            "count": 1,
            "order_id": "o2",
            "status": "executed",
            "settlement_result": "won",
            "settlement_revenue_cents": 100,
        }
        result = _annotate_trade(trade, {}, {})
        assert result is False
        assert trade["settlement_result"] == "won"

    def test_no_side_settlement_result_lost(self):
        """NO-side trade loses when YES wins."""
        trade = {"ticker": "T", "action": "buy", "side": "no", "price_cents": 30, "count": 1}
        _annotate_trade(trade, {"T": {"yes_won": True, "revenue_cents": -30}}, {})
        assert trade["settlement_result"] == "lost"

    def test_no_side_settlement_result_won(self):
        """NO-side trade wins when YES loses."""
        trade = {"ticker": "T", "action": "buy", "side": "no", "price_cents": 30, "count": 2}
        _annotate_trade(trade, {"T": {"yes_won": False, "revenue_cents": 70}}, {})
        assert trade["settlement_result"] == "won"
        assert trade["settlement_revenue_cents"] == 200

    def test_realized_edge_yes_side_won(self):
        """YES-side realized edge: actual=1.0, implied=price/100."""
        trade = {"ticker": "T", "action": "buy", "side": "yes",
                 "price_cents": 30, "model_prob": 0.80, "count": 1}
        _annotate_trade(trade, {"T": {"yes_won": True, "revenue_cents": 70}}, {})
        assert trade["realized_edge"] == 0.70  # 1.0 - 0.30

    def test_realized_edge_yes_side_lost(self):
        """YES-side realized edge: actual=0.0, implied=price/100."""
        trade = {"ticker": "T", "action": "buy", "side": "yes",
                 "price_cents": 30, "model_prob": 0.80, "count": 1}
        _annotate_trade(trade, {"T": {"yes_won": False, "revenue_cents": -30}}, {})
        assert trade["realized_edge"] == -0.30  # 0.0 - 0.30

    def test_realized_edge_no_side_won(self):
        """NO-side realized edge in P(YES) frame: actual=0.0, implied=1-price/100."""
        trade = {"ticker": "T", "action": "buy", "side": "no",
                 "price_cents": 30, "model_prob": 0.20, "count": 1}
        _annotate_trade(trade, {"T": {"yes_won": False, "revenue_cents": 70}}, {})
        # P(YES) frame: NO won means YES lost → actual=0.0, implied=1.0-0.30=0.70
        assert trade["realized_edge"] == -0.70

    def test_realized_edge_no_side_lost(self):
        """NO-side realized edge in P(YES) frame when NO loses."""
        trade = {"ticker": "T", "action": "buy", "side": "no",
                 "price_cents": 30, "model_prob": 0.20, "count": 1}
        _annotate_trade(trade, {"T": {"yes_won": True, "revenue_cents": -30}}, {})
        # P(YES) frame: YES won → actual=1.0, implied=1.0-0.30=0.70
        assert trade["realized_edge"] == 0.30

    def test_fill_price_fallback_to_price_cents(self):
        """Uses price_cents when fill_price_cents is missing."""
        trade = {"ticker": "T", "action": "buy", "side": "yes",
                 "price_cents": 40, "model_prob": 0.60, "count": 1}
        _annotate_trade(trade, {"T": {"yes_won": True, "revenue_cents": 60}}, {})
        assert trade["realized_edge"] == 0.60  # 1.0 - 0.40

    def test_resting_unfilled_order_clears_stale_settlement_fields(self):
        """Unfilled orders should not carry settlement state after normalization."""
        trade = {
            "ticker": "T",
            "action": "buy",
            "side": "yes",
            "price_cents": 20,
            "count": 5,
            "order_id": "resting-1",
            "status": "resting",
            "settlement_result": "won",
            "settlement_revenue_cents": 500,
            "realized_edge": 0.8,
        }
        result = _annotate_trade(trade, {"T": {"yes_won": True, "revenue_cents": 500}}, {})
        assert result is True
        assert trade["settlement_result"] is None
        assert trade["settlement_revenue_cents"] is None
        assert trade["realized_edge"] is None

    def test_fill_count_controls_settlement_payout(self):
        """Gross payout should use the actual filled count, not the requested count."""
        trade = {
            "ticker": "T",
            "action": "buy",
            "side": "yes",
            "price_cents": 9,
            "count": 10,
            "order_id": "fill-1",
            "status": "resting",
        }
        fills = {"fill-1": {"fill_price_cents": 9, "fill_count": 3}}
        _annotate_trade(trade, {"T": {"yes_won": True, "revenue_cents": 300}}, fills)
        assert trade["fill_count"] == 3
        assert trade["settlement_revenue_cents"] == 300

    def test_fill_cost_updates_cost_cents_from_api_fills(self):
        trade = {
            "ticker": "T",
            "action": "buy",
            "side": "yes",
            "price_cents": 55,
            "cost_cents": 999,
            "count": 3,
            "order_id": "fill-2",
            "status": "filled",
        }
        fills = {
            "fill-2": {
                "fill_price_cents": 41,
                "fill_count": 3,
                "fill_cost_cents": 123,
            }
        }

        _annotate_trade(trade, {"T": {"yes_won": True, "revenue_cents": 300}}, fills)

        assert trade["fill_count"] == 3
        assert trade["fill_price_cents"] == 41
        assert trade["cost_cents"] == 123

    def test_executed_status_without_fill_match_still_annotates(self):
        """Executed rows without fill pagination coverage should still settle."""
        trade = {
            "ticker": "T",
            "action": "buy",
            "side": "yes",
            "price_cents": 41,
            "count": 2,
            "order_id": "exec-1",
            "status": "executed",
        }
        _annotate_trade(trade, {"T": {"yes_won": False, "revenue_cents": 0}}, {})
        assert trade["settlement_result"] == "lost"
        assert trade["settlement_revenue_cents"] == 0

    def test_fill_price_fallback_to_default(self):
        """Uses 50 when both fill_price_cents and price_cents are missing."""
        trade = {"ticker": "T", "action": "buy", "side": "yes", "model_prob": 0.80, "count": 1}
        _annotate_trade(trade, {"T": {"yes_won": True, "revenue_cents": 50}}, {})
        assert trade["realized_edge"] == 0.50  # 1.0 - 0.50

    def test_fill_price_prefers_fill_price_cents(self):
        """fill_price_cents takes priority over price_cents."""
        trade = {"ticker": "T", "action": "buy", "side": "yes",
                 "price_cents": 40, "model_prob": 0.80, "count": 1}
        fills = {"order1": {"fill_price_cents": 35, "fill_count": 1}}
        trade["order_id"] = "order1"
        _annotate_trade(trade, {"T": {"yes_won": True, "revenue_cents": 65}}, fills)
        assert trade["fill_price_cents"] == 35
        assert trade["realized_edge"] == 0.65  # 1.0 - 0.35


class TestAllocateIntegerTotal:
    def test_allocations_sum_to_total(self):
        assert sum(allocate_integer_total(11, [3, 2, 1])) == 11

    def test_largest_remainder_rounding(self):
        assert allocate_integer_total(5, [2, 1]) == [3, 2]

    def test_zero_weights_even_split(self):
        assert allocate_integer_total(5, [0, 0]) == [3, 2]
