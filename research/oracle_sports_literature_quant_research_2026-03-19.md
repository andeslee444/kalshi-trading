# Sports Trading Literature Pass

Date: 2026-03-19

## Executive summary

The literature does **not** support a naive belief that sports markets are broadly easy to beat.

It does support a much narrower claim:

1. Closing prices are often efficient or close to efficient.
2. Opening prices, injury/lineup shocks, and some early-season regimes are less efficient.
3. Quote-driven or sentiment-heavy markets can embed behavioral bias.
4. Exchange-like markets tend to be sharper than bookmakers, but their microstructure can still create maker/taker and longshot-style edges.
5. In live betting, the strongest opportunities come from **latency**, **microstructure**, and **behavioral overreaction**, not from generic historical averages.

For Oracle, this lines up with the earlier repo audit:

- The strongest likely edges are still `Real + Kalshi + execution`, not more Book B parameter sweeps.
- The best pregame edges are likely around lineups, injuries, and market-open timing.
- The best live edges are likely around event latency, comeback/late-game mispricing, and maker/taker asymmetry.

## The most useful papers and what they imply

### A. Sports markets are often efficient near close

#### 1. Dare, Dennis, Paul (2015), "Player absence and betting lines in the NBA"

Source:

- https://www.sciencedirect.com/science/article/pii/S1544612315000227

Key result:

- Opening lines are biased when meaningful players are absent.
- Those biases are largely gone by close.

Why it matters for Oracle:

- Injury and absence information is valuable **before** the market fully digests it.
- This supports spending effort on:
  - official injury-report ingestion
  - projected/confirmed lineups
  - automated reaction in the open-to-close window
- It argues against expecting easy alpha from stale historical models at the closing line.

#### 2. Paul and Weinbach (2005), "Bettor Misperceptions in the NBA"

Source:

- https://econpapers.repec.org/article/saejospec/v_3a6_3ay_3a2005_3ai_3a4_3ap_3a390-400.htm

Key result:

- They found evidence of overbetting favorites and profitability in big underdogs and big home underdogs in older NBA spread samples, plus profitability from betting against winning streaks.

Why it matters:

- Not because the exact old rule will still work.
- Because it says the public can systematically overweight salience, favorites, and streak narratives.

Transfer to Kalshi:

- Watch for:
  - popular-team contracts
  - star-player overs
  - streak-driven crowd enthusiasm
  - nationally salient comeback narratives

#### 3. Paul, Weinbach, Humphreys (2013), "Revisiting the Hot Hand Hypothesis in the NBA Betting Market"

Source:

- https://www.ubplj.org/index.php/jgbe/article/view/569/0

Key result:

- Bettors believe in the hot hand and teams on streaks attract more bets, but the closing price remains an unbiased forecast.

Why it matters:

- The public bias is real.
- The edge comes only if you act **before** the market fully absorbs it, or in thinner submarkets where the correction is incomplete.

#### 4. Baryla et al. (2007), "Learning, price formation and the early season bias in the NBA"

Source:

- https://www.sciencedirect.com/science/article/abs/pii/S1544612307000177

Key result:

- NBA totals were upwardly biased early in seasons, and a simple strategy reached a 56.72% win rate in their sample.

Why it matters:

- Early season is a distinct regime.
- When uncertainty is high and priors are unstable, market makers and bettors may not fully incorporate the new state quickly enough.

Transfer to Oracle:

- Create separate preseason / first-10-games / trade-deadline / post-All-Star calibrations.
- Do not pool all regimes together.

### B. Market structure matters a lot

#### 5. Levitt (2004), "Why are Gambling Markets Organised so Differently from Financial Markets?"

Source:

- https://academic.oup.com/ej/article/114/495/223/5086012

Key result:

- Quote-driven bookmakers exploit bettor biases by setting prices away from market-clearing prices.

Why it matters:

- This is a clean reason to separate:
  - sportsbook consensus signals
  - exchange/prediction-market signals
- Bookmaker odds can contain shading and demand effects.
- Exchange-like prices can be more informative, but their order flow can still be behaviorally distorted.

#### 6. Franck, Verbeek, Nuesch (2010), "Prediction accuracy of different market structures -- bookmakers versus a betting exchange"

Source:

- https://www.sciencedirect.com/science/article/pii/S0169207010000105

Key result:

- The betting exchange was more accurate than bookmakers, and discrepancies could generate above-average or positive returns.

Why it matters:

- Kalshi is more exchange-like than a sportsbook.
- Comparing Kalshi to sportsbook consensus is not just redundant data collection. It is a test for **structural disagreement**.

Transfer to our data stack:

- `Kalshi historical prices` + `The Odds API` or `SportsDataIO Betting` can support a bookmaker-vs-exchange divergence model.

#### 7. Brown and Yang (2019), "The wisdom of large and small crowds"

Source:

- https://ideas.repec.org/a/eee/intfor/v35y2019i1p288-296.html

Key result:

- Betting prices became less informative when participation dropped.

Why it matters:

- Crowd quality is endogenous.
- Low-participation, low-attention markets can be less efficient.

Transfer to Oracle:

- Thin Kalshi submarkets may be better than headline markets.
- Use volume, open interest, depth, spread, and event salience as part of the alpha model, not just execution filters.

#### 8. Rothschild and Sethi (2016), "Trading Strategies and Market Microstructure: Evidence from a Prediction Market"

Source:

- https://www.ubplj.org/index.php/jpm/article/view/1179

Key result:

- Prediction markets contain a rich ecology of arbitrage, directional, and possibly manipulative strategies.

Why it matters:

- Do not assume every price move is information.
- Some moves are inventory, manipulation, or one-sided conviction.

Transfer to Oracle:

- Label Kalshi price moves by context:
  - news move
  - depth depletion
  - wide-spread midpoint jump
  - close-to-settlement scramble

### C. Behavioral effects are real and directly relevant

#### 9. Snowberg and Wolfers (2010), "Explaining the Favorite-Longshot Bias"

Source:

- https://www.nber.org/papers/w15923

Key result:

- The evidence favors **probability misperception**, not just rational risk-love, as the driver of favorite-longshot bias.

Why it matters:

- This is highly relevant for Kalshi-style binary contracts.
- Low-price contracts can be systematically overpriced relative to true win rates.

Transfer to Oracle:

- Study whether low-priced YES contracts in sports are worse than the displayed price implies.
- Maker or contrarian strategies may outperform pure directional forecasting in these buckets.

#### 10. Whelan (2025), "Makers and Takers: The Economics of the Kalshi Prediction Market"

Source:

- https://mpra.ub.uni-muenchen.de/126350/

Key result:

- Kalshi prices improve toward close but show favorite-longshot bias.
- Makers and takers exhibit distinct return patterns.

Why it matters:

- This is the most directly relevant current research for Kalshi.
- It supports a serious research branch on:
  - maker alpha
  - low-price YES fade
  - quote quality and spread capture

Caution:

- This is a working paper, not a final journal article.
- Still highly relevant because it is platform-specific.

#### 11. Page (2012), "Yogi Berra bias on prediction markets"

Source:

- https://ideas.repec.org/a/taf/applec/44y2012i1p81-92.html

Key result:

- Prediction-market prices become materially miscalibrated near settlement because traders overestimate the losing side's comeback chances late in the event.

Why it matters:

- This is exactly the kind of live behavioral effect that could appear on sports contracts and late-game Kalshi markets.

Transfer to Oracle:

- Book C should explicitly test for late-game comeback overpricing.
- In close games, the public may overpay for reversal narratives.

#### 12. Stanek (2017), "Home bias in sport betting"

Source:

- https://www.cambridge.org/core/journals/judgment-and-decision-making/article/home-bias-in-sport-betting-evidence-from-czech-betting-market/AF3B2DAC6A17EDED1CEBB22F4049A6AF

Key result:

- Home bias persists even with real money and rapid feedback.

Why it matters:

- Salience and familiarity still distort betting choices.
- This strengthens the case for bias filters tied to:
  - home favorite narratives
  - local/nationally popular teams
  - widely discussed stars

#### 13. Franck, Verbeek, Nuesch (2011), "Sentimental Preferences and the Organizational Regime of Betting Markets"

Source:

- https://ideas.repec.org/a/wly/soecon/v78y2011i2p502-518.html

Key result:

- Quote-driven markets can exploit sentimental demand by offering worse prices on bets with stronger demand.

