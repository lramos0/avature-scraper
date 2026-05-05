import csv
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import csv
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from collections import deque

SCRIPT = "avature-scraper"
CSV_PATH = "avature_subdomains.csv"
OUT_DIR = Path("parallel_outputs")
OUT_DIR.mkdir(exist_ok=True)

MAX_WORKERS = 4  # start low because Playwright/browser work is heavy


LOG_DIR = Path("parallel_logs")

# Very generous because the scraper itself can take time.
HARD_TIMEOUT_SECONDS = 30 * 60

def load_upstreams(path):
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        return [row["domain"].strip() for row in reader if row.get("domain")]

def safe_name(value):
    return (
        value.replace("https://", "")
             .replace("http://", "")
             .replace("/", "_")
             .replace(":", "_")
    )

def run_one(upstream):
    OUT_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)

    name = safe_name(upstream)
    output = OUT_DIR / f"{name}.json"
    log_path = LOG_DIR / f"{name}.log"

    cmd = [
        SCRIPT,
        "https://" + upstream + "/careers",
        "--angry",
        "--output", str(output),
        "--no-upload-to-jobpool",
        "--timeout", "30",
        "--max-jobs", "25",
    ]

    started = time.monotonic()

    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )

        while True:
            code = proc.poll()

            if code is not None:
                elapsed = time.monotonic() - started
                return {
                    "upstream": upstream,
                    "returncode": code,
                    "elapsed": elapsed,
                    "output": str(output),
                    "log": str(log_path),
                }

            elapsed = time.monotonic() - started

            if elapsed > HARD_TIMEOUT_SECONDS:
                proc.kill()
                proc.wait()
                return {
                    "upstream": upstream,
                    "returncode": -9,
                    "elapsed": elapsed,
                    "output": str(output),
                    "log": str(log_path),
                    "error": "hard timeout killed process",
                }

            time.sleep(2)

def main():
    upstreams = load_upstreams(CSV_PATH)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(run_one, u): u for u in upstreams}

        for future in as_completed(futures):
            result = future.result()
            u = result["upstream"]

            if result["returncode"] == 0:
                print(f"[OK] {u} {result['elapsed']:.1f}s")
            else:
                print(
                    f"[FAIL] {u} code={result['returncode']} "
                    f"{result['elapsed']:.1f}s log={result['log']}"
                )

if __name__ == "__main__":
    main()
