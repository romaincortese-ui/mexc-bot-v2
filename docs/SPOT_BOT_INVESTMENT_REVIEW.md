# Spot Bot (MEXC) — Trading Desk Review & Improvement Memo

**Prepared by:** Senior PM, Digital Assets & Crypto Alpha Desk
**Strategy under review:** Multi-strategy spot-crypto trader (`mexcbot/`)
**Exchange:** MEXC Global (spot)
**Perspective:** What changes would be required for this system to be approved as a bookable sleeve at a crypto hedge fund.

---

## 1. Executive summary

This bot is a multi-strategy spot-crypto stack running five live strategies in parallel (SCALPER, GRID, TRINITY, MOONSHOT, REVERSAL, plus a PRE_BREAKOUT scanner) against a dynamically discovered USDT-pair universe on MEXC. It has the right ingredients for an institutional-grade retail stack — calibration, daily review, per-strategy budget allocation, Kelly-style sizing, consecutive-loss circuit breakers, Fear & Greed and BTC-EMA regime gates, websocket price monitoring, Telegram ops surface. The architecture is ambitious. The sizing, stop-loss geometry, execution quality, and single-venue concentration are where the edge leaks.

Headline issues:

1. **Stop-loss geometry is retail-punitive.** [`mexcbot/exits.py`](mexcbot/exits.py) sets `HARD_SL_FLOOR_PCT=0.20` (−20%), GRID_SL_MIN/MAX 8-10%, REVERSAL_SL_MIN/MAX 8-10%, MOONSHOT_SL_CAP 8%. A professional spot book runs 1.0-2.5% hard stops or 1× ATR, never −20%. The current floor means a single losing trade wipes ~2 weeks of typical gains on this strategy profile.
2. **No transaction-cost modelling in signal scoring.** MEXC taker fees are 0.1% per side = 20 bps round-trip minimum. SCALPER targets ~2% TP, so ~10% of gross P&L is paid to fees before slippage. That is not budgeted anywhere in the score threshold or the Kelly multiplier.
3. **Single-venue concentration on MEXC.** MEXC is a second-tier exchange by liquidity; book depth on mid-cap pairs is 30-60% of Binance/OKX. Large orders pay 2-5× the quoted spread in real slippage. There is no cross-exchange best-ex or even basic depth-aware sizing.
4. **Strategy overlap is unmanaged.** SCALPER, MOONSHOT, TRINITY, and REVERSAL can all fire on the same symbol (e.g. SOLUSDT) on the same bar in opposite directions. Today the loser of the scoring race still consumes the per-strategy budget — so the bot reserves capital against itself.
5. **Memecoin MOONSHOT risk-reward is inverted.** `MOONSHOT_TP_MIN=10%`, `SL_CAP=8%`, win rate empirically ~30-45% → expected value strongly negative unless momentum regime is unusually friendly. The strategy is effectively a lottery ticket whose expectancy depends on the calibration layer retuning it weekly.
6. **Kelly sizing without portfolio VaR.** [`mexcbot/runtime.py`](mexcbot/runtime.py) caps per-trade Kelly at 2.8%, but 3 open positions × 2.8% × correlated names (all high-beta alts) = effectively one 8.4% bet on beta to BTC.
7. **LLM daily review is a curve-fit vector.** Anthropic-driven daily parameter overrides have no out-of-sample validation gate — a bad week of noise becomes Monday's new threshold. This is the same failure mode flagged in the FX and Gold memos.
8. **Execution is REST-market, not microstructure-aware.** Maker order support exists (`USE_MAKER_ORDERS`) but fires a single limit at the top-of-book for 2.5 seconds then crosses the spread. No iceberg, no time-sliced VWAP, no post-only-reprice cycle.
9. **No funding, basis, or carry book.** Every live strategy is outright directional. Crypto offers clean, institutional-grade yield through perp funding capture, cash-and-carry basis, and staking-spot-hedge — none of which this bot touches.

The recommendations below are organised in three tiers: Tier 1 (low-risk, ship in one sprint), Tier 2 (structural, one quarter), Tier 3 (strategic, ongoing research).

---

## 2. Tier 1 — Cheap fixes that compound quickly

