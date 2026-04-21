# Futures Bot (MEXC Perpetuals) — Trading Desk Review & Improvement Memo

**Prepared by:** Senior PM, Digital Assets & Crypto Alpha Desk
**Strategy under review:** BTC/ETH-centric perpetual-futures trader (`futuresbot/`)
**Exchange:** MEXC Contract (perpetuals)
**Perspective:** What changes would be required for this system to be approved as a leveraged-crypto sleeve at a prop desk.

---

## 1. Executive summary

This bot trades a coil-breakout + trend-continuation strategy on MEXC perpetual futures, nominally BTC-focused with a configurable multi-symbol list, correlation buckets, funding-rate guards, session-hour gating, and per-symbol parameter overrides. It is a much cleaner, more focused stack than the spot sibling — one strategy, one primary timeframe (15m on the execution leg, 1h on the context leg), and a well-instrumented risk envelope. The logic is sound.

The problem is that high leverage on crypto perps is a completely different risk product from spot, and the current configuration treats leverage as a knob rather than as the dominant P&L driver. A few numbers to orient:

- [`FuturesConfig`](futuresbot/config.py) defaults to `leverage_min=20` / `leverage_max=50`.
- `hard_loss_cap_pct=0.75` — i.e. a single trade is allowed to lose 75% of posted margin before the runtime intervenes.
- At 30× leverage with a 75% margin-loss cap, the permitted adverse price move is 2.5% before force-close. MEXC BTC perps routinely print 2-4% 15-minute wicks around macro prints.
- `min_reward_risk=1.15` — below the desk-standard 1.8-2.0× minimum required to clear trading costs, funding, and the asymmetric slippage profile of leveraged liquidations.
- Single-strategy dependency. There is no diversified edge — if the coil-breakout regime stops paying (chop markets, mean-reverting vol), there is no other book running.

Headline issues:

1. **Liquidation buffer is too thin given leverage.** With 20-50× leverage, even a well-placed stop is too close to the liquidation price to survive normal intraday volatility. A stop at 2% below entry at 40× leverage leaves an 80% margin impairment — but a liquidation cascade (funding rate spike, 1 wick) can still close you at −100% before the stop fills.
2. **No funding-rate adverse-selection model.** Funding is currently a single kill-switch (`funding_rate_abs_max`). Real perps trading requires netting funding cost against expected holding-period return, and preferring entries in the direction that *receives* funding over ones that pay it.
3. **`min_reward_risk=1.15` is non-viable after costs.** Round-trip taker fees on MEXC contracts = 4 bps + 4 bps. Average funding on BTC perp ≈ 0.01%/8h = 30 bps/month. Expected slippage at 30× leverage during exit ≈ 10-30 bps. Real breakeven R:R is closer to 1.5-1.7 before any edge. The system is entering trades with < 1 expected R.
4. **Single strategy = single point of failure.** The coil-breakout logic is a trend-follower in disguise; it makes money in directional regimes and bleeds in chop. 2024-07 → 2024-09 BTC chop would have produced a multi-month drawdown on this strategy alone.
5. **Static leverage selection.** [`_leverage_for_signal`](futuresbot/strategy.py) derives leverage from `certainty × (max-min)` plus a risk cap — but the `certainty` formula is a clipped linear map of raw score above threshold. Certainty is not a probability, and treating it as one size the book monotonically to the most confident signals, which are empirically the most curve-fit ones.
6. **Execution is market-at-signal.** There is no limit-order entry, no funding-time awareness (avoid entering 2min before funding settlement), no VWAP/TWAP on exit. At 30×+ leverage an extra 5 bps of slippage per leg = 3% of margin per trade — this is the dominant cost, not fees.
7. **`recv_window_seconds=30` is 6× the institutional standard.** At 30 seconds of clock-skew tolerance, a stale order can execute under materially different market conditions than the signal that generated it. Standard is 5 seconds; anything looser is a latency-attack surface.
8. **Multi-symbol architecture exists but isn't portfolio-level.** Correlation buckets cap concurrent positions per bucket, but there is no aggregate volatility target, no VaR, and no cross-symbol P&L correlation monitoring. All symbols on this bot are directional crypto — real correlation at intraday horizon is 0.75-0.92, not the bucket-of-one assumed.
9. **No basis, no funding carry, no delta-neutral book.** The entire book is outright directional on leverage 20-50×. The cleanest edges in crypto perps — funding capture, basis arb, liquidation cascade fades — are all unimplemented.
10. **Backtest doesn't model funding or liquidation dynamics.** [`futuresbot/backtest.py`](futuresbot/backtest.py) applies TP/SL against OHLC. In a real perp, positions accrue funding every 8 hours, and liquidation can fire before a technical stop during a wick. The reported backtest P&L is structurally optimistic.

