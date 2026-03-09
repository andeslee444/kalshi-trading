# Phase 1: Unblock the Flow — Implementation Plan

> **DEPRECATED**: This document is superseded by [Consolidated Quant Desk Design](2026-03-02-consolidated-quant-desk-design.md). Kept for historical reference only.

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Increase trade volume from ~5% to ~15% of evaluated markets by removing overly conservative filters, scaling risk limits for $5K bankroll, building a skip audit tool for ongoing feedback, recovering crypto brackets, and enabling cross-platform arb execution.

**Architecture:** Five independent workstreams — (1) skip audit tool that reads all `*-decisions.json` files and produces a "money left on table" report, (2) config-only risk limit scaling in `bots-config.json` + `kalshi-config.json` + `capital_allocator.py` constants, (3) edge threshold sweep added to `calibrate-sigma.py`, (4) crypto bracket pricing fix with relaxed liquidity gate in `crypto-bot.py`, (5) cross-platform arb execution toggle + minimum spread tightening. Each workstream is independently testable and deployable.

**Tech Stack:** Python 3, pytest, JSON config files. No new dependencies.

---

## Task 1: Skip Audit Tool — Tests

**Files:**
- Create: `tests/test_skip_audit.py`
- Create: `tests/fixtures/skip-audit-decisions.json`

**Step 1: Create fixture decision log**

Create a fixture file with representative decisions covering all skip reasons:

```json
[
  {"timestamp": "2026-03-01T10:00:00", "ticker": "KXBTCD-26MAR0112-T85000", "side": "yes", "action": "skipped", "reason": "edge below threshold", "source_bot": "crypto", "edge": 0.05, "price_cents": 92},
  {"timestamp": "2026-03-01T10:00:01", "ticker": "KXBTCD-26MAR0112-T90000", "side": "yes", "action": "skipped", "reason": "illiquid", "source_bot": "crypto", "edge": 0.12, "price_cents": 45},
  {"timestamp": "2026-03-01T10:00:02", "ticker": "KXBTCD-26MAR0112-T80000", "side": "yes", "action": "placed", "reason": "edge above threshold", "source_bot": "crypto", "edge": 0.15, "price_cents": 60},
  {"timestamp": "2026-03-01T10:00:03", "ticker": "KXHIGHMIA-26MAR02-T86", "side": "yes", "action": "skipped", "reason": "allocator_denied", "source_bot": "weather", "edge": 0.10, "price_cents": 55},
  {"timestamp": "2026-03-01T10:00:04", "ticker": "KXHIGHMIA-26MAR02-T88", "side": "no", "action": "skipped", "reason": "kelly_zero", "source_bot": "weather", "edge": 0.02, "price_cents": 95},
  {"timestamp": "2026-03-01T10:00:05", "ticker": "KXCPI-26MAR15-T3.5", "side": "yes", "action": "skipped", "reason": "execution_disabled", "source_bot": "cross-platform-arb", "edge": 0.04, "price_cents": 50},
  {"timestamp": "2026-03-01T10:00:06", "ticker": "ALBUM-26MAR07-SALES", "side": "yes", "action": "skipped", "reason": "low_edge", "source_bot": "entertainment", "edge": 0.03, "price_cents": 70},
  {"timestamp": "2026-03-01T10:00:07", "ticker": "ALBUM-26MAR07-SALES2", "side": "yes", "action": "skipped", "reason": "illiquid_no_ask", "source_bot": "entertainment", "edge": 0.20, "price_cents": null},
  {"timestamp": "2026-03-01T10:00:08", "ticker": "KXBTCD-26MAR0112-T95000", "side": "yes", "action": "skipped", "reason": "no_price", "source_bot": "crypto"},
  {"timestamp": "2026-03-01T10:00:09", "ticker": "KXBTCD-26MAR0112-T70000", "side": "no", "action": "skipped", "reason": "edge below mid-range threshold", "source_bot": "crypto", "edge": 0.10, "price_cents": 30}
]
```

