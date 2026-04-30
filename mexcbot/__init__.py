__all__ = ["LiveConfig", "run_bot"]


def __getattr__(name: str):
	if name == "LiveConfig":
		from mexcbot.config import LiveConfig

		return LiveConfig
	if name == "run_bot":
		from mexcbot.runtime import run_bot

		return run_bot
	raise AttributeError(f"module 'mexcbot' has no attribute {name!r}")