The recommendations below are organised in three tiers: Tier 1 (ship in one sprint, immediate risk-reduction), Tier 2 (structural, one quarter), Tier 3 (strategic).

---

## 2. Tier 1 — Cheap fixes that compound quickly

### 2.1  Cut maximum leverage to 10× and anchor to 1R = 1% NAV
`leverage_max=50` is a retail casino parameter. Reduce to `leverage_max=10`, `leverage_min=5`. Rebuild sizing so that each trade risks exactly 1% NAV on stop-out (not 1% of margin, not "hard_loss_cap × margin"):

```
qty_contracts = (nav × 0.01) / (|entry − sl| × contract_value)
leverage_applied = notional / margin_allocated
```

This is **the** institutional risk convention. It makes every trade comparable in risk terms across volatility regimes, and it caps per-trade drawdown at a number that is statistically survivable (15 sequential stop-outs = −15% NAV, within acceptable tail). The current configuration permits tail outcomes where 3 sequential adverse days produce a 50%+ NAV loss.

### 2.2  Lift `min_reward_risk` to 1.8 after explicit cost budgeting
Compute the true cost of a trade:

```
cost_bps = taker_fee_in + taker_fee_out + expected_slippage_bps + expected_funding_bps_over_hold
```

Require `tp_distance / (sl_distance + cost_bps) >= 1.8`. For any signal where the projected hold time implies > 20 bps funding cost, require 2.0×. This converts the R:R from a notional ratio into a net-expectancy ratio, and structurally filters out the 40% of current entries that are sub-economic.

### 2.3  Funding-aware entry timing
Add three rules:

- **Never open** a new position in the 2 minutes before funding settlement (00:00, 08:00, 16:00 UTC) unless the signal direction receives funding.
- **Prefer** signal direction that receives funding. When funding > 0.03%/8h and signal is short, the short receives ~30 bps/month as a baseline carry before any directional move. This is a genuine edge component and should flow into the score.
- **Add** funding-rate-z-score as a scoring input: extreme positive funding (> 95th percentile) crowded-longs → fade signal, tighter stops on longs, looser stops on shorts.

### 2.4  Tighten `recv_window_seconds` to 5
No legitimate use case for 30s. Tightening this closes a stale-order attack surface and forces the runtime to retry-with-fresh-signature on clock-skew, which is the correct behaviour.

### 2.5  Pre-liquidation buffer monitoring
At the top of every price-check cycle, compute distance-to-liquidation in ATR units:

```
atr_to_liq = (entry − liq_price) / atr_15m          # for longs
```

If `atr_to_liq < 2.0`, force-close at market immediately, regardless of technical stop. A position less than 2 ATRs from liquidation is statistically certain to fill at liquidation (adverse selection on the exit) within the next 1-2 bars. Better to take a manageable stop than a forced liquidation + funding-rate cascade penalty.

### 2.6  Hard cap `hard_loss_cap_pct` at 0.40, not 0.75
Even assuming you keep higher leverage in the interim, 75% margin loss per trade is a book-destroying single-trade tail. 40% margin loss × 10× leverage = 4% NAV loss in a single trade before the runtime intervenes, which is the absolute ceiling on institutional risk discipline.

### 2.7  Portfolio-level drawdown kill
Same specification as the FX/spot memos: 30d/−8% soft throttle (halve position sizes and leverage), 90d/−15% hard halt (paper-trade mode until reset by operator). The tail risk on a leveraged crypto book justifies a slightly wider halt than spot, but the throttle needs to be there.

### 2.8  Session gating needs alignment, not just hours
The `session_hours_utc` gate is free-text hours. Replace with three named sessions mapped to expected vol / liquidity:

- Asia (00-08 UTC): lower volume, wider spreads, lower leverage (cap 5×).
- London (07-16 UTC): full leverage allowed, best liquidity.
- US (13-21 UTC): highest volatility around 13:30 CPI / 14:00 FOMC minutes — full leverage but score threshold +10 points during ±15min of scheduled events.