Why it matters:

- Kalshi is not a sportsbook, but this logic still matters for any quote-driven, attention-heavy venue.
- Sports event contracts can concentrate crowd enthusiasm into the same side.

Transfer to Oracle:

- Measure whether crowd-heavy YES contracts, favorite teams, and star overs carry systematically worse expected value than quieter markets.

#### 14. Goins, Cipriano, Gruca (2016), "Private Information, Overconfidence and Trader Returns in Prediction Markets"

Source:

- https://www.ubplj.org/index.php/jpm/article/view/1138

Key result:

- Overconfidence shapes trading volume and timing; accurate private information still has value, but overconfident trading is visible.

Why it matters:

- If your Real data truly is informationally superior, the edge can exist.
- But overconfidence can also create false conviction and poor trade timing.

Transfer to Oracle:

- Favor measurable information advantage over story-driven discretionary trading.
- Log whether your edge comes from prediction quality or just from being more aggressive.

### D. NBA-specific modeling papers worth using

#### 15. Terner and Franks (2021), "Modeling Player and Team Performance in Basketball"

Source:

- https://www.annualreviews.org/content/journals/10.1146/annurev-statistics-040720-015536

Key result:

- Good basketball analytics uses hierarchical modeling, Markov chains, regularization, and spatio-temporal data rather than single flat heuristics.

Why it matters:

- This is the right conceptual umbrella for Oracle.
- It supports:
  - stat-specific models
  - hierarchical shrinkage
  - live state transitions
  - spatial and possession context where available

#### 16. Page, Barney, McGuire (2013), "Effect of position, usage rate, and per game minutes played on NBA player production curves"

Source:

- https://ideas.repec.org/a/bpj/jqsprt/v9y2013i4p337-345n1.html

Key result:

- Player production curves can be modeled hierarchically using minutes and usage information, borrowing strength across comparable players.

Why it matters:

- This is much closer to a useful prop model than pure recency hit rate.
- Minutes and role are first-class variables.

#### 17. Briz-Redon (2024), "A doubly self-exciting Poisson model for describing scoring levels in NBA basketball"

Source:

- https://academic.oup.com/jrsssc/article/73/3/735/7616121

Key result:

- Game-level and minute-level scoring can be modeled with a doubly self-exciting INGARCH-style process under a Bayesian framework, and the model captures overdispersion.

Why it matters:

- Good live models should allow serial dependence and event clustering.
- This is especially relevant for:
  - Book C
  - live totals
  - comeback / clutch / foul-state models

#### 18. Martin-Gonzalez et al. (2016), "The Poisson model limits in NBA basketball"

Source:

- https://www.sciencedirect.com/science/article/pii/S0378437116304599

Key result:

- A single Poisson law is not sufficient for NBA scoring at all times; close late-game situations can depart materially from simple Poisson assumptions.

Why it matters:

- End-game behavior is special.
- Do not use one simple distribution for all game states.

#### 19. Shi and Song (2021), "A discrete-time and finite-state Markov chain based in-play prediction model for NBA basketball matches"

Source:

- https://www.tandfonline.com/doi/full/10.1080/03610918.2019.1633351

Key result:

- A finite-state Markov chain can produce useful in-play NBA predictions and reported positive returns in their study.

Why it matters:

- Book C should not just be rule-based heuristics.
- A proper state-transition model is justified by the literature.

#### 20. Kiriazis, Genest, Leblanc (2024), "A Bayesian two-stage framework for lineup-independent assessment of individual rebounding ability in the NBA"

Source:

- https://ouci.dntb.gov.ua/en/works/7pXNNw8b/

Key result:

- Rebounding is not well captured by raw individual rebound rates alone; a two-stage Bayesian model is better.

Why it matters:

- Rebounds props should be modeled separately from points and assists.
- Stat-specific micro-models are justified.

#### 21. Ho (2025), "A Bayesian Negative Binomial-Bernoulli Model ... Basketball Games"

Source:

- https://jds-online.org/journal/JDS/article/1440/info

Key result:

- Shot attempts and shot success can be modeled jointly with negative-binomial and Bernoulli components under a Bayesian framework.

Why it matters:

- This supports decomposing player output into:
  - opportunity volume
  - efficiency / conversion
- That is the right architecture for many prop markets.

