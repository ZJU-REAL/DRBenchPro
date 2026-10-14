"""Output ownership and secret-free progress for command-line jobs."""
from contextlib import contextmanager
import json
import math
from pathlib import Path
import sys
import tempfile
import threading

_PROGRESS_LOCK = threading.Lock()


def progress(event, **fields):
    with _PROGRESS_LOCK:
        print(json.dumps({"event": event, **fields}, ensure_ascii=False),
              file=sys.stderr, flush=True)


@contextmanager
def output_lock(output):
    """Reject concurrent writers; the OS releases the lock after interruption."""
    import fcntl
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".runner.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Output is in use by another process") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def check_writable_directory(output):
    """Probe the nearest existing directory without creating a run directory."""
    candidate = Path(output).resolve()
    if candidate.exists() and not candidate.is_dir():
        raise ValueError("Output must be a directory")
    while not candidate.exists():
        candidate = candidate.parent
    with tempfile.TemporaryFile(dir=candidate):
        pass
    return {"writable": True, "checked_existing_parent": str(candidate)}


def cost_rates(input_rate=None, output_rate=None):
    if input_rate is None and output_rate is None:
        return None
    if input_rate is None or output_rate is None:
        raise ValueError("Provide both input and output USD-per-million rates")
    if any(not math.isfinite(rate) or rate < 0 for rate in (input_rate, output_rate)):
        raise ValueError("Token price rates must be finite and nonnegative")
    return {"input_usd_per_million": input_rate, "output_usd_per_million": output_rate}


def estimate_cost(usage, rates):
    if rates is None:
        return None
    return {"estimated_usd": (usage["input_tokens"] * rates["input_usd_per_million"]
                              + usage["output_tokens"] * rates["output_usd_per_million"]) / 1_000_000,
            "rates": rates,
            "note": "Approximation from reported tokens at user-supplied flat rates, not an invoice. "
                    "Cached input uses the input rate; reasoning is already included in output. "
                    "Unreported usage, provider fees and different route prices are excluded."}