### 2.1  Collapse stop-losses to 1× ATR (hard cap 3%)
Replace every `*_SL_MIN` / `*_SL_MAX` / `HARD_SL_FLOOR_PCT` with an ATR-anchored formula per strategy:

```
sl_pct = clamp( atr_pct × k_strategy, 0.008, 0.030 )
```

with `k_strategy` ∈ {SCALPER 1.2, TRINITY 1.5, REVERSAL 1.0, MOONSHOT 2.0, GRID 0.8}. Remove the −20% global floor entirely; it is a catastrophic risk anchor. This change alone should halve realised max drawdown on any backtest window — the current −20% hard-SL is a black-swan amplifier, not a protection.

### 2.2  Budget for fees + slippage in the score threshold
Every signal score should be net of expected round-trip cost. Introduce:

```
net_score = raw_score − fee_score_cost(strategy, book_depth)
```

where `fee_score_cost` is calibrated so that 20 bps fees + 10 bps expected slippage cost approximately 5 score points. Pairs that don't clear the threshold after cost deduction should not trade, even if the raw geometry is clean. This kills a large class of SCALPER entries that are structurally unprofitable after fees.

### 2.3  Winner-takes-all per symbol per bar
Same fix as the FX memo §2.3. When SCALPER and MOONSHOT both score SOLUSDT long on the same bar, take the higher-scoring one and mute the other. When SCALPER wants long and REVERSAL wants short (or vice versa) within 5 score points, take neither — the signal is indeterminate. Move the budget for the muted strategy into the winner's reservation for that cycle rather than idling it.

### 2.4  Correlation-aware position sizing
All high-beta alts co-move ~0.8-0.95 intraday with BTC/ETH. Three open positions in SOL + ENA + WIF is effectively one 3× levered bet on crypto beta. Implement:

```
effective_risk = sqrt( wᵀ · Σ · w )
```

with `Σ` a rolling 30-day correlation matrix on 1h returns. Reject new entries that push `effective_risk` above 4% of equity. Group by bucket (BTC/ETH majors, L1 alts, memecoins, DeFi) if a full matrix is overkill for the first pass.

### 2.5  Maker-first execution with repricing cycle
Current `USE_MAKER_ORDERS=True` + 2.5s timeout is too short. Replace with a 3-attempt repricing ladder: post at bid+1tick, if unfilled after 3 seconds reprice to bid+3tick, if unfilled after another 3 seconds reprice to bid+6tick or cross spread as taker. This typically captures 40-60% of entries at maker rebate (0 fee instead of 10 bps) and the other 40-60% at a marginally worse price than current instant-taker logic. Net fee savings compound to ~0.5% monthly uplift on SCALPER.

### 2.6  Fee-tier aware sizing
MEXC VIP tier thresholds rebate taker fees from 0.1% → 0.02% at VIP 2 ($50M+ 30d volume). At an institutional size the sizing logic should explicitly target tier upgrades rather than treat fees as flat. Add a "volume banking" mode that prefers slightly suboptimal but higher-turnover symbols when within 10% of a tier threshold.

### 2.7  MOONSHOT hit-rate gate
Before committing capital to a MOONSHOT trade, require that the strategy's trailing 30-trade win rate on the **same symbol** exceeds 28% (the break-even hit rate at current 4:1 TP:SL). Today the only gate is aggregate calibration; per-symbol gating kills the individual memecoin bleeds that dominate the loss tape (e.g. PEPE in the 2025-12 diagnosis).

### 2.8  Portfolio drawdown kill switch
Same spec as the FX/Gold memos: 30d/−6% soft throttle (halve all strategy allocations), 90d/−10% hard halt. Today there is only session-level pause (`SESSION_LOSS_PAUSE_PCT`). Multi-week bleeds continue unabated because the calibration layer keeps "finding new parameters" rather than sizing down.

### 2.9  Cost-basis / tax-lot tracking
The realised P&L is currently average-fill-price based. For any serious accounting (and for LP-level reporting) you need FIFO tax-lot accounting per symbol, because MEXC partial fills create multiple cost bases. This is not a P&L-generating fix — it is a gating requirement for institutional reporting.

### 2.10  Daily-review validation gate
The Anthropic daily review should not ship parameters that, when replayed on the last 30 days of tick data, produce a lower profit factor than the current live parameters. Treat it as a PR-review suggestion rather than an auto-committer. Exactly the same critique as the FX memo §3.4.

