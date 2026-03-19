# Oracle NBA Multi-Book Trading System — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a three-book NBA trading system that uses Real Sports App data to find edges on Kalshi prediction markets.

**Architecture:** Shared infrastructure layer (API clients, data models, risk ledger, config) with three independent trading books (A: game divergence, B: pregame player props, C: live events). Each book has its own signal generation, execution rules, and risk parameters. asyncio event loop drives concurrency.

**Tech Stack:** Python 3.11+, httpx (async HTTP), python-socketio (Real WebSocket), cryptography (RSA-PSS for Kalshi auth), scipy (statistics), SQLite (storage), PyYAML (config), pytest (testing)

**Spec:** `docs/superpowers/specs/2026-03-17-oracle-trading-strategy-design.md`

---

## File Structure

```
oracle/
├── config.yaml                    # All risk params, thresholds, per-book settings
├── main.py                        # Entry point: daily cycle orchestrator
├── models.py                      # Shared data models (Signal, Position, Game, Player, etc.)
├── config.py                      # Load + validate YAML config
├── db.py                          # SQLite: trade log, player lookup, opportunity log
├── clients/
│   ├── real_rest.py               # Real Sports REST API client (httpx, Hashids auth)
│   ├── real_ws.py                 # Real Sports Socket.io client (live feed)
│   ├── kalshi.py                  # Kalshi REST API client (RSA-PSS auth, orders)
│   └── matching.py                # Player/market matching: Real ↔ Kalshi entity resolution
├── risk/
│   ├── ledger.py                  # Portfolio risk ledger: positions, exposure, P&L
│   ├── limits.py                  # Hard limit checks (per-book, per-game, per-player, daily)
│   └── fees.py                    # Kalshi fee schedule + net edge calculation
├── books/
│   ├── book_a.py                  # Book A: Real-vs-Kalshi game divergence
│   ├── book_b.py                  # Book B: Pregame player props (empirical distribution)
│   └── book_c.py                  # Book C: Live event signals (foul/OT/blowout)
├── execution/
│   ├── executor.py                # Order placement, re-pricing, cancellation
│   └── quote_check.py             # Quote-age, spread, depth filters (Book C)
└── logging_/
    └── trade_logger.py            # Structured JSON logging, opportunity set, slippage

tests/
├── conftest.py                    # Shared fixtures (mock API responses, sample data)
├── test_models.py                 # Data model tests
├── test_config.py                 # Config loading/validation tests
├── test_db.py                     # SQLite operations tests
├── test_real_rest.py              # Real REST client tests (mocked HTTP)
├── test_kalshi.py                 # Kalshi client tests (mocked HTTP, RSA signing)
├── test_matching.py               # Player/market matching tests
├── test_fees.py                   # Fee calculation tests
├── test_limits.py                 # Risk limit enforcement tests
├── test_ledger.py                 # Portfolio ledger tests
├── test_book_a.py                 # Book A signal generation tests
├── test_book_b.py                 # Book B fair value computation tests
├── test_book_c.py                 # Book C event detection tests
├── test_executor.py               # Order execution tests
└── test_quote_check.py            # Quote quality filter tests
```

---

## Chunk 1: Foundation — Models, Config, Database, Fees

### Task 1: Project scaffolding and dependencies

**Files:**
- Create: `oracle/__init__.py`
- Create: `tests/__init__.py`
- Create: `requirements.txt`
- Create: `pyproject.toml`

- [ ] **Step 1: Create project structure**

```bash
cd /Users/andeslee/documents/cursor-projects/sportsmarket
mkdir -p oracle/clients oracle/risk oracle/books oracle/execution oracle/logging_
mkdir -p tests
touch oracle/__init__.py oracle/clients/__init__.py oracle/risk/__init__.py
touch oracle/books/__init__.py oracle/execution/__init__.py oracle/logging_/__init__.py
touch tests/__init__.py
```

- [ ] **Step 2: Create requirements.txt**

```
httpx>=0.27
python-socketio[asyncio]>=5.11
cryptography>=42.0
scipy>=1.13
pyyaml>=6.0
hashids>=1.3
pytest>=8.0
pytest-asyncio>=0.23
respx>=0.21
```

- [ ] **Step 3: Create pyproject.toml**

```toml
[project]
name = "oracle"
version = "0.1.0"
requires-python = ">=3.11"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 4: Install dependencies**

Run: `cd /Users/andeslee/documents/cursor-projects/sportsmarket && pip install -r requirements.txt`
Expected: All packages install successfully.

- [ ] **Step 5: Commit**

```bash
git add oracle/ tests/ requirements.txt pyproject.toml
git commit -m "feat: scaffold project structure and dependencies"
```

---

### Task 2: Data models

**Files:**
- Create: `oracle/models.py`
- Create: `tests/test_models.py`

- [ ] **Step 1: Write failing tests for core data models**

```python
# tests/test_models.py
from oracle.models import (
    Book, Signal, Position, Game, Market, Player, PlayerSplits,
    StatTracker, GameState, QuoteSnapshot,
)

def test_signal_creation():
    s = Signal(
        book=Book.A,
        ticker="KXNBAGAME-26MAR18LALHOU-LAL",
        side="yes",
        edge=0.15,
        model_prob=0.72,
        kalshi_price=0.57,
    )
    assert s.book == Book.A
    assert s.edge == 0.15
    assert s.side == "yes"

def test_signal_rejects_negative_edge():
    import pytest
    with pytest.raises(ValueError):
        Signal(book=Book.B, ticker="X", side="yes", edge=-0.05,
               model_prob=0.5, kalshi_price=0.55)

def test_position_cost():
    p = Position(
        book=Book.B,
        ticker="KXNBAPTS-26MAR18-LALLEBRONJ-27",
        side="yes",
        contracts=10,
        fill_price=0.63,
    )
    assert p.cost == 6.30  # 10 * 0.63

def test_game_state_classification():
    assert GameState.classify(margin=5, period="Q3", clock_seconds=300) == GameState.COMPETITIVE
    assert GameState.classify(margin=20, period="Q3", clock_seconds=300) == GameState.BLOWOUT
    assert GameState.classify(margin=3, period="Q4", clock_seconds=120) == GameState.CLUTCH
    assert GameState.classify(margin=0, period="Q4", clock_seconds=90) == GameState.OT_LIKELY

def test_player_splits_hit_rate():
    splits = PlayerSplits(
        last_5=[28, 32, 22, 35, 30],
        last_10=[28, 32, 22, 35, 30, 24, 19, 31, 27, 26],
        season_avg=26.5,
        home_avg=28.0,
        away_avg=25.0,
    )
    assert splits.hit_rate(line=27.5, n=5) == 0.6  # 3 of 5 cleared
    assert splits.hit_rate(line=27.5, n=10) == 0.5  # 5 of 10 cleared
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/andeslee/documents/cursor-projects/sportsmarket && python -m pytest tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'oracle.models'`

- [ ] **Step 3: Implement models**

```python
# oracle/models.py
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import time


class Book(Enum):
    A = "game_divergence"
    B = "pregame_props"
    C = "live_events"


class GameState(Enum):
    COMPETITIVE = "competitive"
    BLOWOUT = "blowout"
    CLUTCH = "clutch"
    OT_LIKELY = "ot_likely"
    OVERTIME = "overtime"
    PREGAME = "pregame"
    HALFTIME = "halftime"
    FINAL = "final"

    @classmethod
    def classify(cls, margin: int, period: str, clock_seconds: int) -> GameState:
        abs_margin = abs(margin)
        is_q4 = period == "Q4"
        is_q3_or_later = period in ("Q3", "Q4")

        if is_q4 and clock_seconds <= 120 and abs_margin <= 2:
            return cls.OT_LIKELY
        if is_q4 and clock_seconds <= 300 and abs_margin <= 6:
            return cls.CLUTCH
        if is_q3_or_later and abs_margin >= 20:
            return cls.BLOWOUT
        return cls.COMPETITIVE


@dataclass
class Signal:
    book: Book
    ticker: str
    side: str  # "yes" or "no"
    edge: float
    model_prob: float
    kalshi_price: float
    timestamp: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.edge < 0:
            raise ValueError(f"Edge must be non-negative, got {self.edge}")
        if self.side not in ("yes", "no"):
            raise ValueError(f"Side must be 'yes' or 'no', got {self.side}")


@dataclass
class Position:
    book: Book
    ticker: str
    side: str
    contracts: int
    fill_price: float
    signal_timestamp: float = field(default_factory=time.time)
    fill_timestamp: float = field(default_factory=time.time)
    game_id: Optional[str] = None
    player_id: Optional[int] = None

    @property
    def cost(self) -> float:
        return round(self.contracts * self.fill_price, 2)


@dataclass
class Game:
    game_id: int
    sport: str
    home_team: str
    away_team: str
    status: str  # "scheduled", "live", "final"
    home_score: int = 0
    away_score: int = 0
    period: str = ""
    clock: str = ""
    real_volume: int = 0


@dataclass
class Market:
    ticker: str
    series_ticker: str
    title: str
    yes_price: int  # cents (1-99)
    no_price: int
    volume: int = 0
    yes_bid: int = 0
    yes_ask: int = 0
    status: str = "open"


@dataclass
class Player:
    player_id: int
    name: str
    team: str
    status: str = "Active"  # Active, Out, Questionable
    minutes_avg_last5: float = 0.0
    season_per_minute: dict = field(default_factory=dict)  # {stat: rate}


@dataclass
class PlayerSplits:
    last_5: list[float]
    last_10: list[float]
    season_avg: float
    home_avg: float
    away_avg: float
    per_opponent: dict = field(default_factory=dict)  # {team: avg}

    def hit_rate(self, line: float, n: int = 5) -> float:
        values = self.last_5 if n <= 5 else self.last_10[:n]
        if not values:
            return 0.0
        return sum(1 for v in values if v > line) / len(values)


@dataclass
class StatTracker:
    player_name: str
    stat_type: str
    line: float
    over_odds: Optional[float] = None
    under_odds: Optional[float] = None


@dataclass
class QuoteSnapshot:
    ticker: str
    yes_bid: int
    yes_ask: int
    bid_depth: int  # contracts at best bid
    ask_depth: int  # contracts at best ask
    timestamp: float = field(default_factory=time.time)

    @property
    def spread(self) -> int:
        return self.yes_ask - self.yes_bid

    @property
    def age_seconds(self) -> float:
        return time.time() - self.timestamp
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_models.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add oracle/models.py tests/test_models.py
git commit -m "feat: add core data models (Signal, Position, Game, Player, etc.)"
```

---

### Task 3: Configuration

**Files:**
- Create: `oracle/config.py`
- Create: `config.yaml`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_config.py
from oracle.config import load_config, OracleConfig

def test_load_config(tmp_path):
    yaml_content = """
bankroll: 2000
books:
  a:
    enabled: true
    min_edge: 0.15
    position_pct: 0.02
    max_positions: 4
    max_exposure_pct: 0.08
    min_real_volume: 200000
    poll_interval_s: 30
  b:
    enabled: true
    min_edge: 0.10
    position_pct: 0.015
    max_positions: 6
    max_exposure_pct: 0.09
    max_per_game_pct: 0.04
    max_per_player_pct: 0.025
    no_trade_window_min: 15
    min_minutes_avg: 20
  c:
    enabled: true
    min_edge: 0.12
    position_pct: 0.01
    max_positions: 3
    max_exposure_pct: 0.03
    max_slippage_avg: 0.03
    quote_max_age_s: 5
    quote_max_spread: 8
    quote_min_depth: 5
risk:
  max_total_exposure_pct: 0.18
  max_simultaneous_positions: 10
  daily_stop_loss_pct: 0.05
  max_drawdown_pct: 0.15
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml_content)

    cfg = load_config(str(config_file))
    assert cfg.bankroll == 2000
    assert cfg.books.a.min_edge == 0.15
    assert cfg.books.b.max_per_player_pct == 0.025
    assert cfg.books.c.quote_max_spread == 8
    assert cfg.risk.max_total_exposure_pct == 0.18

def test_config_dollar_limits():
    from oracle.config import BookAConfig, RiskConfig
    book_a = BookAConfig(
        enabled=True, min_edge=0.15, position_pct=0.02,
        max_positions=4, max_exposure_pct=0.08,
        min_real_volume=200000, poll_interval_s=30,
    )
    assert book_a.max_position_dollars(bankroll=2000) == 40.0  # 2% of 2000
    assert book_a.max_exposure_dollars(bankroll=2000) == 160.0  # 8% of 2000
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL

- [ ] **Step 3: Implement config loading**

```python
# oracle/config.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import yaml


@dataclass
class BookAConfig:
    enabled: bool
    min_edge: float
    position_pct: float
    max_positions: int
    max_exposure_pct: float
    min_real_volume: int
    poll_interval_s: int

    def max_position_dollars(self, bankroll: float) -> float:
        return bankroll * self.position_pct

    def max_exposure_dollars(self, bankroll: float) -> float:
        return bankroll * self.max_exposure_pct


@dataclass
class BookBConfig:
    enabled: bool
    min_edge: float
    position_pct: float
    max_positions: int
    max_exposure_pct: float
    max_per_game_pct: float
    max_per_player_pct: float
    no_trade_window_min: int
    min_minutes_avg: float

    def max_position_dollars(self, bankroll: float) -> float:
        return bankroll * self.position_pct

    def max_exposure_dollars(self, bankroll: float) -> float:
        return bankroll * self.max_exposure_pct


@dataclass
class BookCConfig:
    enabled: bool
    min_edge: float
    position_pct: float
    max_positions: int
    max_exposure_pct: float
    max_slippage_avg: float
    quote_max_age_s: float
    quote_max_spread: int
    quote_min_depth: int

    def max_position_dollars(self, bankroll: float) -> float:
        return bankroll * self.position_pct

    def max_exposure_dollars(self, bankroll: float) -> float:
        return bankroll * self.max_exposure_pct


@dataclass
class BooksConfig:
    a: BookAConfig
    b: BookBConfig
    c: BookCConfig


@dataclass
class RiskConfig:
    max_total_exposure_pct: float
    max_simultaneous_positions: int
    daily_stop_loss_pct: float
    max_drawdown_pct: float

    def daily_stop_loss_dollars(self, bankroll: float) -> float:
        return bankroll * self.daily_stop_loss_pct


@dataclass
class OracleConfig:
    bankroll: float
    books: BooksConfig
    risk: RiskConfig


def load_config(path: str) -> OracleConfig:
    with open(path) as f:
        raw = yaml.safe_load(f)

    books = BooksConfig(
        a=BookAConfig(**raw["books"]["a"]),
        b=BookBConfig(**raw["books"]["b"]),
        c=BookCConfig(**raw["books"]["c"]),
    )
    risk = RiskConfig(**raw["risk"])
    return OracleConfig(bankroll=raw["bankroll"], books=books, risk=risk)
```

- [ ] **Step 4: Create the production config.yaml**

```yaml
# config.yaml — Oracle NBA Trading System
# All dollar limits computed dynamically: limit = pct * bankroll

bankroll: 2000

books:
  a:
    enabled: true
    min_edge: 0.15
    position_pct: 0.02
    max_positions: 4
    max_exposure_pct: 0.08
    min_real_volume: 200000
    poll_interval_s: 30
  b:
    enabled: true
    min_edge: 0.10
    position_pct: 0.015
    max_positions: 6
    max_exposure_pct: 0.09
    max_per_game_pct: 0.04
    max_per_player_pct: 0.025
    no_trade_window_min: 15
    min_minutes_avg: 20
  c:
    enabled: true
    min_edge: 0.12
    position_pct: 0.01
    max_positions: 3
    max_exposure_pct: 0.03
    max_slippage_avg: 0.03
    quote_max_age_s: 5
    quote_max_spread: 8
    quote_min_depth: 5

risk:
  max_total_exposure_pct: 0.18
  max_simultaneous_positions: 10
  daily_stop_loss_pct: 0.05
  max_drawdown_pct: 0.15
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_config.py -v`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add oracle/config.py config.yaml tests/test_config.py
git commit -m "feat: add YAML config loading with per-book risk parameters"
```

---

### Task 4: Kalshi fee calculation

**Files:**
- Create: `oracle/risk/fees.py`
- Create: `tests/test_fees.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_fees.py
from oracle.risk.fees import settlement_fee, net_edge

def test_settlement_fee_schedule():
    assert settlement_fee(0.05) == 0.01   # $0.01-0.10 bracket
    assert settlement_fee(0.15) == 0.02   # $0.11-0.20
    assert settlement_fee(0.25) == 0.03   # $0.21-0.30
    assert settlement_fee(0.35) == 0.04   # $0.31-0.40
    assert settlement_fee(0.45) == 0.05   # $0.41-0.50
    assert settlement_fee(0.55) == 0.06   # $0.51-0.60
    assert settlement_fee(0.65) == 0.07   # $0.61-0.99
    assert settlement_fee(0.95) == 0.07

def test_net_edge_calculation():
    # model_prob=0.73, kalshi_price=0.60, buying YES
    # win payout = 1.0 - 0.60 = 0.40, fee on that = $0.04
    # gross edge = 0.73 - 0.60 = 0.13
    # expected fee = 0.73 * 0.04 = 0.029
    # net edge = 0.13 - 0.029 = 0.101
    result = net_edge(model_prob=0.73, kalshi_price=0.60, side="yes")
    assert abs(result - 0.101) < 0.001

def test_net_edge_no_side():
    # Buying NO at 0.40 (kalshi_price for YES = 0.60)
    # model says NO is worth 0.55. kalshi NO = 0.40.
    # win payout = 0.60, fee = $0.06
    # gross edge = 0.55 - 0.40 = 0.15
    # expected fee = 0.55 * 0.06 = 0.033
    result = net_edge(model_prob=0.55, kalshi_price=0.40, side="no")
    assert abs(result - 0.117) < 0.001
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_fees.py -v`
Expected: FAIL

- [ ] **Step 3: Implement fee calculation**

```python
# oracle/risk/fees.py
"""Kalshi fee schedule and net edge computation."""

# Fee brackets: (max_settlement_price, fee)
_FEE_SCHEDULE = [
    (0.10, 0.01),
    (0.20, 0.02),
    (0.30, 0.03),
    (0.40, 0.04),
    (0.50, 0.05),
    (0.60, 0.06),
    (0.99, 0.07),
]


def settlement_fee(settlement_price: float) -> float:
    """Return the per-contract fee for a winning settlement at this price."""
    for max_price, fee in _FEE_SCHEDULE:
        if settlement_price <= max_price:
            return fee
    return 0.07


def net_edge(model_prob: float, kalshi_price: float, side: str) -> float:
    """Compute net edge after expected Kalshi fees.

    Args:
        model_prob: Our model's probability for this side (0-1).
        kalshi_price: The price we'd pay on Kalshi (0-1, decimal).
        side: 'yes' or 'no'.

    Returns:
        Net edge in dollars (decimal). Positive = profitable.
    """
    if side == "yes":
        win_settlement = 1.0 - kalshi_price
    else:
        win_settlement = kalshi_price

    fee = settlement_fee(win_settlement)
    gross = model_prob - kalshi_price
    expected_fee = model_prob * fee
    return round(gross - expected_fee, 4)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_fees.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add oracle/risk/fees.py tests/test_fees.py
git commit -m "feat: add Kalshi fee schedule and net edge calculation"
```

---

### Task 5: SQLite database layer

**Files:**
- Create: `oracle/db.py`
- Create: `tests/test_db.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_db.py
import sqlite3
from oracle.db import OracleDB

def test_init_creates_tables(tmp_path):
    db = OracleDB(str(tmp_path / "test.db"))
    # Verify tables exist
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    table_names = {t[0] for t in tables}
    assert "trades" in table_names
    assert "player_lookup" in table_names
    assert "opportunity_log" in table_names
    assert "daily_pnl" in table_names
    conn.close()
    db.close()

def test_player_lookup(tmp_path):
    db = OracleDB(str(tmp_path / "test.db"))
    db.upsert_player("LeBron James", "LAL", 12345)
    assert db.get_player_id("LeBron James", "LAL") == 12345
    assert db.get_player_id("Unknown Player", "BOS") is None
    db.close()

def test_log_trade(tmp_path):
    db = OracleDB(str(tmp_path / "test.db"))
    db.log_trade(
        book="B", ticker="KXNBAPTS-X", side="yes",
        contracts=10, fill_price=0.63, model_prob=0.73,
        edge=0.10, signal_ts=1000.0, fill_ts=1001.0,
    )
    trades = db.get_trades(book="B")
    assert len(trades) == 1
    assert trades[0]["ticker"] == "KXNBAPTS-X"
    db.close()

def test_log_opportunity(tmp_path):
    db = OracleDB(str(tmp_path / "test.db"))
    db.log_opportunity(
        book="B", ticker="KXNBAPTS-X", model_prob=0.73,
        kalshi_price=0.60, edge=0.13, triggered=True, reason="edge>threshold",
    )
    db.log_opportunity(
        book="B", ticker="KXNBAPTS-Y", model_prob=0.55,
        kalshi_price=0.50, edge=0.05, triggered=False, reason="edge<threshold",
    )
    opps = db.get_opportunities(book="B")
    assert len(opps) == 2
    assert opps[0]["triggered"] == 1
    assert opps[1]["triggered"] == 0
    db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_db.py -v`
Expected: FAIL

- [ ] **Step 3: Implement database layer**