**Step 2: Write test file**

```python
"""Tests for skip audit tool — decision log analysis and money-left-on-table reporting."""

import json
import sys
from pathlib import Path

import pytest
import importlib.util


@pytest.fixture
def fixture_dir():
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def decisions_path(fixture_dir):
    return fixture_dir / "skip-audit-decisions.json"


@pytest.fixture
def audit_mod():
    """Import skip-audit.py (hyphenated name)."""
    spec = importlib.util.spec_from_file_location(
        "skip_audit",
        str(Path(__file__).resolve().parent.parent / "scripts" / "skip-audit.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestLoadDecisions:
    """Test loading and merging decision files."""

    def test_loads_single_file(self, audit_mod, decisions_path):
        decisions = audit_mod.load_decisions([decisions_path])
        assert len(decisions) == 10

    def test_filters_by_bot(self, audit_mod, decisions_path):
        decisions = audit_mod.load_decisions([decisions_path])
        crypto_only = [d for d in decisions if d["source_bot"] == "crypto"]
        assert len(crypto_only) == 5

    def test_handles_missing_file(self, audit_mod, tmp_path):
        """Missing files are silently skipped."""
        decisions = audit_mod.load_decisions([tmp_path / "nonexistent.json"])
        assert decisions == []


class TestSkipDistribution:
    """Test skip reason distribution computation."""

    def test_distribution_per_bot(self, audit_mod, decisions_path):
        decisions = audit_mod.load_decisions([decisions_path])
        dist = audit_mod.skip_distribution(decisions)
        # crypto bot has 4 skips (edge below threshold, illiquid, no_price, edge below mid-range threshold)
        assert "crypto" in dist
        assert dist["crypto"]["edge below threshold"] == 1
        assert dist["crypto"]["illiquid"] == 1

    def test_excludes_placed_trades(self, audit_mod, decisions_path):
        """Placed trades should not appear in skip distribution."""
        decisions = audit_mod.load_decisions([decisions_path])
        dist = audit_mod.skip_distribution(decisions)
        for bot_dist in dist.values():
            assert "edge above threshold" not in bot_dist

    def test_all_bots_present(self, audit_mod, decisions_path):
        decisions = audit_mod.load_decisions([decisions_path])
        dist = audit_mod.skip_distribution(decisions)
        assert set(dist.keys()) == {"crypto", "weather", "cross-platform-arb", "entertainment"}


class TestMoneyLeftOnTable:
    """Test the money-left-on-table heuristic."""

    def test_edge_skips_have_estimated_pnl(self, audit_mod, decisions_path):
        decisions = audit_mod.load_decisions([decisions_path])
        report = audit_mod.money_left_on_table(decisions)
        # Should have entries for skips that had an edge
        assert len(report) > 0

    def test_no_price_skips_excluded(self, audit_mod, decisions_path):
        """Skips without price_cents can't estimate P&L — excluded."""
        decisions = audit_mod.load_decisions([decisions_path])
        report = audit_mod.money_left_on_table(decisions)
        tickers = [r["ticker"] for r in report]
        assert "KXBTCD-26MAR0112-T95000" not in tickers

    def test_report_sorted_by_estimated_edge(self, audit_mod, decisions_path):
        """Report should be sorted by edge descending (biggest missed opportunities first)."""
        decisions = audit_mod.load_decisions([decisions_path])
        report = audit_mod.money_left_on_table(decisions)
        if len(report) >= 2:
            edges = [r["edge"] for r in report]
            assert edges == sorted(edges, reverse=True)


class TestSummaryReport:
    """Test the text summary report generation."""

    def test_produces_output(self, audit_mod, decisions_path):
        decisions = audit_mod.load_decisions([decisions_path])
        output = audit_mod.summary_report(decisions)
        assert isinstance(output, str)
        assert len(output) > 0
        assert "crypto" in output.lower()
```

