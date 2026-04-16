from __future__ import annotations

import logging

from mexcbot.calibration import apply_opportunity_calibration
from mexcbot.config import LiveConfig
from mexcbot.exchange import MexcClient
from mexcbot.models import Opportunity
from mexcbot.strategies.grid import GRID_MIN_SCORE, find_grid_opportunity
from mexcbot.strategies.moonshot import MOONSHOT_MIN_SCORE, find_moonshot_opportunity
from mexcbot.strategies.pre_breakout import PRE_BREAKOUT_MIN_SCORE, find_pre_breakout_opportunity
from mexcbot.strategies.reversal import REVERSAL_MIN_SCORE, find_reversal_opportunity
from mexcbot.strategies.scalper import find_scalper_opportunity, score_symbol_from_frame
from mexcbot.strategies.trinity import TRINITY_MIN_SCORE, find_trinity_opportunity


log = logging.getLogger(__name__)


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

	best: Opportunity | None = None
	for strategy_name in config.strategies:
		resolved_name = strategy_name.upper()
		finder = STRATEGY_FINDERS.get(resolved_name)
		if finder is None:
			log.warning("Unknown strategy '%s' in MEXCBOT_STRATEGIES, skipping", strategy_name)
			continue
		strategy_threshold = threshold_overrides.get(resolved_name)
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
		if candidate is not None:
			base_threshold = max(
				float(strategy_threshold if strategy_threshold is not None else config.score_threshold),
				float(STRATEGY_MIN_SCORES.get(resolved_name) or config.score_threshold),
			)
			candidate = apply_opportunity_calibration(candidate, calibration, base_threshold=base_threshold)
		if candidate is None:
			log.info("[%s] No opportunity found this scan", resolved_name)
		else:
			log.info(
				"[%s] Candidate: %s score=%.2f signal=%s price=%.6f",
				resolved_name,
				candidate.symbol,
				candidate.score,
				candidate.entry_signal,
				candidate.price,
			)
		if candidate is not None and (best is None or candidate.score > best.score):
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