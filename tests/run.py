#!/usr/bin/env python3
"""tests/run.py — the runner.

Exits non-zero on failure, crash OR SKIP, and warns if fewer checks ran than
expected.

That last part is the point. A suite that silently stops running while the
report says "0 failed" is strictly worse than a red build: it tells you
everything is fine while nothing is being checked. So each suite declares a
minimum check count, a skip is counted as a defect rather than a shrug, and a
suite that fails to import at all is a CRASH rather than an absence.

    python3 tests/run.py          run everything
    python3 tests/run.py bot exa  run named suites
"""

from __future__ import annotations

import importlib
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

SUITES = ["pipeline", "places", "people", "bot", "exa", "contract", "invite"]

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)


def main(argv: list[str]) -> int:
    wanted = [a for a in argv[1:] if not a.startswith("-")] or SUITES
    unknown = [w for w in wanted if w not in SUITES]
    if unknown:
        print(f"{RED}unknown suite(s): {', '.join(unknown)}{RESET}")
        print(f"available: {', '.join(SUITES)}")
        return 2

    print(f"\n{BOLD}Shum-AI test suite{RESET}")
    print(f"{DIM}network fully mocked — any unmocked call is a failure{RESET}\n")

    summaries = []
    started = time.time()

    for name in wanted:
        try:
            module = importlib.import_module(f"test_{name}")
            summary = module.run().summary()
        except Exception:
            # A suite that cannot even be imported is the worst case, and the
            # one most likely to be mistaken for "no tests here".
            summaries.append({
                "name": name, "passed": 0, "failed": [], "skipped": [],
                "crashed": [f"suite failed to load\n{traceback.format_exc(limit=4)}"],
                "total": 0, "expect_at_least": 1, "under_expected": True, "ok": False,
            })
            continue
        summaries.append(summary)

    elapsed = time.time() - started

    total_passed = sum(s["passed"] for s in summaries)
    total_failed = sum(len(s["failed"]) for s in summaries)
    total_crashed = sum(len(s["crashed"]) for s in summaries)
    total_skipped = sum(len(s["skipped"]) for s in summaries)
    total_checks = sum(s["total"] for s in summaries)
    under = [s for s in summaries if s["under_expected"]]

    for s in summaries:
        mark = f"{GREEN}PASS{RESET}" if s["ok"] else f"{RED}FAIL{RESET}"
        print(f"  {mark}  {s['name']:<10} {s['passed']:>3} passed"
              f"{('  ' + RED + str(len(s['failed'])) + ' failed' + RESET) if s['failed'] else ''}"
              f"{('  ' + RED + str(len(s['crashed'])) + ' crashed' + RESET) if s['crashed'] else ''}"
              f"{('  ' + YELLOW + str(len(s['skipped'])) + ' skipped' + RESET) if s['skipped'] else ''}"
              f"{('  ' + YELLOW + 'only ' + str(s['total']) + ' of ' + str(s['expect_at_least']) + ' expected checks ran' + RESET) if s['under_expected'] else ''}")

    for s in summaries:
        for item in s["failed"]:
            print(f"\n{RED}FAILED{RESET} [{s['name']}] {item}")
        for item in s["crashed"]:
            print(f"\n{RED}CRASHED{RESET} [{s['name']}] {item}")
        for item in s["skipped"]:
            # Explicitly loud. A skip is a defect in this project, not a note.
            print(f"\n{YELLOW}SKIPPED{RESET} [{s['name']}] {item}"
                  f"\n{DIM}  a skipped check is not a passing check — this fails the run{RESET}")

    print(f"\n{BOLD}{total_checks} checks in {elapsed:.2f}s{RESET} — "
          f"{GREEN}{total_passed} passed{RESET}, "
          f"{total_failed} failed, {total_crashed} crashed, {total_skipped} skipped")

    if under:
        print(f"{YELLOW}WARNING: {len(under)} suite(s) ran fewer checks than expected "
              f"({', '.join(s['name'] for s in under)}). Tests may have been removed "
              f"or silently stopped running.{RESET}")

    bad = total_failed + total_crashed + total_skipped
    if bad == 0 and not under:
        print(f"\n{GREEN}{BOLD}ALL GREEN{RESET}\n")
        return 0
    print(f"\n{RED}{BOLD}NOT GREEN{RESET} — "
          f"{bad} defect(s){' + ' + str(len(under)) + ' under-count' if under else ''}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
