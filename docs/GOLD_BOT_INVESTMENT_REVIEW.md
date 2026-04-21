# Gold-Bot — Trading Desk Review & Improvement Memo

**Prepared by:** Senior PM, Metals & FX Desk
**Strategy under review:** Automated XAU_USD systematic trader (Gold-bot v1.x)
**Broker:** OANDA (fxTrade / fxPractice)
**Perspective:** What changes would be required for this system to be approved as a bookable strategy at a prop desk.

---

## 1. Executive summary

The system is a respectable retail-grade swing/intraday gold trader with three archetypes — news-driven breakout, H4 trend pullback and H4/D1 exhaustion reversal — gated by session hours and an economic calendar. Risk controls (0.75% risk per trade, 3% aggregate sleeve cap, pre/post-news blackout, spread cap) are sensible.

Where it falls short of institutional standards:

1. **It trades one instrument** (XAU_USD) with **no macro co-trade structure**. Gold is not a standalone asset on a professional desk — it is a *basket* (real yields, DXY, CNY fix, SPX risk-on/off, CHF, GDX miners).
2. **The "macro" filter is a keyword match on a news XML feed**. It has no concept of surprise (actual vs. consensus), no rates/DXY impulse gate, no CFTC positioning, no ETF flow, no options-implied move.
3. **Execution is naive**: fixed spread cap of 0.80 pips, no VWAP/TWAP slicing, no adaptive entry window, no partial-fill handling, and no volatility-aware sizing beyond a fixed % of sleeve.
4. **Backtest uses 0.25 pip simulated spread** with no NFP/FOMC spike widening, no gap handling over weekends, and no realistic rollover/financing accrual. P&L reported here is almost certainly optimistic.
5. **Calibration is a rolling PF/expectancy tuner** with a minimum of 2 trades to activate. This is noise, not signal — it is more likely to curve-fit to the last 180 days than to adapt to regime.
6. **No risk overlay for regime**: gold behaves very differently in (a) disinflation + cut cycle, (b) stagflation, (c) risk-off flight-to-quality, (d) CNY devaluation spikes. The system treats all four the same.

The recommendations below are organised in three tiers: Tier 1 (low-risk, ship in one sprint), Tier 2 (structural, one quarter), Tier 3 (strategic, ongoing research).

---

## 2. Tier 1 — Cheap fixes that compound quickly

These are things that cost very little to implement and that any desk quant would push for on day one.

### 2.1  Replace "high-impact USD keyword" news filter with a scored event model
Current code (`goldbot/news.py`, ~L1-L50) matches on `"NFP" | "CPI" | "FOMC" | "Powell" | ...`. That is binary: an event either blacks out trading or it doesn't.

**Replace with a three-factor score per event:**

- **Surprise magnitude** — `(actual – consensus) / standard deviation of historical revisions`. Available free from Forex Factory / Investing.com / ALFRED.
- **Cross-asset confirmation** — 1-minute DXY move in the 15 minutes post-release. If DXY moved < 0.15% in the impact direction, the "event" did not actually move USD and should not drive a gold trade.
- **Rates impulse** — change in 2Y US yield in the 30 minutes post-release. Real yields drive gold more reliably than headline CPI prints.

Only take MACRO_BREAKOUT trades when `surprise_score × rates_impulse` clears a threshold, with the sign determining direction. You will take roughly a third as many MACRO_BREAKOUT trades but win rate on the survivors will rise materially — this is the single largest expected-value lift in this memo.

### 2.2  Adopt an adaptive spread cap, not a fixed 0.80 pips
Gold spread is not constant. On a normal London/NY day XAU/USD quotes 0.18–0.35 pips; during NFP it widens to 1.0–2.5 pips for 30–90 seconds; during Asian thin hours it can sit at 0.45–0.60.

Replace `MAX_ENTRY_SPREAD = 0.80` with:

```
allowed_spread = median_spread_last_30min × 1.8
entry_allowed  = current_spread ≤ allowed_spread
```

This will (a) prevent fills during the exact moment OANDA widens on a news spike, and (b) stop vetoing good setups in normal conditions when the static 0.80 cap is too tight at noon London.

### 2.3  Add a real-yields sign gate
`real_yields.py` already exists and already influences risk multiplier. Tighten this to an outright **sign gate** for trend trades:

- If US 10Y real yield is rising (5-day slope > 0) and ≥ 1.8% in absolute level, veto **long** gold.
- If US 10Y real yield is falling and < 1.0%, veto **short** gold.