#### 22. Papageorgiou, Sarlis, Tjortjis (2024/2025), "An innovative method for accurate NBA player performance forecasting and line-up optimization in daily fantasy sports"

Source:

- https://link.springer.com/article/10.1007/s41060-024-00523-y

Key result:

- Individualized per-player models plus optimization beat simpler benchmarks in DFS-style forecasting.

Why it matters:

- Individual player models are viable.
- Oracle should prefer player-specific or player-clustered models over one global heuristic.

## The formulas that matter most for Oracle

These are not copied from any one paper. They are the best synthesis of the literature for your current stack.

### 1. Binary contract EV

For a YES contract entered at ask `a` with model probability `p`, gross expected value per contract is:

`EV_yes = p * (1 - a) - (1 - p) * a`

For a NO contract entered at price `n`:

`EV_no = (1 - p) * (1 - n) - p * n`

In practice, use:

`EV_net = EV_gross - fees - slippage - adverse_selection_cost`

Why this matters:

- Every research score should reduce to net EV against real entry price.
- This is the simplest discipline missing from the old synthetic backtest.

### 2. Multi-source fair probability blend

Let:

- `p_model` = your statistical model
- `p_real` = Real market / crowd probability
- `p_book` = sportsbook consensus probability
- `p_kalshi` = Kalshi midpoint or microprice

Use a calibrated blend:

`logit(p_fair) = w0 + w1*logit(p_model) + w2*logit(p_real) + w3*logit(p_book) + w4*logit(p_kalshi)`

Then calibrate with logistic regression or isotonic regression on settled trades.

Why this matters:

- The literature supports crowd aggregation and smart-subcrowd effects.
- Your advantage is likely in combining different information sets, not trusting one source.

### 3. Minutes x rate decomposition for props

For player `i` in game `g`:

`M_ig ~ N(mu_M(X_ig), sigma_M^2)`

`log r_ig = alpha_i + beta_opp + gamma_pace + delta_rest + eta_lineup + zeta_home + ...`

`lambda_ig = M_ig * r_ig`

Then use a stat-appropriate observation model:

- points / assists / rebounds / threes: `Y_ig ~ NegBin(mean = lambda_ig, dispersion = k)`
- steals / blocks: often better with zero-inflated or hurdle variants

Why this matters:

- It cleanly separates opportunity from efficiency.
- It aligns with the literature much better than raw last-10 hit rate.

### 4. Hierarchical shrinkage for thin samples

For player effects:

`alpha_i ~ N(mu_position, tau_position^2)`

or cluster by role/archetype:

`alpha_i ~ N(mu_cluster(i), tau_cluster(i)^2)`

Why this matters:

- Many props are quoted on players with small recent samples.
- Hierarchical shrinkage is exactly how you stop role-player noise from dominating.

### 5. Live scoring / state model

For minute-level or short-interval live modeling:

`lambda_t = omega + alpha * y_{t-1} + beta * lambda_{t-1} + theta' * event_t`

where `event_t` can include:

- foul trouble
- margin
- timeout
- clutch state
- possession
- lineup state
- OT risk

Why this matters:

- It is the natural bridge from Real live feed to Book C.
- It is more realistic than static extrapolation from current box score.

### 6. Markov chain for live win / totals / OT

Define state `S_t` as a compact game snapshot:

`S_t = (margin, possession, period, seconds_remaining, foul_state, lineup_state)`

Estimate:

`P(S_{t+1} | S_t)`

Then compute:

- `P(win | S_t)`
- `P(OT | S_t)`
- `P(player clears line | S_t, current_stat)`

Why this matters:

- It is directly supported by the in-play NBA literature.
- It is especially useful in the final minutes, where simple Poisson assumptions break.

### 7. Overreaction / streak indicator

Adapt Wheatcroft's idea into an NBA-friendly feature:

`OR_i(t) = sum_{g=t-n}^{t-1} (actual_i,g - implied_i,g) / n`

where `implied_i,g` comes from the market or your baseline model.

Interpretation:

- high positive `OR`: player/team has recently overperformed expectations
- high negative `OR`: player/team has recently underperformed expectations

Use it to test whether current prices overreact to recent form.

Why this matters:

- The hot-hand and recency literature says bettors overweight recent success.

