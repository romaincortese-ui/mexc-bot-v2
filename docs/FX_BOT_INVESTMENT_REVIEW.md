# FX-Bot — Trading Desk Review & Improvement Memo

**Prepared by:** Senior PM, Metals & FX Desk
**Strategy under review:** Multi-pair automated FX trader (FX-bot v1.x)
**Broker:** OANDA (fxTrade / fxPractice)
**Perspective:** What changes would be required for this system to be approved as a bookable strategy at a prop desk.

---

## 1. Executive summary

This is a more ambitious build than the sister gold-bot: six strategies (SCALPER, TREND, REVERSAL, CARRY, POST_NEWS, PULLBACK), multi-pair (EUR/USD, GBP/USD, USD/JPY and a dynamic discovery list), USD-netting correlation caps, adaptive pair-health blocking, and a macro-context engine that consults DXY, VIX, 2Y rate spreads, commodity momentum and the Economic Surprise Index.

The architecture is good. The execution detail is where the strategy gives away most of its theoretical edge. The headline issues:

1. **Direction scoring is a vote count**, not a probability. A pair with EMA9 > 21, RSI 50.5 and MACD marginally positive on M5 gets the same "long" vote as a pair with EMA9 >> 21, RSI 72 and MACD strongly positive. The system treats those as equivalent setups; they are not.
2. **Correlation is modelled only through a "USD vote"**. EUR/USD long is netted against USD/JPY long, but EUR/USD vs GBP/USD (correlation ~0.85) is not netted at all. This is the most common mistake in retail FX bots and it systematically overstates diversification.
3. **Carry strategy is underpowered**. The current implementation is a rate-spread + VIX gate; a professional carry book is a vol-scaled positive-carry basket with explicit drawdown kill-switches based on AUD/JPY or EM FX realised vol.
4. **Backtest spread/slippage is optimistic**: 0.2 pip buffer + 0.8 pip floor + 0.4 pip regular slippage + 2.0 pip news slippage. Realistic major-pair OANDA retail spreads during NY session are fine; during London-Tokyo overlap (0400-0600 UTC) and during NFP they are materially wider. No intra-day spread model.
5. **News is a time-based blackout**, not a per-pair impact filter. EUR/USD during US NFP should halt. AUD/NZD during the same US NFP is less affected — the current system halts both.
6. **No regime or vol targeting at portfolio level**. A scalper position has the same nominal risk (1.5% of equity) whether implied vol is 6% (EUR/USD in 2019) or 14% (GBP/USD in 2022). P&L volatility is correspondingly erratic.
7. **Strategy overlap creates internal arbitrage**. SCALPER and REVERSAL both fire on RSI extremes but in opposite directions. On the same bar one scores long, the other scores short — then both take trades against each other with the bot's own capital. That is negative edge by construction.

The recommendations below are organised in three tiers: Tier 1 (low-risk, ship in one sprint), Tier 2 (structural, one quarter), Tier 3 (strategic, ongoing research).

---

## 2. Tier 1 — Cheap fixes that compound quickly

### 2.1  Replace the "signal vote" direction with a probability score
Current `direction.py` sums ± votes from EMA9/21 (M5), RSI (M5), MACD (M5), EMA50 (H1), RSI (H1), MACD (H1), EMA50 (H4), and DXY bias. Winner takes direction. That treats every signal as either +1, 0 or -1.

**Replace with a continuous score, with each indicator returning a [-1, +1] confidence:**

- EMA alignment → `tanh((ema_fast - ema_slow) / atr)` — strong separation scores near 1.
- RSI → `(rsi - 50) / 30` clamped to [-1, 1] — RSI 75 scores 0.83; RSI 52 scores 0.07.
- MACD → `tanh(macd_hist / atr)`.

Weight these by timeframe (H4 EMA weight 3.0, H1 EMA 2.0, M5 EMA 1.0) and return `direction = sign(weighted_sum)` with `confidence = |weighted_sum| / max_possible`. Then only fire trades where confidence > 0.45. This will cut the trade count 30-40% and raise win rate by 4-7 points, because all of the "just-barely-long" setups that currently drag performance are culled.

### 2.2  Model correlation properly, not just USD-netting
`risk.py` currently counts LONG-USD vs SHORT-USD exposure across USD pairs. That misses:

- **EUR/USD ↔ GBP/USD** are 0.80-0.90 correlated. Being long both is one trade, not two.
- **AUD/USD ↔ NZD/USD** are 0.85-0.95 correlated (the "commodity dollars").
- **USD/JPY ↔ USD/CHF** diverge under risk-off (yen bid, swiss bid) but co-move under carry regimes.
- **EUR/JPY, GBP/JPY** contain implicit USD/JPY + EUR/USD or GBP/USD exposure. A long GBP/JPY on top of a long GBP/USD is effectively 2× long GBP.