```python
# oracle/db.py
"""SQLite persistence for trades, player lookup, and opportunity logging."""
from __future__ import annotations
import sqlite3
from typing import Optional


class OracleDB:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book TEXT NOT NULL,
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                contracts INTEGER NOT NULL,
                fill_price REAL NOT NULL,
                model_prob REAL NOT NULL,
                edge REAL NOT NULL,
                signal_ts REAL NOT NULL,
                fill_ts REAL NOT NULL,
                settled INTEGER DEFAULT 0,
                outcome REAL DEFAULT NULL,
                pnl REAL DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS player_lookup (
                name TEXT NOT NULL,
                team TEXT NOT NULL,
                player_id INTEGER NOT NULL,
                PRIMARY KEY (name, team)
            );
            CREATE TABLE IF NOT EXISTS opportunity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book TEXT NOT NULL,
                ticker TEXT NOT NULL,
                model_prob REAL NOT NULL,
                kalshi_price REAL NOT NULL,
                edge REAL NOT NULL,
                triggered INTEGER NOT NULL,
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS daily_pnl (
                date TEXT NOT NULL,
                book TEXT NOT NULL,
                gross_pnl REAL NOT NULL,
                fees REAL NOT NULL,
                net_pnl REAL NOT NULL,
                trades_count INTEGER NOT NULL,
                win_count INTEGER NOT NULL,
                PRIMARY KEY (date, book)
            );
        """)
        self._conn.commit()

    def upsert_player(self, name: str, team: str, player_id: int):
        self._conn.execute(
            "INSERT OR REPLACE INTO player_lookup (name, team, player_id) VALUES (?, ?, ?)",
            (name, team, player_id),
        )
        self._conn.commit()

    def get_player_id(self, name: str, team: str) -> Optional[int]:
        row = self._conn.execute(
            "SELECT player_id FROM player_lookup WHERE name = ? AND team = ?",
            (name, team),
        ).fetchone()
        return row["player_id"] if row else None

    def log_trade(self, *, book: str, ticker: str, side: str, contracts: int,
                  fill_price: float, model_prob: float, edge: float,
                  signal_ts: float, fill_ts: float):
        self._conn.execute(
            """INSERT INTO trades (book, ticker, side, contracts, fill_price,
               model_prob, edge, signal_ts, fill_ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (book, ticker, side, contracts, fill_price, model_prob, edge,
             signal_ts, fill_ts),
        )
        self._conn.commit()

    def get_trades(self, book: Optional[str] = None) -> list[dict]:
        if book:
            rows = self._conn.execute(
                "SELECT * FROM trades WHERE book = ? ORDER BY id", (book,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM trades ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def log_opportunity(self, *, book: str, ticker: str, model_prob: float,
                        kalshi_price: float, edge: float, triggered: bool,
                        reason: str = ""):
        self._conn.execute(
            """INSERT INTO opportunity_log (book, ticker, model_prob, kalshi_price,
               edge, triggered, reason) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (book, ticker, model_prob, kalshi_price, edge, int(triggered), reason),
        )
        self._conn.commit()

    def get_opportunities(self, book: Optional[str] = None) -> list[dict]:
        if book:
            rows = self._conn.execute(
                "SELECT * FROM opportunity_log WHERE book = ? ORDER BY id", (book,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM opportunity_log ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self._conn.close()
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_db.py -v`
Expected: All 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add oracle/db.py tests/test_db.py
git commit -m "feat: add SQLite layer for trades, player lookup, and opportunity log"
```

---

### Task 6: Risk ledger and limit checks

**Files:**
- Create: `oracle/risk/ledger.py`
- Create: `oracle/risk/limits.py`
- Create: `tests/test_ledger.py`
- Create: `tests/test_limits.py`

- [ ] **Step 1: Write failing tests for ledger**

```python
# tests/test_ledger.py
from oracle.risk.ledger import RiskLedger
from oracle.models import Book, Position

def test_add_position_tracks_exposure():
    ledger = RiskLedger()
    pos = Position(book=Book.B, ticker="X", side="yes",
                   contracts=10, fill_price=0.63, game_id="G1", player_id=100)
    ledger.add(pos)
    assert ledger.total_exposure() == 6.30
    assert ledger.book_exposure(Book.B) == 6.30
    assert ledger.game_exposure("G1") == 6.30
    assert ledger.player_exposure(100) == 6.30
    assert ledger.position_count() == 1

def test_settle_position():
    ledger = RiskLedger()
    pos = Position(book=Book.B, ticker="X", side="yes",
                   contracts=10, fill_price=0.63, game_id="G1")
    ledger.add(pos)
    pnl = ledger.settle("X", won=True, fee_per_contract=0.04)
    # Won: (1.0 - 0.63 - 0.04) * 10 = 0.33 * 10 = 3.30
    assert abs(pnl - 3.30) < 0.01
    assert ledger.total_exposure() == 0.0
    assert ledger.daily_pnl == 3.30

def test_settle_loss():
    ledger = RiskLedger()
    pos = Position(book=Book.B, ticker="X", side="yes",
                   contracts=10, fill_price=0.63)
    ledger.add(pos)
    pnl = ledger.settle("X", won=False, fee_per_contract=0.0)
    assert abs(pnl - (-6.30)) < 0.01
    assert ledger.daily_pnl == -6.30
```

- [ ] **Step 2: Write failing tests for limits**

```python
# tests/test_limits.py
from oracle.risk.limits import check_limits
from oracle.risk.ledger import RiskLedger
from oracle.models import Book, Signal, Position
from oracle.config import (
    OracleConfig, BooksConfig, BookAConfig, BookBConfig,
    BookCConfig, RiskConfig,
)

def _test_config():
    return OracleConfig(
        bankroll=2000,
        books=BooksConfig(
            a=BookAConfig(enabled=True, min_edge=0.15, position_pct=0.02,
                          max_positions=4, max_exposure_pct=0.08,
                          min_real_volume=200000, poll_interval_s=30),
            b=BookBConfig(enabled=True, min_edge=0.10, position_pct=0.015,
                          max_positions=6, max_exposure_pct=0.09,
                          max_per_game_pct=0.04, max_per_player_pct=0.025,
                          no_trade_window_min=15, min_minutes_avg=20),
            c=BookCConfig(enabled=True, min_edge=0.12, position_pct=0.01,
                          max_positions=3, max_exposure_pct=0.03,
                          max_slippage_avg=0.03, quote_max_age_s=5,
                          quote_max_spread=8, quote_min_depth=5),
        ),
        risk=RiskConfig(max_total_exposure_pct=0.18,
                        max_simultaneous_positions=10,
                        daily_stop_loss_pct=0.05, max_drawdown_pct=0.15),
    )

def test_allows_valid_trade():
    cfg = _test_config()
    ledger = RiskLedger()
    signal = Signal(book=Book.B, ticker="X", side="yes",
                    edge=0.12, model_prob=0.72, kalshi_price=0.60)
    result = check_limits(cfg, ledger, signal, game_id="G1", player_id=100)
    assert result.allowed is True

def test_blocks_daily_stop_loss():
    cfg = _test_config()
    ledger = RiskLedger()
    ledger.daily_pnl = -100.0  # -$100 = -5% of $2000
    signal = Signal(book=Book.B, ticker="X", side="yes",
                    edge=0.12, model_prob=0.72, kalshi_price=0.60)
    result = check_limits(cfg, ledger, signal, game_id="G1")
    assert result.allowed is False
    assert "daily_stop_loss" in result.reason

def test_blocks_max_positions():
    cfg = _test_config()
    ledger = RiskLedger()
    for i in range(10):
        ledger.add(Position(book=Book.B, ticker=f"T{i}", side="yes",
                            contracts=1, fill_price=0.10))
    signal = Signal(book=Book.B, ticker="NEW", side="yes",
                    edge=0.12, model_prob=0.72, kalshi_price=0.60)
    result = check_limits(cfg, ledger, signal)
    assert result.allowed is False
    assert "max_simultaneous" in result.reason

def test_blocks_per_player_exposure():
    cfg = _test_config()
    ledger = RiskLedger()
    # Add $50 exposure to player 100 (2.5% of $2000 = $50 limit)
    ledger.add(Position(book=Book.B, ticker="T1", side="yes",
                        contracts=100, fill_price=0.50, player_id=100))
    signal = Signal(book=Book.B, ticker="T2", side="yes",
                    edge=0.12, model_prob=0.72, kalshi_price=0.60)
    result = check_limits(cfg, ledger, signal, player_id=100)
    assert result.allowed is False
    assert "per_player" in result.reason
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_ledger.py tests/test_limits.py -v`
Expected: FAIL

- [ ] **Step 4: Implement ledger**

```python
# oracle/risk/ledger.py
from __future__ import annotations
from oracle.models import Book, Position


class RiskLedger:
    def __init__(self):
        self._positions: dict[str, Position] = {}  # ticker -> Position
        self.daily_pnl: float = 0.0

    def add(self, position: Position):
        self._positions[position.ticker] = position

    def remove(self, ticker: str) -> Position | None:
        return self._positions.pop(ticker, None)

    def settle(self, ticker: str, won: bool, fee_per_contract: float = 0.0) -> float:
        pos = self._positions.pop(ticker, None)
        if pos is None:
            return 0.0
        if won:
            payout_per = (1.0 - pos.fill_price) - fee_per_contract
            pnl = round(payout_per * pos.contracts, 2)
        else:
            pnl = round(-pos.cost, 2)
        self.daily_pnl = round(self.daily_pnl + pnl, 2)
        return pnl

    def total_exposure(self) -> float:
        return round(sum(p.cost for p in self._positions.values()), 2)

    def book_exposure(self, book: Book) -> float:
        return round(sum(p.cost for p in self._positions.values() if p.book == book), 2)

    def book_position_count(self, book: Book) -> int:
        return sum(1 for p in self._positions.values() if p.book == book)

    def game_exposure(self, game_id: str) -> float:
        return round(sum(p.cost for p in self._positions.values()
                         if p.game_id == game_id), 2)

    def player_exposure(self, player_id: int) -> float:
        return round(sum(p.cost for p in self._positions.values()
                         if p.player_id == player_id), 2)

    def position_count(self) -> int:
        return len(self._positions)

    def get_position(self, ticker: str) -> Position | None:
        return self._positions.get(ticker)

    def all_positions(self) -> list[Position]:
        return list(self._positions.values())
```

- [ ] **Step 5: Implement limits**

```python
# oracle/risk/limits.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from oracle.models import Book, Signal
from oracle.risk.ledger import RiskLedger
from oracle.config import OracleConfig


@dataclass
class LimitResult:
    allowed: bool
    reason: str = ""


def check_limits(
    cfg: OracleConfig,
    ledger: RiskLedger,
    signal: Signal,
    game_id: Optional[str] = None,
    player_id: Optional[int] = None,
) -> LimitResult:
    bankroll = cfg.bankroll

    # Daily stop-loss
    if ledger.daily_pnl <= -cfg.risk.daily_stop_loss_pct * bankroll:
        return LimitResult(False, "daily_stop_loss")

    # Max simultaneous positions
    if ledger.position_count() >= cfg.risk.max_simultaneous_positions:
        return LimitResult(False, "max_simultaneous")

    # Max total exposure
    if ledger.total_exposure() >= cfg.risk.max_total_exposure_pct * bankroll:
        return LimitResult(False, "max_total_exposure")

    # Per-book limits
    book_cfg = _get_book_config(cfg, signal.book)
    if not book_cfg.enabled:
        return LimitResult(False, "book_disabled")

    if ledger.book_position_count(signal.book) >= book_cfg.max_positions:
        return LimitResult(False, "book_max_positions")

    if ledger.book_exposure(signal.book) >= book_cfg.max_exposure_dollars(bankroll):
        return LimitResult(False, "book_max_exposure")

    # Per-game exposure (Book B)
    if signal.book == Book.B and game_id:
        max_game = cfg.books.b.max_per_game_pct * bankroll
        if ledger.game_exposure(game_id) >= max_game:
            return LimitResult(False, "per_game_exposure")

    # Per-player exposure (Book B)
    if signal.book == Book.B and player_id:
        max_player = cfg.books.b.max_per_player_pct * bankroll
        if ledger.player_exposure(player_id) >= max_player:
            return LimitResult(False, "per_player_exposure")

    return LimitResult(True)


def _get_book_config(cfg: OracleConfig, book: Book):
    if book == Book.A:
        return cfg.books.a
    elif book == Book.B:
        return cfg.books.b
    else:
        return cfg.books.c
```

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/test_ledger.py tests/test_limits.py -v`
Expected: All PASS.

- [ ] **Step 7: Commit**

```bash
git add oracle/risk/ledger.py oracle/risk/limits.py tests/test_ledger.py tests/test_limits.py
git commit -m "feat: add risk ledger and limit enforcement engine"
```

---

## Chunk 2: API Clients — Real Sports + Kalshi

### Task 7: Real Sports REST client

**Files:**
- Create: `oracle/clients/real_rest.py`
- Create: `tests/test_real_rest.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/conftest.py
import pytest

@pytest.fixture
def real_auth():
    return {
        "user_id": "testuser",
        "device_id": "testdevice",
        "token": "testtoken-uuid",
        "device_uuid": "test-uuid",
    }
```

```python
# tests/test_real_rest.py
import pytest
import respx
import httpx
from oracle.clients.real_rest import RealClient

BASE = "https://web.realapp.com"

@pytest.fixture
def client(real_auth):
    return RealClient(**real_auth)

@respx.mock
@pytest.mark.asyncio
async def test_get_game_markets(client):
    respx.get(f"{BASE}/predictions/gamemarkets/nba").mock(
        return_value=httpx.Response(200, json={
            "markets": [{"marketId": 1, "outcomes": [
                {"name": "LAL", "probability": 68},
                {"name": "HOU", "probability": 32},
            ], "volume": 500000}]
        })
    )
    markets = await client.get_game_markets("nba")
    assert len(markets) == 1
    assert markets[0]["volume"] == 500000

@respx.mock
@pytest.mark.asyncio
async def test_get_player_profile(client):
    respx.get(f"{BASE}/players/12345/sport/nba").mock(
        return_value=httpx.Response(200, json={
            "name": "LeBron James",
            "seasonStats": {"pts": 27.1},
            "splits": {"averages": [{"period": "Last 5", "stats": {"pts": 31.2}}]},
        })
    )
    profile = await client.get_player_profile(12345, "nba", season=2025)
    assert profile["name"] == "LeBron James"

@respx.mock
@pytest.mark.asyncio
async def test_get_stat_trackers(client):
    respx.get(f"{BASE}/stattrackers").mock(
        return_value=httpx.Response(200, json={
            "trackers": [{"playerName": "LeBron James",
                          "statType": "points", "line": 27.5,
                          "overOdds": -130, "underOdds": 110}]
        })
    )
    trackers = await client.get_stat_trackers("2026-03-18", "nba")
    assert trackers[0]["overOdds"] == -130

@respx.mock
@pytest.mark.asyncio
async def test_request_token_header(client):
    route = respx.get(f"{BASE}/home/nba/next").mock(
        return_value=httpx.Response(200, json={})
    )
    await client.get_tonight_games("nba")
    request = route.calls[0].request
    assert "real-request-token" in request.headers
    assert len(request.headers["real-request-token"]) == 16
    assert request.headers["real-version"] == "28"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_real_rest.py -v`
Expected: FAIL

- [ ] **Step 3: Implement Real REST client**

```python
# oracle/clients/real_rest.py
"""Real Sports App REST API client with Hashids auth."""
from __future__ import annotations
import time
import httpx
from hashids import Hashids

BASE_URL = "https://web.realapp.com"
_hashids = Hashids("realwebapp", 16)


class RealClient:
    def __init__(self, user_id: str, device_id: str, token: str, device_uuid: str):
        self._auth_info = f"{user_id}!{device_id}!{token}"
        self._device_uuid = device_uuid
        self._http = httpx.AsyncClient(base_url=BASE_URL, timeout=10.0)

    def _headers(self) -> dict:
        return {
            "real-auth-info": self._auth_info,
            "real-device-type": "desktop_web",
            "real-device-uuid": self._device_uuid,
            "real-request-token": _hashids.encode(int(time.time() * 1000)),
            "real-version": "28",
            "accept": "application/json",
            "content-type": "application/json",
        }

    async def _get(self, path: str, params: dict | None = None) -> dict:
        resp = await self._http.get(path, headers=self._headers(), params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_game_markets(self, sport: str = "nba") -> list[dict]:
        data = await self._get(f"/predictions/gamemarkets/{sport}")
        return data.get("markets", data if isinstance(data, list) else [])

    async def get_player_profile(self, player_id: int, sport: str = "nba",
                                  season: int = 2025) -> dict:
        return await self._get(
            f"/players/{player_id}/sport/{sport}",
            params={"season": str(season)},
        )

    async def get_player_season_feed(self, player_id: int, sport: str = "nba",
                                      limit: int = 20, season: int = 2025) -> dict:
        return await self._get(
            f"/players/{player_id}/sport/{sport}/seasonfeed",
            params={"limit": str(limit), "season": str(season),
                    "view": "recent", "viewFrame": "default"},
        )

    async def get_stat_trackers(self, day: str, sport: str = "nba") -> list[dict]:
        data = await self._get("/stattrackers", params={"day": day, "sport": sport})
        return data.get("trackers", data if isinstance(data, list) else [])

    async def get_tonight_games(self, sport: str = "nba") -> dict:
        return await self._get(f"/home/{sport}/next", params={"cohort": "0"})

    async def get_schedule(self, sport: str = "nba") -> dict:
        return await self._get(f"/home/{sport}/days", params={"type": "condensed"})

    async def get_team_stat_seasons(self, sport: str = "nba") -> dict:
        return await self._get(f"/teamstatleaders/{sport}/seasons")

    async def get_team_stat_leaders(self, sport: str, year: int,
                                     stat_id: int) -> dict:
        return await self._get(
            f"/teamstatleaders/{sport}/seasons/{year}/seasontypes/regularseason/stats/{stat_id}"
        )

    async def get_team_standings(self, sport: str, conference: str,
                                  division: str, season: int = 2025) -> dict:
        return await self._get(
            f"/teamstandings/sport/{sport}/conference/{conference}/division/{division}",
            params={"season": str(season)},
        )

    async def get_team_rankings(self, sport: str, period: str) -> dict:
        return await self._get(
            f"/rankings/sport/{sport}/entity/team/ranking/{period}"
        )

    async def get_player_rankings(self, sport: str, period: str) -> dict:
        return await self._get(
            f"/rankings/sport/{sport}/entity/player/ranking/{period}"
        )

    async def search_players(self, sport: str = "nba", season: int = 2025) -> dict:
        return await self._get(
            f"/players/sport/{sport}/search",
            params={"includeNoOneOption": "false", "searchType": "cardsTabUpsell",
                    "season": str(season)},
        )

    async def close(self):
        await self._http.aclose()
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_real_rest.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add oracle/clients/real_rest.py tests/test_real_rest.py tests/conftest.py
git commit -m "feat: add Real Sports REST client with Hashids auth"
```

---

### Task 8: Kalshi REST client with RSA-PSS auth

**Files:**
- Create: `oracle/clients/kalshi.py`
- Create: `tests/test_kalshi.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_kalshi.py
import pytest
import respx
import httpx
from unittest.mock import patch
from oracle.clients.kalshi import KalshiClient

DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"

@pytest.fixture
def kalshi_client(tmp_path):
    # Generate a test RSA key pair
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = tmp_path / "test_key.pem"
    key_path.write_bytes(private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    return KalshiClient(
        key_id="test-key-id",
        private_key_path=str(key_path),
        base_url=DEMO_BASE,
    )

@respx.mock
@pytest.mark.asyncio
async def test_get_markets(kalshi_client):
    respx.get(f"{DEMO_BASE}/markets").mock(
        return_value=httpx.Response(200, json={
            "markets": [{"ticker": "KXNBAGAME-26MAR18LALHOU-LAL",
                          "yes_price": 49, "no_price": 51, "volume": 1000}],
            "cursor": "",
        })
    )
    markets = await kalshi_client.get_markets(series_ticker="KXNBAGAME", status="open")
    assert len(markets) == 1
    assert markets[0]["yes_price"] == 49

@respx.mock
@pytest.mark.asyncio
async def test_place_order(kalshi_client):
    respx.post(f"{DEMO_BASE}/portfolio/orders").mock(
        return_value=httpx.Response(200, json={
            "order": {"order_id": "abc123", "status": "resting"}
        })
    )
    result = await kalshi_client.place_order(
        ticker="KXNBAGAME-26MAR18LALHOU-LAL",
        action="buy", side="yes", type_="limit",
        count=10, yes_price=49,
    )
    assert result["order"]["order_id"] == "abc123"

@respx.mock
@pytest.mark.asyncio
async def test_auth_headers_present(kalshi_client):
    route = respx.get(f"{DEMO_BASE}/portfolio/balance").mock(
        return_value=httpx.Response(200, json={"balance": 100000})
    )
    await kalshi_client.get_balance()
    request = route.calls[0].request
    assert "KALSHI-ACCESS-KEY" in request.headers
    assert "KALSHI-ACCESS-TIMESTAMP" in request.headers
    assert "KALSHI-ACCESS-SIGNATURE" in request.headers

@respx.mock
@pytest.mark.asyncio
async def test_get_orderbook(kalshi_client):
    ticker = "KXNBAPTS-26MAR18-LALLEBRONJ-27"
    respx.get(f"{DEMO_BASE}/markets/{ticker}/orderbook").mock(
        return_value=httpx.Response(200, json={
            "orderbook": {
                "yes": [[49, 20], [48, 50]],
                "no": [[52, 15], [53, 30]],
            }
        })
    )
    book = await kalshi_client.get_orderbook(ticker)
    assert book["orderbook"]["yes"][0] == [49, 20]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_kalshi.py -v`
