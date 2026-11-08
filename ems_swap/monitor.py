"""The polling loop: check every ~10 s, back off on trouble, hand over to the Swapper."""

from __future__ import annotations

import logging
from typing import Callable, Dict, Optional, Set

from playwright.sync_api import Error as PWError

from .alerts import Alerts
from .backoff import Backoff
from .config import Config
from .errors import BrowserClosed, CaptchaDetected, PortalError, RateLimited, SessionExpired
from .portal import EnrolState, Portal, SeatState
from .session import BrowserSession
from .swap import Result, SwapReport, Swapper

log = logging.getLogger("ems_swap")

EXIT_OK, EXIT_FAILED, EXIT_CONFIG, EXIT_DECLINED, EXIT_CRITICAL, EXIT_INTERRUPTED = 0, 1, 2, 3, 4, 130


def short_name(full: str) -> str:
    """'Global Experience – Hong Kong' -> 'Hong Kong' for compact log lines."""
    for sep in ("–", "—", " - ", ":"):
        if sep in full:
            return full.split(sep)[-1].strip()
    return full


class Monitor:
    def __init__(self, cfg: Config, session: BrowserSession, alerts: Alerts, dry_run: bool,
                 once: bool = False, ask: Callable[[str], str] = input,
                 backoff: Optional[Backoff] = None, max_checks: Optional[int] = None) -> None:
        self.cfg = cfg
        self.s = session
        self.alerts = alerts
        self.dry_run = dry_run
        self.once = once
        self.portal = Portal(session, cfg, dry_run)
        self.swapper = Swapper(self.portal, cfg, alerts, dry_run, relogin=session.relogin, ask=ask,
                               sleep=session.sleep)
        self.backoff = backoff or Backoff(cfg.interval_s, cfg.jitter_min_s, cfg.jitter_max_s, cfg.max_interval_s)
        self.prev_available: Set[str] = set()
        self.cycle = 0
        self.max_checks = max_checks  # for tests

    # ------------------------------------------------------------------ main loop
    def run(self) -> int:
        mode = "DRY RUN - nothing will be clicked" if self.dry_run else "LIVE - will enrol/drop when a seat opens"
        log.info("Mode: %s", mode)
        log.info("Watching: %s | will drop: %s",
                 ", ".join(t.name for t in self.cfg.targets), self.cfg.drop.name)
        self.s.ensure_logged_in()

        already = self._startup()
        if already is not None:
            return already

        while True:
            self.cycle += 1
            terminal: Optional[int] = None
            status_line = ""
            try:
                status_line, terminal = self.check_once()
                self.backoff.success()
            except SessionExpired as e:
                log.info("check #%d | SESSION EXPIRED - pausing until you sign in again", self.cycle)
                self.s.relogin(f"Session expired ({e}).")
                continue
            except CaptchaDetected as e:
                self.backoff.failure()
                status_line = f"CAPTCHA: {e}"
                self.alerts.attention("EMS is showing a CAPTCHA / bot check",
                                      "Backing off. If the browser is visible you can solve it by hand; "
                                      "the script never tries to.")
            except RateLimited as e:
                self.backoff.failure()
                status_line = f"RATE LIMITED: {e}"
            except (PortalError, PWError) as e:
                self.backoff.failure()
                status_line = f"ERROR: {type(e).__name__}: {str(e).splitlines()[0][:200]}"

            if terminal is not None:
                return terminal
            if self.once or (self.max_checks is not None and self.cycle >= self.max_checks):
                log.info("check #%d | %s | (exiting after %d check(s))", self.cycle, status_line, self.cycle)
                return EXIT_OK if self.backoff.failures == 0 else EXIT_FAILED
            delay = self.backoff.next_delay()
            backing_off = f" (backing off x{2 ** self.backoff.failures})" if self.backoff.failures else ""
            log.info("check #%d | %s | next check in %.1fs%s", self.cycle, status_line, delay, backing_off)
            self.s.sleep(delay)

    def _startup(self) -> Optional[int]:
        enrolled = None
        for attempt in range(1, 6):
            try:
                enrolled = self.swapper.startup_check()
                break
            except SessionExpired as e:
                self.s.relogin(f"Session expired ({e}).")
            except (PortalError, PWError) as e:
                log.warning("Could not read the enrolment page (attempt %d/5): %s", attempt, e)
                self.backoff.failure()
                if attempt < 5:
                    self.s.sleep(self.backoff.next_delay())
        else:
            self.alerts.failure("Could not read your enrolments",
                                "Check pages.enrolments / selectors in ems_config.toml (run `inspect`).")
            return EXIT_FAILED
        self.backoff.success()
        if enrolled is not None:
            self.alerts.success(f"Already enrolled in {enrolled.name} - nothing to do.",
                                f"{self.cfg.drop.name} will NOT be touched by this script. Exiting.")
            return EXIT_OK
        return None

    # ------------------------------------------------------------------ one check
    def check_once(self):
        self.portal.load_availability()
        statuses = [self.portal.seat_status(t) for t in self.cfg.targets]
        line = " | ".join(f"{short_name(s.course.name)}: {s}" for s in statuses)

        # Stop if you've ended up in a target by other means (e.g. enrolled manually).
        recheck_now = self.portal.enrolments_same_as_availability or self.cycle % self.cfg.enrolment_recheck_every == 0
        if recheck_now:
            st = (self.portal.enrolments_on_current_page() if self.portal.enrolments_same_as_availability
                  else self.portal.read_enrolments())
            done = self.swapper.enrolled_target(st)
            if done is not None:
                log.info("check #%d | %s", self.cycle, line)
                self.alerts.success(f"You are now enrolled in {done.name} - stopping.",
                                    f"{self.cfg.drop.name}: {st[self.cfg.drop.name].state.value} "
                                    "(not touched).")
                return line, EXIT_OK

        available = [s.course for s in statuses if s.state == SeatState.AVAILABLE]
        newly = [c for c in available if c.name not in self.prev_available]
        self.prev_available = {c.name for c in available}
        for s in statuses:
            if s.state in (SeatState.NOT_FOUND, SeatState.AMBIGUOUS, SeatState.UNKNOWN):
                log.debug("%s row text: %s", s.course.name, s.detail[:300])

        if not available:
            return line, None

        if self.dry_run:
            if newly:
                log.info("check #%d | %s", self.cycle, line)
                self.alerts.attention("SEAT OPEN (dry run): " + ", ".join(c.name for c in newly),
                                      "Logging what would be clicked; nothing is clicked in --dry-run.")
                for c in newly:
                    self.swapper.plan(c)
                return "(dry run - plan logged above)", None
            return line, None

        log.info("check #%d | %s", self.cycle, line)
        self.alerts.attention("SEAT OPEN: " + ", ".join(c.name for c in available), "Attempting the swap now...")
        report = self.swapper.execute(available)
        return "(swap attempted - see above)", self.finish(report)

    # ------------------------------------------------------------------ outcome
    def finish(self, report: SwapReport) -> Optional[int]:
        r = report.result
        if r in (Result.SEAT_GONE, Result.RETRY):
            log.warning("No swap this time (%s): %s - back to monitoring.", r.value, report.message)
            self.prev_available.clear()
            return None
        # Always re-read the enrolment page for the final report.
        final = self.swapper.final_state() or report.final
        state_txt = self.swapper.summary(final) if final else "(could not read the final state)"
        if r == Result.SUCCESS:
            ok = (final is not None and report.target is not None
                  and final[report.target.name].state == EnrolState.ENROLLED
                  and final[self.cfg.drop.name].state == EnrolState.NOT_ENROLLED)
            if ok:
                self.alerts.success("SWAP SUCCEEDED", f"{report.message}\n\nFinal enrolment state:\n{state_txt}")
                return EXIT_OK
            self.alerts.failure("Swap reported success but the final check disagrees",
                                f"Final enrolment state:\n{state_txt}")
            return EXIT_FAILED
        title: Dict[Result, str] = {
            Result.DECLINED: "Stopped - you declined drop-then-add",
            Result.NEEDS_HUMAN: "SWAP STOPPED - needs your attention",
            Result.CRITICAL: "CRITICAL - check EMS manually NOW",
            Result.BLOCKED: "Swap blocked",
        }
        self.alerts.failure(title.get(r, f"Swap ended: {r.value}"),
                            f"{report.message}\n\nFinal enrolment state:\n{state_txt}")
        return {Result.DECLINED: EXIT_DECLINED, Result.CRITICAL: EXIT_CRITICAL}.get(r, EXIT_FAILED)


__all__ = ["Monitor", "BrowserClosed", "EXIT_OK", "EXIT_FAILED", "EXIT_CONFIG", "EXIT_DECLINED",
           "EXIT_CRITICAL", "EXIT_INTERRUPTED"]
