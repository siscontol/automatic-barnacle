"""The swap decision logic. All the safety rules live here.

Rules (from the brief):
  * Already enrolled in a target  -> do nothing, stop.
  * Prefer EMS's native swap/replace (atomic).
  * Otherwise ADD the target first; drop Helsinki only once the add is confirmed on
    the enrolment page.
  * If the add is blocked by a clash / credit cap, STOP and ask before drop-then-add.
  * Never drop Helsinki unless a target is confirmed (the only exception is the
    drop-then-add path, which requires you to type the confirmation phrase).
  * Re-read the enrolment page at the end and report the final state.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Optional, Sequence

from playwright.sync_api import Error as PWError

from .alerts import Alerts
from .config import Config, Course
from .errors import PortalError, SessionExpired
from .portal import ActionResult, EnrolState, Enrolments, Outcome, Portal, SeatState

log = logging.getLogger("ems_swap")

CONSENT_PHRASE = "DROP HELSINKI"


class Result(str, Enum):
    SUCCESS = "SUCCESS"  # in a target, Helsinki dropped
    ALREADY_ENROLLED = "ALREADY ENROLLED"  # nothing to do
    SEAT_GONE = "SEAT GONE"  # keep monitoring
    RETRY = "RETRY"  # transient failure, keep monitoring
    BLOCKED = "BLOCKED"  # add blocked by clash/cap
    DECLINED = "DECLINED"  # user said no to drop-then-add
    NEEDS_HUMAN = "NEEDS HUMAN"  # stopped safely, needs a look
    CRITICAL = "CRITICAL"  # Helsinki lost and target not obtained


@dataclass
class SwapReport:
    result: Result
    target: Optional[Course] = None
    message: str = ""
    final: Optional[Enrolments] = field(default=None, repr=False)


class SafetyViolation(RuntimeError):
    pass


class Swapper:
    def __init__(self, portal: Portal, cfg: Config, alerts: Alerts, dry_run: bool,
                 relogin: Callable[[str], None], ask: Callable[[str], str] = input,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.p = portal
        self.cfg = cfg
        self.alerts = alerts
        self.dry_run = dry_run
        self.relogin = relogin
        self.ask = ask
        self.sleep = sleep
        self.failed_adds: Counter = Counter()

    # ------------------------------------------------------------------ helpers
    @property
    def drop(self) -> Course:
        return self.cfg.drop

    def others(self, course: Course) -> List[Course]:
        return [c for c in self.cfg.all_courses if c is not course]

    def enrolments(self) -> Enrolments:
        return self.p.read_enrolments()

    def state(self, st: Enrolments, course: Course) -> EnrolState:
        return st[course.name].state

    def enrolled_target(self, st: Enrolments) -> Optional[Course]:
        for t in self.cfg.targets:
            if self.state(st, t) == EnrolState.ENROLLED:
                return t
        return None

    def summary(self, st: Enrolments) -> str:
        return "\n".join(f"{c.name}: {self.state(st, c).value}" for c in self.cfg.all_courses)

    def _with_relogin(self, fn, *args, attempts: int = 3):
        for i in range(attempts):
            try:
                return fn(*args)
            except SessionExpired as e:
                if i == attempts - 1:
                    raise
                self.relogin(f"Session expired ({e}).")

    def _transient(self, e: Exception, errors: int, limit: int = 4) -> bool:
        """Log a mid-swap error and wait a little. False once we've had too many."""
        log.warning("Error mid-swap (%d/%d): %s", errors, limit, str(e).splitlines()[0][:200])
        if errors >= limit:
            return False
        self.sleep(min(5 * errors, 20))
        return True

    def final_state(self) -> Optional[Enrolments]:
        try:
            return self._with_relogin(self.enrolments)
        except Exception as e:
            log.error("Could not re-read the enrolment page for the final report: %s", e)
            return None

    # ------------------------------------------------------------------ startup
    def startup_check(self) -> Optional[Course]:
        st = self._with_relogin(self.p.read_enrolments, True)
        log.info("Current enrolment state:\n%s", self.summary(st))
        bad = [c.name for c in self.cfg.all_courses if self.state(st, c) == EnrolState.AMBIGUOUS]
        if bad:
            log.warning("Enrolment state is AMBIGUOUS for %s - check selectors with `inspect`. "
                        "Helsinki will not be dropped while this is the case.", ", ".join(bad))
        if self.state(st, self.drop) != EnrolState.ENROLLED:
            log.warning("%s does not show as ENROLLED (%s). If a seat opens the script will add the "
                        "target and has nothing to drop.", self.drop.name, self.state(st, self.drop).value)
        return self.enrolled_target(st)

    # ------------------------------------------------------------------ dry run
    def plan(self, target: Course) -> None:
        """Log exactly what would be clicked, without clicking anything."""
        lines = [f"Seat open in {target.name}. Plan:"]
        try:
            swap = self.p.find_swap(target)
            if swap:
                lines.append(f"  1. Native swap found -> click {swap.describe()}, "
                             f"choose the other course, confirm. Then verify on the enrolment page.")
            else:
                lines.append("  1. No native swap/replace control found -> use ADD then DROP.")
                add = self.p.find_add(target)
                if add:
                    lines.append(f"  2. ADD: click {add.describe()} and confirm its dialog.")
                else:
                    lines.append(f"  2. ADD: no enabled Add/Enrol button found for {target.name} (would not act).")
                lines.append(f"  3. Re-read the enrolment page; continue only if {target.name} is ENROLLED.")
                drop = self.p.find_drop(self.drop)
                if drop:
                    lines.append(f"  4. DROP: click {drop.describe()} and confirm its dialog.")
                else:
                    lines.append(f"  4. DROP: no Drop button found for {self.drop.name} yet "
                                 "(it may only appear after the add).")
                lines.append("  5. Re-read the enrolment page and report the final state.")
                lines.append("  If the add were blocked by a clash/credit cap: stop and ask you before any drop.")
        except SessionExpired:
            raise
        except Exception as e:
            lines.append(f"  (could not finish planning: {e})")
        log.info("[DRY RUN] " + "\n".join(lines))

    # ------------------------------------------------------------------ live
    def execute(self, available: Sequence[Course]) -> SwapReport:
        """Try each available target in priority order."""
        if self.dry_run:
            raise SafetyViolation("execute() called in dry-run mode")
        blocked: List[SwapReport] = []
        last: Optional[SwapReport] = None
        for target in available:
            report = self.swap_to(target)
            last = report
            if report.result in (Result.SUCCESS, Result.NEEDS_HUMAN, Result.CRITICAL):
                return report
            if report.result == Result.BLOCKED:
                blocked.append(report)
        if blocked:
            return self.drop_then_add_with_consent(blocked[0].target, blocked[0].message)
        return last or SwapReport(Result.SEAT_GONE)

    def swap_to(self, target: Course) -> SwapReport:
        """Resumable state machine: re-reads the enrolment page before every decision."""
        native_tried = add_tried = False
        drop_attempts = errors = 0
        last_add: Optional[ActionResult] = None
        helsinki_at_start: Optional[EnrolState] = None
        # A confirmation dialog for adding the target must not mention any other course
        # (e.g. "this will replace Helsinki") - if it does, we cancel and ask you.
        forbid_for_target = self.others(target)

        for _ in range(14):
            try:
                st = self.enrolments()
                t, h = self.state(st, target), self.state(st, self.drop)
                if helsinki_at_start is None:
                    helsinki_at_start = h
                log.info("State: %s=%s, %s=%s", target.name, t.value, self.drop.name, h.value)

                if t == EnrolState.ENROLLED:
                    if h == EnrolState.NOT_ENROLLED:
                        return SwapReport(Result.SUCCESS, target, f"Enrolled in {target.name}; "
                                          f"{self.drop.name} dropped.", st)
                    if h != EnrolState.ENROLLED:
                        return SwapReport(Result.NEEDS_HUMAN, target,
                                          f"{target.name} is confirmed but {self.drop.name} shows "
                                          f"{h.value}; not touching it.", st)
                    if drop_attempts >= 2:
                        return SwapReport(Result.NEEDS_HUMAN, target,
                                          f"You are in BOTH {target.name} and {self.drop.name}: the drop did "
                                          "not go through after 2 attempts. Drop Helsinki manually.", st)
                    drop_attempts += 1
                    r = self.drop_helsinki(confirmed_target=target, consent=False)
                    if r.outcome == Outcome.NEEDS_HUMAN:
                        return SwapReport(Result.NEEDS_HUMAN, target,
                                          f"In {target.name}, but dropping {self.drop.name} needs a human: "
                                          f"{r.message}", st)
                    continue

                if t in (EnrolState.WAITLISTED, EnrolState.PENDING):
                    return SwapReport(Result.NEEDS_HUMAN, target,
                                      f"{target.name} shows {t.value}, not a confirmed enrolment. "
                                      f"{self.drop.name} was not touched.", st)
                if EnrolState.AMBIGUOUS in (t, h):
                    return SwapReport(Result.NEEDS_HUMAN, target,
                                      "The enrolment page is ambiguous (see log) - not acting. "
                                      "Tune selectors with `inspect`.", st)

                # Target not enrolled yet.
                if helsinki_at_start == EnrolState.ENROLLED and h != EnrolState.ENROLLED:
                    # Helsinki disappeared without the target being confirmed - recover.
                    self.alerts.failure(f"{self.drop.name} is gone but {target.name} is not confirmed!",
                                        "Trying to recover (add target, else re-add Helsinki)...")
                    return self.recover(target)

                if not native_tried:
                    native_tried = True
                    swap = self.p.find_swap(target)
                    if swap is not None:
                        r = self.p.perform_swap(swap, target, [c for c in self.cfg.targets if c is not target])
                        if r.outcome == Outcome.SEAT_GONE:
                            return SwapReport(Result.SEAT_GONE, target, r.message)
                        if r.outcome == Outcome.NEEDS_HUMAN:
                            return SwapReport(Result.NEEDS_HUMAN, target, r.message)
                        if r.outcome == Outcome.INCOMPLETE:
                            log.warning("Native swap could not be completed (%s); falling back to add-then-drop.",
                                        r.message)
                        continue  # re-verify state
                    log.info("No native swap/replace option found; using add-then-drop.")

                if not add_tried:
                    add_tried = True
                    add = self.p.find_add(target)
                    if add is None:
                        return SwapReport(Result.SEAT_GONE, target,
                                          f"No enabled Add/Enrol button for {target.name} any more.")
                    last_add = self.p.perform(add, "add", target, forbid_for_target)
                    continue

                # Add was tried and the target is still not enrolled.
                assert last_add is not None
                o, msg = last_add.outcome, last_add.message
                if o in (Outcome.CLASH, Outcome.CAP):
                    return SwapReport(Result.BLOCKED, target, f"{o.value}: {msg}")
                if o == Outcome.SEAT_GONE:
                    return SwapReport(Result.SEAT_GONE, target, msg)
                if o == Outcome.NEEDS_HUMAN:
                    return SwapReport(Result.NEEDS_HUMAN, target, msg, st)
                self.failed_adds[target.name] += 1
                if self.failed_adds[target.name] >= self.cfg.max_failed_add_attempts:
                    return SwapReport(Result.NEEDS_HUMAN, target,
                                      f"Adding {target.name} failed {self.failed_adds[target.name]} times "
                                      f"(last: {o.value} {msg}). Stopping so you can take a look.", st)
                return SwapReport(Result.RETRY, target, f"Add not confirmed ({o.value} {msg}); will retry.")
            except SessionExpired as e:
                self.relogin(f"Session expired during the swap ({e}).")
            except SafetyViolation as e:
                return SwapReport(Result.NEEDS_HUMAN, target, f"Safety check stopped the swap: {e}")
            except (PortalError, PWError) as e:
                errors += 1
                if not self._transient(e, errors):
                    return SwapReport(Result.NEEDS_HUMAN, target, f"Repeated errors mid-swap: {e}")
        return SwapReport(Result.NEEDS_HUMAN, target, "Too many steps without reaching a final state.")

    def drop_helsinki(self, confirmed_target: Optional[Course], consent: bool) -> ActionResult:
        """THE guarded drop. Without explicit consent a confirmed target is mandatory."""
        if not consent:
            if confirmed_target is None:
                raise SafetyViolation("drop requested without a confirmed target")
        drop = self.p.find_drop(self.drop)
        if drop is None:
            return ActionResult(Outcome.NEEDS_HUMAN, f"no Drop button found for {self.drop.name}")
        if not consent:
            # Re-check immediately before clicking, on the freshest page we have.
            assert confirmed_target is not None
            st = (self.p.enrolments_on_current_page() if drop.page_label == "enrolments page"
                  else self.enrolments())
            if self.state(st, confirmed_target) != EnrolState.ENROLLED:
                raise SafetyViolation(f"{confirmed_target.name} not confirmed right before dropping")
            if drop.page_label != "enrolments page":
                drop = self.p.find_drop(self.drop)
                if drop is None:
                    return ActionResult(Outcome.NEEDS_HUMAN, f"no Drop button found for {self.drop.name}")
        return self.p.perform(drop, "drop", self.drop, self.cfg.targets)

    # ------------------------------------------------------------------ drop-then-add
    def drop_then_add_with_consent(self, target: Course, reason: str) -> SwapReport:
        self.alerts.attention(
            "DECISION NEEDED - the add was blocked",
            f"EMS would not add {target.name}: {reason}\n"
            f"The only way may be to DROP {self.drop.name} FIRST and then add {target.name}.\n"
            f"RISK: if the seat goes in between, you lose {self.drop.name} AND don't get {target.name}.\n"
            "(If that happens the script immediately tries to re-add Helsinki, but that seat may be gone too.)",
        )
        answer = self._ask_with_reminders(
            f"Type {CONSENT_PHRASE} to drop-then-add, or anything else to stop: ")
        if answer.strip().upper() != CONSENT_PHRASE:
            return SwapReport(Result.DECLINED, target,
                              f"You declined drop-then-add. {self.drop.name} untouched.", self.final_state())
        log.info("Consent given for drop-then-add into %s.", target.name)
        return self._drop_then_add(target, already_dropped=False)

    def _ask_with_reminders(self, prompt: str) -> str:
        """Ask on the console, re-playing the alert every minute until answered."""
        answered = threading.Event()

        def remind() -> None:
            while not answered.wait(60):
                self.alerts.attention("Still waiting for your answer in the terminal",
                                      f"Type {CONSENT_PHRASE} or anything else to stop.")

        threading.Thread(target=remind, daemon=True).start()
        try:
            return self.ask(prompt)
        except (EOFError, KeyboardInterrupt):
            return ""
        finally:
            answered.set()

    def recover(self, target: Course) -> SwapReport:
        return self._drop_then_add(target, already_dropped=True)

    def _drop_then_add(self, target: Course, already_dropped: bool) -> SwapReport:
        dropped = already_dropped
        seat_checked = add_tried = readd_tried = False
        errors = 0
        forbid_for_target = [c for c in self.cfg.targets if c is not target] + [self.drop]
        forbid_for_drop = [c for c in self.cfg.targets if c is not target]

        for _ in range(12):
            try:
                st = self.enrolments()
                t, h = self.state(st, target), self.state(st, self.drop)
                log.info("State: %s=%s, %s=%s", target.name, t.value, self.drop.name, h.value)
                if EnrolState.AMBIGUOUS in (t, h):
                    return SwapReport(Result.NEEDS_HUMAN, target, "Enrolment page ambiguous - stopped.", st)
                if t == EnrolState.ENROLLED:
                    if h == EnrolState.NOT_ENROLLED:
                        return SwapReport(Result.SUCCESS, target,
                                          f"Enrolled in {target.name}; {self.drop.name} dropped.", st)
                    if h == EnrolState.ENROLLED:
                        return self.swap_to(target)  # normal guarded drop path
                    return SwapReport(Result.NEEDS_HUMAN, target, f"{self.drop.name} shows {h.value}.", st)
                if t in (EnrolState.WAITLISTED, EnrolState.PENDING):
                    return SwapReport(Result.NEEDS_HUMAN, target, f"{target.name} shows {t.value}.", st)

                if readd_tried:
                    if h == EnrolState.ENROLLED:
                        return SwapReport(Result.NEEDS_HUMAN, target,
                                          f"Could not get {target.name}; {self.drop.name} was RE-ADDED, "
                                          "so you're back where you started.", st)
                    return SwapReport(Result.CRITICAL, target,
                                      f"Lost {self.drop.name}, could not get {target.name}, and re-adding "
                                      f"{self.drop.name} failed. Act manually NOW.", st)

                if h == EnrolState.ENROLLED:
                    if dropped:
                        return SwapReport(Result.NEEDS_HUMAN, target,
                                          f"The drop of {self.drop.name} didn't take effect; nothing was lost.", st)
                    if not seat_checked:
                        seat_checked = True
                        self.p.load_availability()
                        s = self.p.seat_status(target)
                        if s.state != SeatState.AVAILABLE:
                            return SwapReport(Result.SEAT_GONE, target,
                                              f"{target.name} is {s} now - NOT dropping {self.drop.name}.", st)
                        log.info("Re-checked: %s is still %s - dropping %s now.", target.name, s, self.drop.name)
                    r = self.drop_helsinki(confirmed_target=None, consent=True)
                    if r.outcome == Outcome.NEEDS_HUMAN or not r.clicked:
                        return SwapReport(Result.NEEDS_HUMAN, target, r.message, st)
                    dropped = True
                    continue

                # Helsinki not enrolled, target not enrolled.
                if not add_tried:
                    add_tried = True
                    add = self.p.find_add(target)
                    if add is not None:
                        self.p.perform(add, "add", target, forbid_for_target)
                        continue
                    log.error("No Add button for %s after dropping %s.", target.name, self.drop.name)
                if not dropped:
                    return SwapReport(Result.SEAT_GONE, target, f"Could not add {target.name}.", st)
                readd_tried = True
                self.alerts.failure(f"Could not get {target.name} - trying to re-add {self.drop.name}")
                readd = self.p.find_add(self.drop)
                if readd is not None:
                    self.p.perform(readd, "add", self.drop, forbid_for_drop)
                continue
            except SessionExpired as e:
                self.relogin(f"Session expired during drop-then-add ({e}).")
            except SafetyViolation as e:
                return SwapReport(Result.NEEDS_HUMAN, target, f"Safety check stopped: {e}")
            except (PortalError, PWError) as e:
                errors += 1
                if not self._transient(e, errors):
                    return SwapReport(Result.CRITICAL if dropped else Result.NEEDS_HUMAN, target,
                                      f"Repeated errors during drop-then-add: {e}")
        return SwapReport(Result.NEEDS_HUMAN, target, "Too many steps without reaching a final state.")