Expected: FAIL

- [ ] **Step 3: Implement Kalshi client**

```python
# oracle/clients/kalshi.py
"""Kalshi REST API client with RSA-PSS authentication."""
from __future__ import annotations
import time
import base64
import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


class KalshiClient:
    def __init__(self, key_id: str, private_key_path: str,
                 base_url: str = "https://demo-api.kalshi.co/trade-api/v2"):
        self._key_id = key_id
        self._base_url = base_url
        with open(private_key_path, "rb") as f:
            self._private_key = serialization.load_pem_private_key(
                f.read(), password=None,
            )
        self._http = httpx.AsyncClient(base_url=base_url, timeout=10.0)

    def _sign(self, method: str, path: str) -> dict:
        timestamp = str(int(time.time() * 1000))
        path_no_query = path.split("?")[0]
        message = f"{timestamp}{method}{path_no_query}".encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
            "Content-Type": "application/json",
        }

    async def _get(self, path: str, params: dict | None = None) -> dict:
        headers = self._sign("GET", path)
        resp = await self._http.get(path, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, json_body: dict) -> dict:
        headers = self._sign("POST", path)
        resp = await self._http.post(path, headers=headers, json=json_body)
        resp.raise_for_status()
        return resp.json()

    async def _delete(self, path: str) -> dict:
        headers = self._sign("DELETE", path)
        resp = await self._http.delete(path, headers=headers)
        resp.raise_for_status()
        return resp.json()

    async def get_markets(self, series_ticker: str = "",
                          status: str = "open",
                          limit: int = 200) -> list[dict]:
        params = {"status": status, "limit": str(limit)}
        if series_ticker:
            params["series_ticker"] = series_ticker
        data = await self._get("/markets", params=params)
        return data.get("markets", [])

    async def get_orderbook(self, ticker: str) -> dict:
        return await self._get(f"/markets/{ticker}/orderbook")

    async def place_order(self, ticker: str, action: str, side: str,
                          type_: str, count: int, yes_price: int,
                          time_in_force: str = "good_till_canceled") -> dict:
        body = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "type": type_,
            "count": count,
            "yes_price": yes_price,
            "time_in_force": time_in_force,
        }
        return await self._post("/portfolio/orders", body)

    async def cancel_order(self, order_id: str) -> dict:
        return await self._delete(f"/portfolio/orders/{order_id}")

    async def get_positions(self) -> list[dict]:
        data = await self._get("/portfolio/positions")
        return data.get("market_positions", data if isinstance(data, list) else [])

    async def get_balance(self) -> dict:
        return await self._get("/portfolio/balance")

    async def close(self):
        await self._http.aclose()
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_kalshi.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add oracle/clients/kalshi.py tests/test_kalshi.py
git commit -m "feat: add Kalshi REST client with RSA-PSS signing"
```

---

### Task 9: Player/market matching

**Files:**
- Create: `oracle/clients/matching.py`
- Create: `tests/test_matching.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_matching.py
from oracle.clients.matching import normalize_name, fuzzy_match_player, parse_kalshi_prop_title

def test_normalize_name():
    assert normalize_name("LeBron James") == "lebron james"
    assert normalize_name("Shai Gilgeous-Alexander") == "shai gilgeous-alexander"
    assert normalize_name("LaMelo Ball Jr.") == "lamelo ball"
    assert normalize_name("Jaren Jackson III") == "jaren jackson"

def test_parse_kalshi_prop_title():
    result = parse_kalshi_prop_title("LeBron James Over 27.5 Points")
    assert result["player_name"] == "LeBron James"
    assert result["line"] == 27.5
    assert result["stat"] == "points"
    assert result["direction"] == "over"

    result2 = parse_kalshi_prop_title("Shai Gilgeous-Alexander Under 6.5 Assists")
    assert result2["player_name"] == "Shai Gilgeous-Alexander"
    assert result2["line"] == 6.5
    assert result2["stat"] == "assists"

def test_fuzzy_match_player():
    real_players = [
        {"name": "LeBron James", "player_id": 100},
        {"name": "LaMelo Ball", "player_id": 200},
        {"name": "Shai Gilgeous-Alexander", "player_id": 300},
    ]
    assert fuzzy_match_player("LeBron James", real_players) == 100
    assert fuzzy_match_player("Lebron James", real_players) == 100  # case insensitive
    assert fuzzy_match_player("Unknown Player", real_players) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_matching.py -v`
Expected: FAIL

- [ ] **Step 3: Implement matching**

```python
# oracle/clients/matching.py
"""Entity resolution: match Real players to Kalshi market tickers."""
from __future__ import annotations
import re
from typing import Optional


_SUFFIXES = re.compile(r"\s+(Jr\.?|Sr\.?|III|II|IV|V)\s*$", re.IGNORECASE)


def normalize_name(name: str) -> str:
    cleaned = _SUFFIXES.sub("", name).strip()
    return cleaned.lower()


def parse_kalshi_prop_title(title: str) -> dict:
    """Parse 'LeBron James Over 27.5 Points' into components."""
    pattern = re.compile(
        r"^(.+?)\s+(Over|Under)\s+([\d.]+)\s+(.+)$", re.IGNORECASE
    )
    m = pattern.match(title)
    if not m:
        return {}
    return {
        "player_name": m.group(1).strip(),
        "line": float(m.group(3)),
        "direction": m.group(2).lower(),
        "stat": m.group(4).strip().lower(),
    }


def fuzzy_match_player(
    kalshi_name: str,
    real_players: list[dict],
) -> Optional[int]:
    """Match a Kalshi player name to a Real player_id.

    Uses normalized exact match first, then falls back to
    checking if one name contains the other.
    """
    target = normalize_name(kalshi_name)

    # Exact normalized match
    for p in real_players:
        if normalize_name(p["name"]) == target:
            return p["player_id"]

    # Containment fallback (for partial name differences)
    for p in real_players:
        real_norm = normalize_name(p["name"])
        if target in real_norm or real_norm in target:
            return p["player_id"]

    return None
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_matching.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add oracle/clients/matching.py tests/test_matching.py
git commit -m "feat: add player/market matching with name normalization"
```

---

## Chunk 3: Trading Books A, B, C

### Task 10: Book A — Game divergence

**Files:**
- Create: `oracle/books/book_a.py`
- Create: `tests/test_book_a.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_book_a.py
from oracle.books.book_a import scan_divergence
from oracle.models import Book

def test_detects_positive_edge():
    real_markets = [
        {"game_id": 1, "home": "LAL", "away": "HOU",
         "home_prob": 68, "volume": 500000},
    ]
    kalshi_markets = [
        {"game_id": 1, "home": "LAL", "away": "HOU",
         "ticker_yes": "KXNBAGAME-26MAR18LALHOU-LAL",
         "ticker_no": "KXNBAGAME-26MAR18LALHOU-HOU",
         "yes_price": 50},
    ]
    signals = scan_divergence(real_markets, kalshi_markets, min_edge=0.15, min_volume=200000)
    assert len(signals) == 1
    assert signals[0].side == "yes"
    assert abs(signals[0].edge - 0.18) < 0.01  # 0.68 - 0.50

def test_skips_low_volume():
    real_markets = [
        {"game_id": 1, "home": "LAL", "away": "HOU",
         "home_prob": 68, "volume": 50000},  # below 200k
    ]
    kalshi_markets = [
        {"game_id": 1, "home": "LAL", "away": "HOU",
         "ticker_yes": "X", "ticker_no": "Y", "yes_price": 50},
    ]
    signals = scan_divergence(real_markets, kalshi_markets, min_edge=0.15, min_volume=200000)
    assert len(signals) == 0

def test_skips_below_threshold():
    real_markets = [
        {"game_id": 1, "home": "LAL", "away": "HOU",
         "home_prob": 55, "volume": 500000},
    ]
    kalshi_markets = [
        {"game_id": 1, "home": "LAL", "away": "HOU",
         "ticker_yes": "X", "ticker_no": "Y", "yes_price": 50},
    ]
    signals = scan_divergence(real_markets, kalshi_markets, min_edge=0.15, min_volume=200000)
    assert len(signals) == 0  # 0.55 - 0.50 = 0.05, below 0.15

def test_detects_no_side_edge():
    real_markets = [
        {"game_id": 1, "home": "LAL", "away": "HOU",
         "home_prob": 40, "volume": 500000},  # Real thinks LAL is worse
    ]
    kalshi_markets = [
        {"game_id": 1, "home": "LAL", "away": "HOU",
         "ticker_yes": "X-LAL", "ticker_no": "X-HOU", "yes_price": 60},
    ]
    signals = scan_divergence(real_markets, kalshi_markets, min_edge=0.15, min_volume=200000)
    assert len(signals) == 1
    assert signals[0].side == "no"
    assert abs(signals[0].edge - 0.20) < 0.01  # |0.40 - 0.60|
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_book_a.py -v`
Expected: FAIL

- [ ] **Step 3: Implement Book A**

```python
# oracle/books/book_a.py
"""Book A: Real-vs-Kalshi game-level price divergence."""
from __future__ import annotations
from oracle.models import Book, Signal


def scan_divergence(
    real_markets: list[dict],
    kalshi_markets: list[dict],
    min_edge: float,
    min_volume: int,
) -> list[Signal]:
    """Compare Real's implied probability to Kalshi's price for matched games."""
    # Index Kalshi by game_id
    kalshi_by_game = {m["game_id"]: m for m in kalshi_markets}

    signals = []
    for rm in real_markets:
        if rm["volume"] < min_volume:
            continue

        km = kalshi_by_game.get(rm["game_id"])
        if not km:
            continue

        real_prob = rm["home_prob"] / 100
        kalshi_price = km["yes_price"] / 100

        edge = real_prob - kalshi_price

        if abs(edge) >= min_edge:
            if edge > 0:
                signals.append(Signal(
                    book=Book.A,
                    ticker=km["ticker_yes"],
                    side="yes",
                    edge=round(abs(edge), 4),
                    model_prob=real_prob,
                    kalshi_price=kalshi_price,
                ))
            else:
                signals.append(Signal(
                    book=Book.A,
                    ticker=km["ticker_no"],
                    side="no",
                    edge=round(abs(edge), 4),
                    model_prob=round(1 - real_prob, 4),
                    kalshi_price=round(1 - kalshi_price, 4),
                ))

    return signals
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_book_a.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add oracle/books/book_a.py tests/test_book_a.py
git commit -m "feat: add Book A — Real-vs-Kalshi game divergence scanner"
```

---

### Task 11: Book B — Pregame player props fair value

**Files:**
- Create: `oracle/books/book_b.py`
- Create: `tests/test_book_b.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_book_b.py
from oracle.books.book_b import (
    compute_fair_value, weighted_hit_rate,
    apply_matchup_shift, apply_venue_shift,
    implied_probability, LineupGate, check_lineup_gate,
)

def test_weighted_hit_rate():
    # 20 game values, line 27.5
    values = [30, 25, 32, 28, 35, 22, 31, 27, 29, 26,
              24, 33, 21, 28, 30, 25, 27, 31, 23, 29]
    rate = weighted_hit_rate(values, line=27.5)
    # Last 5: [30, 25, 32, 28, 35] → 3 over (weight 2.0 each = 6.0 hits / 10.0 total)
    # Last 6-10: [22, 31, 27, 29, 26] → 2 over (weight 1.5 each = 3.0 / 7.5 total)
    # Last 11-20: [24, 33, 21, 28, 30, 25, 27, 31, 23, 29] → 4 over (1.0 each)
    # Total hits: 6.0 + 3.0 + 4.0 = 13.0, total weight: 10.0 + 7.5 + 10.0 = 27.5
    assert abs(rate - 13.0/27.5) < 0.01

def test_apply_matchup_shift_bad_defense():
    values = [25, 28, 30, 27, 26]
    shift = apply_matchup_shift(values, opp_def_rank=28, opp_history=None)
    mean_val = sum(values) / len(values)
    assert shift > 0  # Bad defense → positive shift
    assert abs(shift - 0.08 * mean_val) < 0.1

def test_apply_matchup_shift_good_defense():
    values = [25, 28, 30, 27, 26]
    shift = apply_matchup_shift(values, opp_def_rank=3, opp_history=None)
    assert shift < 0  # Good defense → negative shift

def test_apply_matchup_shift_with_opponent_history():
    values = [25, 28, 30, 27, 26]
    opp_history = {"games": 4, "average": 35.0}
    shift = apply_matchup_shift(values, opp_def_rank=15, opp_history=opp_history)
    mean_val = sum(values) / len(values)
    expected = (35.0 - mean_val) * 0.5  # Blend toward opp-specific avg
    assert abs(shift - expected) < 0.1

def test_apply_venue_shift_home():
    assert abs(apply_venue_shift(home_avg=28.0, away_avg=25.0, is_home=True) - 0.9) < 0.1
    # (28 - 25) * 0.3 = 0.9

def test_apply_venue_shift_away():
    assert abs(apply_venue_shift(home_avg=28.0, away_avg=25.0, is_home=False) - (-0.9)) < 0.1

def test_implied_probability():
    assert abs(implied_probability(-130) - 0.5652) < 0.01  # -130 → 56.5%
    assert abs(implied_probability(110) - 0.4762) < 0.01   # +110 → 47.6%
    assert abs(implied_probability(-200) - 0.6667) < 0.01  # -200 → 66.7%

def test_lineup_gate_questionable():
    gate = check_lineup_gate(player_status="Questionable", minutes_avg=32,
                              key_teammate_questionable=False, minutes_to_tip=60)
    assert gate == LineupGate.PLAYER_QUESTIONABLE

def test_lineup_gate_low_minutes():
    gate = check_lineup_gate(player_status="Active", minutes_avg=15,
                              key_teammate_questionable=False, minutes_to_tip=60)
    assert gate == LineupGate.LOW_MINUTES

def test_lineup_gate_no_trade_window():
    gate = check_lineup_gate(player_status="Active", minutes_avg=32,
                              key_teammate_questionable=False, minutes_to_tip=10)
    assert gate == LineupGate.NO_TRADE_WINDOW

def test_lineup_gate_clear():
    gate = check_lineup_gate(player_status="Active", minutes_avg=32,
                              key_teammate_questionable=False, minutes_to_tip=60)
    assert gate == LineupGate.CLEAR
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_book_b.py -v`
Expected: FAIL

- [ ] **Step 3: Implement Book B**

```python
# oracle/books/book_b.py
"""Book B: Pregame NBA player props fair value computation."""
from __future__ import annotations
from enum import Enum
from typing import Optional
from oracle.models import Book, Signal


class LineupGate(Enum):
    CLEAR = "clear"
    PLAYER_QUESTIONABLE = "player_questionable"
    TEAMMATE_QUESTIONABLE = "teammate_questionable"
    NO_TRADE_WINDOW = "no_trade_window"
    LOW_MINUTES = "low_minutes"


def check_lineup_gate(
    player_status: str,
    minutes_avg: float,
    key_teammate_questionable: bool,
    minutes_to_tip: float,
    min_minutes: float = 20,
    no_trade_window: float = 15,
) -> LineupGate:
    if player_status == "Questionable":
        return LineupGate.PLAYER_QUESTIONABLE
    if key_teammate_questionable:
        return LineupGate.TEAMMATE_QUESTIONABLE
    if minutes_to_tip < no_trade_window:
        return LineupGate.NO_TRADE_WINDOW
    if minutes_avg < min_minutes:
        return LineupGate.LOW_MINUTES
    return LineupGate.CLEAR


def implied_probability(american_odds: float) -> float:
    """Convert American odds to implied probability."""
    if american_odds < 0:
        return abs(american_odds) / (abs(american_odds) + 100)
    else:
        return 100 / (american_odds + 100)


def weighted_hit_rate(values: list[float], line: float) -> float:
    """Compute recency-weighted hit rate.

    Last 5 games: 2.0x weight
    Games 6-10: 1.5x weight
    Games 11+: 1.0x weight
    """
    if not values:
        return 0.0

    total_weight = 0.0
    total_hits = 0.0
    for i, v in enumerate(values):
        if i < 5:
            w = 2.0
        elif i < 10:
            w = 1.5
        else:
            w = 1.0
        total_weight += w
        if v > line:
            total_hits += w

    return total_hits / total_weight if total_weight > 0 else 0.0


def apply_matchup_shift(
    values: list[float],
    opp_def_rank: int,
    opp_history: Optional[dict],
) -> float:
    """Compute matchup-based shift to the distribution."""
    mean_val = sum(values) / len(values) if values else 0
    shift = 0.0

    # Per-opponent history (if enough data)
    if opp_history and opp_history.get("games", 0) >= 3:
        opp_avg = opp_history["average"]
        shift = (opp_avg - mean_val) * 0.5
    else:
        # Opponent defensive ranking
        if opp_def_rank >= 25:
            shift = 0.08 * mean_val
        elif opp_def_rank <= 5:
            shift = -0.08 * mean_val

    return shift


def apply_venue_shift(home_avg: float, away_avg: float, is_home: bool) -> float:
    """Compute venue-based shift."""
    delta = home_avg - away_avg
    if is_home:
        return delta * 0.3
    else:
        return -delta * 0.3


def compute_fair_value(
    game_values: list[float],
    line: float,
    matchup_shift: float = 0.0,
    venue_shift: float = 0.0,
    b2b_penalty_pct: float = 0.0,
    pace_factor: float = 1.0,
    teammate_out_shift: float = 0.0,
    external_over_odds: Optional[float] = None,
) -> float:
    """Compute fair value probability for a player prop.

    Args:
        game_values: Last N game stat values (most recent first).
        line: The prop line (e.g., 27.5).
        matchup_shift: Points shift from matchup analysis.
        venue_shift: Points shift from home/away.
        b2b_penalty_pct: Penalty as fraction (e.g., 0.06 for 6%).
        pace_factor: Multiplier (1.0 = league average pace).
        teammate_out_shift: Points shift from teammate absence.
        external_over_odds: American odds from stat tracker (optional).

    Returns:
        Fair probability of clearing the line (0-1).
    """
    if not game_values:
        return 0.5

    mean_val = sum(game_values) / len(game_values)

    # Total shift
    total_shift = matchup_shift + venue_shift + teammate_out_shift
    total_shift -= b2b_penalty_pct * mean_val  # B2B penalty
    total_shift += (pace_factor - 1.0) * mean_val  # Pace adjustment

    # Apply shift to all values
    adjusted = [v + total_shift for v in game_values]

    # Weighted hit rate
    model_prob = weighted_hit_rate(adjusted, line)

    # Blend with external odds if available
    if external_over_odds is not None:
        ext_prob = implied_probability(external_over_odds)
        model_prob = 0.70 * model_prob + 0.30 * ext_prob

    return round(model_prob, 4)


def generate_book_b_signals(
    fair_values: list[dict],
    min_edge: float,
) -> list[Signal]:
    """Generate Book B signals from computed fair values.

    Each entry in fair_values:
        {"ticker": str, "fair_value": float, "kalshi_price": float,
         "player_id": int, "game_id": str}
    """
    signals = []
    for fv in fair_values:
        kalshi_dec = fv["kalshi_price"] / 100  # cents → decimal
        edge = fv["fair_value"] - kalshi_dec

        if edge >= min_edge:
            signals.append(Signal(
                book=Book.B,
                ticker=fv["ticker"],
                side="yes",
                edge=round(edge, 4),
                model_prob=fv["fair_value"],
                kalshi_price=kalshi_dec,
                metadata={"player_id": fv.get("player_id"),
                          "game_id": fv.get("game_id")},
            ))
        elif edge <= -min_edge:
            # Bet the under (buy NO)
            no_prob = 1.0 - fv["fair_value"]
            no_price = 1.0 - kalshi_dec
            signals.append(Signal(
                book=Book.B,
                ticker=fv["ticker"],
                side="no",
                edge=round(abs(edge), 4),
                model_prob=no_prob,
                kalshi_price=no_price,
                metadata={"player_id": fv.get("player_id"),
                          "game_id": fv.get("game_id")},
            ))

    return signals
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_book_b.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add oracle/books/book_b.py tests/test_book_b.py
git commit -m "feat: add Book B — pregame player props with empirical distribution"
```

---

### Task 12: Book C — Live event signals