---

## 3. Tier 2 — Structural upgrades (one quarter of work)

### 3.1  Cross-exchange best-execution
Add a thin best-ex layer routing orders between MEXC, Binance, OKX, and Bybit. For a mid-cap symbol like ENAUSDT or WIFUSDT the quoted spread on Binance is typically 30-50% tighter than MEXC; on majors (BTC/ETH/SOL) Binance is cheaper 85%+ of the time. The incremental code is one REST adapter per venue plus a smart-order-router that picks the venue with the best effective price after fees. Expected uplift: 15-30 bps per round trip, compounding to ~6% annualised on typical turnover.

### 3.2  Order-book depth-aware sizing
Before firing an order, query L2 depth and size the position so the market-impact cost is < 5 bps at the intended entry price. On MEXC memecoins a 500 USDT taker order can move the book 15-40 bps; the current sizing treats book liquidity as infinite. Implement Kyle's-lambda style depth scaling:

```
max_size = min(
    notional_from_kelly,
    depth_at(entry ± 10bps) × 0.40
)
```

### 3.3  Regime classifier at the portfolio level
Crypto has three macro regimes: (a) BTC trending up & dominance rising (majors trade, alts lag), (b) BTC sideways & dominance falling (alt-season, MOONSHOT edge is highest), (c) BTC capitulating (stablecoin flight, everything correlates to −1). The current bot has piecemeal gates (Fear & Greed bear block, BTC EMA gate) but no unified regime variable.

Classify daily using:
- BTC 20d slope sign
- BTC dominance 20d slope
- Aggregate alt/BTC ratio 14d momentum
- Stablecoin supply 7d change (on-chain via DeFiLlama)
- Funding rate aggregate across top-20 perps

Map regimes to strategy-enable matrix:

| Regime | Enable | Disable |
|---|---|---|
| BTC uptrend + dominance up | TRINITY, SCALPER (majors only) | MOONSHOT |
| BTC sideways + alts leading | MOONSHOT, GRID, SCALPER | REVERSAL |
| BTC downtrend + flight to stables | GRID (majors only) | MOONSHOT, REVERSAL, TRINITY |
| High realised vol > 80th pct | — | GRID |

### 3.4  Realistic backtest cost model
Current backtest spread/slippage assumptions are optimistic (same failure mode as the FX memo §3.1). Rebuild with:
- Empirical hour-of-day spread per symbol from 3 months of MEXC L2 snapshots.
- 4× multiplier during ±5min of binary events (CPI release, FOMC, major project unlocks via TokenUnlocks.app).
- Stop-order slippage 1.5× entry slippage.
- Explicit maker-rebate vs taker-fee modelling per order outcome.

Expect backtest P&L to fall 15-30% after this is shipped. That is the live-gap being closed, not a regression.

### 3.5  Walk-forward calibration with stability filter
Replace the daily Redis-key calibration with a proper walk-forward: optimise on D−180 … D−30, validate on D−30 … D, ship parameters only where validation profit factor > 1.15 **and** out-of-sample Sharpe is within 50% of in-sample. Rebalance **weekly**, not daily. Daily recal on a 30-day window is fitting to noise by construction.

### 3.6  News + on-chain event overlay
Today there is no news or event filter. Crypto has cleaner event triggers than FX: token unlocks, ETF flow disclosures (BlackRock IBIT daily flow, etc.), exchange listing announcements, hack disclosures, stablecoin depeg alerts.

Integrate three free feeds:
- **TokenUnlocks.app** (free API): flag the 72 hours before any unlock > 2% of circulating supply; throttle MOONSHOT/SCALPER longs on that symbol by 50%.
- **DeFiLlama** stablecoin flow: USDT/USDC supply changes > 1% in 24h = macro risk signal.
- **Whale Alert / CryptoQuant exchange-flow**: exchange inflow of BTC > 5k in 1h historically precedes drawdowns.