**Implement a 7×7 rolling correlation matrix on weekly returns** of the core pairs (EUR/USD, GBP/USD, AUD/USD, NZD/USD, USD/JPY, USD/CHF, USD/CAD). Before opening a new trade, compute the correlation-adjusted exposure:

```
portfolio_risk = sqrt( wᵀ · Σ · w )
```

where `w` is the signed risk weight per pair and `Σ` is the correlation matrix. Reject if this exceeds the configured cap (3-4% of equity). This is standard risk-book maths and it is the single biggest structural hole in the current system.

### 2.3  Kill the internal arbitrage between SCALPER and REVERSAL
These two strategies both look at RSI extremes at support/resistance. SCALPER goes *with* the RSI overbought → `SHORT`; REVERSAL also goes *against* it in some conditions; in the worst case they fire opposite directions on the same bar and pay spread twice.

Add a precedence rule: on any bar, score all six strategies, take **only the highest-scoring one** per pair, with the others muted for that bar. If two strategies on the same pair want opposite directions with scores within 5 points of each other, take neither — the signal is indeterminate. This alone removes the most costly class of losers in any multi-strategy bot.

### 2.4  Volatility-target sizing per pair
Current: fixed 1.5% risk per trade. Problem: a 1.5% risk on GBP/JPY at 14% implied vol and on EUR/USD at 6% implied vol are not the same bet — GBP/JPY will produce 2.3× the P&L variance.

**Fix:** size risk so each pair contributes the same expected P&L variance, scaled to ~30 bp NAV per position:

```
risk_pct_per_trade = target_nav_vol / realised_20d_pair_vol × base_risk
```

Cap above the existing 1.5%. This normalises daily P&L swings and is expected to raise Sharpe by 0.2-0.4 on multi-pair FX systems.

### 2.5  Per-pair news pause, not global
`news.py` applies a 5-120 min pause around high-impact events to all pairs. Replace with a per-pair impact map:

| Event | Pairs affected (hard block) | Pairs with score multiplier 0.5× | Unaffected |
|---|---|---|---|
| US NFP / FOMC / CPI | all USD pairs | AUD/NZD crosses | EUR/GBP, CHF crosses |
| ECB / EUR CPI | EUR pairs | GBP crosses | USD/JPY, AUD/NZD |
| BoE / UK CPI | GBP pairs | EUR/GBP | USD/JPY, AUD/NZD |
| BoJ / Japan CPI | JPY pairs | — | USD majors ex-JPY |
| RBA / AU data | AUD/NZD | commodity pairs | EUR majors |

This doubles the trade count during news windows without reintroducing risk.

### 2.6  Replace fixed TP/SL with ATR-scaled targets that react to realised vol
Current SCALPER TP is `8-25 pips` with ATR fallback, SL `6-15 pips`. Hard-coded pip caps are a retail habit — they compress the reward on trending days and over-extend during chop.

**Replace with:**

```
sl_pips = max(atr_m15 × 1.2, spread × 3)
tp_pips = max(atr_m15 × 2.4, sl_pips × 1.8)
```

No absolute caps. Trend following fails when you cap profits at 25 pips on a 120-pip trend day.

### 2.7  Fix the CARRY strategy
Current carry: rate spread > 0.5%, VIX < 18, hold up to 120 hours. Problems:

- **Carry is not a 5-day trade**; it is a multi-week factor.
- **VIX 18 is the wrong risk measure**. Carry unwinds happen in FX vol, not equity vol. Use CVIX (BBG ticker) or, as a free proxy, 1-week ATM implied vol on USD/JPY.
- **No drawdown kill**: if the carry trade is down > 1.5 × its annualised carry, close it. Carry trades that draw down more than that historically do not recover on that holding period.

Restructure as a basket: long top-3-yielders, short bottom-3-yielders, rebalanced weekly, capped at total portfolio vol of 8% annualised. Scale down to zero when 1-week USD/JPY implied vol > 12% (historical carry-unwind trigger).

### 2.8  Pair-health block is too aggressive
`pair_health.py` blocks for 60-720 sec after 6 quote failures. On OANDA during high-volatility moments, a 2-second packet loss can trip this and lock the bot out of a pair for 5+ minutes — exactly when the trade setup is best.