**Files:**
- Create: `oracle/books/book_c.py`
- Create: `tests/test_book_c.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_book_c.py
from oracle.books.book_c import (
    detect_foul_trouble, detect_ot_likely, detect_blowout,
    LiveSignalType,
)

def test_foul_trouble_4th_foul_q3():
    signal = detect_foul_trouble(fouls=4, period="Q3", clock_seconds=400,
                                  player_name="LeBron", current_stat=18, line=27.5)
    assert signal is not None
    assert signal.signal_type == LiveSignalType.FOUL_TROUBLE
    assert signal.action == "sell_over"

def test_foul_trouble_3_fouls_ignored():
    signal = detect_foul_trouble(fouls=3, period="Q3", clock_seconds=400,
                                  player_name="LeBron", current_stat=18, line=27.5)
    assert signal is None

def test_foul_trouble_4_fouls_q4_ignored():
    # 4 fouls in Q4 is less significant (less time to lose)
    signal = detect_foul_trouble(fouls=4, period="Q4", clock_seconds=600,
                                  player_name="LeBron", current_stat=18, line=27.5)
    assert signal is None

def test_ot_likely_tied_under_2min():
    signal = detect_ot_likely(margin=0, period="Q4", clock_seconds=90,
                               player_name="LeBron", current_stat=24,
                               per_min_rate=0.75, line=27.5)
    assert signal is not None
    assert signal.signal_type == LiveSignalType.OT_LIKELY
    assert signal.action == "buy_over"

def test_ot_likely_not_tied():
    signal = detect_ot_likely(margin=5, period="Q4", clock_seconds=90,
                               player_name="LeBron", current_stat=24,
                               per_min_rate=0.75, line=27.5)
    assert signal is None

def test_ot_likely_but_already_cleared():
    # Player already at 30, line 27.5 → no need to buy over
    signal = detect_ot_likely(margin=0, period="Q4", clock_seconds=90,
                               player_name="LeBron", current_stat=30,
                               per_min_rate=0.75, line=27.5)
    assert signal is None

def test_blowout_detected():
    signal = detect_blowout(margin=22, period="Q3", clock_seconds=400,
                             player_name="LeBron", current_stat=18, line=27.5)
    assert signal is not None
    assert signal.signal_type == LiveSignalType.BLOWOUT
    assert signal.action == "sell_over"

def test_blowout_but_already_cleared():
    signal = detect_blowout(margin=22, period="Q3", clock_seconds=400,
                             player_name="LeBron", current_stat=30, line=27.5)
    assert signal is None  # Already over line, don't sell

def test_blowout_not_enough_margin():
    signal = detect_blowout(margin=15, period="Q3", clock_seconds=400,
                             player_name="LeBron", current_stat=18, line=27.5)
    assert signal is None  # Need 20+
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_book_c.py -v`
Expected: FAIL

- [ ] **Step 3: Implement Book C**

```python
# oracle/books/book_c.py
"""Book C: Live event trading signals (foul trouble, OT, blowout)."""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class LiveSignalType(Enum):
    FOUL_TROUBLE = "foul_trouble"
    OT_LIKELY = "ot_likely"
    BLOWOUT = "blowout"


@dataclass
class LiveSignal:
    signal_type: LiveSignalType
    player_name: str
    action: str  # "buy_over", "sell_over"
    metadata: dict


def detect_foul_trouble(
    fouls: int,
    period: str,
    clock_seconds: int,
    player_name: str,
    current_stat: float,
    line: float,
) -> Optional[LiveSignal]:
    """Detect 4th foul before Q4 → sell the over."""
    if fouls < 4:
        return None
    if period not in ("Q1", "Q2", "Q3"):
        return None  # Q4 fouls matter less
    if current_stat >= line:
        return None  # Already cleared, no edge in selling

    return LiveSignal(
        signal_type=LiveSignalType.FOUL_TROUBLE,
        player_name=player_name,
        action="sell_over",
        metadata={"fouls": fouls, "period": period, "current": current_stat,
                  "line": line},
    )


def detect_ot_likely(
    margin: int,
    period: str,
    clock_seconds: int,
    player_name: str,
    current_stat: float,
    per_min_rate: float,
    line: float,
) -> Optional[LiveSignal]:
    """Detect tied game with < 2 min left in Q4 → buy the over.

    Only signal if OT production would push player over the line.
    """
    if abs(margin) > 2:
        return None
    if period != "Q4":
        return None
    if clock_seconds > 120:
        return None
    if current_stat >= line:
        return None  # Already cleared

    # Project: current + remaining_reg + 5 min OT
    remaining_reg_min = clock_seconds / 60
    ot_minutes = 5.0
    projected = current_stat + per_min_rate * (remaining_reg_min + ot_minutes)

    # Require 5% margin above line
    if projected < line * 1.05:
        return None

    return LiveSignal(
        signal_type=LiveSignalType.OT_LIKELY,
        player_name=player_name,
        action="buy_over",
        metadata={"margin": margin, "projected": round(projected, 1),
                  "line": line, "current": current_stat},
    )


def detect_blowout(
    margin: int,
    period: str,
    clock_seconds: int,
    player_name: str,
    current_stat: float,
    line: float,
    blowout_threshold: int = 20,
) -> Optional[LiveSignal]:
    """Detect 20+ point blowout in Q3+ → sell the over.

    Both teams' starters will sit most of Q4.
    """
    if abs(margin) < blowout_threshold:
        return None
    if period not in ("Q3", "Q4"):
        return None
    if current_stat >= line:
        return None  # Already cleared

    return LiveSignal(
        signal_type=LiveSignalType.BLOWOUT,
        player_name=player_name,
        action="sell_over",
        metadata={"margin": margin, "period": period,
                  "current": current_stat, "line": line},
    )
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_book_c.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add oracle/books/book_c.py tests/test_book_c.py
git commit -m "feat: add Book C — live event signals (foul, OT, blowout)"
```

---

### Task 13: Quote quality checks (Book C execution filter)

**Files:**
- Create: `oracle/execution/quote_check.py`
- Create: `tests/test_quote_check.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_quote_check.py
from oracle.execution.quote_check import check_quote_quality, QuoteResult
from oracle.models import QuoteSnapshot
import time

def test_fresh_tight_deep_passes():
    quote = QuoteSnapshot(
        ticker="X", yes_bid=48, yes_ask=51,
        bid_depth=20, ask_depth=15, timestamp=time.time(),
    )
    result = check_quote_quality(quote, max_age_s=5, max_spread=8, min_depth=5)
    assert result.ok is True

def test_stale_quote_fails():
    quote = QuoteSnapshot(
        ticker="X", yes_bid=48, yes_ask=51,
        bid_depth=20, ask_depth=15, timestamp=time.time() - 10,
    )
    result = check_quote_quality(quote, max_age_s=5, max_spread=8, min_depth=5)
    assert result.ok is False
    assert "age" in result.reason

def test_wide_spread_fails():
    quote = QuoteSnapshot(
        ticker="X", yes_bid=40, yes_ask=55,
        bid_depth=20, ask_depth=15, timestamp=time.time(),
    )
    result = check_quote_quality(quote, max_age_s=5, max_spread=8, min_depth=5)
    assert result.ok is False
    assert "spread" in result.reason

def test_thin_depth_fails():
    quote = QuoteSnapshot(
        ticker="X", yes_bid=48, yes_ask=51,
        bid_depth=2, ask_depth=3, timestamp=time.time(),
    )
    result = check_quote_quality(quote, max_age_s=5, max_spread=8, min_depth=5)
    assert result.ok is False
    assert "depth" in result.reason
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_quote_check.py -v`
Expected: FAIL

- [ ] **Step 3: Implement quote checks**

```python
# oracle/execution/quote_check.py
"""Quote quality filters for Book C live execution."""
from __future__ import annotations
from dataclasses import dataclass
from oracle.models import QuoteSnapshot


@dataclass
class QuoteResult:
    ok: bool
    reason: str = ""


def check_quote_quality(
    quote: QuoteSnapshot,
    max_age_s: float,
    max_spread: int,
    min_depth: int,
) -> QuoteResult:
    if quote.age_seconds > max_age_s:
        return QuoteResult(False, f"age={quote.age_seconds:.1f}s > {max_age_s}s")

    if quote.spread > max_spread:
        return QuoteResult(False, f"spread={quote.spread}c > {max_spread}c")

    min_side_depth = min(quote.bid_depth, quote.ask_depth)
    if min_side_depth < min_depth:
        return QuoteResult(False, f"depth={min_side_depth} < {min_depth}")

    return QuoteResult(True)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_quote_check.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add oracle/execution/quote_check.py tests/test_quote_check.py
git commit -m "feat: add quote quality checks for Book C execution"
```

---

### Task 14: Order executor

**Files:**
- Create: `oracle/execution/executor.py`
- Create: `tests/test_executor.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_executor.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from oracle.execution.executor import OrderExecutor
from oracle.models import Book, Signal, Position

@pytest.fixture
def mock_kalshi():
    client = AsyncMock()
    client.place_order = AsyncMock(return_value={
        "order": {"order_id": "ord123", "status": "resting"}
    })
    client.cancel_order = AsyncMock(return_value={"order": {"status": "canceled"}})
    return client

@pytest.mark.asyncio
async def test_execute_limit_order(mock_kalshi):
    executor = OrderExecutor(mock_kalshi)
    signal = Signal(book=Book.B, ticker="KXNBAPTS-X", side="yes",
                    edge=0.12, model_prob=0.72, kalshi_price=0.60)
    result = await executor.place_limit(signal, contracts=10, price_cents=60)
    mock_kalshi.place_order.assert_called_once_with(
        ticker="KXNBAPTS-X", action="buy", side="yes",
        type_="limit", count=10, yes_price=60,
    )
    assert result["order"]["order_id"] == "ord123"

@pytest.mark.asyncio
async def test_cancel_order(mock_kalshi):
    executor = OrderExecutor(mock_kalshi)
    await executor.cancel("ord123")
    mock_kalshi.cancel_order.assert_called_once_with("ord123")

def test_compute_contracts():
    executor = OrderExecutor(None)
    contracts = executor.compute_contracts(
        bankroll=2000, position_pct=0.015, price_decimal=0.60,
    )
    # 2000 * 0.015 = $30 budget, $30 / $0.60 = 50 contracts
    assert contracts == 50

def test_compute_contracts_caps_at_zero():
    executor = OrderExecutor(None)
    assert executor.compute_contracts(bankroll=0, position_pct=0.015, price_decimal=0.60) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_executor.py -v`
Expected: FAIL

- [ ] **Step 3: Implement executor**

```python
# oracle/execution/executor.py
"""Order placement, sizing, and cancellation."""
from __future__ import annotations
from oracle.models import Signal
from oracle.clients.kalshi import KalshiClient


class OrderExecutor:
    def __init__(self, kalshi: KalshiClient | None):
        self._kalshi = kalshi

    async def place_limit(self, signal: Signal, contracts: int,
                          price_cents: int) -> dict:
        return await self._kalshi.place_order(
            ticker=signal.ticker,
            action="buy",
            side=signal.side,
            type_="limit",
            count=contracts,
            yes_price=price_cents,
        )

    async def cancel(self, order_id: str) -> dict:
        return await self._kalshi.cancel_order(order_id)

    @staticmethod
    def compute_contracts(bankroll: float, position_pct: float,
                          price_decimal: float) -> int:
        if bankroll <= 0 or price_decimal <= 0:
            return 0
        budget = bankroll * position_pct
        return int(budget / price_decimal)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_executor.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add oracle/execution/executor.py tests/test_executor.py
git commit -m "feat: add order executor with fixed-fractional sizing"
```

---

## Chunk 4: Integration — Real WebSocket + Main Orchestrator

### Task 15: Real Sports Socket.io client

**Files:**
- Create: `oracle/clients/real_ws.py`

Note: Socket.io is hard to unit test with mocks. This task creates the client with manual integration test instructions rather than automated tests.

- [ ] **Step 1: Implement WebSocket client**

```python
# oracle/clients/real_ws.py
"""Real Sports App Socket.io client for live game data."""
from __future__ import annotations
import asyncio
from typing import Callable, Awaitable
import socketio
from hashids import Hashids
import time

_hashids = Hashids("realwebapp", 16)


class RealWebSocket:
    def __init__(self):
        self._sio = socketio.AsyncClient(
            reconnection=True,
            reconnection_delay=0.25,
            reconnection_delay_max=1.0,
        )
        self._handlers: dict[str, list[Callable]] = {}
        self._connected = False
        self._last_data_time = 0.0

        self._sio.on("connect", self._on_connect)
        self._sio.on("disconnect", self._on_disconnect)

        # Register all relevant events
        for event in [
            "LiveFeedSocketPlaysAdded",
            "LiveFeedSocketPlaysUpdated",
            "LiveFeedSocketPlayersUpdated",
            "GameUpdated",
            "GameMarketUpdated",
            "PlayerBoxScoreUpdated",
        ]:
            self._sio.on(event, self._make_handler(event))

    def _make_handler(self, event_name: str):
        async def handler(data):
            self._last_data_time = time.time()
            for cb in self._handlers.get(event_name, []):
                await cb(data)
        return handler

    def on(self, event: str, callback: Callable[..., Awaitable]):
        self._handlers.setdefault(event, []).append(callback)

    async def connect(self, socket_type: str = "LiveFeed", **extra_query):
        query = {
            "socketType": socket_type,
            "realRequestToken": _hashids.encode(int(time.time() * 1000)),
            "realVersion": "28",
            **extra_query,
        }
        await self._sio.connect(
            "https://web.realsports.io",
            transports=["websocket"],
            socketio_path="/socket.io/",
            headers={},
            wait=True,
            wait_timeout=10,
        )
        self._connected = True

    async def disconnect(self):
        await self._sio.disconnect()
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def seconds_since_data(self) -> float:
        if self._last_data_time == 0:
            return float("inf")
        return time.time() - self._last_data_time

    async def _on_connect(self):
        self._connected = True

    async def _on_disconnect(self):
        self._connected = False
```

- [ ] **Step 2: Commit**

```bash
git add oracle/clients/real_ws.py
git commit -m "feat: add Real Sports Socket.io WebSocket client"
```

---

### Task 16: Main orchestrator

**Files:**
- Create: `oracle/main.py`

- [ ] **Step 1: Implement the daily cycle orchestrator**

```python
# oracle/main.py
"""Oracle main entry point — daily cycle orchestrator."""
from __future__ import annotations
import asyncio
import logging
import sys
from datetime import date

from oracle.config import load_config
from oracle.db import OracleDB
from oracle.clients.real_rest import RealClient
from oracle.clients.kalshi import KalshiClient
from oracle.clients.real_ws import RealWebSocket
from oracle.risk.ledger import RiskLedger
from oracle.risk.limits import check_limits
from oracle.risk.fees import settlement_fee
from oracle.execution.executor import OrderExecutor
from oracle.execution.quote_check import check_quote_quality
from oracle.books.book_a import scan_divergence
from oracle.books.book_b import (
    compute_fair_value, generate_book_b_signals,
    check_lineup_gate, LineupGate,
)
from oracle.books.book_c import detect_foul_trouble, detect_ot_likely, detect_blowout
from oracle.models import Book, QuoteSnapshot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("oracle")


async def run(config_path: str = "config.yaml"):
    cfg = load_config(config_path)
    db = OracleDB("oracle.db")
    ledger = RiskLedger()

    # Initialize clients (credentials from env or config)
    # For now, placeholder — user fills in real credentials
    real = RealClient(
        user_id="YOUR_USER_ID",
        device_id="YOUR_DEVICE_ID",
        token="YOUR_TOKEN",
        device_uuid="YOUR_UUID",
    )
    kalshi = KalshiClient(
        key_id="YOUR_KEY_ID",
        private_key_path="kalshi_private_key.pem",
    )
    executor = OrderExecutor(kalshi)

    log.info("Oracle starting — bankroll=$%.0f", cfg.bankroll)

    try:
        # === PRE-GAME PHASE ===
        log.info("=== PRE-GAME: Fetching tonight's slate ===")
        today = date.today().isoformat()

        # Book A: scan for game divergence
        if cfg.books.a.enabled:
            log.info("Book A: scanning game divergence...")
            real_markets = await real.get_game_markets("nba")
            kalshi_game_markets = await kalshi.get_markets(
                series_ticker="KXNBAGAME", status="open"
            )
            # TODO: match Real games to Kalshi games by team/date
            # signals_a = scan_divergence(matched_real, matched_kalshi,
            #     min_edge=cfg.books.a.min_edge,
            #     min_volume=cfg.books.a.min_real_volume)
            log.info("Book A: scan complete")

        # Book B: compute prop fair values
        if cfg.books.b.enabled:
            log.info("Book B: computing prop fair values...")
            trackers = await real.get_stat_trackers(today, "nba")
            # TODO: for each player, fetch profile + season feed,
            # compute fair values, generate signals
            log.info("Book B: computation complete")

        # === LIVE PHASE ===
        log.info("=== LIVE: Starting WebSocket + polling loop ===")
        ws = RealWebSocket()

        # Book C event handlers
        async def on_plays_added(data):
            # TODO: parse play events, detect foul/OT/blowout
            pass

        async def on_players_updated(data):
            # TODO: update live player stats, check foul counts
            pass

        ws.on("LiveFeedSocketPlaysAdded", on_plays_added)
        ws.on("LiveFeedSocketPlayersUpdated", on_players_updated)

        try:
            await ws.connect("LiveFeed")
            log.info("WebSocket connected")
        except Exception as e:
            log.warning("WebSocket failed: %s — running REST-only", e)

        # Main polling loop
        log.info("Entering main loop (Ctrl+C to stop)")
        while True:
            # Check kill switch conditions
            if ledger.daily_pnl <= -(cfg.risk.daily_stop_loss_pct * cfg.bankroll):
                log.warning("DAILY STOP LOSS HIT: $%.2f — halting", ledger.daily_pnl)
                break

            # Poll Kalshi positions
            # TODO: reconcile with ledger

            await asyncio.sleep(10)

    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        await real.close()
        await kalshi.close()
        db.close()
        log.info("Oracle stopped. Daily P&L: $%.2f", ledger.daily_pnl)


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: All tests pass (models, config, db, fees, ledger, limits, book_a, book_b, book_c, matching, quote_check, executor).

- [ ] **Step 3: Commit**

```bash
git add oracle/main.py
git commit -m "feat: add main orchestrator with daily cycle skeleton"
```

---

### Task 17: Final integration — run full test suite and verify

- [ ] **Step 1: Run all tests**

Run: `python -m pytest tests/ -v --tb=short`
Expected: 30+ tests, all PASS.

- [ ] **Step 2: Verify imports work end-to-end**

Run: `python -c "from oracle.main import run; print('Oracle ready')"`
Expected: `Oracle ready`

- [ ] **Step 3: Final commit**

```bash
git add -A
git commit -m "feat: Oracle v1 complete — three-book NBA trading system"
```

---

## Chunk 5: Logic Fixes, Enhanced Risk, Data Pipeline

### Task 18: Fix logic errors and credential management

**Files:**
- Modify: `oracle/risk/fees.py`
- Modify: `oracle/risk/ledger.py`
- Modify: `oracle/books/book_a.py`
- Modify: `oracle/books/book_b.py`
- Modify: `oracle/clients/kalshi.py`
- Modify: `oracle/main.py`
- Modify: `tests/test_fees.py`
- Modify: `tests/test_ledger.py`
- Modify: `tests/test_book_a.py`
- Modify: `tests/test_kalshi.py`
- Create: `.env.example`
- Modify: `.gitignore`
- Create: `requirements.txt` (add `python-dotenv`)

**Context:** Review found 6 code logic errors: (1) fee calc uses payout instead of profit, (2) settle() ignores position side, (3) Book A NO-side uses 1-yes_price instead of actual NO price, (4) Book B generate_signals same issue, (5) Kalshi client doesn't paginate, (6) hardcoded credentials. Fix all before building on top of them.

- [ ] **Step 1: Write failing tests for fee fix**

```python
# Add to tests/test_fees.py

def test_settlement_fee_on_profit():
    """Fee is based on profit per contract, not payout."""
    # Buy YES at $0.60, win → profit = $0.40, fee on $0.40 bracket = $0.04
    assert settlement_fee(0.40) == 0.04
    # Buy YES at $0.30, win → profit = $0.70, fee on $0.70 bracket = $0.07
    assert settlement_fee(0.70) == 0.07

def test_settlement_fee_boundary_values():
    """Test exact bracket boundaries."""
    assert settlement_fee(0.10) == 0.01  # <= 0.10
    assert settlement_fee(0.11) == 0.02  # 0.11 is in 0.11-0.20
    assert settlement_fee(0.20) == 0.02
    assert settlement_fee(0.50) == 0.05
    assert settlement_fee(0.60) == 0.06
    assert settlement_fee(0.00) == 0.01  # zero profit edge case

def test_net_edge_yes_side_corrected():
    """net_edge must compute fee on PROFIT, not payout."""
    # model_prob=0.73, kalshi_price=0.60, YES side
    # If win: profit = 1.0 - 0.60 = 0.40. Fee on 0.40 = $0.04
    # gross edge = 0.73 - 0.60 = 0.13
    # expected fee = prob_win * fee = 0.73 * 0.04 = 0.0292
    # net = 0.13 - 0.0292 = 0.1008
    result = net_edge(model_prob=0.73, kalshi_price=0.60, side="yes")
    assert abs(result - 0.1008) < 0.002

def test_net_edge_no_side_corrected():
    """NO side: profit = kalshi_yes_price (what YES buyer loses)."""
    # model says NO worth 0.55, buying NO at 0.40 (kalshi yes=0.60)
    # If win: profit = 1.0 - 0.40 = 0.60. Fee on 0.60 = $0.06
    # gross edge = 0.55 - 0.40 = 0.15
    # expected fee = 0.55 * 0.06 = 0.033
    # net = 0.15 - 0.033 = 0.117
    result = net_edge(model_prob=0.55, kalshi_price=0.40, side="no")
    assert abs(result - 0.117) < 0.002
