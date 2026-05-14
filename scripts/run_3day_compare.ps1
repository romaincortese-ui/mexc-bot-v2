# Run a 3-day backtest spanning May 11 13:00 UTC -> May 14 12:00 UTC
# Mirrors production env vars so backtest output is comparable to live trades.

param(
    [string]$Label = "baseline",
    [string]$Start = "2026-05-11T13:00:00Z",
    [string]$End   = "2026-05-14T12:00:00Z",
    [switch]$ApplyFixes
)

$ErrorActionPreference = "Stop"
$py = "C:/Users/Rocot/AppData/Local/Python/pythoncore-3.14-64/python.exe"
$root = "C:\Users\Rocot\Downloads\mexc-bot2"
$out = Join-Path $root "backtest_3day_$Label.txt"

# Symbols that traded in prod May 11-13 + a wider universe so signals have room to fire.
$tradedSymbols = "BCHUSDT,TONUSDT,WLDUSDT,FILUSDT,APTUSDT,ADAUSDT,PENGUUSDT,ENAUSDT,PEPEUSDT,BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,AVAXUSDT,LINKUSDT,TRUMPUSDT,WIFUSDT,ATLAUSDT,JUPUSDT"

# Mirror production env vars (from `railway variables --kv` on 2026-05-14)
$env:PYTHONPATH = $root
$env:BACKTEST_PRINT_FULL_REPORT = "false"
$env:BACKTEST_START = $Start
$env:BACKTEST_END = $End
$env:BACKTEST_INTERVAL = "5m"
$env:BACKTEST_INITIAL_BALANCE = "500.0"
$env:BACKTEST_SYMBOLS = $tradedSymbols
$env:BACKTEST_GRID_SYMBOLS = $tradedSymbols
$env:BACKTEST_REVERSAL_SYMBOLS = $tradedSymbols
$env:BACKTEST_SCALPER_SYMBOLS = $tradedSymbols
$env:BACKTEST_MOONSHOT_SYMBOLS = $tradedSymbols
$env:BACKTEST_TAKER_FEE_RATE = "0.0005"  # MEXC default; matches log fee residuals
$env:BACKTEST_MAKER_FEE_RATE = "0.0"

# Mirror prod strategy config
$env:GRID_ALLOCATION_PCT = "0.10"
$env:GRID_BUDGET_PCT = "0.25"
$env:SCALPER_ALLOCATION_PCT = "0.35"
$env:SCALPER_BUDGET_PCT = "0.60"
$env:SCALPER_THRESHOLD = "52.0"
$env:MOONSHOT_ALLOCATION_PCT = "0.30"
$env:MOONSHOT_BUDGET_PCT = "0.03"
$env:REVERSAL_BUDGET_PCT = "0.85"
$env:REVERSAL_BREAKEVEN_ACT = "0.020"
$env:REVERSAL_PEAK_DROP_PCT = "0.010"
$env:REVERSAL_DIVERGENCE_CLIMAX_MIN_SCORE = "70"
$env:SCALPER_BREAKEVEN_ACT = "0.014"
$env:SCALPER_PEAK_DROP_PCT = "0.008"
$env:TRINITY_ALLOCATION_PCT = "0.10"
$env:MAX_OPEN_POSITIONS = "5"
$env:MIN_EXPECTED_NET_PROFIT_USDT = "0.25"
$env:MAX_CONSECUTIVE_LOSSES = "3"
$env:WIN_RATE_CB_THRESHOLD = "0.30"
$env:WIN_RATE_CB_WINDOW = "10"
$env:SESSION_LOSS_PAUSE_PCT = "0.03"
$env:STRATEGY_LOSS_STREAK_MAX = "3"
$env:BACKTEST_OUTPUT_DIR = "backtest_output_$Label"
$env:MEXCBOT_STRATEGIES = "SCALPER,GRID,MOONSHOT,REVERSAL"
$env:BACKTEST_CACHE_DIR = "backtest_cache"

if ($ApplyFixes) {
    # Post-fix env flags (all default OFF in code; enabled here to simulate proposed prod rollout).
    $env:REVERSAL_BTC_MACRO_GATE_ENABLED = "true"
    $env:REVERSAL_MAX_CONCURRENT = "1"
    $env:BREAKEVEN_MIN_HOLD_MINUTES = "15"
    $env:BREAKEVEN_ACTIVATION_UPLIFT = "0.004"
    $env:CLOSE_VERIFY_MIN_NOTIONAL_USDT = "1.0"
} else {
    # Ensure baseline run never inherits stray flags from the shell.
    Remove-Item Env:REVERSAL_BTC_MACRO_GATE_ENABLED -ErrorAction SilentlyContinue
    Remove-Item Env:REVERSAL_MAX_CONCURRENT -ErrorAction SilentlyContinue
    Remove-Item Env:BREAKEVEN_MIN_HOLD_MINUTES -ErrorAction SilentlyContinue
    Remove-Item Env:BREAKEVEN_ACTIVATION_UPLIFT -ErrorAction SilentlyContinue
    Remove-Item Env:CLOSE_VERIFY_MIN_NOTIONAL_USDT -ErrorAction SilentlyContinue
}

Push-Location $root
& $py -m backtest.run_backtest 2>&1 | Tee-Object -FilePath $out
$exitCode = $LASTEXITCODE
Pop-Location

Write-Host "exit=$exitCode  output=$out"