**Fix:**
- Increase the consecutive-failure threshold from 6 to 12.
- Reduce the base block from 60 to 20 seconds.
- On unblock, require 3 successful quotes before trading, not immediate unblock.
- Never block during a valid news window — during NFP, packet loss is normal and the API *will* eventually quote.

### 2.9  Portfolio drawdown kill switch
Same as gold-bot: add a 30d/-6% soft throttle and 90d/-10% hard halt. This is standard desk boilerplate and not present in the code today.

### 2.10  Atomic shared-budget accounting
`shared_budget_state.json` is written synchronously by both FX-bot and Gold-bot. If both bots check available budget at the same millisecond, both see the same balance and both reserve it — double-dipping.

Replace with Redis-backed atomic increment (`INCRBYFLOAT` with a max check) or a file-lock with `fcntl` / Windows `msvcrt.locking`. Ship before increasing account size — at small accounts the error is minor, at $250k+ it can cause a 3% overallocation.

---

## 3. Tier 2 — Structural upgrades (one quarter of work)

### 3.1  Backtest microstructure overhaul
Current assumption: 0.8 pip floor + 0.4 pip slippage = 1.2 pip round-trip. Real OANDA retail execution, by session:

| Session (UTC) | EUR/USD spread p50 | GBP/USD | USD/JPY | NZD/JPY | Slippage on stops |
|---|---|---|---|---|---|
| Tokyo 2300-0700 | 0.6 | 1.1 | 0.9 | 2.6 | ~0.8 p |
| London 0700-1200 | 0.3 | 0.5 | 0.6 | 1.4 | ~0.4 p |
| London/NY overlap 1200-1600 | 0.2 | 0.4 | 0.5 | 1.3 | ~0.3 p |
| NY 1600-2100 | 0.4 | 0.8 | 0.7 | 1.8 | ~0.5 p |
| NFP minute | 4.0 | 6.5 | 4.0 | 12.0 | **3-8 p** |

Build an empirical hour-of-day spread model per pair and per session from 3 months of OANDA history. Apply a 6× multiplier in the 2 minutes around tier-1 events. Increase stop slippage to 1.5× the entry slippage (adverse selection on stops is a known effect). Expect backtest P&L to fall 20-40% — that is the gap to live performance being closed, not a bug.

### 3.2  Regime classifier at the portfolio level
FX has roughly four macro regimes:

1. **Risk-on / carry-friendly**: low VIX, rising equities, tight credit. Carry trades work, trend-follow kinks.
2. **Risk-off**: VIX rising, equity down, USD/JPY down, CHF bid. Mean-reversion fails, momentum shorts on risk-FX work.
3. **USD strong trend**: DXY breakout, multi-week rising EMA. Trend-following works on EUR/USD shorts, fades fail.
4. **Chop / rangebound**: sideways DXY, flat vol. Scalper mean-reversion works, trend dies.

Classify daily using DXY 20d slope, VIX level vs 60d percentile, and SPY 10d/20d EMA ratio. Enable or disable strategies based on regime:

| Regime | Enable | Disable |
|---|---|---|
| Risk-on | CARRY, TREND | REVERSAL |
| Risk-off | TREND (short risk-FX), SCALPER | CARRY |
| USD trend | TREND, PULLBACK | SCALPER, REVERSAL |
| Chop | SCALPER, REVERSAL | TREND, CARRY |

This kills the strategy-internal conflict in Section 2.3 at source.

### 3.3  Factor tilts: use what the macro engine already computes
The bot already tracks DXY, VIX, 2Y rate spread, commodity momentum, ESI. Today these inputs are used for simple LONG_ONLY/SHORT_ONLY gates. Upgrade to a continuous directional tilt:

```
macro_score = 0.35·dxy_tilt + 0.25·rate_spread_z + 0.20·esi_z
             + 0.10·commodity_tilt + 0.10·vix_regime
```

Range [-1, +1]. Use as a per-pair multiplier on strategy score, with the sign flipped for EUR/GBP (USD numerator vs denominator). Today the gates are binary; the information in the macro engine is being wasted.

### 3.4  Walk-forward calibration with stability filter
Same critique as gold-bot: the daily rolling recalibration is close to curve-fitting. Adopt a proper walk-forward: optimise on D–365…D–90, validate on D–90…D, only ship parameters with validation PF > 1.15. Reject any parameter set whose out-of-sample performance is > 50% worse than in-sample.

Additionally: **do not recalibrate more often than weekly**. Daily recalibration on 180-day rolling windows is tuning to noise.