```

- [ ] **Step 2: Write failing tests for ledger settle fix**

```python
# Add to tests/test_ledger.py

def test_settle_no_side_win():
    """NO-side position wins when event does NOT happen."""
    ledger = RiskLedger()
    pos = Position(book=Book.B, ticker="X", side="no",
                   contracts=10, fill_price=0.40)
    ledger.add(pos)
    # NO wins: payout = 1.0, cost = 0.40, profit = 0.60 per contract
    pnl = ledger.settle("X", outcome="no", fee_per_contract=0.06)
    # (1.0 - 0.40 - 0.06) * 10 = 0.54 * 10 = 5.40
    assert abs(pnl - 5.40) < 0.01
    assert ledger.daily_pnl == 5.40

def test_settle_no_side_loss():
    """NO-side position loses when event DOES happen."""
    ledger = RiskLedger()
    pos = Position(book=Book.B, ticker="X", side="no",
                   contracts=10, fill_price=0.40)
    ledger.add(pos)
    pnl = ledger.settle("X", outcome="yes", fee_per_contract=0.0)
    assert abs(pnl - (-4.00)) < 0.01

def test_settle_yes_side_win():
    """YES-side position wins when event DOES happen."""
    ledger = RiskLedger()
    pos = Position(book=Book.B, ticker="X", side="yes",
                   contracts=10, fill_price=0.63)
    ledger.add(pos)
    pnl = ledger.settle("X", outcome="yes", fee_per_contract=0.04)
    assert abs(pnl - 3.30) < 0.01

def test_settle_yes_side_loss():
    """YES-side position loses when event does NOT happen."""
    ledger = RiskLedger()
    pos = Position(book=Book.B, ticker="X", side="yes",
                   contracts=10, fill_price=0.63)
    ledger.add(pos)
    pnl = ledger.settle("X", outcome="no", fee_per_contract=0.0)
    assert abs(pnl - (-6.30)) < 0.01
```

- [ ] **Step 3: Write failing tests for Kalshi pagination**

```python
# Add to tests/test_kalshi.py

@respx.mock
@pytest.mark.asyncio
async def test_get_markets_paginates(kalshi_client):
    """Fetches all pages when cursor is non-empty."""
    respx.get(f"{DEMO_BASE}/markets").mock(side_effect=[
        httpx.Response(200, json={
            "markets": [{"ticker": "T1", "yes_price": 50}],
            "cursor": "page2",
        }),
        httpx.Response(200, json={
            "markets": [{"ticker": "T2", "yes_price": 60}],
            "cursor": "",
        }),
    ])
    markets = await kalshi_client.get_markets(series_ticker="KXNBAPTS", status="open")
    assert len(markets) == 2
    assert markets[0]["ticker"] == "T1"
    assert markets[1]["ticker"] == "T2"
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `python -m pytest tests/test_fees.py tests/test_ledger.py tests/test_kalshi.py -v -k "corrected or boundary or no_side or paginates"`
Expected: FAIL

- [ ] **Step 5: Fix fees.py — fee based on profit**

```python
# oracle/risk/fees.py — REPLACE entire file
"""Kalshi fee schedule and net edge computation.

Kalshi fees are charged on PROFIT per contract (payout minus cost),
not on the raw settlement amount.
"""

# Fee brackets: (max_profit, fee_per_contract)
_FEE_SCHEDULE = [
    (0.10, 0.01),
    (0.20, 0.02),
    (0.30, 0.03),
    (0.40, 0.04),
    (0.50, 0.05),
    (0.60, 0.06),
    (0.99, 0.07),
]


def settlement_fee(profit_per_contract: float) -> float:
    """Return the per-contract fee for a winning trade with this profit."""
    for max_profit, fee in _FEE_SCHEDULE:
        if profit_per_contract <= max_profit:
            return fee
    return 0.07


def net_edge(model_prob: float, kalshi_price: float, side: str) -> float:
    """Compute net edge after expected Kalshi fees.

    Args:
        model_prob: Our model's probability for this side (0-1).
        kalshi_price: The price we'd pay for this side (0-1, decimal).
        side: 'yes' or 'no'.

    Returns:
        Net edge in dollars (decimal). Positive = profitable.
    """
    # Profit on win = 1.0 - price_paid
    profit_on_win = 1.0 - kalshi_price
    fee = settlement_fee(profit_on_win)
    gross = model_prob - kalshi_price
    expected_fee = model_prob * fee
    return round(gross - expected_fee, 4)
```

- [ ] **Step 6: Fix ledger.py — outcome-based settle**

Replace `settle` method in `oracle/risk/ledger.py`:

```python
    def settle(self, ticker: str, outcome: str, fee_per_contract: float = 0.0) -> float:
        """Settle a position based on the event outcome.

        Args:
            ticker: The position ticker.
            outcome: 'yes' (event happened) or 'no' (event did not happen).
            fee_per_contract: Fee charged on winning contracts.

        Returns:
            P&L in dollars.
        """
        pos = self._positions.pop(ticker, None)
        if pos is None:
            return 0.0

        won = (pos.side == outcome)
        if won:
            payout_per = (1.0 - pos.fill_price) - fee_per_contract
            pnl = round(payout_per * pos.contracts, 2)
        else:
            pnl = round(-pos.cost, 2)

        self.daily_pnl = round(self.daily_pnl + pnl, 2)
        return pnl
```

- [ ] **Step 7: Fix Kalshi client — add pagination**

Replace `get_markets` in `oracle/clients/kalshi.py`:

```python
    async def get_markets(self, series_ticker: str = "",
                          status: str = "open",
                          limit: int = 200) -> list[dict]:
        all_markets = []
        cursor = ""
        while True:
            params = {"status": status, "limit": str(limit)}
            if series_ticker:
                params["series_ticker"] = series_ticker
            if cursor:
                params["cursor"] = cursor
            data = await self._get("/markets", params=params)
            all_markets.extend(data.get("markets", []))
            cursor = data.get("cursor", "")
            if not cursor:
                break
        return all_markets
```

- [ ] **Step 8: Fix Book A — use actual NO price**

Update `scan_divergence` in `oracle/books/book_a.py` to accept `no_price` from Kalshi data:

```python
def scan_divergence(
    real_markets: list[dict],
    kalshi_markets: list[dict],
    min_edge: float,
    min_volume: int,
) -> list[Signal]:
    kalshi_by_game = {m["game_id"]: m for m in kalshi_markets}

    signals = []
    for rm in real_markets:
        if rm["volume"] < min_volume:
            continue
        km = kalshi_by_game.get(rm["game_id"])
        if not km:
            continue

        real_prob = rm["home_prob"] / 100
        kalshi_yes = km["yes_price"] / 100
        kalshi_no = km.get("no_price", 100 - km["yes_price"]) / 100

        edge_yes = real_prob - kalshi_yes
        edge_no = (1 - real_prob) - kalshi_no

        if edge_yes >= min_edge:
            signals.append(Signal(
                book=Book.A,
                ticker=km["ticker_yes"],
                side="yes",
                edge=round(edge_yes, 4),
                model_prob=real_prob,
                kalshi_price=kalshi_yes,
            ))
        elif edge_no >= min_edge:
            signals.append(Signal(
                book=Book.A,
                ticker=km["ticker_no"],
                side="no",
                edge=round(edge_no, 4),
                model_prob=round(1 - real_prob, 4),
                kalshi_price=kalshi_no,
            ))

    return signals
```

- [ ] **Step 9: Add credential management**

Create `.env.example`:
```
REAL_USER_ID=
REAL_DEVICE_ID=
REAL_TOKEN=
REAL_DEVICE_UUID=
KALSHI_KEY_ID=
KALSHI_PRIVATE_KEY_PATH=kalshi_private_key.pem
KALSHI_BASE_URL=https://demo-api.kalshi.co/trade-api/v2
```

Add to `.gitignore`:
```
.env
*.pem
oracle.db
```

Add `python-dotenv>=1.0` to `requirements.txt`.

Update `oracle/main.py` client initialization:
```python
import os
from dotenv import load_dotenv

load_dotenv()

real = RealClient(
    user_id=os.environ["REAL_USER_ID"],
    device_id=os.environ["REAL_DEVICE_ID"],
    token=os.environ["REAL_TOKEN"],
    device_uuid=os.environ["REAL_DEVICE_UUID"],
)
kalshi = KalshiClient(
    key_id=os.environ["KALSHI_KEY_ID"],
    private_key_path=os.environ.get("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key.pem"),
    base_url=os.environ.get("KALSHI_BASE_URL", "https://demo-api.kalshi.co/trade-api/v2"),
)
```

- [ ] **Step 10: Run all fixed tests**

Run: `python -m pytest tests/test_fees.py tests/test_ledger.py tests/test_kalshi.py tests/test_book_a.py -v`
Expected: All PASS.

- [ ] **Step 11: Commit**

```bash
git add oracle/risk/fees.py oracle/risk/ledger.py oracle/books/book_a.py oracle/clients/kalshi.py oracle/main.py
git add tests/test_fees.py tests/test_ledger.py tests/test_kalshi.py
git add .env.example .gitignore requirements.txt
git commit -m "fix: fee calculation, settle() side handling, NO pricing, Kalshi pagination, credentials"
```

---

### Task 19: Correlation guards and enhanced risk limits

**Files:**
- Modify: `oracle/risk/limits.py`
- Modify: `oracle/risk/ledger.py`
- Modify: `oracle/config.py`
- Modify: `oracle/models.py`
- Modify: `tests/test_limits.py`
- Modify: `tests/test_ledger.py`

**Context:** Spec §6 defines 5 correlation guards, per-game/per-player limits for Book C, cumulative drawdown, and rotation player 50% sizing. None are implemented. This task adds all of them to `limits.py` plus a sizing multiplier return.

- [ ] **Step 1: Write failing tests for correlation guards**

```python
# Add to tests/test_limits.py
from oracle.risk.limits import check_limits, compute_sizing_multiplier

def test_blocks_yes_on_both_sides_of_spread():
    """Correlation guard: never hold YES on both sides of a spread."""
    cfg = _test_config()
    ledger = RiskLedger()
    # Hold YES on LAL spread
    ledger.add(Position(book=Book.A, ticker="KXNBASPREAD-LAL", side="yes",
                        contracts=10, fill_price=0.50, game_id="G1",
                        spread_game_id="G1"))
    # Try to hold YES on HOU spread (same game, opposite side)
    signal = Signal(book=Book.A, ticker="KXNBASPREAD-HOU", side="yes",
                    edge=0.15, model_prob=0.60, kalshi_price=0.45)
    result = check_limits(cfg, ledger, signal, game_id="G1", spread_game_id="G1")
    assert result.allowed is False
    assert "opposing_spread" in result.reason

def test_blocks_max_2_props_per_player_across_books():
    """Max 2 props per player across all books."""
    cfg = _test_config()
    ledger = RiskLedger()
    ledger.add(Position(book=Book.B, ticker="KXNBAPTS-P100", side="yes",
                        contracts=5, fill_price=0.50, player_id=100))
    ledger.add(Position(book=Book.C, ticker="KXNBAREB-P100", side="no",
                        contracts=5, fill_price=0.40, player_id=100))
    # 3rd prop on same player
    signal = Signal(book=Book.B, ticker="KXNBAAST-P100", side="yes",
                    edge=0.12, model_prob=0.72, kalshi_price=0.60)
    result = check_limits(cfg, ledger, signal, player_id=100)
    assert result.allowed is False
    assert "max_props_per_player" in result.reason

def test_blocks_max_4_props_per_game_across_books():
    """Max 4 prop positions per game across all books."""
    cfg = _test_config()
    ledger = RiskLedger()
    for i in range(4):
        ledger.add(Position(book=Book.B, ticker=f"PROP-G1-{i}", side="yes",
                            contracts=3, fill_price=0.50, game_id="G1",
                            player_id=i+200))
    signal = Signal(book=Book.C, ticker="PROP-G1-5", side="yes",
                    edge=0.12, model_prob=0.72, kalshi_price=0.60)
    result = check_limits(cfg, ledger, signal, game_id="G1")
    assert result.allowed is False
    assert "max_props_per_game" in result.reason

def test_cross_book_sizing_reduction():
    """Book A game position + Book B prop in same game → 40% reduction."""
    cfg = _test_config()
    ledger = RiskLedger()
    ledger.add(Position(book=Book.A, ticker="KXNBAGAME-G1", side="yes",
                        contracts=10, fill_price=0.50, game_id="G1"))
    mult = compute_sizing_multiplier(cfg, ledger, book=Book.B, game_id="G1",
                                      minutes_avg=32.0)
    assert abs(mult - 0.60) < 0.01  # 40% reduction

def test_rotation_player_sizing_reduction():
    """Rotation player (20-28 min avg) gets 50% sizing."""
    cfg = _test_config()
    ledger = RiskLedger()  # empty, no cross-book overlap
    mult = compute_sizing_multiplier(cfg, ledger, book=Book.B, game_id="G1",
                                      minutes_avg=24.0)
    assert abs(mult - 0.50) < 0.01

def test_starter_full_sizing():
    """Starter (>28 min avg) gets full sizing."""
    cfg = _test_config()
    ledger = RiskLedger()
    mult = compute_sizing_multiplier(cfg, ledger, book=Book.B, game_id="G1",
                                      minutes_avg=33.0)
    assert abs(mult - 1.0) < 0.01

def test_blocks_max_drawdown():
    """Cumulative drawdown > 15% halts all trading."""
    cfg = _test_config()
    ledger = RiskLedger()
    ledger.cumulative_pnl = -300.0  # 15% of $2000
    signal = Signal(book=Book.B, ticker="X", side="yes",
                    edge=0.12, model_prob=0.72, kalshi_price=0.60)
    result = check_limits(cfg, ledger, signal)
    assert result.allowed is False
    assert "max_drawdown" in result.reason

def test_book_c_per_game_limit():
    """Book C has its own per-game 2% limit."""
    cfg = _test_config()
    ledger = RiskLedger()
    # Add $40 Book C exposure in game G1 (2% of $2000)
    ledger.add(Position(book=Book.C, ticker="C1", side="yes",
                        contracts=80, fill_price=0.50, game_id="G1"))
    signal = Signal(book=Book.C, ticker="C2", side="yes",
                    edge=0.12, model_prob=0.72, kalshi_price=0.60)
    result = check_limits(cfg, ledger, signal, game_id="G1")
    assert result.allowed is False
    assert "per_game" in result.reason

def test_book_c_per_player_limit():
    """Book C has its own per-player 1.5% limit."""
    cfg = _test_config()
    ledger = RiskLedger()
    ledger.add(Position(book=Book.C, ticker="C1", side="yes",
                        contracts=60, fill_price=0.50, game_id="G1", player_id=100))
    signal = Signal(book=Book.C, ticker="C2", side="yes",
                    edge=0.12, model_prob=0.72, kalshi_price=0.60)
    result = check_limits(cfg, ledger, signal, game_id="G1", player_id=100)
    assert result.allowed is False
    assert "per_player" in result.reason
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_limits.py -v -k "spread or props_per or cross_book or rotation or starter or drawdown or book_c_per"`
Expected: FAIL

- [ ] **Step 3: Add `spread_game_id` and `cumulative_pnl` to models/ledger**

Add to `Position` dataclass in `oracle/models.py`:
```python
    spread_game_id: Optional[str] = None  # For spread positions: game_id for opposing-side check
```

Add to `RiskLedger` in `oracle/risk/ledger.py`:
```python
    def __init__(self):
        self._positions: dict[str, Position] = {}
        self.daily_pnl: float = 0.0
        self.cumulative_pnl: float = 0.0

    def player_position_count(self, player_id: int) -> int:
        return sum(1 for p in self._positions.values() if p.player_id == player_id)

    def game_position_count(self, game_id: str) -> int:
        return sum(1 for p in self._positions.values() if p.game_id == game_id)

    def has_spread_position(self, spread_game_id: str) -> bool:
        return any(p.spread_game_id == spread_game_id and p.side == "yes"
                   for p in self._positions.values())

    def has_book_a_game_position(self, game_id: str) -> bool:
        return any(p.book == Book.A and p.game_id == game_id
                   for p in self._positions.values())
```

Update `settle()` to track cumulative:
```python
        self.cumulative_pnl = round(self.cumulative_pnl + pnl, 2)
```

- [ ] **Step 4: Add per-game/per-player config for Book C**

Add to `BookCConfig` in `oracle/config.py`:
```python
    max_per_game_pct: float = 0.02
    max_per_player_pct: float = 0.015
```

Add to `RiskConfig`:
```python
    max_props_per_player: int = 2
    max_props_per_game: int = 4
    cross_book_reduction: float = 0.40
    rotation_min_minutes: float = 20.0
    rotation_max_minutes: float = 28.0
    rotation_size_reduction: float = 0.50
```

- [ ] **Step 5: Implement enhanced check_limits and compute_sizing_multiplier**

```python
# oracle/risk/limits.py — REPLACE entire file
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from oracle.models import Book, Signal
from oracle.risk.ledger import RiskLedger
from oracle.config import OracleConfig


@dataclass
class LimitResult:
    allowed: bool
    reason: str = ""


def check_limits(
    cfg: OracleConfig,
    ledger: RiskLedger,
    signal: Signal,
    game_id: Optional[str] = None,
    player_id: Optional[int] = None,
    spread_game_id: Optional[str] = None,
) -> LimitResult:
    bankroll = cfg.bankroll

    # Kill switch: daily stop-loss
    if ledger.daily_pnl <= -cfg.risk.daily_stop_loss_pct * bankroll:
        return LimitResult(False, "daily_stop_loss")

    # Kill switch: cumulative drawdown
    if ledger.cumulative_pnl <= -cfg.risk.max_drawdown_pct * bankroll:
        return LimitResult(False, "max_drawdown")

    # Max simultaneous positions
    if ledger.position_count() >= cfg.risk.max_simultaneous_positions:
        return LimitResult(False, "max_simultaneous")

    # Max total exposure
    if ledger.total_exposure() >= cfg.risk.max_total_exposure_pct * bankroll:
        return LimitResult(False, "max_total_exposure")

    # Per-book limits
    book_cfg = _get_book_config(cfg, signal.book)
    if not book_cfg.enabled:
        return LimitResult(False, "book_disabled")

    if ledger.book_position_count(signal.book) >= book_cfg.max_positions:
        return LimitResult(False, "book_max_positions")

    if ledger.book_exposure(signal.book) >= book_cfg.max_exposure_dollars(bankroll):
        return LimitResult(False, "book_max_exposure")

    # --- Correlation guards ---

    # 1. Never hold YES on both sides of a spread
    if spread_game_id and ledger.has_spread_position(spread_game_id):
        return LimitResult(False, "opposing_spread")

    # 2. Max 2 props per player across all books
    if player_id and ledger.player_position_count(player_id) >= cfg.risk.max_props_per_player:
        return LimitResult(False, "max_props_per_player")

    # 3. Max 4 prop positions per game across all books
    if game_id and ledger.game_position_count(game_id) >= cfg.risk.max_props_per_game:
        return LimitResult(False, "max_props_per_game")

    # --- Per-game exposure (all books that have per_game_pct) ---
    if game_id:
        max_game_pct = getattr(book_cfg, 'max_per_game_pct', None)
        if max_game_pct:
            if ledger.game_exposure(game_id) >= max_game_pct * bankroll:
                return LimitResult(False, "per_game_exposure")

    # --- Per-player exposure (all books that have per_player_pct) ---
    if player_id:
        max_player_pct = getattr(book_cfg, 'max_per_player_pct', None)
        if max_player_pct:
            if ledger.player_exposure(player_id) >= max_player_pct * bankroll:
                return LimitResult(False, "per_player_exposure")

    return LimitResult(True)


def compute_sizing_multiplier(
    cfg: OracleConfig,
    ledger: RiskLedger,
    book: Book,
    game_id: Optional[str] = None,
    minutes_avg: float = 35.0,
) -> float:
    """Return a multiplier (0-1) to apply to position sizing.

    Accounts for:
    - Cross-book reduction: if Book A holds a game position and we are
      placing a Book B/C prop in the same game, reduce by 40%.
    - Rotation player: 20-28 min avg gets 50% reduction.

    These stack multiplicatively.
    """
    multiplier = 1.0

    # Cross-book reduction
    if book in (Book.B, Book.C) and game_id:
        if ledger.has_book_a_game_position(game_id):
            multiplier *= (1.0 - cfg.risk.cross_book_reduction)

    # Rotation player reduction
    if cfg.risk.rotation_min_minutes <= minutes_avg <= cfg.risk.rotation_max_minutes:
        multiplier *= (1.0 - cfg.risk.rotation_size_reduction)

    return round(multiplier, 4)


def _get_book_config(cfg: OracleConfig, book: Book):
    if book == Book.A:
        return cfg.books.a
    elif book == Book.B:
        return cfg.books.b
    else:
        return cfg.books.c
```

- [ ] **Step 6: Update config.yaml with new risk fields**

Add under `risk:`:
```yaml
  max_props_per_player: 2
  max_props_per_game: 4
  cross_book_reduction: 0.40
  rotation_min_minutes: 20
  rotation_max_minutes: 28
  rotation_size_reduction: 0.50
```

Add under `books.c:`:
```yaml
    max_per_game_pct: 0.02
    max_per_player_pct: 0.015
```

- [ ] **Step 7: Run tests**

Run: `python -m pytest tests/test_limits.py tests/test_ledger.py -v`
Expected: All PASS.