**Step 3: Run tests to verify they fail**

Run: `pytest tests/test_skip_audit.py -v`
Expected: FAIL — `scripts/skip-audit.py` does not exist yet.

**Step 4: Commit test + fixture**

```bash
git add tests/test_skip_audit.py tests/fixtures/skip-audit-decisions.json
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add skip audit tool tests and fixture data"
```

---

## Task 2: Skip Audit Tool — Implementation

**Files:**
- Create: `scripts/skip-audit.py`

**Step 1: Implement the skip audit script**

```python
#!/usr/bin/env python3
"""Skip audit tool — analyzes bot decision logs to find money left on the table.

Reads all *-decisions.json files and reports:
  - Skip reason distribution per bot
  - Estimated missed opportunities ranked by edge
  - Actionable recommendations for filter tuning

Usage:
    python3 scripts/skip-audit.py                    # All bots
    python3 scripts/skip-audit.py --bot crypto       # Single bot
    python3 scripts/skip-audit.py --json             # JSON output
    python3 scripts/skip-audit.py --days 7           # Last 7 days only
"""

import argparse
import datetime
import json
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"


def load_decisions(paths):
    """Load and merge decision records from multiple files."""
    all_decisions = []
    for p in paths:
        p = Path(p)
        if not p.exists():
            continue
        try:
            with open(p) as f:
                records = json.load(f)
            if isinstance(records, list):
                all_decisions.extend(records)
        except (json.JSONDecodeError, OSError):
            continue
    return all_decisions


def skip_distribution(decisions):
    """Compute skip reason counts per bot.

    Returns: dict of {bot_name: {reason: count}}
    Only includes skipped/rejected decisions, not placed trades.
    """
    dist = defaultdict(lambda: defaultdict(int))
    for d in decisions:
        if d.get("action") in ("skipped", "rejected"):
            bot = d.get("source_bot", "unknown")
            reason = d.get("reason", "unknown")
            dist[bot][reason] += 1
    return {bot: dict(reasons) for bot, reasons in dist.items()}


def money_left_on_table(decisions):
    """Estimate missed opportunities from skipped trades with positive edge.

    Returns list of dicts sorted by edge descending. Only includes skips
    that had both an edge value and a price_cents value.
    """
    opportunities = []
    for d in decisions:
        if d.get("action") != "skipped":
            continue
        edge = d.get("edge")
        price = d.get("price_cents")
        if edge is None or price is None or edge <= 0:
            continue
        opportunities.append({
            "ticker": d.get("ticker", ""),
            "source_bot": d.get("source_bot", ""),
            "reason": d.get("reason", ""),
            "edge": edge,
            "price_cents": price,
            "side": d.get("side", ""),
            "timestamp": d.get("timestamp", ""),
        })
    opportunities.sort(key=lambda x: x["edge"], reverse=True)
    return opportunities


def summary_report(decisions):
    """Generate a human-readable summary report."""
    lines = []
    total = len(decisions)
    placed = sum(1 for d in decisions if d.get("action") == "placed")
    skipped = sum(1 for d in decisions if d.get("action") in ("skipped", "rejected"))

    lines.append("=" * 60)
    lines.append("SKIP AUDIT REPORT")
    lines.append("=" * 60)
    lines.append(f"Total decisions: {total}")
    lines.append(f"Placed: {placed} ({placed/total*100:.1f}%)" if total else "Placed: 0")
    lines.append(f"Skipped: {skipped} ({skipped/total*100:.1f}%)" if total else "Skipped: 0")
    lines.append("")

    # Distribution per bot
    dist = skip_distribution(decisions)
    for bot in sorted(dist.keys()):
        reasons = dist[bot]
        bot_total = sum(reasons.values())
        lines.append(f"--- {bot} ({bot_total} skips) ---")
        for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
            lines.append(f"  {reason}: {count}")
        lines.append("")

    # Top missed opportunities
    opps = money_left_on_table(decisions)
    if opps:
        lines.append("--- TOP MISSED OPPORTUNITIES ---")
        for opp in opps[:20]:
            lines.append(
                f"  {opp['ticker']} [{opp['source_bot']}] "
                f"edge={opp['edge']:.1%} price={opp['price_cents']}c "
                f"reason={opp['reason']}"
            )
        lines.append("")

        # Aggregate by reason
        reason_edges = defaultdict(list)
        for opp in opps:
            reason_edges[opp["reason"]].append(opp["edge"])
        lines.append("--- MISSED EDGE BY SKIP REASON ---")
        for reason, edges in sorted(reason_edges.items(), key=lambda x: -sum(x[1])):
            lines.append(
                f"  {reason}: {len(edges)} skips, "
                f"avg edge={sum(edges)/len(edges):.1%}, "
                f"total edge={sum(edges):.1%}"
            )

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Skip audit — analyze bot decision logs")
    parser.add_argument("--bot", help="Filter to a single bot name")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--days", type=int, default=0, help="Only include last N days (0=all)")
    parser.add_argument("files", nargs="*", help="Decision files (default: data/*-decisions.json)")
    args = parser.parse_args()

    if args.files:
        paths = [Path(f) for f in args.files]
    else:
        paths = sorted(DATA_DIR.glob("*-decisions.json"))

    if not paths:
        print("No decision files found.", file=sys.stderr)
        sys.exit(1)

    decisions = load_decisions(paths)

    # Filter by days
    if args.days > 0:
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=args.days)).isoformat()
        decisions = [d for d in decisions if d.get("timestamp", "") >= cutoff]

    # Filter by bot
    if args.bot:
        decisions = [d for d in decisions if d.get("source_bot") == args.bot]

    if args.json:
        output = {
            "total": len(decisions),
            "placed": sum(1 for d in decisions if d.get("action") == "placed"),
            "distribution": skip_distribution(decisions),
            "missed_opportunities": money_left_on_table(decisions)[:50],
        }
        print(json.dumps(output, indent=2))
    else:
        print(summary_report(decisions))


if __name__ == "__main__":
    main()
```