### 2.9  Stop-loss tightening in high-funding regimes
When absolute funding rate > 0.06%/8h on a symbol, the market is overheated in one direction. Tighten stops on positions aligned with the crowded direction to 0.7× ATR, widen stops on counter-positions to 1.2× ATR. Let the crowded-side shakeouts work in your favour.

---

## 3. Tier 2 — Structural upgrades (one quarter of work)

### 3.1  Real backtest: funding + liquidation + slippage model
Current backtest checks TP/SL against OHLC only. Real perp P&L requires:

- **Funding accrual** every 8h window spent open, using historical funding for that symbol-exchange.
- **Liquidation simulation**: if any bar's low (for longs) / high (for shorts) breaches `liq_price`, close at `liq_price × (1 − 0.005)` (liquidation slippage) regardless of where the TP/SL sit.
- **Slippage by leverage**: 0.5 bps per 1× leverage on entry, 1.5× that on exit (stops adversely-selected).
- **Funding at open/close**: if opened ≤ 2min before funding, pay/receive full funding; otherwise pro-rata.

Expect reported backtest returns to fall 30-50% after this is implemented. That is the gap to live performance being closed, not a bug.

### 3.2  Second strategy: mean-reversion in chop regimes
The current coil-breakout logic loses in mean-reverting markets (structurally — it enters on breakouts, gets chopped). Add an orthogonal mean-reversion strategy that fires *only* when a portfolio-level regime classifier flags "chop":

- Regime = range-bound if 20d BTC slope |·| < 2% and ADX(1h) < 18 and realised vol percentile < 30.
- Mean-reversion entry: price > 2σ Bollinger band on 1h + RSI > 72 → short; mirror for long.
- TP = mean (EMA20), SL = 1.2× ATR beyond the band.
- Runs only when regime flag is active. Muted in trend regimes.

This dramatically smooths the equity curve by pairing two orthogonal edges rather than running one strategy through every regime.

### 3.3  Regime classifier at portfolio level
Formalise the regime logic above into a single daily classifier with 4 states: **TREND_UP**, **TREND_DOWN**, **CHOP**, **VOL_SHOCK**. Rotate strategies:

| Regime | Enable | Disable |
|---|---|---|
| TREND_UP | coil-breakout longs, trend-continuation longs | mean-reversion, shorts |
| TREND_DOWN | coil-breakout shorts, trend-continuation shorts | mean-reversion, longs |
| CHOP | mean-reversion (both sides) | coil-breakout |
| VOL_SHOCK (realised vol > 90th pct) | — | all (paper-trade until regime clears) |

The current runtime has no regime classifier; it runs the same strategy through all states.

### 3.4  Walk-forward calibration with stability filter
Same specification as every other bot in the stack. Daily calibration on a 60-day rolling window is tuning to noise. Weekly walk-forward with OOS PF > 1.15 gate, explicit IS/OOS degradation filter (reject if OOS performance is > 40% worse than IS). Reject parameter sets that fail.

### 3.5  Execution: maker-first with taker-fallback, pre-funding avoidance
Replace market-at-signal with a 5-step ladder:

1. Post a limit at mid ± 1 tick (maker side).
2. Wait 2 seconds. If not filled, reprice mid ± 2 tick.
3. Wait 2 seconds. If not filled, reprice mid ± 4 tick.
4. Wait 1 second. If not filled, cross the spread as taker.
5. If within 90 seconds of funding settlement, skip limit attempts and cross spread immediately (to guarantee fill before settlement).

This captures 30-50% of entries at maker rebate (MEXC perps offer negative maker fees at VIP 2+), improving net cost by 6-8 bps per round-trip.

### 3.6  Cross-symbol VaR and correlation
Replace `correlation_buckets` + `max_per_bucket` with a proper daily-rebuilt correlation matrix on 4h returns across all tracked symbols. Before opening a position, compute:

```
portfolio_vol = sqrt( w.T @ Σ @ w )
```

Reject entries that push `portfolio_vol` above 8% annualised. Today the bucket system treats BTC/ETH/SOL as fully independent; in reality their 4h return correlation runs 0.75-0.90 and the "diversification" is illusory.

### 3.7  Liquidation-cascade fade strategy
When aggregate long-liquidation volume on a symbol exceeds 95th-pct of the 30d window within a 15-minute bar, that symbol is statistically oversold on a 4-hour horizon. Fade the move: enter a counter-position at 0.5-0.8× the normal size, with a tighter TP (1.5× ATR) and a 2× ATR stop. This is a well-documented edge in crypto perps (see Coinalyze / Coinglass liquidation data). Requires a liquidation data feed (free via Coinglass API).