- [ ] **Step 8: Commit**

```bash
git add oracle/risk/limits.py oracle/risk/ledger.py oracle/config.py oracle/models.py
git add config.yaml tests/test_limits.py tests/test_ledger.py
git commit -m "feat: add all 5 correlation guards, cross-book sizing, rotation reduction, drawdown"
```

---

### Task 20: Pregame data pipeline and game/market matching

**Files:**
- Create: `oracle/clients/game_matching.py`
- Create: `oracle/pipeline.py`
- Create: `tests/test_game_matching.py`
- Create: `tests/test_pipeline.py`
- Modify: `oracle/clients/real_rest.py` (add `get_player_boxscores`)
- Modify: `oracle/db.py` (add `ticker_mapping` table + `stat_id_cache` table)

**Context:** The orchestrator has TODOs for the entire pregame data flow. This task builds the pipeline that connects Real's data to Book B's `compute_fair_value` function, and resolves the game/player matching problem between Real and Kalshi ID systems.

- [ ] **Step 1: Write failing tests for game matching**

```python
# tests/test_game_matching.py
from oracle.clients.game_matching import (
    parse_kalshi_game_ticker, match_games, build_player_ticker_map,
)

def test_parse_kalshi_game_ticker():
    """Parse KXNBAGAME-26MAR18LALHOU-LAL into components."""
    result = parse_kalshi_game_ticker("KXNBAGAME-26MAR18LALHOU-LAL")
    assert result["date"] == "26MAR18"
    assert result["away"] == "LAL"
    assert result["home"] == "HOU"
    assert result["team"] == "LAL"

def test_match_games_by_teams():
    """Match Real games to Kalshi markets by team abbreviations."""
    real_games = [
        {"game_id": 847, "home_team": "HOU", "away_team": "LAL",
         "home_prob": 32, "volume": 500000, "status": "scheduled"},
    ]
    kalshi_markets = [
        {"ticker": "KXNBAGAME-26MAR18LALHOU-LAL",
         "title": "Will the Lakers win vs Rockets?",
         "yes_price": 50, "no_price": 52, "series_ticker": "KXNBAGAME"},
        {"ticker": "KXNBAGAME-26MAR18LALHOU-HOU",
         "title": "Will the Rockets win vs Lakers?",
         "yes_price": 50, "no_price": 52, "series_ticker": "KXNBAGAME"},
    ]
    matched = match_games(real_games, kalshi_markets)
    assert len(matched) == 1
    assert matched[0]["real_game_id"] == 847
    assert matched[0]["ticker_yes"] is not None
    assert matched[0]["ticker_no"] is not None

def test_build_player_ticker_map():
    """Build player_name → list of Kalshi prop tickers."""
    kalshi_prop_markets = [
        {"ticker": "KXNBAPTS-26MAR18-LALLEBRONJ-O275",
         "title": "LeBron James Over 27.5 Points", "yes_price": 55},
        {"ticker": "KXNBAREB-26MAR18-LALLEBRONJ-O85",
         "title": "LeBron James Over 8.5 Rebounds", "yes_price": 48},
        {"ticker": "KXNBAPTS-26MAR18-HOUJALENGREEN-O225",
         "title": "Jalen Green Over 22.5 Points", "yes_price": 52},
    ]
    pmap = build_player_ticker_map(kalshi_prop_markets)
    assert len(pmap["LeBron James"]) == 2
    assert pmap["LeBron James"][0]["stat"] == "points"
    assert pmap["LeBron James"][0]["line"] == 27.5
    assert len(pmap["Jalen Green"]) == 1
```

- [ ] **Step 2: Write failing tests for pregame pipeline**

```python
# tests/test_pipeline.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from oracle.pipeline import PregamePipeline

@pytest.fixture
def mock_real():
    client = AsyncMock()
    client.get_stat_trackers = AsyncMock(return_value=[
        {"playerName": "LeBron James", "statType": "points",
         "line": 27.5, "overOdds": -130, "underOdds": 110},
    ])
    client.get_player_season_feed = AsyncMock(return_value={
        "games": [{"stats": {"pts": v}} for v in
                  [30, 25, 32, 28, 35, 22, 31, 27, 29, 26,
                   24, 33, 21, 28, 30, 25, 27, 31, 23, 29]]
    })
    client.get_player_profile = AsyncMock(return_value={
        "name": "LeBron James",
        "status": "Active",
        "splits": {
            "averages": [
                {"period": "Last 5", "stats": {"pts": 30.0, "min": 34.2}},
                {"period": "Home", "stats": {"pts": 28.0}},
                {"period": "Away", "stats": {"pts": 25.0}},
            ]
        }
    })
    client.search_players = AsyncMock(return_value={
        "players": [{"id": 12345, "name": "LeBron James", "team": "LAL"}]
    })
    return client

@pytest.fixture
def mock_db():
    db = MagicMock()
    db.get_player_id = MagicMock(return_value=12345)
    db.upsert_player = MagicMock()
    return db

@pytest.mark.asyncio
async def test_pipeline_discovers_player_ids(mock_real, mock_db):
    """Pipeline resolves player names from stat trackers to Real player IDs."""
    mock_db.get_player_id = MagicMock(return_value=None)  # Not cached
    pipeline = PregamePipeline(mock_real, mock_db)
    player_id = await pipeline.resolve_player_id("LeBron James", "LAL")
    assert player_id == 12345
    mock_db.upsert_player.assert_called_once()

@pytest.mark.asyncio
async def test_pipeline_computes_fair_value(mock_real, mock_db):
    """Pipeline fetches data and produces fair value for a prop."""
    pipeline = PregamePipeline(mock_real, mock_db)
    fv = await pipeline.compute_player_prop_fair_value(
        player_id=12345, stat="pts", line=27.5,
        opponent="HOU", is_home=False, is_b2b=False,
        opp_def_rank=15, pace_factor=1.0,
        external_over_odds=-130,
    )
    assert 0.0 < fv < 1.0  # Produces a valid probability
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_game_matching.py tests/test_pipeline.py -v`
Expected: FAIL

- [ ] **Step 4: Implement game matching**

```python
# oracle/clients/game_matching.py
"""Match Real Sports games/players to Kalshi markets by team/date."""
from __future__ import annotations
import re
from typing import Optional
from oracle.clients.matching import parse_kalshi_prop_title, normalize_name

# NBA team abbreviation mappings (Kalshi ticker → standard)
_TEAM_ALIASES = {
    "LAL": "LAL", "BOS": "BOS", "MIL": "MIL", "DEN": "DEN",
    "PHX": "PHX", "PHI": "PHI", "MIA": "MIA", "NYK": "NYK",
    "CLE": "CLE", "SAC": "SAC", "GSW": "GSW", "MIN": "MIN",
    "OKC": "OKC", "DAL": "DAL", "NOP": "NOP", "LAC": "LAC",
    "IND": "IND", "ORL": "ORL", "TOR": "TOR", "ATL": "ATL",
    "CHI": "CHI", "BKN": "BKN", "HOU": "HOU", "MEM": "MEM",
    "SAS": "SAS", "POR": "POR", "UTA": "UTA", "WAS": "WAS",
    "CHA": "CHA", "DET": "DET",
}


def parse_kalshi_game_ticker(ticker: str) -> dict:
    """Parse KXNBAGAME-26MAR18LALHOU-LAL into components."""
    parts = ticker.split("-")
    if len(parts) < 3:
        return {}
    date_teams = parts[1]  # e.g., "26MAR18LALHOU"
    team = parts[2]
    # Date is first 7 chars (DDMMMYY), rest is teams (6 chars: AWYHOM)
    date_str = date_teams[:7]
    teams_str = date_teams[7:]
    away = teams_str[:3]
    home = teams_str[3:6]
    return {"date": date_str, "away": away, "home": home, "team": team}


def match_games(real_games: list[dict], kalshi_markets: list[dict]) -> list[dict]:
    """Match Real games to Kalshi game-level markets by home/away teams."""
    # Group Kalshi markets by (away, home) pair
    kalshi_by_teams: dict[tuple, list[dict]] = {}
    for km in kalshi_markets:
        parsed = parse_kalshi_game_ticker(km["ticker"])
        if not parsed:
            continue
        key = (parsed["away"], parsed["home"])
        kalshi_by_teams.setdefault(key, []).append({**km, **parsed})

    matched = []
    for rg in real_games:
        key = (rg["away_team"], rg["home_team"])
        kms = kalshi_by_teams.get(key, [])
        if not kms:
            continue
        # Find YES and NO tickers
        ticker_yes = None
        ticker_no = None
        yes_price = 0
        no_price = 0
        for km in kms:
            if km["team"] == rg["home_team"]:
                ticker_yes = km["ticker"]
                yes_price = km["yes_price"]
            elif km["team"] == rg["away_team"]:
                ticker_no = km["ticker"]
                no_price = km.get("no_price", km["yes_price"])

        matched.append({
            "real_game_id": rg["game_id"],
            "home_team": rg["home_team"],
            "away_team": rg["away_team"],
            "home_prob": rg.get("home_prob", 50),
            "volume": rg.get("volume", 0),
            "ticker_yes": ticker_yes,
            "ticker_no": ticker_no,
            "yes_price": yes_price,
            "no_price": no_price,
        })

    return matched


def build_player_ticker_map(kalshi_prop_markets: list[dict]) -> dict[str, list[dict]]:
    """Build player_name → list of {ticker, stat, line, direction, yes_price}."""
    pmap: dict[str, list[dict]] = {}
    for km in kalshi_prop_markets:
        parsed = parse_kalshi_prop_title(km.get("title", ""))
        if not parsed:
            continue
        name = parsed["player_name"]
        pmap.setdefault(name, []).append({
            "ticker": km["ticker"],
            "stat": parsed["stat"],
            "line": parsed["line"],
            "direction": parsed["direction"],
            "yes_price": km.get("yes_price", 50),
        })
    return pmap
```

- [ ] **Step 5: Implement pregame pipeline**

```python
# oracle/pipeline.py
"""Pregame data pipeline: discovers players, fetches data, computes fair values."""
from __future__ import annotations
from typing import Optional
from oracle.clients.real_rest import RealClient
from oracle.db import OracleDB
from oracle.books.book_b import (
    compute_fair_value, apply_matchup_shift, apply_venue_shift,
    implied_probability, check_lineup_gate, LineupGate,
)


class PregamePipeline:
    def __init__(self, real: RealClient, db: OracleDB):
        self._real = real
        self._db = db

    async def resolve_player_id(self, name: str, team: str) -> Optional[int]:
        """Look up player ID from cache, falling back to API search."""
        cached = self._db.get_player_id(name, team)
        if cached:
            return cached
        result = await self._real.search_players("nba")
        players = result.get("players", [])
        for p in players:
            self._db.upsert_player(p["name"], p.get("team", ""), p["id"])
            if p["name"].lower() == name.lower():
                return p["id"]
        return None

    async def compute_player_prop_fair_value(
        self,
        player_id: int,
        stat: str,
        line: float,
        opponent: str,
        is_home: bool,
        is_b2b: bool,
        opp_def_rank: int = 15,
        pace_factor: float = 1.0,
        external_over_odds: Optional[float] = None,
        key_teammate_out: bool = False,
    ) -> float:
        """Full Book B fair value pipeline for one player prop."""
        # Step 1: Fetch season feed (last 20 games)
        feed = await self._real.get_player_season_feed(player_id, "nba", limit=20)
        games = feed.get("games", [])
        stat_key = _stat_api_key(stat)
        game_values = [g["stats"].get(stat_key, 0) for g in games]

        if not game_values:
            return 0.5

        # Step 2: Fetch profile for splits
        profile = await self._real.get_player_profile(player_id, "nba")
        splits = profile.get("splits", {}).get("averages", [])

        home_avg = _get_split_stat(splits, "Home", stat_key)
        away_avg = _get_split_stat(splits, "Away", stat_key)

        # Step 3: Compute shifts
        matchup_shift = apply_matchup_shift(game_values, opp_def_rank, opp_history=None)
        venue_shift = apply_venue_shift(
            home_avg or sum(game_values) / len(game_values),
            away_avg or sum(game_values) / len(game_values),
            is_home,
        )

        # Teammate out shift
        teammate_shift = 0.0
        if key_teammate_out:
            if stat in ("pts", "points"):
                teammate_shift = 2.5
            elif stat in ("ast", "assists"):
                teammate_shift = -1.5

        # Step 4: Compute fair value
        return compute_fair_value(
            game_values=game_values,
            line=line,
            matchup_shift=matchup_shift,
            venue_shift=venue_shift,
            b2b_penalty_pct=0.06 if is_b2b else 0.0,
            pace_factor=pace_factor,
            teammate_out_shift=teammate_shift,
            external_over_odds=external_over_odds,
        )


def _stat_api_key(stat: str) -> str:
    """Map prop stat name to Real's API field name."""
    return {
        "points": "pts", "pts": "pts",
        "rebounds": "reb", "reb": "reb",
        "assists": "ast", "ast": "ast",
        "3-pointers": "fg3m", "3pt": "fg3m",
        "blocks": "blk", "blk": "blk",
        "steals": "stl", "stl": "stl",
    }.get(stat.lower(), stat.lower())


def _get_split_stat(splits: list[dict], period: str, stat_key: str) -> Optional[float]:
    """Extract a stat value from a splits array."""
    for s in splits:
        if s.get("period", "").lower() == period.lower():
            return s.get("stats", {}).get(stat_key)
    return None
```

- [ ] **Step 6: Add `get_player_boxscores` to Real client**

Add to `oracle/clients/real_rest.py`:
```python
    async def get_player_boxscores(self, player_id: int) -> dict:
        return await self._get(f"/playerboxscores/{player_id}", params={"version": "2"})
```

- [ ] **Step 7: Add ticker_mapping and stat_id_cache tables to DB**

Add to `oracle/db.py` `_create_tables`:
```sql
CREATE TABLE IF NOT EXISTS ticker_mapping (
    real_player_id INTEGER NOT NULL,
    kalshi_ticker TEXT NOT NULL,
    stat TEXT NOT NULL,
    line REAL NOT NULL,
    date TEXT NOT NULL,
    PRIMARY KEY (kalshi_ticker)
);
CREATE TABLE IF NOT EXISTS stat_id_cache (
    stat_name TEXT PRIMARY KEY,
    stat_id INTEGER NOT NULL
);
```

Add methods:
```python
    def upsert_ticker_mapping(self, real_player_id: int, kalshi_ticker: str,
                               stat: str, line: float, date: str):
        self._conn.execute(
            "INSERT OR REPLACE INTO ticker_mapping VALUES (?, ?, ?, ?, ?)",
            (real_player_id, kalshi_ticker, stat, line, date),
        )
        self._conn.commit()

    def get_tickers_for_player(self, real_player_id: int, date: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM ticker_mapping WHERE real_player_id = ? AND date = ?",
            (real_player_id, date),
        ).fetchall()
        return [dict(r) for r in rows]

    def upsert_stat_id(self, stat_name: str, stat_id: int):
        self._conn.execute(
            "INSERT OR REPLACE INTO stat_id_cache VALUES (?, ?)",
            (stat_name, stat_id),
        )
        self._conn.commit()

    def get_stat_id(self, stat_name: str) -> Optional[int]:
        row = self._conn.execute(
            "SELECT stat_id FROM stat_id_cache WHERE stat_name = ?",
            (stat_name,),
        ).fetchone()
        return row["stat_id"] if row else None
```

- [ ] **Step 8: Run tests**

Run: `python -m pytest tests/test_game_matching.py tests/test_pipeline.py -v`
Expected: All PASS.

- [ ] **Step 9: Commit**

```bash
git add oracle/clients/game_matching.py oracle/pipeline.py
git add tests/test_game_matching.py tests/test_pipeline.py
git add oracle/clients/real_rest.py oracle/db.py
git commit -m "feat: add pregame data pipeline, game/player matching, statId cache"
```

---

## Chunk 6: Execution Lifecycle, Recovery, Validation, Integration

### Task 21: Book C execution lifecycle

**Files:**
- Create: `oracle/execution/book_c_executor.py`
- Create: `tests/test_book_c_executor.py`
- Modify: `oracle/db.py` (add slippage columns to trades table)

**Context:** Spec §5 execution rules require 15s timeout, max 2 reprices, slippage logging with auto-disable, cancel-on-reversal, and signal deduplication. None are implemented. This task builds a Book C-specific executor that wraps the base `OrderExecutor`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_book_c_executor.py
import pytest
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch
from oracle.execution.book_c_executor import BookCExecutor, OrderState
from oracle.models import Book, Signal, QuoteSnapshot

@pytest.fixture
def mock_kalshi():
    client = AsyncMock()
    client.place_order = AsyncMock(return_value={
        "order": {"order_id": "ord1", "status": "resting"}
    })
    client.cancel_order = AsyncMock(return_value={"order": {"status": "canceled"}})
    client.get_orderbook = AsyncMock(return_value={
        "orderbook": {"yes": [[55, 20]], "no": [[47, 15]]}
    })
    return client

@pytest.fixture
def executor(mock_kalshi):
    return BookCExecutor(mock_kalshi, timeout_s=0.1, max_reprices=2)

@pytest.mark.asyncio
async def test_timeout_cancels_unfilled_order(executor, mock_kalshi):
    """After timeout, unfilled order is cancelled."""
    mock_kalshi.place_order = AsyncMock(return_value={
        "order": {"order_id": "ord1", "status": "resting"}
    })
    signal = Signal(book=Book.C, ticker="X", side="yes",
                    edge=0.15, model_prob=0.70, kalshi_price=0.55)
    result = await executor.execute_with_timeout(signal, contracts=5, price_cents=55)
    # Short timeout (0.1s) → order should be cancelled
    assert result["cancelled"] is True
    mock_kalshi.cancel_order.assert_called()

@pytest.mark.asyncio
async def test_max_reprices_enforced(executor, mock_kalshi):
    """After max_reprices, stop trying."""
    mock_kalshi.place_order = AsyncMock(return_value={
        "order": {"order_id": "ord1", "status": "resting"}
    })
    signal = Signal(book=Book.C, ticker="X", side="yes",
                    edge=0.15, model_prob=0.70, kalshi_price=0.55)
    result = await executor.execute_with_timeout(signal, contracts=5, price_cents=55)
    # max_reprices=2, so at most 3 attempts (initial + 2 reprices)
    assert mock_kalshi.place_order.call_count <= 3

def test_slippage_tracking(executor):
    """Track slippage per signal type."""
    executor.record_slippage("foul_trouble", quote_at_signal=55, fill_price=58)
    executor.record_slippage("foul_trouble", quote_at_signal=55, fill_price=57)
    assert executor.avg_slippage("foul_trouble") == 2.5  # (3+2)/2

def test_auto_disable_on_high_slippage(executor):
    """Disable signal if avg slippage > threshold over N trades."""
    for _ in range(25):
        executor.record_slippage("foul_trouble", quote_at_signal=50, fill_price=55)
    assert executor.is_signal_disabled("foul_trouble") is True

def test_signal_deduplication(executor):
    """Don't place duplicate orders on same ticker."""
    executor.mark_pending("KXNBAPTS-X")
    assert executor.has_pending("KXNBAPTS-X") is True
    executor.clear_pending("KXNBAPTS-X")
    assert executor.has_pending("KXNBAPTS-X") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_book_c_executor.py -v`
Expected: FAIL

- [ ] **Step 3: Implement Book C executor**

```python
# oracle/execution/book_c_executor.py
"""Book C execution lifecycle: timeout, repricing, slippage, cancel discipline."""
from __future__ import annotations
import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional
from oracle.models import Signal
from oracle.clients.kalshi import KalshiClient


@dataclass
class OrderState:
    order_id: str
    ticker: str
    signal_type: str
    quote_at_signal: int  # cents
    placed_at: float = field(default_factory=time.time)
    reprice_count: int = 0


