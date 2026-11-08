"""Command-line entry point.

    python -m ems_swap --dry-run          # watch, log what would be clicked
    python -m ems_swap                    # watch and swap for real
    python -m ems_swap inspect            # capture the real DOM for selector tuning
    python -m ems_swap sound-test         # check the alert sounds work
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import List, Optional

from .alerts import Alerts, setup_logging
from .config import ConfigError, load_config
from .errors import BrowserClosed
from .monitor import EXIT_CONFIG, EXIT_FAILED, EXIT_INTERRUPTED, Monitor
from .session import BrowserSession

log = logging.getLogger("ems_swap")

DEFAULT_PROFILE = Path.home() / ".lbs-ems-swap" / "profile"
COMMANDS = ("watch", "inspect", "sound-test")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=None,
                        help="TOML config (default: ./ems_config.toml if present)")
    common.add_argument("--profile-dir", type=Path, default=Path(os.environ.get("EMS_PROFILE_DIR", DEFAULT_PROFILE)),
                        help=f"browser profile that keeps your EMS session (default: {DEFAULT_PROFILE})")
    common.add_argument("--headless", action="store_true",
                        help="run without a window (ignored on first run; a window opens when you need to log in)")
    common.add_argument("--channel", default=os.environ.get("EMS_BROWSER_CHANNEL"),
                        help="use an installed browser, e.g. 'chrome' or 'msedge' (default: Playwright's Chromium)")
    common.add_argument("--browser-path", default=os.environ.get("EMS_BROWSER_PATH"),
                        help="path to a Chromium/Chrome executable")
    common.add_argument("--no-autofill", action="store_true",
                        help="don't type LBS_USER/LBS_PASS into the login form; you type them")
    common.add_argument("--no-sound", action="store_true", help="console alerts only")
    common.add_argument("--log-file", default="ems_swap.log",
                        help="also log to this file (default: ems_swap.log; pass '' to disable)")
    common.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    p = argparse.ArgumentParser(prog="python -m ems_swap",
                                description="Watch LBS EMS and swap Helsinki for Hong Kong / Vietnam.")
    sub = p.add_subparsers(dest="command")
    w = sub.add_parser("watch", parents=[common], help="monitor and swap (default)")
    w.add_argument("--dry-run", action="store_true",
                   help="never click Add/Drop/Swap; only log when a seat appears and what would be clicked")
    w.add_argument("--once", action="store_true", help="do a single check and exit")
    i = sub.add_parser("inspect", parents=[common], help="capture the real DOM to tune selectors")
    i.add_argument("--out", type=Path, default=Path("inspect_output"), help="where to save captures")
    sub.add_parser("sound-test", parents=[common], help="play the three alert sounds")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in COMMANDS + ("-h", "--help"):
        argv.insert(0, "watch")
    args = build_parser().parse_args(argv)

    setup_logging(Path(args.log_file) if args.log_file else None, args.verbose)
    alerts = Alerts(sound=not args.no_sound)

    if args.command == "sound-test":
        import time
        for kind in ("attention", "success", "failure"):
            alerts.notify(kind, f"Sound test: {kind}")
            time.sleep(3)
        return 0

    config_path = args.config or (Path("ems_config.toml") if Path("ems_config.toml").exists() else None)
    try:
        cfg = load_config(config_path)
    except ConfigError as e:
        log.error("Config error: %s", e)
        return EXIT_CONFIG
    log.info("Config: %s", cfg.source or "built-in defaults")

    if not os.environ.get("LBS_USER") or not os.environ.get("LBS_PASS"):
        log.warning("LBS_USER and/or LBS_PASS are not set; you'll need to type your credentials when asked.")

    session = BrowserSession(cfg, args.profile_dir, alerts, headless=args.headless, channel=args.channel,
                             executable_path=args.browser_path, autofill=not args.no_autofill)
    try:
        session.start()
        if args.command == "inspect":
            from .inspect_dom import run_inspect
            return run_inspect(cfg, session, args.out)
        return Monitor(cfg, session, alerts, dry_run=args.dry_run, once=args.once).run()
    except KeyboardInterrupt:
        log.info("Stopped by you (Ctrl-C). Nothing is left half-done unless a message above says so.")
        return EXIT_INTERRUPTED
    except BrowserClosed:
        alerts.failure("The browser window was closed - stopping.")
        return EXIT_FAILED
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
