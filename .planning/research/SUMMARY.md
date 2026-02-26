# Research Summary: Kalshi Quant Trading System

**Domain:** Automated prediction market trading (Kalshi)
**Researched:** 2026-02-26
**Overall confidence:** MEDIUM (infrastructure exists but zero validation data)

## Executive Summary

The Kalshi trading system has extensive infrastructure -- authenticated API client, probability models for 6 market types, safety guardrails, supervisor, dashboard, and decision logging -- but is producing near-zero executed trades and has zero model validation. The system is in a "pre-revenue" state despite being technically operational.

The core problem is the absence of a feedback loop. Zero settlement reconciliation has been done, Brier scores are null, calibration.json is empty, and the position monitor has zero exits. Without knowing whether models produce accurate probabilities, every trade is a guess. This is the single highest-priority gap and blocks all downstream optimization.

The system's alpha thesis -- speed edge from getting external data (NWS actuals, album sales, earnings) before markets price it in -- is sound but unvalidated. Prediction market research shows information edge is the most reliable source of alpha for retail participants, with documented cases of automated bots generating $150K+ from 8,894 trades on crypto prediction contracts. However, the Kalshi API caches market snapshots every 5 seconds, limiting true speed advantage and favoring medium-frequency event-driven strategies over high-frequency approaches.

The recommended approach is: (1) establish feedback loop (reconcile, score, calibrate), (2) get existing bots actually trading (debug the zero-execution problems), (3) validate and expand. Building new strategies before validating existing ones is premature optimization.

## Key Findings

**Stack:** Python-only system with no heavy dependencies (math.erf instead of scipy). Infrastructure is mature and well-architected. No stack changes needed.

**Architecture:** File-based state with atomic JSON writes works for single-machine deployment. Cross-process file locking is the main fragility point. SQLite migration would help but is not urgent.

**Critical pitfall:** The system has zero model validation -- all probability estimates are unverified theories. Trading without calibration is gambling, not quantitative trading.

## Implications for Roadmap

Based on research, suggested phase structure:

1. **Phase 1: Feedback Loop** - Settlement reconciliation, Brier scores, calibration curves, bankroll basis fix
   - Addresses: Settlement reconciliation, model validation, fee standardization
   - Avoids: Trading on unvalidated models, oversizing positions
   - Rationale: Everything else depends on knowing whether models work

2. **Phase 2: Activate Trading** - Debug non-executing bots, run calibration, fix position exits
   - Addresses: Zero-execution bugs in entertainment, economics, strategy bots; empty calibration.json; zero exits
   - Avoids: Building new strategies before existing ones work
   - Rationale: 7 of 10 bots have near-zero trades despite being technically operational

3. **Phase 3: Validate & Expand** - Automated recalibration, per-bot P&L, cross-platform arb, new strategies
   - Addresses: Model drift, new alpha sources (box office), cross-platform arb execution
   - Avoids: Premature ML/DL, market making, multi-exchange sprawl
   - Rationale: Only expand after core bots prove profitable for 2-4 weeks

4. **Phase 4: Operational Hardening** - Daily automation, data source fallbacks, circuit breaker granularity
   - Addresses: Reliability, monitoring, alerting gaps
   - Avoids: Over-engineering before profitability is proven

**Phase ordering rationale:**
- Phase 1 before Phase 2 because you cannot fix what you cannot measure
- Phase 2 before Phase 3 because existing infrastructure should produce trades before building new strategies
- Phase 3 before Phase 4 because operational hardening is only valuable for a system that is trading

**Research flags for phases:**
- Phase 1: Standard patterns, unlikely to need deeper research (reconciliation and Brier scores are well-understood)
- Phase 2: Likely needs debugging-specific research (why entertainment bot has 99.7% skip rate)
- Phase 3: May need research on Polymarket CLOB API specifics and box office data sources
- Phase 4: Standard operational patterns, unlikely to need research

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | Python stack is appropriate. No changes needed. Verified against codebase. |
| Features | HIGH | Table stakes (feedback loop, exits) and differentiators (speed edge, models) clearly identified from codebase + market research. |
| Architecture | MEDIUM | File-based state works for now. Lock contention risk is documented but unquantified. |
| Pitfalls | HIGH | Zero-validation pitfall is clearly documented in PROJECT.md, CONCERNS.md, and confirmed by empty calibration.json. |

## Gaps to Address

- Actual Brier score magnitudes for each model (unknown until reconciliation runs)
- Whether the NWS speed edge is minutes or hours (unquantified until validated)
- Cleveland Fed scraper status (may need API endpoint research)
- HDD Sanity CMS API current availability (was disabled due to broken endpoints)
- Polymarket CLOB API reliability for live arb execution
- Optimal scan frequency for each bot (currently arbitrary: 5 min to 6 hours)