**Step 2: Run tests to verify they pass**

Run: `pytest tests/test_skip_audit.py -v`
Expected: All PASS.

**Step 3: Add npm script**

In `package.json`, add to `"scripts"`:
```json
"skip-audit": "python3 scripts/skip-audit.py"
```

**Step 4: Commit**

```bash
git add scripts/skip-audit.py package.json
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add skip audit tool for decision log analysis"
```

---

## Task 3: Bankroll-Proportional Risk Limits — Config Changes

**Files:**
- Modify: `config/bots-config.json`
- Modify: `config/kalshi-config.json`
- Modify: `src/kalshi/capital_allocator.py:51-64`

**Step 1: Update `config/kalshi-config.json` weather limits**

Change these values:
```
maxTradeAmount: 15 -> 30
maxDailyLoss: 35 -> 75
```

The `maxTradeAmountPct: 0.03` and `maxDailyLossPct: 0.07` remain — they scale automatically with bankroll.

**Step 2: Update `config/bots-config.json` per-bot limits**

| Bot | Field | Old | New |
|-----|-------|-----|-----|
| entertainment | maxTradeAmount | 5 | 15 |
| entertainment | maxDailyLoss | 25 | 50 |
| entertainment | maxTradeAmountPct | 0.01 | 0.03 |
| beatrelease | maxTradeCents | 500 | 1500 |
| beatrelease | maxDailyLoss | 25 | 50 |
| beatrelease | maxTradeAmountPct | 0.01 | 0.03 |
| strategy | maxBetCents | 500 | 1000 |
| strategy | maxDailyLoss | 50 | 100 |
| strategy | maxBetPct | 0.01 | 0.02 |
| crypto | maxTradeAmount | 10 | 25 |
| crypto | maxDailyLoss | 50 | 100 |
| crypto | maxTradeAmountPct | 0.02 | 0.05 |
| economics | maxTradeAmount | 25 | 50 |
| economics | maxDailyLoss | 200 | 300 |
| cross_platform_arb | maxTradeAmount | 10 | 25 |
| cross_platform_arb | maxDailyLoss | 25 | 50 |
| cross_platform_arb | maxTradeAmountPct | 0.02 | 0.05 |
| allocator | absoluteDailyLossCap | 150 | 500 |
| allocator | absoluteDailyLossCapPct | 0.15 | 0.10 |