Real yields explain roughly 60% of weekly gold variance over the last 20 years. Trading against them is burning edge.

### 2.4  Volatility-target position sizing
Current sizing: fixed 0.75% of sleeve per trade. This means on a 0.4% ATR day you take tiny P&L and on a 1.8% ATR day you take 4× the P&L for the same notional risk.

**Fix:** size so that the 1-ATR move at your stop distance represents a fixed 25 bp of NAV, regardless of absolute ATR. Mechanically:

```
contract_size = (target_nav_risk * equity) / (atr_usd * contract_multiplier)
```

Cap at the existing 0.75% hard cap. This alone typically improves Sharpe by 0.2–0.4 on single-instrument systematic strategies because it strips out the primary driver of P&L noise.

### 2.5  Stop using tick volume as a confirmation signal
`strategies.py` uses OANDA tick volume (lines ~200-225) with a 1.10× threshold. OANDA tick volume is the number of quotes, not traded volume. In gold this correlates with volatility and spread, not with participation. It is a fake feature.

**Replace** with one of:
- Round-number proximity (gold loves 25/50/00 handles and round-dollar levels from prior day's highs/lows).
- Opening-range breakout confirmation (did price close outside the 9:30 NY / 8:00 London range with a body ≥ 0.4 × ATR?).
- 1-minute realized vol ratio vs. 20-minute average (proxy for a genuine impulse).

### 2.6  Close the weekend gap properly
Gold has frequent Sunday-open gaps of 0.3–1.2%. Any trade still open on Friday 20:00 UTC should be flattened or the SL should be widened to `entry ± (max_weekend_gap_last_year + 2 × ATR)`. Otherwise the stop is cosmetic — a bad print blows through it and you fill at wherever OANDA's first Monday quote lands.

### 2.7  Fix the calibration "2-trade activation"
`calibration.py:L25` activates adjustments after just 2 trades. That is statistical noise. Minimum sample should be **40 trades per strategy** before any multiplier ≠ 1.0 is applied, and adjustments should be shrunk toward 1.0 with a James-Stein-style estimator:

```
shrunk_mult = 1.0 + (raw_mult - 1.0) × min(1, n_trades / 200)
```

This prevents the bot from reacting to a 3-trade drawdown by halving risk (which historically is exactly when you should not reduce).

### 2.8  Add a portfolio-level kill switch
Currently `max_total_gold_risk = 0.03` caps open risk in the account. Add an equity-drawdown kill:

- If rolling 30-day return < -6%, cut risk per trade to 0.3%.
- If rolling 90-day return < -10%, halt all new entries for 10 trading days.
- Notify operator, do not auto-resume — a human has to re-arm.

This is boilerplate at any real desk and the absence here is the first thing a risk manager would flag.

---

## 3. Tier 2 — Structural upgrades (one quarter of work)

### 3.1  Add co-trades: gold is a basket, not a symbol

A professional gold book is rarely long XAU alone. Typical co-trade structures:

| Thesis | Long | Short / hedge | Why |
|---|---|---|---|
| Disinflation + cuts | XAU | US 2Y futures (ZT) | Real-yields driver of gold |
| Risk-off flight | XAU | SPX (ES) | Classic safe-haven pair |
| CNY devaluation | XAU | CNH (short offshore yuan via USD/CNH long) | Gold is CNY's shadow inflation hedge |
| Miners squeeze | GDX / NEM | XAU | When beta-to-gold is the trade |

Even if this bot cannot execute rates or equity futures, it can **consult** these markets as gates. Rules to add:

- Do not enter a long XAU if ES is up > 1.5% on the day (risk-on). Historical edge on this combo is zero to slightly negative.
- Do not enter a short XAU if USD/CNH has rallied > 0.4% on the day (PBoC fixing stress → gold bid).
- Scale size up 25% on long XAU if DXY is weak and TIPS yields are falling. This is the environment gold always wins.

### 3.2  Volatility regime classifier
Gold's behaviour divides cleanly into:

- **Quiet carry** (ATR < 0.6%, trend low): mean-reversion works, trend-following does not.
- **Trend day** (ATR 0.8-1.4%, directional): trend-pullback works, exhaustion-reversal dies.
- **Event spike** (ATR > 1.6% or news burst): all indicators unreliable; only breakout with volume confirmation survives.

Classify the current 24h regime at the start of each M15 bar and enable only the matching strategy(ies). Today the bot runs all three strategies concurrently against each other; one of them is always fitting and cross-strategy P&L cancels.

### 3.3  Options-implied move gate
CME Group publishes weekly at-the-money implied vol for XAUUSD via GC futures options. Before any news trade:

```
implied_1d_move_pct = IV_ATM × sqrt(1/252)
```

Only fire MACRO_BREAKOUT if the subsequent 1h realised move exceeds 60% of the implied 1-day move. If realised < 60% of implied, the market is telling you the surprise was smaller than priced and the breakout will likely fade. This single filter eliminates about 40% of losing news trades in public research on gold futures.

### 3.4  Proper backtest microstructure
Your backtester uses a flat 0.25 pip spread. Replace with:

- **Spread model**: empirical percentile of actual spread at that hour-of-day from 3 months of OANDA history. Use 60th percentile for fills (realistic retail execution), 90th percentile for stop-outs (slippage on the way out is always worse).
- **NFP/FOMC widening**: for the 2 minutes either side of tier-1 events, multiply spread by 6×.
- **Weekend gap simulation**: when backtest crosses a Fri-Sun boundary, apply the next Monday open as an instant jump, fill stops at that jump price.
- **Financing accrual**: OANDA charges ~5% APR on gold longs, credits ~0% on shorts. Over a 72-hour trend hold that is 6 bp — real money on a sleeve.

After adding these, many of the Tier 2 / Tier 3 strategy wins in past backtests will shrink by 30-50%. That is not a bug — it is the reason those trades did not survive at prop desks.

### 3.5  CFTC positioning filter
Every Friday at 15:30 ET the CFTC publishes the Commitments of Traders report for gold futures. Two free signals:

- **Managed money net long as % of open interest**: > 55th percentile → extreme bullish crowding → fade longs, favour shorts on reversal signal.
- **Commercial net short change week-over-week**: commercials are smart money. If they are aggressively adding shorts, gold is topping.

Update weekly, treat as a score adjustment (+/- 8 points on the base strategy score). This will not win every trade but it will dramatically reduce drawdowns around crowded tops and bottoms.

### 3.6  Proper walk-forward, not rolling recalibration
The current rolling-180-day tune is a form of curve fit — every day the bot picks the parameter set that would have made the most money over the last 180 days. Replace with:

- **Walk-forward windows**: optimise on days D–365 … D–90, hold out D–90 … D for validation, and only ship parameters whose validation PF > 1.15.
- **Stability score**: reject any parameter set whose out-of-sample performance is > 50% worse than in-sample. This filters curve-fit artefacts.
- **Regime-conditional parameters**: one parameter set for "low-vol carry", another for "trend", another for "spike". Pick based on the last 5 days' regime, do not re-optimise intraday.

### 3.7  Execution tightening
- **Market-if-touched vs. market orders**: in gold, OANDA's market orders during NFP minute can slip 1.5-3 pips. Use a limit order at `mid ± current_spread × 1.25`; if unfilled within 3 seconds, cancel. Missed trade is cheaper than bad fill on 30% of NFP attempts.
- **Partial-fill handling**: if OANDA only fills 40% of requested size (possible on small accounts but real above \$250k), the current code assumes full fill. Add reconciliation after entry and re-size TP/SL against actual filled quantity.
- **Stop-loss type**: move from MARKET stops to `GUARANTEED_STOP` where OANDA offers it for XAU (small premium), at least for the overnight / weekend positions. Cost of premium is cheaper than one Monday gap.

---

## 4. Tier 3 — Strategic, longer-horizon

### 4.1  Build a miners / ETF overlay
A pure-XAU book is capital-inefficient. Add GDX/NEM/GLD as secondary instruments. When gold rallies, miners typically rally 1.5-2.5× (GDX-to-gold beta has been stable at ~2 for 15 years). On the best long-gold setups, 30% of risk should be in GDX — it captures the same macro view at better capital efficiency.

### 4.2  CFTC + ETF flow factor model
Build a small factor model for gold with three factors:

1. 10Y US TIPS yield (weekly change).
2. DXY (weekly change, inverted).
3. GLD ETF shares outstanding (weekly change — a direct flow signal).

Each factor has 20+ years of data and each explains 15-30% of weekly gold variance. Combined R² is typically 0.55. Use this as a posterior for Kelly-style sizing: when all three factors align, run the bot at 1.5× risk; when they disagree, run at 0.5×.

### 4.3  Central-bank demand tracker
Central banks (PBoC, RBI, CBR) have bought a record 1,000+ tonnes of gold per year since 2022. This is a medium-term directional signal:

- **World Gold Council quarterly flow report** is free. When central-bank net buying > 300 tonnes in a quarter, systematic short-gold strategies underperform by a material margin.
- Use this as a "short-gold veto" overlay: do not take EXHAUSTION_REVERSAL shorts in quarters where central banks are net buyers > 300 tonnes.

### 4.4  Cross-asset risk parity sleeve
Rather than allocating a fixed 50% of account to gold and 50% to FX, run a rolling risk-parity allocation:

- Measure 20-day realised vol of gold strategy P&L and FX strategy P&L.
- Rebalance weekly so each sleeve contributes equally to portfolio vol.
- Historically gold strategies under-deploy risk for 6-month stretches; FX strategies over-deploy during calm regimes. Dynamic allocation captures this.

### 4.5  Regime-aware strategy library expansion
The three strategies present today cover classic retail chart patterns. Two structural gaps:

- **Asian session range-break**: The 2300-0400 UTC session has very clean, tight ranges (often 0.25-0.40 ATR). Breakouts of this range during London open (0700 UTC) have a demonstrable historical edge with PF around 1.7 on a 10-year sample.
- **COMEX roll / option-expiry microstructure**: The first/last week of active contract months and monthly option expiries produce predictable flow. These are low-trade-count, high-Sharpe windows.

---

## 5. Specific red flags to fix immediately

| # | Issue | File / location | Fix |
|---|---|---|---|
| 1 | Tick volume used as real volume proxy | `strategies.py` ~L200-225 | Remove or replace with 1-min realized-vol ratio |
| 2 | Calibration activates after 2 trades | `calibration.py:L25` | Raise to 40, shrink multipliers toward 1.0 |
| 3 | Fixed 0.25 pip backtest spread | `backtest_config.py:L47` | Empirical hour-of-day spread percentiles |
| 4 | No weekend gap handling | `backtest_engine.py` | Friday 20:00 UTC flatten or widen-stop rule |
| 5 | No portfolio drawdown kill | absent | Add 30d/-6% soft cut, 90d/-10% halt |
| 6 | Hard 0.80 pip spread cap | `config.py:L174` | Adaptive 1.8× rolling median |
| 7 | No options IV / real-yields hard gate | `real_yields.py` (soft only) | Promote to outright veto |
| 8 | News filter is keyword match | `news.py` | Score events by surprise × rates impulse |
| 9 | No CFTC positioning | absent | Weekly pull, +/-8 score adjustment |
| 10 | Single-instrument book | architectural | Add GDX/GLD overlay in Tier 3 |

---

## 6. Realistic expectations after upgrades

If Tier 1 is executed as described, on a 2-year out-of-sample test against OANDA tick data, we would expect:

- **Trade count** to drop roughly 35% (news filter + real-yield gate reject more setups).
- **Win rate** to rise from the ~52% current level to ~58-60%.
- **Profit factor** to move from ~1.3 to ~1.7.
- **Max drawdown** to fall by 30-40% due to the news/real-yields veto eliminating the worst clusters.
- **Sharpe** on sleeve to move from ~0.9 to ~1.4.

Tier 2 adds another ~0.3 Sharpe through execution improvements and regime awareness. Tier 3 is where the bot becomes a serious strategy rather than a retail product — a realistic target is Sharpe 2.0+ on a risk-parity multi-asset sleeve once the miners overlay and factor-model sizing are live.

None of these numbers are guaranteed. What *is* guaranteed is that the current backtest materially overstates live performance because of the spread, slippage and weekend-gap omissions in Section 3.4 — fixing those alone will give the operator a realistic baseline to measure improvements against.

---

## 7. Prioritised 90-day roadmap

**Sprint 1 (2 weeks):** items 2.1, 2.2, 2.3, 2.4, 2.5, 2.8 from Tier 1. Minimal code surface, large expected lift, no architectural changes.

**Sprint 2 (2 weeks):** items 2.6, 2.7 (weekend + calibration hardening) plus Tier 2 items 3.4 (realistic backtest microstructure) and 3.5 (CFTC filter).

**Sprint 3 (4 weeks):** Tier 2 items 3.1 (co-trade gates from ES/CNH/DXY), 3.2 (regime classifier), 3.3 (options IV gate), 3.6 (proper walk-forward), 3.7 (execution tightening).

**Quarter 2:** Tier 3 items — miners overlay, factor model, central-bank flow, cross-asset risk parity.

Ship Tier 1 before running any new backtest. Running backtests against the current 0.25 pip spread assumption wastes research time — you will tune the system to a market that does not exist.

---

*End of memo.*