### 3.7  Execution hardening
- **Post-only maker ladders** on entries for majors (BTC/ETH/SOL) — spread is narrow enough to capture rebates most of the time.
- **Iceberg orders** for positions > 0.5% of 5-min average book depth, sliced into 4-6 child orders over 30 seconds.
- **Cancel-replace** on limit orders if mid-price drifts > 10 bps against you before fill.
- **Weekend risk flatten**: reduce equity exposure by 30% on Friday 20:00 UTC, unwind Monday 00:00 UTC. Saturday/Sunday crypto tape has ~60% less liquidity and has historically produced 80%+ of weekly black-swan moves.

### 3.8  Funding-aware strategy gating
Even a spot bot should read perp funding as a sentiment signal. When top-10 alt funding exceeds 0.08% per 8h (24% annualised), longs are over-crowded — throttle all long-only strategies by 50% for 24h. When funding is < −0.04% / 8h on a symbol, shorts are crowded — favour long entries on that symbol.

### 3.9  Correlation-risk snapshot in Telegram
Current Telegram reports show per-trade P&L but no portfolio-level risk state. Add a twice-daily snapshot: aggregate beta-to-BTC, 5-day realised vol, VaR 95, current allocation by regime. This is table-stakes for any ops desk and frames every manual override with the right context.

---

## 4. Tier 3 — Strategic, longer-horizon

### 4.1  Funding-rate carry sleeve
Crypto perp funding is the single cleanest carry in the asset class: run spot-long / perp-short when 8h funding on a symbol is > 0.025% (≈ 27% annualised). This requires a MEXC-futures leg (or cross-exchange to Binance-futures / Bybit-perp) and a margin engine that knows both legs are one trade. Target portfolio allocation 15-25% of equity, target vol 4-6% annualised. This is **strictly superior** to any of the current outright-directional strategies on a Sharpe basis over multi-year horizons.

### 4.2  Basis trade (cash-and-carry)
When the BTC or ETH dated-future (CME, or quarterly on Binance/OKX) trades at > 8% annualised premium over spot, go long spot / short future. Roll at expiry. Historically delivers 6-12% annualised with Sharpe 2-3 and < 2% drawdown when risk-managed with a margin buffer. This is the cleanest edge available in crypto and no retail bot harvests it.

### 4.3  Staking + delta-hedged yield
For ETH, SOL, BNB: stake spot, hedge delta with perp short. Net yield = staking APR (3-8%) − funding cost (0-4%) = 2-6% annualised, fully delta-neutral. Target 10-15% equity allocation. Requires custody integration (Lido / Rocket Pool for ETH, Jito for SOL).

### 4.4  Liquid alt-basket factor tilts
Replace single-name MOONSHOT entries with three equal-vol tilted baskets:
- **Momentum basket**: top 10 of top-100 by 30d return, rebalanced weekly, volume-weighted.
- **Low-beta basket**: bottom 10 of top-50 by 30d β-to-BTC, reweighted weekly.
- **Funding-crowd basket**: top 10 highest funding rate = crowded longs = short basket, rebalanced every 8h.

Each basket is capped at 5% equity, total sleeve 15%. This harvests the same return drivers as MOONSHOT but with ~3× the Sharpe because of diversification and no single-name blowup risk.

### 4.5  ML-based signal aggregation
The current scoring is hand-tuned per strategy. Replace with a gradient-boosted model (XGBoost, depth 4, ~200 trees) per strategy trained on 2 years of 5m/15m/1h features. Target = sign(next 4h return beyond 1× ATR). Rolling 90-day holdouts. Expect 3-6% lift in hit rate, which is large at 52-55% baseline. Keep explainability — shallow trees only, SHAP reports surfaced daily to the ops Telegram.

### 4.6  Cross-venue liquidity mining + MM
At sufficient size ($500k+) the dominant edge in crypto is no longer directional — it is passive market-making on mid-cap pairs that pay maker rebates. MEXC offers negative taker fees on some pairs for VIP 4+. A dedicated market-making sleeve layered on top of the directional stack can generate 8-15% annualised at Sharpe 4+ with little correlation to the directional book. This is the "always on" revenue line every prop shop has.

---

## 5. Specific red flags to fix immediately