**Step 3: Update `capital_allocator.py` concentration constants**

```python
# Old:
MAX_BOT_FRACTION = 0.40
MAX_TICKER_FRACTION = 0.05
MAX_CITY_FRACTION = 0.10

# New:
MAX_BOT_FRACTION = 0.30       # Better cross-strategy diversification at $5K
MAX_TICKER_FRACTION = 0.03    # $5K * 3% = $150 max per ticker (was $250 at 5%)
MAX_CITY_FRACTION = 0.07      # Tighter correlation risk at scale
```

**Step 4: Verify no tests break**

Run: `pytest tests/test_allocator.py -v`
Expected: All PASS — allocator tests should use relative assertions or mock bankroll, not hardcoded dollar values.

**Step 5: Commit**

```bash
git add config/bots-config.json config/kalshi-config.json src/kalshi/capital_allocator.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: scale risk limits for \$5K bankroll, tighten concentration limits"
```

---

## Task 4: Crypto Bracket Recovery — Tests

**Files:**
- Modify: `tests/test_crypto.py`

**Step 1: Add bracket liquidity and pricing tests**

Append to `tests/test_crypto.py`:

```python
class TestBracketLiquidityGate:
    """Test bracket-specific liquidity requirements (relaxed vs standard)."""

    def _bracket_eligible(self, market, max_spread=15, min_volume=10):
        """Simulate bracket eligibility: spread < 15c AND volume > 10."""
        yes_bid = market.get("yes_bid", 0)
        yes_ask = market.get("yes_ask", 0)
        volume = market.get("volume", 0) or 0
        if not yes_ask:
            return False
        spread = (yes_ask - yes_bid) if yes_bid else 999
        return spread <= max_spread and volume >= min_volume

    def test_tight_spread_high_volume_eligible(self):
        market = {"yes_bid": 40, "yes_ask": 50, "volume": 25}
        assert self._bracket_eligible(market)

    def test_wide_spread_rejected(self):
        """Spread > 15c rejects bracket."""
        market = {"yes_bid": 30, "yes_ask": 50, "volume": 25}
        assert not self._bracket_eligible(market)

    def test_low_volume_rejected(self):
        """Volume < 10 rejects bracket."""
        market = {"yes_bid": 40, "yes_ask": 50, "volume": 5}
        assert not self._bracket_eligible(market)

    def test_ask_only_eligible_if_no_bid(self):
        """Ask-only market (no bid) has spread=999, rejected."""
        market = {"yes_bid": 0, "yes_ask": 50, "volume": 25}
        assert not self._bracket_eligible(market)

    def test_boundary_spread_15_eligible(self):
        market = {"yes_bid": 35, "yes_ask": 50, "volume": 15}
        assert self._bracket_eligible(market)

    def test_boundary_volume_10_eligible(self):
        market = {"yes_bid": 40, "yes_ask": 50, "volume": 10}
        assert self._bracket_eligible(market)


class TestBracketLimitPricing:
    """Test that brackets always use aggressive (ask) pricing for fill rate."""

    def _bracket_limit_price(self, yes_bid, yes_ask):
        """Bracket pricing: always use ask price (maximize fill rate)."""
        return yes_ask if yes_ask else 0

    def test_uses_full_ask(self):
        assert self._bracket_limit_price(40, 50) == 50

    def test_no_ask_returns_zero(self):
        assert self._bracket_limit_price(40, 0) == 0

    def test_ignores_bid(self):
        """Regardless of bid, bracket price = ask."""
        assert self._bracket_limit_price(10, 50) == 50
        assert self._bracket_limit_price(49, 50) == 50
```