### 3.8  Funding-delta-neutral carry book (as a separate sleeve)
The cleanest edge in crypto is perp funding capture with a delta hedge. When BTC 8h funding > 0.03%:

- Long BTC spot (on cheapest exchange).
- Short equivalent BTC perp notional on MEXC.
- Collect funding every 8h, pay 2× taker fees on entry/exit, pay borrow on the spot side.

Net carry historically runs 8-15% annualised on BTC, 12-25% on altcoin perps, with near-zero directional exposure and Sharpe 2.5-4. This is a completely separate sleeve from the directional strategy. Target 20-30% of equity allocated to this sleeve at institutional size.

### 3.9  Execution monitoring and slippage attribution
For every fill, record:
- Quoted price at signal
- Fill price
- Slippage bps
- Maker vs taker
- Distance from funding settlement

Surface weekly slippage attribution to the ops Telegram. Today there is no visibility into how much alpha is lost at execution vs strategy. At 30× leverage this is the dominant cost component and it should be the most instrumented part of the stack.

---

## 4. Tier 3 — Strategic, longer-horizon

### 4.1  Basis trade (quarterly futures)
When the BTC or ETH quarterly future (CME for BTC/ETH; OKX/Binance/Deribit for quarterlies) trades at > 8% annualised premium to spot, go long spot / short future, hold to expiry or roll. Historically Sharpe 3-5 at 6-12% annualised with < 2% drawdown. This is the single cleanest sleeve available in crypto. Requires cross-venue margin management (OKX for the future leg is the cheapest retail option that supports it).

### 4.2  Options overlay (Deribit)
For BTC/ETH directional positions, hedge tail risk with 5-delta weekly puts. Cost: ~15 bps per week on notional. Benefit: caps liquidation risk at the option strike regardless of perp leverage. At 10× leverage with a weekly 5-delta hedge, the liquidation-tail risk collapses to approximately zero. This is how a real book runs leverage safely.

### 4.3  Cross-exchange arbitrage
MEXC perp prices can deviate 5-30 bps from Binance/OKX/Bybit for 10-120 seconds during vol spikes. With low-latency (colocated) connections this is a near-riskless arb, historically producing 1-4% annualised with Sharpe 5+. At retail latency the opportunity surface is 70-80% smaller but still deliverable at ~6% annualised. Requires multi-venue account setup and a thin routing layer; logic is simple.

### 4.4  Liquidity provision on perps (market-making)
At $1M+ book size, passive market-making on MEXC mid-cap perps pays 2-4 bps in rebates per round-trip while delta-neutralising the inventory. Historically Sharpe 4-6 with very low correlation to the directional book. This is the "always on" revenue line at institutional scale.

### 4.5  ML-based regime classification
The regime classifier in §3.3 is rule-based. Replace with a 3-class classification model (LightGBM, depth 4) trained on 2 years of: BTC 20d slope, BTC dominance, aggregate alt funding, realised vol, order-book imbalance. Target = strategy that most outperformed in the next 7 days. Rolling 30-day holdouts. Expected lift: 5-8% in strategy-selection accuracy, which translates to ~0.3-0.5 Sharpe on the blended book.

### 4.6  Proper multi-account margin optimisation
At institutional size the bottleneck becomes margin efficiency across exchanges. A single-venue (MEXC) book is margin-inefficient vs a multi-venue setup where Binance futures carries the directional book, OKX carries the basis trade, Bybit carries the funding carry, and Deribit carries the options hedge. Each venue specialises in its cheapest product. A cross-venue margin optimiser lowers the capital required to run the same book by 30-45%.

---

## 5. Specific red flags to fix immediately

