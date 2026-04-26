"""Shared utilities — colour logging, config loader, progress bar."""
from __future__ import annotations
import json, sys, time
from pathlib import Path
from datetime import datetime


class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    RED    = "\033[91m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    CYAN   = "\033[96m"
    MAGENTA= "\033[95m"


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log_head(msg: str):
    print(f"\n{C.BOLD}{C.MAGENTA}{'═'*60}\n  {msg}\n{'═'*60}{C.RESET}")

def log_info(msg: str):
    print(f"{C.CYAN}[{_ts()}] ℹ  {msg}{C.RESET}")

def log_ok(msg: str):
    print(f"{C.GREEN}[{_ts()}] ✔  {msg}{C.RESET}")

def log_warn(msg: str):
    print(f"{C.YELLOW}[{_ts()}] ⚠  {msg}{C.RESET}")

def log_err(msg: str):
    print(f"{C.RED}[{_ts()}] ✖  {msg}{C.RESET}", file=sys.stderr)

def log_step(i: int, n, msg: str, width: int = 30):
    total = int(n) if str(n).isdigit() else 0
    filled = int(width * i / total) if total > 0 else 0
    bar = "█" * filled + "░" * (width - filled)
    label = f"{i}/{n}" if total else f"{i}/?"
    print(f"\r{C.CYAN}[{bar}] {label}  {msg}{C.RESET}", end="", flush=True)
    if total and i >= total:
        print()


def load_config(path: str = "config.json") -> dict:
    p = Path(path)
    if not p.exists():
        log_err(f"config.json not found at {path}")
        sys.exit(1)
    return json.loads(p.read_text(encoding="utf-8"))