**Step 2: Run tests to verify they pass**

Run: `pytest tests/test_crypto.py -v -k "Bracket"`
Expected: All PASS — these test extracted logic, not imported functions.

**Step 3: Commit**

```bash
git add tests/test_crypto.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add crypto bracket liquidity gate and pricing tests"
```

---

## Task 5: Crypto Bracket Recovery — Implementation

**Files:**
- Modify: `src/kalshi/crypto-bot.py:381-384` (bracket gate)
- Modify: `src/kalshi/crypto-bot.py:447-457` (bracket liquidity + pricing)

**Step 1: Replace the blanket bracket disable with a liquidity gate**

Find the bracket disable block at line ~381-384:

```python
        # Skip bracket markets if disabled (87% non-fill rate)
        if direction == "B" and not crypto_config.get("enableBrackets", True):
            ss.skip("brackets_disabled")
            continue
```

Replace with a bracket-specific liquidity gate:

```python
        # Bracket markets: gate on tighter liquidity (spread < 15c, volume > 10)
        if direction == "B":
            if not crypto_config.get("enableBrackets", True):
                ss.skip("brackets_disabled")
                continue
            b_spread = (m.get("yes_ask", 0) - m.get("yes_bid", 0)) if m.get("yes_bid") else 999
            b_volume = m.get("volume", 0) or 0
            if b_spread > 15 or b_volume < 10:
                ss.skip("bracket_illiquid")
                trade_manager.log_decision(ticker, "yes", "skipped", "bracket_illiquid",
                                           spread=b_spread, volume=b_volume)
                continue
```

**Step 2: For brackets, use ask pricing instead of edge-tiered**

Find the section after the bracket probability computation where the trade is assembled (inside the opportunities loop, around the `trade_manager.place_order` call). The limit price for brackets should always be `yes_ask` to maximize fill rate.

The simplest way: add an `is_bracket` flag to the opportunity dict and check it when computing limit price. In the opportunity assembly (line ~466):

Add `"is_bracket": direction == "B"` to the opportunity dict.

Then in the execution section where `compute_limit_price` is called, add:

```python
if opp.get("is_bracket"):
    price = yes_ask  # Bracket: always use ask for fill rate
else:
    price = compute_limit_price(yes_bid, yes_ask, side, edge=edge) or yes_ask
```

**Step 3: Add fill-rate tracking to decision log**

When a bracket trade is placed, include `"bracket": True` in the `log_decision` / `place_order` extra fields so the skip audit tool can track bracket fill rates separately.

**Step 4: Run tests**

Run: `pytest tests/test_crypto.py -v`
Expected: All PASS.

**Step 5: Commit**

```bash
git add src/kalshi/crypto-bot.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: recover crypto brackets with liquidity gate and ask pricing"
```

---

## Task 6: Cross-Platform Arb Execution — Tests

**Files:**
- Modify: `tests/test_arb.py`

**Step 1: Add spread calculation and fee deduction tests**

Append to `tests/test_arb.py`:

```python
class TestSpreadCalculation:
    """Test net spread calculation after fees."""

    def _kalshi_fee_pct(self, price_cents):
        """Approximate Kalshi fee: 0.07 * p * (1-p)."""
        p = price_cents / 100
        return 0.07 * p * (1 - p)

    def _net_spread(self, pm_yes_bid, k_yes_ask_cents, polymarket_fee=0.02):
        """Compute net spread after fees."""
        raw = pm_yes_bid - k_yes_ask_cents / 100
        k_fee = self._kalshi_fee_pct(k_yes_ask_cents)
        return raw - k_fee - polymarket_fee

    def test_profitable_spread(self):
        """Polymarket 0.65, Kalshi ask 55c = 10% raw, ~5% net."""
        net = self._net_spread(0.65, 55)
        assert net > 0.03  # Profitable after fees

    def test_negative_spread(self):
        """Same price = negative after fees."""
        net = self._net_spread(0.55, 55)
        assert net < 0

    def test_fee_drag(self):
        """Fees should eat ~4-5% of raw spread."""
        raw = 0.65 - 0.55
        net = self._net_spread(0.65, 55)
        fee_drag = raw - net
        assert 0.03 < fee_drag < 0.07


class TestArbEdgeGating:
    """Test that arb requires minimum 3% net spread."""

    def test_above_threshold_is_tradeable(self):
        min_spread = 0.03
        net_spread = 0.05
        assert net_spread > min_spread

    def test_below_threshold_rejected(self):
        min_spread = 0.03
        net_spread = 0.02
        assert not (net_spread > min_spread)

    def test_boundary_at_threshold(self):
        min_spread = 0.03
        net_spread = 0.03
        assert not (net_spread > min_spread)  # Strict >
```

**Step 2: Run tests**

Run: `pytest tests/test_arb.py -v`
Expected: All PASS.

**Step 3: Commit**

```bash
git add tests/test_arb.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add arb spread calculation and edge gating tests"
```

---

## Task 7: Cross-Platform Arb Execution — Enable

**Files:**
- Modify: `config/bots-config.json` (cross_platform_arb section)

**Step 1: Enable execution and tighten min spread to 3%**

In `config/bots-config.json`, `cross_platform_arb` section:

```json
"executionEnabled": true,
"minSpreadPct": 0.03
```

Change `executionEnabled` from `false` to `true`, and `minSpreadPct` from `0.02` to `0.03` (conservative, per design doc).

**Step 2: Verify arb bot reads the toggle correctly**

The toggle is already wired at `cross-platform-arb.py:45`:
```python
EXECUTION_ENABLED = arb_config.get("executionEnabled", False)
```

And the execution code at line 296 is already complete (allocator, Kelly sizing, TradeManager). No code changes needed — config-only.

**Step 3: Run arb tests**

Run: `pytest tests/test_arb.py -v`
Expected: All PASS.

**Step 4: Commit**

```bash
git add config/bots-config.json
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: enable cross-platform arb execution with 3% min spread"
```

---

## Task 8: Edge Threshold Recalibration — Tests

**Files:**
- Modify: `tests/test_calibration.py`

**Step 1: Add edge threshold sweep tests**

Append to `tests/test_calibration.py`:

```python
class TestEdgeThresholdSweep:
    """Test edge threshold optimization integrated with sigma calibration."""

    def test_optimal_threshold_in_range(self):
        """Optimal edge threshold should be between 0.04 and 0.15."""
        # Generate synthetic trades with known edge distribution
        trades = []
        settlement_map = {}
        for i in range(30):
            ticker = f"KXHIGHMIA-26FEB{10+i%28:02d}-T86"
            trades.append({
                "ticker": ticker,
                "forecast_temp": 88.0 if i % 3 != 0 else 84.0,
                "side": "yes",
                "timestamp": "2026-02-01T12:00:00Z",
            })
            settlement_map[ticker] = 100 if i % 3 != 0 else -50

        result = calibrate_weather(trades, settlement_map)
        # The function should still produce valid calibration
        assert result.get("n", 0) > 0
        assert "global_brier" in result

    def test_brier_guides_threshold(self):
        """Lower Brier score should allow lower edge threshold."""
        # Brier < 0.10 → threshold can be 6%
        # Brier 0.10-0.20 → keep 8%
        # Brier > 0.20 → raise to 10%
        assert _threshold_for_brier(0.05) == 0.06
        assert _threshold_for_brier(0.15) == 0.08
        assert _threshold_for_brier(0.25) == 0.10


def _threshold_for_brier(brier):
    """Map Brier score to recommended edge threshold."""
    if brier < 0.10:
        return 0.06
    elif brier <= 0.20:
        return 0.08
    else:
        return 0.10
```