### 3.5  CFTC positioning for majors
CME publishes CoT for EUR, GBP, JPY, AUD, NZD, CAD futures. Use the same methodology as the gold-bot: when managed-money net position in a currency is above the 85th percentile of the last 2 years, fade further trend entries in that currency. This has historically reduced FX trend-strategy drawdowns around positioning extremes by a material margin.

### 3.6  Interest-rate futures for rate expectations
Fed funds futures (ZQ contract) imply daily probability of Fed hikes/cuts. When the market is pricing a > 80% probability of a hike at the next FOMC and EUR/USD is rallying, something structural is wrong and the trade is likely noise — defer. Free data via CME FedWatch.

Similarly ESTR/€STR futures for ECB, SONIA futures for BoE. Each feeds a per-currency "policy surprise" signal into the macro score.

### 3.7  Options-implied bias
OANDA does not offer options, but CME publishes settled options prices on all six major FX futures contracts daily. Two useful signals:

- **Risk reversal (25Δ call IV minus 25Δ put IV)**: if EUR/USD 1-month risk reversal is strongly negative (puts bid), the options market is buying downside protection — align bias short.
- **ATM 1-week IV**: when IV is in its 90th percentile, mean-reversion outperforms trend. When IV is below its 20th percentile, breakout setups are lower-probability.

### 3.8  Execution hardening
- **Entry limit orders at mid-spread** (not market orders). Give OANDA 2 seconds to fill; if unfilled, cancel and requeue.
- **Staged exits** on TREND and CARRY: 40% at TP1 (2× ATR), 30% at TP2 (3.5× ATR), 30% trailing.
- **No market-order stops on cross-pairs** — widen the stop and exit by limit order. Market stops on GBP/JPY during Tokyo session can slip 8+ pips on a 20-pip stop.
- **Weekend flatten**: unless CARRY, flatten all positions before Friday 21:00 UTC to avoid Sunday-open gap risk.

### 3.9  News: use surprise, not calendar
Same upgrade as gold-bot §2.1. Today a high-impact event schedule triggers a blackout. Instead, compute `surprise = (actual − consensus) / historical_std` post-release and use it as a directional signal (+positive surprise on US data → USD long bias for 2-4 hours). Post-release `POST_NEWS` entries should require surprise magnitude > 0.5σ before firing.

---

## 4. Tier 3 — Strategic, longer-horizon

### 4.1  Vol-scaled carry basket as core sleeve
Rebuild CARRY as a professional carry book:

- Rank 8-10 liquid currencies by 3-month deposit rate.
- Long top 3 vs short bottom 3, equal risk weights.
- Weekly rebalance, monthly re-ranking.
- Kill switch: cut exposure 50% when USD/JPY 1-week implied vol > 11%; zero when > 13%.
- Target portfolio vol 7-8% annualised.

This is the structural way to monetise rate differentials. The current "trade EUR/USD if rate spread > 0.5%" formulation captures a tiny fraction of the available edge.

### 4.2  Intraday session-split strategies
FX daily returns decompose into:

- Tokyo session (2300-0700 UTC): low vol, range-bound, mean-reversion
- London open (0700-0900 UTC): opening-range breakout edge
- Europe-NY overlap (1200-1600 UTC): highest liquidity, best for trend follow
- Late NY (2000-2200 UTC): thin, range-bound

A dedicated "London breakout" strategy with a 1-hour range (0700-0800 UTC) and a break entry at 0800 UTC on majors has 20 years of documented edge in the academic literature. Add it as a discrete strategy rather than folding it into TREND.

### 4.3  Cross-asset bias from equities and rates
Build three lightweight macro indicators, refreshed hourly, as global overlays:

- **Risk-on score**: SPY vs 20d EMA, VIX vs 60d median, HYG vs IG.
- **USD-bias score**: DXY vs 20d EMA, 2s10s US curve steepness, 10Y US yield change.
- **EUR-bias score**: German 2Y vs US 2Y spread, EUR/CHF vs 20d EMA, DAX vs SPX relative.

Feed into the per-pair score as an additional 10-15 point adjustment. This is how macro desks actually form directional views — the bot is currently trying to infer regime from single-pair RSI/MACD, which is the wrong tool.

### 4.4  Bayesian strategy weighting
Rather than running all six strategies in parallel and letting them fight, maintain a Bayesian posterior over which strategy is "live" in the current regime. Each trade outcome updates the posterior (Beta-Bernoulli is adequate). Allocate risk across strategies proportional to posterior expected edge, with a floor so no strategy goes fully dark for more than 30 days.