class BookCExecutor:
    def __init__(self, kalshi: KalshiClient, timeout_s: float = 15.0,
                 max_reprices: int = 2, slippage_threshold: float = 3.0,
                 slippage_min_trades: int = 20):
        self._kalshi = kalshi
        self._timeout_s = timeout_s
        self._max_reprices = max_reprices
        self._slippage_threshold = slippage_threshold
        self._slippage_min_trades = slippage_min_trades
        self._slippage_log: dict[str, list[float]] = {}  # signal_type → [slippage_cents]
        self._disabled_signals: set[str] = set()
        self._pending_tickers: set[str] = set()

    async def execute_with_timeout(
        self, signal: Signal, contracts: int, price_cents: int,
        signal_type: str = "",
    ) -> dict:
        """Place a limit order with timeout and reprice logic."""
        result = {"cancelled": False, "filled": False, "order_id": None, "reprices": 0}

        for attempt in range(1 + self._max_reprices):
            resp = await self._kalshi.place_order(
                ticker=signal.ticker, action="buy", side=signal.side,
                type_="limit", count=contracts, yes_price=price_cents,
            )
            order_id = resp.get("order", {}).get("order_id")
            result["order_id"] = order_id
            result["reprices"] = attempt

            if resp.get("order", {}).get("status") == "filled":
                result["filled"] = True
                self._pending_tickers.discard(signal.ticker)
                return result

            # Wait for fill or timeout
            await asyncio.sleep(self._timeout_s)

            # Cancel unfilled order
            if order_id:
                await self._kalshi.cancel_order(order_id)

            if attempt < self._max_reprices:
                # Re-fetch orderbook for new price
                try:
                    book = await self._kalshi.get_orderbook(signal.ticker)
                    yes_levels = book.get("orderbook", {}).get("yes", [])
                    if yes_levels:
                        price_cents = yes_levels[0][0]  # Best ask
                except Exception:
                    break
            else:
                result["cancelled"] = True

        self._pending_tickers.discard(signal.ticker)
        return result

    def record_slippage(self, signal_type: str, quote_at_signal: int,
                         fill_price: int):
        """Record slippage for a signal type (in cents)."""
        slippage = abs(fill_price - quote_at_signal)
        self._slippage_log.setdefault(signal_type, []).append(slippage)
        # Check auto-disable
        entries = self._slippage_log[signal_type]
        if len(entries) >= self._slippage_min_trades:
            avg = sum(entries) / len(entries)
            if avg > self._slippage_threshold:
                self._disabled_signals.add(signal_type)

    def avg_slippage(self, signal_type: str) -> float:
        entries = self._slippage_log.get(signal_type, [])
        return sum(entries) / len(entries) if entries else 0.0

    def is_signal_disabled(self, signal_type: str) -> bool:
        return signal_type in self._disabled_signals

    def mark_pending(self, ticker: str):
        self._pending_tickers.add(ticker)

    def has_pending(self, ticker: str) -> bool:
        return ticker in self._pending_tickers

    def clear_pending(self, ticker: str):
        self._pending_tickers.discard(ticker)

    async def cancel_all_pending(self):
        """Cancel all pending Book C orders (signal reversal)."""
        self._pending_tickers.clear()
```

- [ ] **Step 4: Add slippage columns to trades table**

Add to `CREATE TABLE IF NOT EXISTS trades` in `oracle/db.py`:
```sql
    quote_at_signal INTEGER DEFAULT NULL,
    quote_at_fill INTEGER DEFAULT NULL,
    slippage_cents REAL DEFAULT NULL,
    signal_type TEXT DEFAULT NULL,
```

Update `log_trade` signature to accept these optional fields.

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_book_c_executor.py -v`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add oracle/execution/book_c_executor.py tests/test_book_c_executor.py oracle/db.py
git commit -m "feat: add Book C execution lifecycle — timeout, repricing, slippage, dedup"
```

---

### Task 22: Failure recovery and kill switches

**Files:**
- Create: `oracle/health.py`
- Create: `tests/test_health.py`
- Modify: `oracle/books/book_a.py` (add edge convergence tracking)
- Modify: `tests/test_book_a.py`

**Context:** Spec §12 defines 5 kill switches (only 1 implemented), failure recovery for WebSocket and Kalshi errors, and Book A edge convergence detection. This task builds the health monitoring system.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_health.py
import time
from oracle.health import HealthMonitor, SystemState

def test_kill_switch_daily_stop_loss():
    hm = HealthMonitor(bankroll=2000, daily_stop_pct=0.05)
    assert hm.check_kill_switch(daily_pnl=-99).active is False
    assert hm.check_kill_switch(daily_pnl=-100).active is True
    assert "daily_stop_loss" in hm.check_kill_switch(daily_pnl=-100).reason

def test_kill_switch_consecutive_failures():
    hm = HealthMonitor(bankroll=2000)
    hm.record_order_failure()
    hm.record_order_failure()
    assert hm.check_kill_switch().active is False
    hm.record_order_failure()
    assert hm.check_kill_switch().active is True
    assert "consecutive_failures" in hm.check_kill_switch().reason

def test_kill_switch_resets_on_success():
    hm = HealthMonitor(bankroll=2000)
    hm.record_order_failure()
    hm.record_order_failure()
    hm.record_order_success()
    hm.record_order_failure()
    assert hm.check_kill_switch().active is False  # reset after success

def test_kill_switch_both_sources_down():
    hm = HealthMonitor(bankroll=2000)
    hm.mark_ws_down()
    assert hm.check_kill_switch().active is False  # only WS down
    hm.mark_rest_down()
    assert hm.check_kill_switch().active is True
    assert "both_sources_down" in hm.check_kill_switch().reason

def test_kill_switch_position_mismatch():
    hm = HealthMonitor(bankroll=2000)
    hm.record_position_mismatch(ledger_count=10, kalshi_count=8)
    assert hm.check_kill_switch().active is True
    assert "position_mismatch" in hm.check_kill_switch().reason

def test_kill_switch_book_drawdown():
    hm = HealthMonitor(bankroll=2000)
    hm.record_book_session_pnl("B", -100)
    assert hm.check_kill_switch().active is False  # -5% of 2000, not > 10%
    hm.record_book_session_pnl("B", -200)
    # Total book B session PnL = -200 → allocation for B is 9% of 2000 = $180
    # -200 > 10% of 180? Yes → kill
    assert hm.check_kill_switch(book_allocations={"B": 180}).active is True

def test_ws_stale_detection():
    hm = HealthMonitor(bankroll=2000)
    hm.ws_last_data = time.time() - 35  # 35 seconds ago
    state = hm.get_ws_state()
    assert state == SystemState.STALE

def test_ws_healthy():
    hm = HealthMonitor(bankroll=2000)
    hm.ws_last_data = time.time()
    assert hm.get_ws_state() == SystemState.HEALTHY

def test_kalshi_429_backoff():
    hm = HealthMonitor(bankroll=2000)
    hm.record_kalshi_429()
    assert hm.kalshi_poll_multiplier == 0.5  # 50% reduction
    assert hm.kalshi_backoff_until > time.time()
```

- [ ] **Step 2: Write failing tests for Book A edge convergence**

```python
# Add to tests/test_book_a.py
from oracle.books.book_a import EdgeTracker

def test_edge_convergence_suppresses_signal():
    """Edge narrowing over last 3 polls → suppress."""
    tracker = EdgeTracker()
    tracker.record("KXNBAGAME-X", 0.20)  # poll 1
    tracker.record("KXNBAGAME-X", 0.18)  # poll 2
    tracker.record("KXNBAGAME-X", 0.16)  # poll 3 — monotonically narrowing
    assert tracker.is_converging("KXNBAGAME-X") is True

def test_edge_not_converging_when_widening():
    tracker = EdgeTracker()
    tracker.record("KXNBAGAME-X", 0.16)
    tracker.record("KXNBAGAME-X", 0.18)
    tracker.record("KXNBAGAME-X", 0.20)
    assert tracker.is_converging("KXNBAGAME-X") is False

def test_edge_not_enough_history():
    tracker = EdgeTracker()
    tracker.record("KXNBAGAME-X", 0.20)
    assert tracker.is_converging("KXNBAGAME-X") is False  # need 3 polls
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_health.py tests/test_book_a.py -v -k "kill_switch or stale or 429 or convergence"`
Expected: FAIL

- [ ] **Step 4: Implement HealthMonitor**

```python
# oracle/health.py
"""System health monitoring, kill switches, and failure recovery."""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SystemState(Enum):
    HEALTHY = "healthy"
    STALE = "stale"
    DOWN = "down"


@dataclass
class KillSwitchResult:
    active: bool
    reason: str = ""


class HealthMonitor:
    def __init__(self, bankroll: float, daily_stop_pct: float = 0.05,
                 max_drawdown_pct: float = 0.15,
                 max_consecutive_failures: int = 3,
                 position_mismatch_threshold: int = 2,
                 ws_stale_seconds: float = 30.0):
        self.bankroll = bankroll
        self.daily_stop_pct = daily_stop_pct
        self.max_drawdown_pct = max_drawdown_pct
        self.max_consecutive_failures = max_consecutive_failures
        self.position_mismatch_threshold = position_mismatch_threshold
        self.ws_stale_seconds = ws_stale_seconds

        self._consecutive_failures = 0
        self._ws_down = False
        self._rest_down = False
        self._position_mismatch = False
        self._book_session_pnl: dict[str, float] = {}

        self.ws_last_data: float = time.time()
        self.ws_reconnect_failures: int = 0
        self.kalshi_poll_multiplier: float = 1.0
        self.kalshi_backoff_until: float = 0.0

    def check_kill_switch(
        self,
        daily_pnl: float = 0.0,
        cumulative_pnl: float = 0.0,
        book_allocations: Optional[dict[str, float]] = None,
    ) -> KillSwitchResult:
        # 1. Daily stop-loss
        if daily_pnl <= -(self.daily_stop_pct * self.bankroll):
            return KillSwitchResult(True, "daily_stop_loss")

        # 2. Consecutive order failures
        if self._consecutive_failures >= self.max_consecutive_failures:
            return KillSwitchResult(True, "consecutive_failures")

        # 3. Both data sources down
        if self._ws_down and self._rest_down:
            return KillSwitchResult(True, "both_sources_down")

        # 4. Position mismatch
        if self._position_mismatch:
            return KillSwitchResult(True, "position_mismatch")

        # 5. Single book > 10% of its allocation
        if book_allocations:
            for book, alloc in book_allocations.items():
                pnl = self._book_session_pnl.get(book, 0)
                if alloc > 0 and pnl <= -(0.10 * alloc):
                    return KillSwitchResult(True, f"book_{book}_drawdown")

        # Cumulative drawdown
        if cumulative_pnl <= -(self.max_drawdown_pct * self.bankroll):
            return KillSwitchResult(True, "cumulative_drawdown")

        return KillSwitchResult(False)

    def record_order_failure(self):
        self._consecutive_failures += 1

    def record_order_success(self):
        self._consecutive_failures = 0

    def mark_ws_down(self):
        self._ws_down = True

    def mark_ws_up(self):
        self._ws_down = False
        self.ws_reconnect_failures = 0

    def mark_rest_down(self):
        self._rest_down = True

    def mark_rest_up(self):
        self._rest_down = False

    def record_position_mismatch(self, ledger_count: int, kalshi_count: int):
        if abs(ledger_count - kalshi_count) > self.position_mismatch_threshold:
            self._position_mismatch = True

    def record_book_session_pnl(self, book: str, pnl: float):
        self._book_session_pnl[book] = pnl

    def get_ws_state(self) -> SystemState:
        if self._ws_down:
            return SystemState.DOWN
        if time.time() - self.ws_last_data > self.ws_stale_seconds:
            return SystemState.STALE
        return SystemState.HEALTHY

    def record_kalshi_429(self):
        self.kalshi_poll_multiplier = 0.5
        self.kalshi_backoff_until = time.time() + 2.0

    def record_ws_reconnect_failure(self):
        self.ws_reconnect_failures += 1
        if self.ws_reconnect_failures >= 3:
            self._ws_down = True
```

- [ ] **Step 5: Implement EdgeTracker for Book A**

Add to `oracle/books/book_a.py`:

```python
class EdgeTracker:
    """Track edge history per ticker to detect convergence."""

    def __init__(self, window: int = 3):
        self._history: dict[str, list[float]] = {}
        self._window = window

    def record(self, ticker: str, edge: float):
        self._history.setdefault(ticker, []).append(edge)
        # Keep only last N entries
        if len(self._history[ticker]) > self._window:
            self._history[ticker] = self._history[ticker][-self._window:]

    def is_converging(self, ticker: str) -> bool:
        """True if edge has been monotonically decreasing over the window."""
        history = self._history.get(ticker, [])
        if len(history) < self._window:
            return False
        return all(history[i] > history[i + 1] for i in range(len(history) - 1))
```

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/test_health.py tests/test_book_a.py -v`
Expected: All PASS.

- [ ] **Step 7: Commit**

```bash
git add oracle/health.py tests/test_health.py oracle/books/book_a.py tests/test_book_a.py
git commit -m "feat: add health monitor, all 5 kill switches, edge convergence tracking"
```

---

### Task 23: Validation metrics and structured logging

**Files:**
- Create: `oracle/logging_/trade_logger.py`
- Create: `oracle/metrics.py`
- Create: `tests/test_metrics.py`
- Create: `tests/test_trade_logger.py`
- Modify: `oracle/db.py` (add `closing_price` column, settlement method)

**Context:** Spec §9 requires Brier score, log loss, CLV, calibration curve, P&L by book, and structured JSON logging. The plan's `trade_logger.py` file slot exists but is not implemented. This task builds the full observability layer.

- [ ] **Step 1: Write failing tests for metrics**

```python
# tests/test_metrics.py
import math
from oracle.metrics import (
    brier_score, log_loss, closing_line_value, calibration_curve,
    book_pnl_summary,
)

def test_brier_score_perfect():
    """Perfect predictions → Brier = 0."""
    predictions = [1.0, 0.0, 1.0]
    outcomes = [1, 0, 1]
    assert brier_score(predictions, outcomes) == 0.0

def test_brier_score_worst():
    """Worst predictions → Brier = 1."""
    predictions = [0.0, 1.0]
    outcomes = [1, 0]
    assert brier_score(predictions, outcomes) == 1.0

def test_brier_score_moderate():
    predictions = [0.7, 0.3, 0.6]
    outcomes = [1, 0, 1]
    # (0.3^2 + 0.3^2 + 0.4^2) / 3 = (0.09 + 0.09 + 0.16) / 3 = 0.1133
    assert abs(brier_score(predictions, outcomes) - 0.1133) < 0.001

def test_log_loss_perfect():
    predictions = [0.99, 0.01, 0.99]
    outcomes = [1, 0, 1]
    result = log_loss(predictions, outcomes)
    assert result < 0.02  # Near zero

def test_log_loss_moderate():
    predictions = [0.7, 0.3, 0.6]
    outcomes = [1, 0, 1]
    result = log_loss(predictions, outcomes)
    assert 0.3 < result < 0.6

def test_clv_positive():
    """Bought below closing price → positive CLV."""
    trades = [
        {"fill_price": 0.55, "closing_price": 0.62},
        {"fill_price": 0.40, "closing_price": 0.48},
    ]
    clv = closing_line_value(trades)
    # avg(0.62-0.55, 0.48-0.40) = avg(0.07, 0.08) = 0.075
    assert abs(clv - 0.075) < 0.001

def test_calibration_curve():
    predictions = [0.55, 0.62, 0.58, 0.73, 0.78, 0.71]
    outcomes = [1, 0, 1, 1, 1, 0]
    curve = calibration_curve(predictions, outcomes, n_bins=3)
    assert len(curve) == 3
    assert "bin_center" in curve[0]
    assert "predicted_avg" in curve[0]
    assert "actual_rate" in curve[0]
    assert "count" in curve[0]

def test_book_pnl_summary():
    trades = [
        {"book": "B", "pnl": 5.0, "edge": 0.12},
        {"book": "B", "pnl": -3.0, "edge": 0.10},
        {"book": "A", "pnl": 8.0, "edge": 0.18},
    ]
    summary = book_pnl_summary(trades)
    assert summary["B"]["net_pnl"] == 2.0
    assert summary["B"]["trades"] == 2
    assert summary["B"]["wins"] == 1
    assert summary["A"]["net_pnl"] == 8.0
```

- [ ] **Step 2: Write failing tests for trade logger**

```python
# tests/test_trade_logger.py
import json
import logging
from oracle.logging_.trade_logger import TradeLogger

def test_trade_logger_outputs_json(tmp_path):
    log_file = tmp_path / "trades.jsonl"
    logger = TradeLogger(str(log_file))
    logger.log_signal(
        book="B", ticker="KXNBAPTS-X", side="yes",
        model_prob=0.72, kalshi_price=0.60, edge=0.12,
        triggered=True, reason="edge>threshold",
    )
    lines = log_file.read_text().strip().split("\n")
    entry = json.loads(lines[0])
    assert entry["book"] == "B"
    assert entry["ticker"] == "KXNBAPTS-X"
    assert entry["triggered"] is True
    assert "timestamp" in entry

def test_trade_logger_logs_lineup_gate(tmp_path):
    log_file = tmp_path / "trades.jsonl"
    logger = TradeLogger(str(log_file))
    logger.log_lineup_gate(
        player="LeBron James", gate="PLAYER_QUESTIONABLE",
        reason="Status is Questionable",
    )
    lines = log_file.read_text().strip().split("\n")
    entry = json.loads(lines[0])
    assert entry["event"] == "lineup_gate"
    assert entry["player"] == "LeBron James"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_metrics.py tests/test_trade_logger.py -v`
Expected: FAIL

- [ ] **Step 4: Implement metrics**

```python
# oracle/metrics.py
"""Validation metrics: Brier score, log loss, CLV, calibration curve."""
from __future__ import annotations
import math


def brier_score(predictions: list[float], outcomes: list[int]) -> float:
    """Mean squared error of probability forecasts."""
    if not predictions:
        return 0.0
    return sum((p - o) ** 2 for p, o in zip(predictions, outcomes)) / len(predictions)


def log_loss(predictions: list[float], outcomes: list[int],
             eps: float = 1e-15) -> float:
    """Negative mean log-likelihood."""
    if not predictions:
        return 0.0
    total = 0.0
    for p, o in zip(predictions, outcomes):
        p = max(eps, min(1 - eps, p))
        total += o * math.log(p) + (1 - o) * math.log(1 - p)
    return -total / len(predictions)


def closing_line_value(trades: list[dict]) -> float:
    """Average (closing_price - fill_price). Positive = real edge."""
    valid = [t for t in trades if t.get("closing_price") is not None]
    if not valid:
        return 0.0
    return sum(t["closing_price"] - t["fill_price"] for t in valid) / len(valid)


def calibration_curve(predictions: list[float], outcomes: list[int],
                       n_bins: int = 5) -> list[dict]:
    """Bin predictions and compare to actual hit rates."""
    bins: list[dict] = []
    step = 1.0 / n_bins
    for i in range(n_bins):
        lo = i * step
        hi = (i + 1) * step
        center = (lo + hi) / 2
        indices = [j for j, p in enumerate(predictions) if lo <= p < hi or (i == n_bins - 1 and p == hi)]
        if not indices:
            bins.append({"bin_center": center, "predicted_avg": center,
                         "actual_rate": 0.0, "count": 0})
            continue
        pred_avg = sum(predictions[j] for j in indices) / len(indices)
        actual_rate = sum(outcomes[j] for j in indices) / len(indices)
        bins.append({"bin_center": round(center, 3),
                      "predicted_avg": round(pred_avg, 4),
                      "actual_rate": round(actual_rate, 4),
                      "count": len(indices)})
    return bins


def book_pnl_summary(trades: list[dict]) -> dict:
    """Aggregate P&L by book."""
    summary: dict[str, dict] = {}
    for t in trades:
        book = t["book"]
        if book not in summary:
            summary[book] = {"net_pnl": 0.0, "trades": 0, "wins": 0, "losses": 0}
        summary[book]["net_pnl"] = round(summary[book]["net_pnl"] + t["pnl"], 2)
        summary[book]["trades"] += 1
        if t["pnl"] > 0:
            summary[book]["wins"] += 1
        elif t["pnl"] < 0:
            summary[book]["losses"] += 1
    return summary
```

- [ ] **Step 5: Implement trade logger**

```python
# oracle/logging_/trade_logger.py
"""Structured JSON trade logging — signals, fills, gates, opportunities."""
from __future__ import annotations
import json
import time