### 8. Ladder monotonicity constraint

For a prop ladder:

If `l1 < l2`, then:

`P(Y >= l1) >= P(Y >= l2)`

Fit smoothed probabilities using isotonic regression or monotone rearrangement.

Why this matters:

- It converts noisy adjacent Kalshi quotes into a coherent probability curve.
- Mispricing can then be measured as deviation from the monotone fitted curve.

## The psychological phenomena most worth testing on Kalshi

### 1. Favorite-longshot bias

Hypothesis:

- Low-price YES contracts are overpriced.

Most relevant data:

- Kalshi historical trades and fills
- Kalshi orderbook snapshots

Why it fits your setup:

- Platform-specific Kalshi work already points in this direction.
- Longshot sports and entertainment contracts tend to be especially vulnerable.

### 2. Yogi Berra bias

Hypothesis:

- In the last minutes, losing teams and comeback paths are overpriced.

Most relevant data:

- Real live feed
- Kalshi live orderbooks and trades
- play-by-play state

Why it fits your setup:

- This is a live, behavioral, event-driven mispricing.
- It is exactly the kind of thing a faster feed can exploit.

### 3. Hot-hand / recency bias

Hypothesis:

- Recent standout performances cause inflated prices on next-game overs or on current live momentum narratives.

Most relevant data:

- sportsbook consensus history
- Kalshi history
- player recent-game logs
- social/popularity proxies if available

### 4. Home / salience / sentimental bias

Hypothesis:

- Popular teams, home teams, and high-attention stars get worse prices.

Most relevant data:

- team popularity proxies
- nationally televised games
- Real crowd participation or volume if observable
- sportsbook and Kalshi splits by market popularity

### 5. Overconfidence / one-sided trader flow

Hypothesis:

- Some price moves are not information but conviction or crowd pressure.

Most relevant data:

- orderbook depletion
- trade aggressor direction if inferable
- depth before and after move
- quote recovery after move

## How this ties to the sources we already researched

### Real Sports API

Best use:

- live event edge
- live market and crowd disagreement
- smaller-smarter-crowd ideas
- Book A and Book C

Literature fit:

- crowd-size and smart-subcrowd papers
- Yogi Berra bias
- hot-hand / salience / overconfidence

### Kalshi API

Best use:

- real price-aware backtests
- maker/taker research
- favorite-longshot bias tests
- CLV and execution-quality measurement

Literature fit:

- Kalshi maker/taker paper
- prediction-market microstructure
- favorite-longshot literature

### SportsDataIO

Best use:

- projected lineups
- confirmed lineups
- injuries
- officials
- opening-line / pregame reaction strategies

Official source notes:

- live game state and player stats are about `15-20 seconds` behind TV
- projected lineups are available by `9am ET`
- confirmed lineups are typically confirmed within minutes of release

Literature fit:

- player-absence paper
- early-season and opening-line inefficiency work

### Sportradar

Best use:

- structured push stats and play-by-play
- deeper possession-level or shot-level modeling
- robust backup to Real

Literature fit:

- Markov chain models
- self-exciting models
- spatio-temporal and shot modeling

### The Odds API

Best use:

- sportsbook consensus
- historical prop snapshots
- bookmaker-vs-exchange divergence

Official source notes:

- historical player props available from `2023-05-03`
- snapshots available at `5-minute` intervals

Literature fit:

- bookmaker vs exchange pricing
- overreaction and recency studies

## The best alpha hypotheses to test next

### Hypothesis 1

`Pregame edge = lineup / injury / officials timing`

Test:

- compare Oracle fair price versus Kalshi open
- condition on projected lineup changes, confirmed lineups, and injury updates
- measure whether edge decays by close

Expected value:

- high

Why:

- directly supported by the NBA absence paper and by your available data vendors

### Hypothesis 2

`Live edge = Real event arrives before Kalshi reprices`

Test:

- timestamp Real play update
- timestamp first Kalshi quote move
- timestamp first fillable quote
- bucket by event type

Expected value:

- very high if latency gap exists

### Hypothesis 3

`Late-game comeback contracts are overpriced`

Test:

- compute realized win rates versus displayed probabilities in final 2 minutes by margin bucket
- compare losing-side contracts to empirical outcomes

Expected value:

- high

### Hypothesis 4

`Low-priced YES contracts on Kalshi are systematically bad buys`

Test:

- bin historical trades and end-of-day snapshots by price bucket
- compute realized win rates net of fees and spread
- compare maker versus taker style entries if possible

Expected value:

- medium-high

### Hypothesis 5

`Streak narratives inflate next-game star overs`

Test:

- build a recency/overreaction feature from recent overperformance versus expectation
- test whether market prices overreact more in high-salience players and teams

Expected value:

- medium

### Hypothesis 6

`Props are only worth serious effort in liquid stat classes`

Test:

- separate by points, rebounds, assists, 3PT, steals, blocks
- require real fill prices

Expected value:

- high, because it prevents wasting research effort

## What this implies for strategy

The literature points to a strategy stack that looks like this:

1. `Execution-first`
   Real market edges die quickly; if you cannot measure fill quality and latency, you do not know whether you have alpha.

2. `Open-to-close information trading`
   Injuries, lineups, and officials are likely the cleanest pregame edge sources.

3. `Book C live trading`
   Use Real live feed with event-state models and test explicitly for late-game comeback bias.

4. `Kalshi microstructure`
   Study maker/taker returns, longshot buckets, and queue quality.

5. `Stat-specific prop models`
   Minutes x rate, hierarchical shrinkage, and stat-specific observation models.

6. `Cross-market disagreement`
   Kalshi versus sportsbook consensus versus Real crowd versus your model.

## Honest conclusion

This literature pass strengthens the earlier conclusion.

The path to a real edge is **not**:

- more generic autoresearch sweeps
- more recency-weight tuning
- more synthetic-price backtests

The path to a real edge is:

- real prices
- real fills
- real latency measurement
- behavioral bias detection
- stat-specific NBA modeling
- information timing around lineups, absences, and live event state

The papers make me more confident that a real edge is plausible.

They do **not** make me confident that it already exists in production.

## Source links

### Market efficiency / betting

- https://www.sciencedirect.com/science/article/pii/S1544612315000227
- https://econpapers.repec.org/article/saejospec/v_3a6_3ay_3a2005_3ai_3a4_3ap_3a390-400.htm
- https://www.ubplj.org/index.php/jgbe/article/view/569/0
- https://www.sciencedirect.com/science/article/abs/pii/S1544612307000177
- https://academic.oup.com/ej/article/114/495/223/5086012
- https://www.sciencedirect.com/science/article/pii/S0169207010000105
- https://ideas.repec.org/a/eee/intfor/v35y2019i1p288-296.html

### Prediction markets / behavioral / Kalshi

- https://mpra.ub.uni-muenchen.de/126350/
- https://www.ubplj.org/index.php/jpm/article/view/1179
- https://www.nber.org/papers/w15923
- https://ideas.repec.org/a/taf/applec/44y2012i1p81-92.html
- https://www.cambridge.org/core/journals/judgment-and-decision-making/article/home-bias-in-sport-betting-evidence-from-czech-betting-market/AF3B2DAC6A17EDED1CEBB22F4049A6AF
- https://ideas.repec.org/a/wly/soecon/v78y2011i2p502-518.html
- https://www.ubplj.org/index.php/jpm/article/view/1138
- https://www.microsoft.com/en-us/research/publication/the-wisdom-of-smaller-smarter-crowds/

### NBA modeling

- https://www.annualreviews.org/content/journals/10.1146/annurev-statistics-040720-015536
- https://ideas.repec.org/a/bpj/jqsprt/v9y2013i4p337-345n1.html
- https://academic.oup.com/jrsssc/article/73/3/735/7616121
- https://www.sciencedirect.com/science/article/pii/S0378437116304599
- https://www.tandfonline.com/doi/full/10.1080/03610918.2019.1633351
- https://ouci.dntb.gov.ua/en/works/7pXNNw8b/
- https://jds-online.org/journal/JDS/article/1440/info
- https://link.springer.com/article/10.1007/s41060-024-00523-y

### Data sources

- https://docs.kalshi.com/getting_started/historical_data
- https://sportsdata.io/developers/workflow-guide/nba
- https://developer.sportradar.com/basketball/v7/reference/push-statistics
- https://the-odds-api.com/historical-odds-data/