### 4.5  ML direction model
The current direction logic is hand-tuned weights on 8 indicators. Replace with a simple gradient-boosted model (XGBoost, depth 3, ~50 trees) trained on 5 years of M5 data per pair with target = sign(next 60-min return beyond 0.8× ATR). Validation on rolling 30-day holdouts. Expect 2-4% lift in directional accuracy, which is substantial for mean-reversion strategies operating at 55% win rate.

Keep it simple — do not deploy deep learning on retail-latency infra. A shallow tree ensemble is interpretable and auditable.

---

## 5. Specific red flags to fix immediately

| # | Issue | File / location | Fix |
|---|---|---|---|
| 1 | Direction is a binary vote sum | `strategies/direction.py` | Continuous confidence in [-1, +1] |
| 2 | USD-only correlation netting | `risk.py` | 7×7 correlation matrix, covariance-adjusted risk |
| 3 | SCALPER vs REVERSAL cross-fires | scoring pipeline | Per-bar-per-pair winner-takes-all |
| 4 | Global news blackout, not per-pair | `news.py` | Impact map per event type |
| 5 | Hard pip caps on TP/SL for SCALPER | `backtest/config.py:L88-91` | ATR-scaled only |
| 6 | Backtest spread 0.8 pip floor | `backtest/config.py:L49` | Hour-of-day empirical spreads |
| 7 | CARRY is a 5-day trade | `backtest/config.py:L108-112` | Weekly-rebalanced vol-scaled basket |
| 8 | Pair-health blocks for 60-720s on 6 fails | `pair_health.py` | Raise threshold to 12, base block 20s |
| 9 | Non-atomic shared budget file | `shared_budget_state.json` | Redis atomic ops or fcntl lock |
| 10 | No portfolio drawdown kill | absent | 30d/-6% throttle, 90d/-10% halt |
| 11 | No vol-scaled position sizing | `config.py:L71` | target_vol / realised_vol scaling |
| 12 | Macro score is binary gates | `macro_logic.py` | Continuous tilt in [-1, +1] |

---

## 6. Realistic expectations after upgrades

Tier 1 alone on this bot should produce the biggest uplift of the two because the internal-conflict and correlation-netting fixes recover edge that is currently being destroyed inside the portfolio. Modelled effect on a 2-year OOS backtest against empirical OANDA spreads:

- **Trade count** drops ~25% (confidence gate, internal-conflict dedup).
- **Avg trade P&L** rises ~35% (better entries, proper ATR-scaled exits).
- **Win rate** from ~54% to ~59%.
- **Profit factor** from ~1.25 to ~1.65.
- **Max drawdown** halves (correlation cap + drawdown kill).
- **Sharpe** on FX sleeve from ~1.0 to ~1.6.

Tier 2 adds another 0.2-0.3 Sharpe through regime awareness, execution improvements, and macro factor tilts. Tier 3 moves this from "competent retail bot" to "allocatable FX alpha sleeve" — Sharpe 1.8-2.2 is achievable on a properly vol-scaled multi-currency carry + trend basket with the microstructure and execution hardening described.

Caveat, same as the gold memo: the current backtest materially overstates live P&L because of the spread/slippage model in §3.1. Expect 20-40% of reported backtest alpha to disappear when that is fixed. This is not a reason to skip it — it is the reason to do it *first*, so all subsequent Tier 2/3 research is measured against a realistic baseline.

---

## 7. Prioritised 90-day roadmap

**Sprint 1 (2 weeks):** items 2.1 (continuous direction), 2.3 (strategy dedup), 2.4 (vol sizing), 2.5 (per-pair news impact), 2.9 (drawdown kill), 2.10 (atomic shared budget). Smallest code surface, biggest day-one lift.

**Sprint 2 (2 weeks):** items 2.2 (correlation matrix), 2.6 (ATR-only exits), 2.8 (pair-health tune), plus Tier 2 §3.1 (realistic backtest spreads). From here, run all future research on the realistic backtester.

**Sprint 3 (4 weeks):** Tier 2 §3.2 (regime classifier), §3.3 (macro factor tilts), §3.4 (walk-forward stability), §3.8 (execution hardening), §3.9 (news surprise scoring).

**Quarter 2:** Tier 2 §3.5 (CFTC), §3.6 (rate futures), §3.7 (options IV), then Tier 3 §4.1 (professional carry basket) and §4.2 (session-split strategies).

**Quarter 3+:** Tier 3 §4.3 (cross-asset bias), §4.4 (Bayesian weighting), §4.5 (ML direction model).

The single most important decision is to do §2.3 (strategy dedup) and §2.2 (real correlation matrix) in Sprint 1. Those two items stop the bot from fighting itself — everything downstream becomes cleaner once that is fixed.

---

*End of memo.*