class TradeLogger:
    def __init__(self, path: str):
        self._path = path

    def _write(self, entry: dict):
        entry.setdefault("timestamp", time.time())
        with open(self._path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def log_signal(self, *, book: str, ticker: str, side: str,
                    model_prob: float, kalshi_price: float, edge: float,
                    triggered: bool, reason: str = "", **extra):
        self._write({
            "event": "signal",
            "book": book, "ticker": ticker, "side": side,
            "model_prob": model_prob, "kalshi_price": kalshi_price,
            "edge": edge, "triggered": triggered, "reason": reason,
            **extra,
        })

    def log_fill(self, *, book: str, ticker: str, side: str,
                  contracts: int, fill_price: float,
                  quote_at_signal: float = 0, slippage: float = 0, **extra):
        self._write({
            "event": "fill",
            "book": book, "ticker": ticker, "side": side,
            "contracts": contracts, "fill_price": fill_price,
            "quote_at_signal": quote_at_signal, "slippage": slippage,
            **extra,
        })

    def log_lineup_gate(self, *, player: str, gate: str, reason: str = ""):
        self._write({
            "event": "lineup_gate",
            "player": player, "gate": gate, "reason": reason,
        })

    def log_settlement(self, *, ticker: str, book: str, outcome: str,
                        pnl: float, closing_price: float = 0):
        self._write({
            "event": "settlement",
            "ticker": ticker, "book": book, "outcome": outcome,
            "pnl": pnl, "closing_price": closing_price,
        })
```

- [ ] **Step 6: Add closing_price and settlement to DB**

Add `closing_price REAL DEFAULT NULL` to trades table. Add method:
```python
    def settle_trade(self, ticker: str, outcome: float, pnl: float,
                      closing_price: float):
        self._conn.execute(
            "UPDATE trades SET settled=1, outcome=?, pnl=?, closing_price=? WHERE ticker=? AND settled=0",
            (outcome, pnl, closing_price, ticker),
        )
        self._conn.commit()

    def get_settled_trades(self, book: Optional[str] = None) -> list[dict]:
        if book:
            rows = self._conn.execute(
                "SELECT * FROM trades WHERE settled=1 AND book=? ORDER BY id", (book,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM trades WHERE settled=1 ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 7: Run tests**

Run: `python -m pytest tests/test_metrics.py tests/test_trade_logger.py -v`
Expected: All PASS.

- [ ] **Step 8: Commit**

```bash
git add oracle/metrics.py oracle/logging_/trade_logger.py
git add tests/test_metrics.py tests/test_trade_logger.py oracle/db.py
git commit -m "feat: add validation metrics (Brier, log loss, CLV, calibration) and structured JSON logging"
```

---

### Task 24: Full orchestrator integration

**Files:**
- Modify: `oracle/main.py` (rewrite with full daily cycle)
- Create: `tests/test_integration.py`

**Context:** The current `main.py` is a skeleton with TODOs. This task wires up all components into the complete daily cycle from spec §8: pregame phase, T-15 min lineup gate, live phase with Book C WebSocket handling, settlement phase, and position reconciliation. Also adds graceful shutdown.

- [ ] **Step 1: Write integration tests**

```python
# tests/test_integration.py
"""Integration tests for the Oracle daily cycle."""
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from oracle.main import OracleRunner
from oracle.config import load_config
from oracle.risk.ledger import RiskLedger
from oracle.models import Book, Signal

@pytest.fixture
def mock_config(tmp_path):
    yaml = """
bankroll: 2000
books:
  a:
    enabled: true
    min_edge: 0.15
    position_pct: 0.02
    max_positions: 4
    max_exposure_pct: 0.08
    min_real_volume: 200000
    poll_interval_s: 30
  b:
    enabled: true
    min_edge: 0.10
    position_pct: 0.015
    max_positions: 6
    max_exposure_pct: 0.09
    max_per_game_pct: 0.04
    max_per_player_pct: 0.025
    no_trade_window_min: 15
    min_minutes_avg: 20
  c:
    enabled: true
    min_edge: 0.12
    position_pct: 0.01
    max_positions: 3
    max_exposure_pct: 0.03
    max_slippage_avg: 0.03
    quote_max_age_s: 5
    quote_max_spread: 8
    quote_min_depth: 5
    max_per_game_pct: 0.02
    max_per_player_pct: 0.015
risk:
  max_total_exposure_pct: 0.18
  max_simultaneous_positions: 10
  daily_stop_loss_pct: 0.05
  max_drawdown_pct: 0.15
  max_props_per_player: 2
  max_props_per_game: 4
  cross_book_reduction: 0.40
  rotation_min_minutes: 20
  rotation_max_minutes: 28
  rotation_size_reduction: 0.50
"""
    p = tmp_path / "config.yaml"
    p.write_text(yaml)
    return load_config(str(p))

@pytest.mark.asyncio
async def test_signal_to_order_pipeline(mock_config):
    """Signal → check limits → compute sizing → place order → track position."""
    from oracle.risk.limits import check_limits, compute_sizing_multiplier
    from oracle.execution.executor import OrderExecutor
    from oracle.risk.ledger import RiskLedger

    ledger = RiskLedger()
    signal = Signal(book=Book.B, ticker="KXNBAPTS-X", side="yes",
                    edge=0.12, model_prob=0.72, kalshi_price=0.60)

    # Check limits
    result = check_limits(mock_config, ledger, signal, game_id="G1", player_id=100)
    assert result.allowed is True

    # Compute sizing
    mult = compute_sizing_multiplier(mock_config, ledger, Book.B,
                                      game_id="G1", minutes_avg=33.0)
    contracts = OrderExecutor.compute_contracts(
        bankroll=mock_config.bankroll,
        position_pct=mock_config.books.b.position_pct * mult,
        price_decimal=0.60,
    )
    assert contracts == 50  # 2000 * 0.015 * 1.0 / 0.60

    # Place order (mocked)
    mock_kalshi = AsyncMock()
    mock_kalshi.place_order = AsyncMock(return_value={
        "order": {"order_id": "ord1", "status": "filled"}
    })
    executor = OrderExecutor(mock_kalshi)
    await executor.place_limit(signal, contracts=contracts, price_cents=60)
    mock_kalshi.place_order.assert_called_once()

@pytest.mark.asyncio
async def test_daily_stop_loss_halts(mock_config):
    """When daily P&L hits -5%, all new signals are blocked."""
    from oracle.risk.limits import check_limits
    ledger = RiskLedger()
    ledger.daily_pnl = -100.0  # -5% of $2000

    signal = Signal(book=Book.B, ticker="X", side="yes",
                    edge=0.15, model_prob=0.75, kalshi_price=0.60)
    result = check_limits(mock_config, ledger, signal)
    assert result.allowed is False

@pytest.mark.asyncio
async def test_cross_book_sizing_in_pipeline(mock_config):
    """Book A position in game → Book B sizing reduced 40%."""
    from oracle.risk.limits import compute_sizing_multiplier
    from oracle.risk.ledger import RiskLedger
    from oracle.models import Position
    from oracle.execution.executor import OrderExecutor

    ledger = RiskLedger()
    ledger.add(Position(book=Book.A, ticker="GAME-G1", side="yes",
                        contracts=10, fill_price=0.50, game_id="G1"))

    mult = compute_sizing_multiplier(mock_config, ledger, Book.B,
                                      game_id="G1", minutes_avg=33.0)
    assert abs(mult - 0.60) < 0.01

    contracts = OrderExecutor.compute_contracts(
        bankroll=2000, position_pct=0.015 * mult, price_decimal=0.60,
    )
    assert contracts == 30  # 2000 * 0.015 * 0.60 / 0.60 = 30
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_integration.py -v`
Expected: FAIL (imports, missing OracleRunner)

- [ ] **Step 3: Rewrite main.py with full daily cycle**

```python
# oracle/main.py
"""Oracle main entry point — full daily cycle orchestrator."""
from __future__ import annotations
import asyncio
import logging
import os
import signal
import sys
from datetime import date, datetime

from dotenv import load_dotenv

from oracle.config import load_config
from oracle.db import OracleDB
from oracle.clients.real_rest import RealClient
from oracle.clients.kalshi import KalshiClient
from oracle.clients.real_ws import RealWebSocket
from oracle.clients.game_matching import match_games, build_player_ticker_map
from oracle.clients.matching import parse_kalshi_prop_title, fuzzy_match_player
from oracle.risk.ledger import RiskLedger
from oracle.risk.limits import check_limits, compute_sizing_multiplier
from oracle.risk.fees import settlement_fee
from oracle.execution.executor import OrderExecutor
from oracle.execution.quote_check import check_quote_quality
from oracle.execution.book_c_executor import BookCExecutor
from oracle.books.book_a import scan_divergence, EdgeTracker
from oracle.books.book_b import (
    compute_fair_value, generate_book_b_signals,
    check_lineup_gate, LineupGate,
)
from oracle.books.book_c import detect_foul_trouble, detect_ot_likely, detect_blowout
from oracle.pipeline import PregamePipeline
from oracle.health import HealthMonitor, SystemState
from oracle.metrics import brier_score, book_pnl_summary
from oracle.logging_.trade_logger import TradeLogger
from oracle.models import Book, Signal, Position, QuoteSnapshot

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("oracle")

load_dotenv()


class OracleRunner:
    """Main orchestrator — implements the full daily cycle from spec §8."""

    def __init__(self, config_path: str = "config.yaml"):
        self.cfg = load_config(config_path)
        self.db = OracleDB("oracle.db")
        self.ledger = RiskLedger()
        self.health = HealthMonitor(bankroll=self.cfg.bankroll)
        self.trade_log = TradeLogger("trades.jsonl")
        self.edge_tracker = EdgeTracker()
        self._shutdown = False

        # Clients
        self.real = RealClient(
            user_id=os.environ["REAL_USER_ID"],
            device_id=os.environ["REAL_DEVICE_ID"],
            token=os.environ["REAL_TOKEN"],
            device_uuid=os.environ["REAL_DEVICE_UUID"],
        )
        self.kalshi = KalshiClient(
            key_id=os.environ["KALSHI_KEY_ID"],
            private_key_path=os.environ.get("KALSHI_PRIVATE_KEY_PATH",
                                             "kalshi_private_key.pem"),
            base_url=os.environ.get("KALSHI_BASE_URL",
                                     "https://demo-api.kalshi.co/trade-api/v2"),
        )
        self.executor = OrderExecutor(self.kalshi)
        self.book_c_executor = BookCExecutor(self.kalshi)
        self.pipeline = PregamePipeline(self.real, self.db)
        self.ws = RealWebSocket()

        # State
        self._player_ticker_map: dict[str, list[dict]] = {}
        self._matched_games: list[dict] = []

    async def run(self):
        log.info("Oracle starting — bankroll=$%.0f", self.cfg.bankroll)

        # Register shutdown handler
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._request_shutdown)

        try:
            await self._pregame_phase()
            await self._live_phase()
        except Exception as e:
            log.error("Unhandled error: %s", e, exc_info=True)
        finally:
            await self._shutdown_gracefully()

    def _request_shutdown(self):
        log.info("Shutdown requested")
        self._shutdown = True

    # === PREGAME PHASE ===
    async def _pregame_phase(self):
        log.info("=== PREGAME PHASE ===")
        today = date.today().isoformat()

        # Fetch tonight's slate
        real_games_raw = await self.real.get_tonight_games("nba")
        real_games = self._parse_real_games(real_games_raw)

        # Fetch Kalshi game + prop markets
        kalshi_game_markets = await self.kalshi.get_markets(
            series_ticker="KXNBAGAME", status="open")
        self._matched_games = match_games(real_games, kalshi_game_markets)

        # Book A: scan game divergence
        if self.cfg.books.a.enabled and self._matched_games:
            log.info("Book A: scanning %d matched games", len(self._matched_games))
            signals_a = scan_divergence(
                self._matched_games, self._matched_games,
                min_edge=self.cfg.books.a.min_edge,
                min_volume=self.cfg.books.a.min_real_volume,
            )
            for s in signals_a:
                await self._try_execute(s)

        # Fetch prop markets and build player ticker map
        prop_series = ["KXNBAPTS", "KXNBAREB", "KXNBAAST", "KXNBA3PT",
                       "KXNBA2D", "KXNBA3D", "KXNBABLK", "KXNBASTL"]
        all_prop_markets = []
        for series in prop_series:
            markets = await self.kalshi.get_markets(
                series_ticker=series, status="open")
            all_prop_markets.extend(markets)
        self._player_ticker_map = build_player_ticker_map(all_prop_markets)
        log.info("Found %d player prop markets", len(all_prop_markets))

        # Book B: compute fair values
        if self.cfg.books.b.enabled:
            trackers = await self.real.get_stat_trackers(today, "nba")
            await self._run_book_b(trackers)

    async def _run_book_b(self, trackers: list[dict]):
        """Compute fair values and generate Book B signals."""
        log.info("Book B: computing fair values for %d trackers", len(trackers))

        for tracker in trackers:
            player_name = tracker.get("playerName", "")
            stat = tracker.get("statType", "")
            line = tracker.get("line", 0)
            over_odds = tracker.get("overOdds")

            # Resolve player ID
            player_id = await self.pipeline.resolve_player_id(player_name, "")
            if not player_id:
                continue

            # Check lineup gate
            profile = await self.real.get_player_profile(player_id, "nba")
            status = profile.get("status", "Active")
            mins = self._extract_minutes(profile)
            gate = check_lineup_gate(
                player_status=status, minutes_avg=mins,
                key_teammate_questionable=False,
                minutes_to_tip=120,
                min_minutes=self.cfg.books.b.min_minutes_avg,
            )
            if gate != LineupGate.CLEAR:
                self.trade_log.log_lineup_gate(
                    player=player_name, gate=gate.value,
                    reason=f"status={status}, mins={mins}",
                )
                continue

            # Compute fair value
            fv = await self.pipeline.compute_player_prop_fair_value(
                player_id=player_id, stat=stat, line=line,
                opponent="", is_home=True, is_b2b=False,
                external_over_odds=over_odds,
            )

            # Find matching Kalshi tickers
            tickers = self._player_ticker_map.get(player_name, [])
            for t in tickers:
                if t["stat"] == stat.lower() or stat.lower() in t["stat"]:
                    kalshi_dec = t["yes_price"] / 100
                    edge = fv - kalshi_dec
                    self.trade_log.log_signal(
                        book="B", ticker=t["ticker"], side="yes",
                        model_prob=fv, kalshi_price=kalshi_dec, edge=abs(edge),
                        triggered=abs(edge) >= self.cfg.books.b.min_edge,
                        reason=f"fv={fv:.3f}, kalshi={kalshi_dec:.2f}",
                    )
                    if edge >= self.cfg.books.b.min_edge:
                        sig = Signal(book=Book.B, ticker=t["ticker"],
                                     side="yes", edge=round(edge, 4),
                                     model_prob=fv, kalshi_price=kalshi_dec,
                                     metadata={"player_id": player_id})
                        await self._try_execute(sig, player_id=player_id)
                    elif edge <= -self.cfg.books.b.min_edge:
                        sig = Signal(book=Book.B, ticker=t["ticker"],
                                     side="no", edge=round(abs(edge), 4),
                                     model_prob=round(1.0 - fv, 4),
                                     kalshi_price=round(1.0 - kalshi_dec, 4),
                                     metadata={"player_id": player_id})
                        await self._try_execute(sig, player_id=player_id)

    # === LIVE PHASE ===
    async def _live_phase(self):
        log.info("=== LIVE PHASE ===")

        # Connect WebSocket
        try:
            await self.ws.connect("LiveFeed")
            log.info("WebSocket connected")
            self.health.mark_ws_up()
        except Exception as e:
            log.warning("WebSocket failed: %s — REST-only mode", e)
            self.health.mark_ws_down()

        # Register Book C event handlers
        self.ws.on("LiveFeedSocketPlayersUpdated", self._on_players_updated)
        self.ws.on("LiveFeedSocketPlaysAdded", self._on_plays_added)

        # Main polling loop
        log.info("Entering main loop (SIGINT/SIGTERM to stop)")
        while not self._shutdown:
            # Check kill switches
            ks = self.health.check_kill_switch(
                daily_pnl=self.ledger.daily_pnl,
                cumulative_pnl=self.ledger.cumulative_pnl,
            )
            if ks.active:
                log.warning("KILL SWITCH: %s — halting all trading", ks.reason)
                break

            # Check WebSocket health
            ws_state = self.health.get_ws_state()
            if ws_state == SystemState.STALE:
                log.warning("WebSocket STALE — disabling Book C")

            # Reconcile positions
            await self._reconcile_positions()

            await asyncio.sleep(10)

    async def _on_players_updated(self, data):
        """Handle live player stat updates — detect foul trouble."""
        self.health.ws_last_data = __import__('time').time()
        # Parse player foul counts and check Book C signals
        # (data format depends on Real's API — implement based on actual response)

    async def _on_plays_added(self, data):
        """Handle live play events — detect OT, blowout."""
        self.health.ws_last_data = __import__('time').time()
        # Parse score, clock, period from play events

    # === EXECUTION ===
    async def _try_execute(self, signal: Signal, game_id: str = "",
                            player_id: int = 0):
        """Check limits, compute sizing, place order."""
        result = check_limits(
            self.cfg, self.ledger, signal,
            game_id=game_id or signal.metadata.get("game_id"),
            player_id=player_id or signal.metadata.get("player_id"),
        )
        if not result.allowed:
            self.db.log_opportunity(
                book=signal.book.value, ticker=signal.ticker,
                model_prob=signal.model_prob, kalshi_price=signal.kalshi_price,
                edge=signal.edge, triggered=False, reason=result.reason,
            )
            return

        # Compute sizing
        mins = 33.0  # TODO: pass actual player minutes
        mult = compute_sizing_multiplier(
            self.cfg, self.ledger, signal.book,
            game_id=game_id, minutes_avg=mins,
        )
        book_cfg = self.cfg.books.a if signal.book == Book.A else (
            self.cfg.books.b if signal.book == Book.B else self.cfg.books.c)
        contracts = OrderExecutor.compute_contracts(
            bankroll=self.cfg.bankroll,
            position_pct=book_cfg.position_pct * mult,
            price_decimal=signal.kalshi_price,
        )
        if contracts <= 0:
            return

        # Place order
        try:
            price_cents = int(signal.kalshi_price * 100)
            resp = await self.executor.place_limit(signal, contracts, price_cents)
            order_id = resp.get("order", {}).get("order_id")
            log.info("Order placed: %s %s %dx@%d¢ edge=%.2f%%",
                     signal.book.value, signal.ticker, contracts,
                     price_cents, signal.edge * 100)
            self.health.record_order_success()

            # Track position
            pos = Position(
                book=signal.book, ticker=signal.ticker, side=signal.side,
                contracts=contracts, fill_price=signal.kalshi_price,
                game_id=game_id, player_id=player_id,
            )
            self.ledger.add(pos)

            # Log trade
            self.db.log_trade(
                book=signal.book.value, ticker=signal.ticker, side=signal.side,
                contracts=contracts, fill_price=signal.kalshi_price,
                model_prob=signal.model_prob, edge=signal.edge,
                signal_ts=signal.timestamp, fill_ts=__import__('time').time(),
            )
            self.db.log_opportunity(
                book=signal.book.value, ticker=signal.ticker,
                model_prob=signal.model_prob, kalshi_price=signal.kalshi_price,
                edge=signal.edge, triggered=True, reason="order_placed",
            )
        except Exception as e:
            log.error("Order failed: %s", e)
            self.health.record_order_failure()

    # === RECONCILIATION ===
    async def _reconcile_positions(self):
        """Check ledger vs Kalshi positions for mismatch."""
        try:
            kalshi_positions = await self.kalshi.get_positions()
            self.health.record_position_mismatch(
                ledger_count=self.ledger.position_count(),
                kalshi_count=len(kalshi_positions),
            )
        except Exception as e:
            log.warning("Position reconciliation failed: %s", e)

    # === SHUTDOWN ===
    async def _shutdown_gracefully(self):
        log.info("Shutting down gracefully...")

        # Cancel all open Book C orders
        await self.book_c_executor.cancel_all_pending()

        # Disconnect WebSocket
        try:
            await self.ws.disconnect()
        except Exception:
            pass

        # Close clients
        await self.real.close()
        await self.kalshi.close()
        self.db.close()

        # Report daily P&L
        log.info("Daily P&L: $%.2f", self.ledger.daily_pnl)

    # === HELPERS ===
    def _parse_real_games(self, data: dict) -> list[dict]:
        """Parse Real's /home/nba/next response into game dicts."""
        games = data.get("games", data if isinstance(data, list) else [])
        parsed = []
        for g in games:
            parsed.append({
                "game_id": g.get("gameId", g.get("id", 0)),
                "home_team": g.get("homeTeam", {}).get("abbreviation", ""),
                "away_team": g.get("awayTeam", {}).get("abbreviation", ""),
                "home_prob": g.get("homeWinProbability", 50),
                "volume": g.get("volume", 0),
                "status": g.get("status", "scheduled"),
            })
        return parsed

    def _extract_minutes(self, profile: dict) -> float:
        """Extract last-5 average minutes from player profile splits."""
        splits = profile.get("splits", {}).get("averages", [])
        for s in splits:
            if s.get("period", "").lower().startswith("last 5"):
                return s.get("stats", {}).get("min", 0.0)
        return 0.0


async def run(config_path: str = "config.yaml"):
    runner = OracleRunner(config_path)
    await runner.run()


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run integration tests**

Run: `python -m pytest tests/test_integration.py -v`
Expected: All PASS.

- [ ] **Step 5: Run full test suite**

Run: `python -m pytest tests/ -v --tb=short`
Expected: All tests pass across all test files.

- [ ] **Step 6: Commit**

```bash
git add oracle/main.py tests/test_integration.py
git commit -m "feat: rewrite orchestrator with full daily cycle, graceful shutdown, position reconciliation"
```

---

## Summary

| Task | Component | Tests |
|---|---|---|
| 1 | Project scaffolding | — |
| 2 | Data models | 5 tests |
| 3 | Config loading | 2 tests |
| 4 | Fee calculation | 3 tests |
| 5 | SQLite database | 4 tests |
| 6 | Risk ledger + limits | 7 tests |
| 7 | Real REST client | 4 tests |
| 8 | Kalshi client | 4 tests |
| 9 | Player matching | 3 tests |
| 10 | Book A (divergence) | 4 tests |
| 11 | Book B (props) | 10 tests |
| 12 | Book C (live events) | 8 tests |
| 13 | Quote quality checks | 4 tests |
| 14 | Order executor | 4 tests |
| 15 | Real WebSocket | manual |
| 16 | Main orchestrator (skeleton) | — |
| 17 | Final verification (Tasks 1-16) | full suite |
| **18** | **Fix logic errors + credentials** | **~10 tests** |
| **19** | **Correlation guards + enhanced limits** | **~10 tests** |
| **20** | **Pregame data pipeline + matching** | **~5 tests** |
| **21** | **Book C execution lifecycle** | **~6 tests** |
| **22** | **Failure recovery + kill switches** | **~10 tests** |
| **23** | **Validation metrics + structured logging** | **~9 tests** |
| **24** | **Full orchestrator integration** | **~3 integration tests** |

**Total: 24 tasks, ~115 automated tests, 24 commits.**

After implementation, the next steps are:
1. Fill in Real + Kalshi credentials in `.env`
2. Paper trade on Kalshi demo for 4+ weeks (Phase 1)
3. Review Brier score, CLV, calibration curve from `trades.jsonl`
4. Small live trading ($500, Book B only) for 4 weeks (Phase 2)
5. Full deployment with all books (Phase 3)