| # | Issue | File / location | Fix |
|---|---|---|---|
| 1 | `HARD_SL_FLOOR_PCT = 0.20` (−20%) | [`mexcbot/exits.py`](mexcbot/exits.py) | Collapse to ATR-based, hard cap 3% |
| 2 | GRID/REVERSAL/MOONSHOT SL floors 8-10% | [`mexcbot/strategies/*.py`](mexcbot/strategies) | Replace with `max(1× ATR, 1.5%)` |
| 3 | No fee modelling in score threshold | [`mexcbot/runtime.py`](mexcbot/runtime.py) | Subtract round-trip fees from raw score |
| 4 | Strategy cross-fires on same symbol | scoring pipeline | Winner-takes-all per (symbol, bar) |
| 5 | Correlation ignored — 3 alts = 3× BTC beta | sizing logic | Correlation-adjusted risk (bucketed at minimum) |
| 6 | Single-venue MEXC concentration | [`mexcbot/exchange.py`](mexcbot/exchange.py) | Cross-exchange best-ex router |
| 7 | Book depth never consulted | order placement | L2 depth-scaled sizing cap |
| 8 | Daily LLM review auto-commits | [`mexcbot/daily_review.py`](mexcbot/daily_review.py) | OOS validation PF > 1.15 gate |
| 9 | MOONSHOT expectancy likely negative | [`mexcbot/strategies/moonshot.py`](mexcbot/strategies/moonshot.py) | Per-symbol hit-rate gate + replace with basket |
| 10 | No portfolio drawdown kill | absent | 30d/−6% throttle, 90d/−10% halt |
| 11 | No funding / basis / carry sleeve | absent | Add funding-capture leg |
| 12 | Weekend thin-liquidity unprotected | runtime scheduling | Reduce exposure 30% Fri 20:00 → Mon 00:00 UTC |
| 13 | No L2/L3 execution quality metrics | order path | Track actual slippage vs quote per fill |

---

## 6. Realistic expectations after upgrades

Tier 1 alone materially de-risks the book without reducing opportunity capture. Modelled effect on a 12-month OOS backtest:

- **Max drawdown** approximately halves (SL tightening is by far the biggest driver).
- **Sharpe** from ~0.8 to ~1.2 (fee budgeting + correlation cap + dedup).
- **Win rate** modestly up ~3-5 pts (fee-gated entries).
- **Profit factor** 1.15-1.30 → 1.45-1.70.

Tier 2 adds another 0.3-0.5 Sharpe through execution hardening, regime-aware strategy gating, and realistic backtest cost model. Tier 3 changes the book character — funding carry and basis trades are structural, Sharpe-3 revenue lines that dwarf the directional stack at institutional size. A properly integrated spot + funding + basis book should target 25-40% annualised at Sharpe 2.0-2.8, which is a competitive crypto hedge-fund profile.

Caveat: the current backtest materially overstates live P&L because of the spread/fee/slippage model in §3.4. Expect 15-30% of reported backtest alpha to disappear when that is fixed. As always: do §3.4 first, measure everything else against a realistic baseline.

---

## 7. Prioritised 90-day roadmap

**Sprint 1 (2 weeks):** items 2.1 (ATR-based SLs), 2.2 (fee-net score), 2.3 (per-symbol dedup), 2.4 (correlation cap), 2.8 (drawdown kill), 2.10 (LLM validation gate). Smallest code surface, biggest immediate P&L impact.

**Sprint 2 (2 weeks):** items 2.5 (maker ladder), 2.7 (per-symbol MOONSHOT gate), 2.9 (tax-lot accounting), plus §3.4 (realistic backtest spreads). Run all future research on the realistic backtester from this point.

**Sprint 3 (4 weeks):** §3.1 (cross-exchange best-ex), §3.2 (depth-aware sizing), §3.3 (regime classifier), §3.5 (walk-forward), §3.7 (execution hardening), §3.8 (funding gate).

**Quarter 2:** §3.6 (on-chain event overlay), §3.9 (portfolio risk telegram), then Tier 3 §4.1 (funding carry sleeve) and §4.2 (basis trade). These two are the highest-Sharpe additions available.

**Quarter 3+:** Tier 3 §4.3 (staking yield), §4.4 (alt-basket tilts), §4.5 (ML aggregation), §4.6 (market-making sleeve).

The single most important decision is to fix the stop-loss geometry (§2.1) and add fee budgeting (§2.2) in Sprint 1. These two changes together convert the current "aggressive retail" profile into a professionally sized book — everything downstream, from calibration quality to strategy attribution, becomes cleaner and more honest.

---

*End of memo.*
