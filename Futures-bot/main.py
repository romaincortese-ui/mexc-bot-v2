import os
import sys
import traceback

# Ensure logs flow to Railway/Docker stdout immediately rather than sitting in
# a block buffer until the container dies.
os.environ.setdefault("PYTHONUNBUFFERED", "1")
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

print("=== futuresbot main.py boot ===", flush=True)

try:
    from futuresbot.runtime import run_runtime
except Exception:
    print("=== IMPORT FAILED ===", flush=True)
    traceback.print_exc()
    sys.stdout.flush()
    sys.stderr.flush()
    raise


if __name__ == "__main__":
    try:
        run_runtime()
    except Exception:
        print("=== run_runtime CRASHED ===", flush=True)
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        raise
