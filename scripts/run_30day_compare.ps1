# Run a 30-day MEXC backtest, mirroring production env vars.
# Use -ApplyAggressive to enable R1/R2/R3/R4 flags (default OFF).

param(
    [string]$Label = "baseline_30d",
    [string]$Start = "2026-04-14T00:00:00Z",
    [string]$End   = "2026-05-14T00:00:00Z",
    [switch]$ApplyAggressive,
    [switch]$ApplyDefensive
)

$ErrorActionPreference = "Stop"
$py = "C:/Users/Rocot/AppData/Local/Python/pythoncore-3.14-64/python.exe"
$root = "C:\Users\Rocot\Downloads\mexc-bot2"
$out = Join-Path $root "backtest_30day_$Label.txt"

# A wider universe so MOONSHOT/SCALPER have momentum candidates to chew on.
$symbols = "BCHUSDT,TONUSDT,WLDUSDT,FILUSDT,APTUSDT,ADAUSDT,PENGUUSDT,ENAUSDT,PEPEUSDT,BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,AVAXUSDT,LINKUSDT,TRUMPUSDT,WIFUSDT,JUPUSDT,SUIUSDT,LTCUSDT,DOTUSDT,UNIUSDT,NEARUSDT,ATOMUSDT,ARBUSDT,OPUSDT,INJUSDT"

$env:PYTHONPATH = $root
$env:BACKTEST_PRINT_FULL_REPORT = "false"
$env:BACKTEST_START = $Start
$env:BACKTEST_END = $End
$env:BACKTEST_INTERVAL = "5m"
$env:BACKTEST_INITIAL_BALANCE = "500.0"
$env:BACKTEST_SYMBOLS = $symbols
$env:BACKTEST_GRID_SYMBOLS = $symbols
$env:BACKTEST_REVERSAL_SYMBOLS = $symbols
$env:BACKTEST_SCALPER_SYMBOLS = $symbols
$env:BACKTEST_MOONSHOT_SYMBOLS = $symbols
$env:BACKTEST_TAKER_FEE_RATE = "0.0005"
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

# Defensive flags (already shipped in prod via Railway).
if ($ApplyDefensive -or $ApplyAggressive) {
    $env:REVERSAL_BTC_MACRO_GATE_ENABLED = "true"
    $env:REVERSAL_MAX_CONCURRENT = "1"
    $env:BREAKEVEN_MIN_HOLD_MINUTES = "15"
    $env:BREAKEVEN_ACTIVATION_UPLIFT = "0.004"
    $env:CLOSE_VERIFY_MIN_NOTIONAL_USDT = "1.0"
} else {
    Remove-Item Env:REVERSAL_BTC_MACRO_GATE_ENABLED -ErrorAction SilentlyContinue
    Remove-Item Env:REVERSAL_MAX_CONCURRENT -ErrorAction SilentlyContinue
    Remove-Item Env:BREAKEVEN_MIN_HOLD_MINUTES -ErrorAction SilentlyContinue
    Remove-Item Env:BREAKEVEN_ACTIVATION_UPLIFT -ErrorAction SilentlyContinue
    Remove-Item Env:CLOSE_VERIFY_MIN_NOTIONAL_USDT -ErrorAction SilentlyContinue
}

# Aggressive flags (R1-R4).
if ($ApplyAggressive) {
    $env:MOONSHOT_GAINER_BOOST_ENABLED = "true"
    $env:MOONSHOT_GAINER_EXTRA_CANDIDATES = "30"
    $env:MOONSHOT_GAINER_MIN_CHANGE_PCT = "5.0"
    $env:MOONSHOT_GAINER_MIN_VOLUME_USDT = "250000"
    $env:MOMENTUM_CONTINUATION_ENABLED = "true"
    $env:MOMENTUM_CONTINUATION_MAX_RETURN_PCT = "25.0"
    $env:MOMENTUM_CONTINUATION_RETURN_MULT = "4.0"
    $env:HIGH_CONF_TRAIL_ENABLED = "true"
    $env:HIGH_CONF_SCORE_FLOOR = "75.0"
    $env:HIGH_CONF_TP_PCT = "0.15"
    $env:HIGH_CONF_TRAIL_DROP_PCT = "0.05"
    $env:AGGRESSIVE_SIZING_ENABLED = "true"
    $env:AGGRESSIVE_SIZING_HIGH_SCORE = "80.0"
    $env:AGGRESSIVE_SIZING_HIGH_MULT = "2.0"
    $env:AGGRESSIVE_SIZING_MID_SCORE = "60.0"
    $env:AGGRESSIVE_SIZING_MID_MULT = "1.0"
    $env:AGGRESSIVE_SIZING_LOW_MULT = "0.5"
    $env:AGGRESSIVE_SIZING_CAP_PCT = "0.60"
} else {
    Remove-Item Env:MOONSHOT_GAINER_BOOST_ENABLED -ErrorAction SilentlyContinue
    Remove-Item Env:MOONSHOT_GAINER_EXTRA_CANDIDATES -ErrorAction SilentlyContinue
    Remove-Item Env:MOONSHOT_GAINER_MIN_CHANGE_PCT -ErrorAction SilentlyContinue
    Remove-Item Env:MOONSHOT_GAINER_MIN_VOLUME_USDT -ErrorAction SilentlyContinue
    Remove-Item Env:MOMENTUM_CONTINUATION_ENABLED -ErrorAction SilentlyContinue
    Remove-Item Env:MOMENTUM_CONTINUATION_MAX_RETURN_PCT -ErrorAction SilentlyContinue
    Remove-Item Env:MOMENTUM_CONTINUATION_RETURN_MULT -ErrorAction SilentlyContinue
    Remove-Item Env:HIGH_CONF_TRAIL_ENABLED -ErrorAction SilentlyContinue
    Remove-Item Env:HIGH_CONF_SCORE_FLOOR -ErrorAction SilentlyContinue
    Remove-Item Env:HIGH_CONF_TP_PCT -ErrorAction SilentlyContinue
    Remove-Item Env:HIGH_CONF_TRAIL_DROP_PCT -ErrorAction SilentlyContinue
    Remove-Item Env:AGGRESSIVE_SIZING_ENABLED -ErrorAction SilentlyContinue
    Remove-Item Env:AGGRESSIVE_SIZING_HIGH_SCORE -ErrorAction SilentlyContinue
    Remove-Item Env:AGGRESSIVE_SIZING_HIGH_MULT -ErrorAction SilentlyContinue
    Remove-Item Env:AGGRESSIVE_SIZING_MID_SCORE -ErrorAction SilentlyContinue
    Remove-Item Env:AGGRESSIVE_SIZING_MID_MULT -ErrorAction SilentlyContinue
    Remove-Item Env:AGGRESSIVE_SIZING_LOW_MULT -ErrorAction SilentlyContinue
    Remove-Item Env:AGGRESSIVE_SIZING_CAP_PCT -ErrorAction SilentlyContinue
}

Push-Location $root
& $py -m backtest.run_backtest *>&1 | Tee-Object -FilePath $out
$exitCode = $LASTEXITCODE
Pop-Location

Write-Host "exit=$exitCode  output=$out"
