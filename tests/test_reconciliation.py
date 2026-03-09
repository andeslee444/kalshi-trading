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

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _annotate_trade(trade, settlements, fills):
    """Local copy of reconcile-trades.py::_annotate_trade for testing.

    Validated against production source by TestAnnotateTradeSourceSync.
    """
    # Skip sell (exit) records — legacy records without action are assumed buys
    if trade.get("action", "buy") != "buy":
        return False

    # Skip already-annotated records
    if trade.get("settlement_result") is not None:
        return False

    ticker = trade.get("ticker", "")
    order_id = trade.get("order_id", "")
    side = trade.get("side", "yes")
    modified = False

    # Match settlement
    if ticker in settlements:
        s = settlements[ticker]
        yes_won = s["yes_won"]

        if side == "yes":
            trade["settlement_result"] = "won" if yes_won else "lost"
        else:
            trade["settlement_result"] = "won" if not yes_won else "lost"

        trade["settlement_revenue_cents"] = s["revenue_cents"]
        modified = True

    # Match fill price
    if order_id and order_id in fills:
        f = fills[order_id]
        trade["fill_price_cents"] = f["fill_price_cents"]
        modified = True

    # Compute realized edge (in P(YES) frame to match model_prob convention)
    if trade.get("settlement_result") and trade.get("model_prob") is not None:
        fill_price = trade.get("fill_price_cents")
        if fill_price is None:
            fill_price = trade.get("price_cents")
        if fill_price is None:
            fill_price = 50
        if side == "yes":
            actual = 1.0 if trade["settlement_result"] == "won" else 0.0
            implied = fill_price / 100.0
        else:
            actual = 0.0 if trade["settlement_result"] == "won" else 1.0
            implied = 1.0 - fill_price / 100.0
        trade["realized_edge"] = round(actual - implied, 4)
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
        }
        settlements = {"TEST-TICKER": {"yes_won": True, "revenue_cents": 300}}
        result = _annotate_trade(trade_buy, settlements, {})
        assert result is True
        assert trade_buy["settlement_result"] == "won"

    def test_legacy_records_without_action_are_annotated(self):
        """Old records without action field should still be annotated."""
        trade_old = {
            "ticker": "TEST-TICKER",
            "side": "yes",
            "price_cents": 30,
            "order_id": "order789",
        }
        settlements = {"TEST-TICKER": {"yes_won": False, "revenue_cents": -30}}
        result = _annotate_trade(trade_old, settlements, {})
        assert result is True
        assert trade_old["settlement_result"] == "lost"

    def test_already_annotated_skipped(self):
        """Records with settlement_result already set should be skipped."""
        trade = {
            "ticker": "TEST-TICKER",
            "action": "buy",
            "side": "yes",
            "price_cents": 30,
            "settlement_result": "won",
        }
        result = _annotate_trade(trade, {"TEST-TICKER": {"yes_won": True, "revenue_cents": 300}}, {})
        assert result is False

    def test_no_side_settlement_result_lost(self):
        """NO-side trade loses when YES wins."""
        trade = {"ticker": "T", "action": "buy", "side": "no", "price_cents": 30}
        _annotate_trade(trade, {"T": {"yes_won": True, "revenue_cents": -30}}, {})
        assert trade["settlement_result"] == "lost"

    def test_no_side_settlement_result_won(self):
        """NO-side trade wins when YES loses."""
        trade = {"ticker": "T", "action": "buy", "side": "no", "price_cents": 30}
        _annotate_trade(trade, {"T": {"yes_won": False, "revenue_cents": 70}}, {})
        assert trade["settlement_result"] == "won"

    def test_realized_edge_yes_side_won(self):
        """YES-side realized edge: actual=1.0, implied=price/100."""
        trade = {"ticker": "T", "action": "buy", "side": "yes",
                 "price_cents": 30, "model_prob": 0.80}
        _annotate_trade(trade, {"T": {"yes_won": True, "revenue_cents": 70}}, {})
        assert trade["realized_edge"] == 0.70  # 1.0 - 0.30

    def test_realized_edge_yes_side_lost(self):
        """YES-side realized edge: actual=0.0, implied=price/100."""
        trade = {"ticker": "T", "action": "buy", "side": "yes",
                 "price_cents": 30, "model_prob": 0.80}
        _annotate_trade(trade, {"T": {"yes_won": False, "revenue_cents": -30}}, {})
        assert trade["realized_edge"] == -0.30  # 0.0 - 0.30

    def test_realized_edge_no_side_won(self):
        """NO-side realized edge in P(YES) frame: actual=0.0, implied=1-price/100."""
        trade = {"ticker": "T", "action": "buy", "side": "no",
                 "price_cents": 30, "model_prob": 0.20}
        _annotate_trade(trade, {"T": {"yes_won": False, "revenue_cents": 70}}, {})
        # P(YES) frame: NO won means YES lost → actual=0.0, implied=1.0-0.30=0.70
        assert trade["realized_edge"] == -0.70

    def test_realized_edge_no_side_lost(self):
        """NO-side realized edge in P(YES) frame when NO loses."""
        trade = {"ticker": "T", "action": "buy", "side": "no",
                 "price_cents": 30, "model_prob": 0.20}
        _annotate_trade(trade, {"T": {"yes_won": True, "revenue_cents": -30}}, {})
        # P(YES) frame: YES won → actual=1.0, implied=1.0-0.30=0.70
        assert trade["realized_edge"] == 0.30

    def test_fill_price_fallback_to_price_cents(self):
        """Uses price_cents when fill_price_cents is missing."""
        trade = {"ticker": "T", "action": "buy", "side": "yes",
                 "price_cents": 40, "model_prob": 0.60}
        _annotate_trade(trade, {"T": {"yes_won": True, "revenue_cents": 60}}, {})
        assert trade["realized_edge"] == 0.60  # 1.0 - 0.40

    def test_fill_price_fallback_to_default(self):
        """Uses 50 when both fill_price_cents and price_cents are missing."""
        trade = {"ticker": "T", "action": "buy", "side": "yes", "model_prob": 0.80}
        _annotate_trade(trade, {"T": {"yes_won": True, "revenue_cents": 50}}, {})
        assert trade["realized_edge"] == 0.50  # 1.0 - 0.50

    def test_fill_price_prefers_fill_price_cents(self):
        """fill_price_cents takes priority over price_cents."""
        trade = {"ticker": "T", "action": "buy", "side": "yes",
                 "price_cents": 40, "model_prob": 0.80}
        fills = {"order1": {"fill_price_cents": 35}}
        trade["order_id"] = "order1"
        _annotate_trade(trade, {"T": {"yes_won": True, "revenue_cents": 65}}, fills)
        assert trade["fill_price_cents"] == 35
        assert trade["realized_edge"] == 0.65  # 1.0 - 0.35
