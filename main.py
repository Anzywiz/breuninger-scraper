"""
main.py — Orchestrator for the Breuninger scraper pipeline.

Usage:
  python main.py --phase1
  python main.py --phase2
  python main.py --phase2 --fresh
  python main.py --phase3
  python main.py --phase3 --limit 50
  python main.py --phase4
  python main.py --phase5
  python main.py --all
  python main.py --phase1 --phase2 --phase3 --phase4 --phase5
"""
import argparse
import subprocess
import sys

from utils import log_err, log_head, log_info, log_ok


def run(script: str, extra: list[str] | None = None):
    cmd = [sys.executable, script] + (extra or [])
    log_info(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        log_err(f"{script} exited with code {result.returncode}")
        sys.exit(result.returncode)
    log_ok(f"{script} completed successfully.")


def main():
    ap = argparse.ArgumentParser(description="Breuninger scraper pipeline")
    ap.add_argument("--all",    action="store_true", help="Run all phases")
    ap.add_argument("--phase1", action="store_true")
    ap.add_argument("--phase2", action="store_true")
    ap.add_argument("--phase3", action="store_true")
    ap.add_argument("--phase4", action="store_true")
    ap.add_argument("--phase5", action="store_true")
    ap.add_argument("--fresh",   action="store_true", help="Pass --fresh to phase2/3")
    ap.add_argument("--brand",   help="Phase2: only process this brand slug")
    ap.add_argument("--workers", type=int, help="Phase2/3: override worker count")
    ap.add_argument("--limit",   type=int, help="Phase3: only enrich N products")
    args = ap.parse_args()

    if args.all:
        args.phase1 = args.phase2 = args.phase3 = args.phase4 = args.phase5 = True

    if not any([args.phase1, args.phase2, args.phase3, args.phase4, args.phase5]):
        ap.print_help()
        return

    log_head("Breuninger Scraper Pipeline")

    if args.phase1:
        run("phase1_get_brands.py")

    if args.phase2:
        extra = []
        if args.fresh:   extra += ["--fresh"]
        if args.brand:   extra += ["--brand", args.brand]
        if args.workers: extra += ["--workers", str(args.workers)]
        run("phase2_scrape_products.py", extra)

    if args.phase3:
        extra = []
        if args.fresh:   extra += ["--fresh"]
        if args.workers: extra += ["--workers", str(args.workers)]
        if args.limit:   extra += ["--limit", str(args.limit)]
        run("phase3_enrich.py", extra)

    if args.phase4:
        run("phase4_to_txt.py")

    if args.phase5:
        run("phase5_dbq_upload.py")

    log_ok("Pipeline finished.")


if __name__ == "__main__":
    main()
