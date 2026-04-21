from __future__ import annotations

import logging

from mexcbot.calibration import apply_opportunity_calibration
from mexcbot.config import LiveConfig, env_float, env_int
from mexcbot.exchange import MexcClient
from mexcbot.models import Opportunity
from mexcbot.strategies.grid import GRID_MIN_SCORE, find_grid_opportunity
from mexcbot.strategies.moonshot import MOONSHOT_MIN_SCORE, find_moonshot_opportunity
from mexcbot.strategies.pre_breakout import PRE_BREAKOUT_MIN_SCORE, find_pre_breakout_opportunity
from mexcbot.strategies.reversal import REVERSAL_MIN_SCORE, find_reversal_opportunity
from mexcbot.strategies.scalper import find_scalper_opportunity, score_symbol_from_frame
from mexcbot.strategies.trinity import TRINITY_MIN_SCORE, find_trinity_opportunity


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Feature flags (default OFF). Flip in env to enable memo integrations.
# ---------------------------------------------------------------------------
# §2.3 Winner-takes-all per-symbol dedup across strategies.
USE_STRATEGY_DEDUP = env_int("USE_STRATEGY_DEDUP", 0)
# §2.2 Fee-net score filter — reject candidates whose (raw - fee_score_cost)
# falls below the strategy threshold. Reward risk estimate comes from tp/sl.
USE_FEE_NET_SCORE = env_int("USE_FEE_NET_SCORE", 0)
# Taker fee rate for MEXC round-trip budgeting. Default to 0.001 per side.
FEE_NET_TAKER_RATE = env_float("FEE_NET_TAKER_RATE", 0.001)


STRATEGY_FINDERS = {
	"SCALPER": find_scalper_opportunity,
	"GRID": find_grid_opportunity,
	"TRINITY": find_trinity_opportunity,
	"MOONSHOT": find_moonshot_opportunity,
	"REVERSAL": find_reversal_opportunity,
	"PRE_BREAKOUT": find_pre_breakout_opportunity,
}

STRATEGY_MIN_SCORES = {
	"SCALPER": None,
	"GRID": GRID_MIN_SCORE,
	"TRINITY": TRINITY_MIN_SCORE,
	"MOONSHOT": MOONSHOT_MIN_SCORE,
	"REVERSAL": REVERSAL_MIN_SCORE,
	"PRE_BREAKOUT": PRE_BREAKOUT_MIN_SCORE,
}


def find_best_opportunity(
	client: MexcClient,
	config: LiveConfig,
	exclude: str | set[str] | None = None,
	open_symbols: set[str] | None = None,
	calibration: dict | None = None,
	threshold_overrides: dict[str, float] | None = None,
) -> Opportunity | None:
	excluded = {exclude.upper()} if isinstance(exclude, str) and exclude else set()
	if isinstance(exclude, set):
		excluded = {symbol.upper() for symbol in exclude}
	threshold_overrides = {str(strategy).upper(): float(value) for strategy, value in (threshold_overrides or {}).items()}

	# Track the per-strategy resolved threshold for candidate filtering later.
	thresholds_by_strategy: dict[str, float] = {}
	candidates: list[Opportunity] = []
	for strategy_name in config.strategies:
		resolved_name = strategy_name.upper()
		finder = STRATEGY_FINDERS.get(resolved_name)
		if finder is None:
			log.warning("Unknown strategy '%s' in MEXCBOT_STRATEGIES, skipping", strategy_name)
			continue
		default_threshold = config.scalper_threshold if resolved_name == "SCALPER" else config.score_threshold
		strategy_threshold = threshold_overrides.get(resolved_name, default_threshold)
		thresholds_by_strategy[resolved_name] = float(strategy_threshold)
		try:
			if resolved_name == "SCALPER":
				candidate = find_scalper_opportunity(
					client,
					config,
					exclude=excluded,
					open_symbols=open_symbols or set(),
					score_threshold=strategy_threshold,
				)
			elif resolved_name == "MOONSHOT":
				candidate = find_moonshot_opportunity(
					client,
					config,
					exclude=excluded,
					open_symbols=open_symbols or set(),
					score_threshold=strategy_threshold,
				)
			else:
				candidate = finder(client, config, exclude=excluded, open_symbols=open_symbols or set())
		except Exception as exc:
			log.exception("[%s] Strategy finder raised exception: %s", resolved_name, exc)
			continue
		if candidate is not None:
			base_threshold = max(
				float(strategy_threshold),
				float(STRATEGY_MIN_SCORES.get(resolved_name) or config.score_threshold),
			)
			candidate = apply_opportunity_calibration(candidate, calibration, base_threshold=base_threshold)
		if candidate is None:
			log.info("[%s] No opportunity found this scan", resolved_name)
			continue
		log.info(
			"[%s] Candidate: %s score=%.2f signal=%s price=%.6f",
			resolved_name,
			candidate.symbol,
			candidate.score,
			candidate.entry_signal,
			candidate.price,
		)
		candidates.append(candidate)

	# §2.3 dedup — winner-takes-all per symbol + opposite-side mute.
	if USE_STRATEGY_DEDUP and len(candidates) > 1:
		from mexcbot.strategy_dedup import apply_dedup_to_opportunities

		kept, muted = apply_dedup_to_opportunities(candidates)
		if muted:
			log.info(
				"[DEDUP] Muted %d candidate(s): %s",
				len(muted),
				", ".join(f"{m.strategy}:{m.symbol}({m.score:.1f})" for m in muted),
			)
		candidates = list(kept)

	# §2.2 fee-net score filter — reject candidates whose fee-adjusted score
	# falls below their strategy's threshold.
	if USE_FEE_NET_SCORE and candidates:
		from mexcbot.cost_budget import compute_cost_budget, passes_net_threshold

		survivors: list[Opportunity] = []
		for opp in candidates:
			threshold = thresholds_by_strategy.get(opp.strategy.upper(), config.score_threshold)
			budget = compute_cost_budget(
				strategy=opp.strategy,
				raw_score=float(opp.score),
				taker_fee_rate=FEE_NET_TAKER_RATE,
			)
			if passes_net_threshold(budget, threshold=threshold):
				survivors.append(opp)
			else:
				log.info(
					"[FEE_NET] Dropped %s:%s raw=%.2f net=%.2f < thr=%.2f",
					opp.strategy,
					opp.symbol,
					budget.raw_score,
					budget.net_score,
					threshold,
				)
		candidates = survivors

	best: Opportunity | None = None
	for candidate in candidates:
		if best is None or candidate.score > best.score:
			best = candidate
	if best is not None:
		log.info(
			"[SCAN] Best overall: %s [%s] score=%.2f",
			best.symbol,
			best.strategy,
			best.score,
		)
	return best


__all__ = ["find_best_opportunity", "find_scalper_opportunity", "find_grid_opportunity", "find_trinity_opportunity", "find_moonshot_opportunity", "find_reversal_opportunity", "find_pre_breakout_opportunity", "score_symbol_from_frame"]