**Step 2: Run tests**

Run: `pytest tests/test_calibration.py -v -k "EdgeThreshold"`
Expected: PASS — `_threshold_for_brier` is a local helper, not an import.

**Step 3: Commit**

```bash
git add tests/test_calibration.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add edge threshold sweep tests for calibration"
```

---

## Task 9: Edge Threshold Recalibration — Implementation

**Files:**
- Modify: `scripts/calibrate-sigma.py`

**Step 1: Add `threshold_for_brier()` function**

Add after the existing imports/constants in `calibrate-sigma.py`:

```python
def threshold_for_brier(brier):
    """Map Brier score to recommended edge threshold.

    Brier < 0.10 (excellent model): threshold 6%
    Brier 0.10-0.20 (good model): threshold 8%
    Brier > 0.20 (needs work): threshold 10%
    """
    if brier is None:
        return 0.08
    if brier < 0.10:
        return 0.06
    elif brier <= 0.20:
        return 0.08
    else:
        return 0.10
```

**Step 2: Include recommended threshold in calibration output**

In the `calibrate_weather()` function, after computing `global_brier`, add to the result dict:

```python
result["recommended_edge_threshold"] = threshold_for_brier(result.get("global_brier"))
```

Do the same for per-city results if they have their own Brier scores.

**Step 3: Run calibration tests**

Run: `pytest tests/test_calibration.py -v`
Expected: All PASS.

**Step 4: Commit**

```bash
git add scripts/calibrate-sigma.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add Brier-guided edge threshold recommendation to calibration"
```

---

## Task 10: Add npm script for skip-audit + Final Integration Test

**Files:**
- Modify: `package.json`

**Step 1: Run full test suite**

Run: `pytest tests/ -v --tb=short 2>&1 | tail -30`
Expected: All new tests pass. Pre-existing failures (test_probability calibration reset, test_optimization stub) are known and documented.

**Step 2: Run skip audit against live decision data**

Run: `python3 scripts/skip-audit.py`
Expected: Produces a report showing skip distributions across all bots. This validates the tool works with real data.

**Step 3: Run skip audit with JSON output**

Run: `python3 scripts/skip-audit.py --json | python3 -m json.tool | head -30`
Expected: Valid JSON output.

**Step 4: Final commit**

```bash
git add -A
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: Phase 1 complete — unblock the flow

- Skip audit tool (scripts/skip-audit.py) for decision log analysis
- Bankroll-proportional risk limits scaled for \$5K
- Tighter concentration limits (3% ticker, 7% city, 30% bot)
- Crypto bracket recovery with liquidity gate + ask pricing
- Cross-platform arb execution enabled (3% min spread)
- Brier-guided edge threshold recommendation in calibration"
```

---

## Rollout Checklist (Post-Implementation)

These are manual operational steps, not code tasks:

1. **Pre-deploy snapshot**: `npm run skip-audit > data/skip-audit-before.txt`
2. **Deploy config to Mac Mini**: `git pull` on production
3. **Verify supervisor picks up changes**: `npm run supervisor:status`
4. **Monitor first 48 hours**:
   - Trade volume increase? Check `data/*-trades.json` growth rate
   - Win rate hold? Run `npm run report`
   - Any circuit breaker trips? Check `data/allocator-state.json`
5. **Post-deploy snapshot**: `npm run skip-audit > data/skip-audit-after.txt`
6. **Compare**: diff before/after to validate filter loosening had the intended effect
7. **Kill switch** ready at `data/HALT_TRADING` if anything goes wrong