| # | Issue | File / location | Fix |
|---|---|---|---|
| 1 | `leverage_max=50` with `hard_loss_cap_pct=0.75` | [`futuresbot/config.py`](futuresbot/config.py) | `leverage_max=10`, risk = 1% NAV/trade |
| 2 | `min_reward_risk=1.15` — sub-breakeven after costs | [`futuresbot/config.py`](futuresbot/config.py) | Raise to 1.8 net of fees+funding |
| 3 | No funding-cost model in entry decision | [`futuresbot/strategy.py`](futuresbot/strategy.py) | Net expected funding into R:R calc |
| 4 | `recv_window_seconds=30` — 6× institutional standard | [`futuresbot/config.py`](futuresbot/config.py) | Tighten to 5 |
| 5 | No liquidation-buffer monitor | [`futuresbot/runtime.py`](futuresbot/runtime.py) | Force-close if distance-to-liq < 2 ATR |
| 6 | Single strategy = single point of failure | [`futuresbot/strategy.py`](futuresbot/strategy.py) | Add mean-reversion in chop regimes |
| 7 | Backtest ignores funding + liquidation | [`futuresbot/backtest.py`](futuresbot/backtest.py) | Funding accrual + liq-cascade simulation |
| 8 | No portfolio drawdown kill switch | absent | 30d/−8% throttle, 90d/−15% halt |
| 9 | Correlation buckets ≠ real correlation | [`futuresbot/runtime.py`](futuresbot/runtime.py) | Full matrix, VaR cap 8% annualised |
| 10 | No pre-funding entry avoidance | entry scheduling | Block entries ±2min of funding unless receiving |
| 11 | No regime classifier | absent | 4-state daily regime, strategy rotation |
| 12 | Market-at-signal execution, no maker ladder | order path | 5-step maker ladder with taker fallback |
| 13 | Daily calibration on 60d = curve-fit | calibration.py | Weekly walk-forward, OOS PF > 1.15 |
| 14 | No cross-venue, no basis, no carry sleeve | absent | Add funding-capture + basis trade books |

---

## 6. Realistic expectations after upgrades

Tier 1 alone is primarily risk-reduction — reported backtest returns will likely fall because the leverage cap reduces gross notional, but realised drawdown should roughly halve and Sharpe should rise from ~1.0 to ~1.4 on the directional book. A single-strategy leveraged-crypto book cannot realistically target much above Sharpe 1.5 after honest cost modelling.

Tier 2 is where the real alpha arrives. Adding a second orthogonal strategy (mean-reversion), a proper regime classifier, and maker-first execution should lift the directional sleeve to Sharpe 1.8-2.2 with max drawdown contained in the 8-12% range on a sub-year window.

Tier 3 reshapes the book. A funding-carry sleeve (§3.8 / §4.1) is structurally superior to any directional strategy on Sharpe and is the single most important addition to this stack. At institutional size the carry + basis + market-making stack should target 30-45% annualised at Sharpe 2.5-3.5, which is a competitive perpetuals-focused crypto fund profile.

Caveat: the current backtest materially overstates live P&L because of the funding + liquidation + slippage model gaps in §3.1. Expect 30-50% of reported backtest alpha to disappear when that is fixed — larger than any other bot in the stack because leverage amplifies the gap. Do §3.1 first; every downstream measurement depends on it.

---

## 7. Prioritised 90-day roadmap

**Sprint 1 (2 weeks):** items 2.1 (10× leverage + 1% NAV risk), 2.2 (R:R ≥ 1.8 net of costs), 2.4 (recv_window), 2.5 (liquidation buffer), 2.6 (hard loss cap), 2.7 (drawdown kill), 2.8 (session-aligned leverage). Ships the system from "retail leverage casino" to "institutional risk envelope".

**Sprint 2 (2 weeks):** items 2.3 (funding-aware entry), 2.9 (funding-regime stop tightening), plus §3.1 (backtest overhaul — funding + liquidation + slippage). From this point forward all research runs against the realistic backtest.

**Sprint 3 (4 weeks):** §3.2 (mean-reversion strategy), §3.3 (regime classifier), §3.4 (walk-forward calibration), §3.5 (maker-first execution), §3.6 (cross-symbol VaR), §3.9 (slippage attribution).

**Quarter 2:** §3.7 (liquidation-cascade fade), then Tier 3 §4.1 (basis trade) and §3.8 (funding-delta-neutral sleeve). These are the two highest-Sharpe additions available.

**Quarter 3+:** Tier 3 §4.2 (options overlay), §4.3 (cross-exchange arb), §4.4 (market-making sleeve), §4.5 (ML regime), §4.6 (multi-venue margin).

The single most important decision is to do §2.1 (lower leverage + 1%-NAV risk sizing) and §3.1 (realistic backtest) in Sprint 1/2. Those two items turn this from a configuration that can wipe the book in a bad week into one that can be allocated institutional capital. Everything downstream becomes measurable once that is in place.

---

*End of memo.*
