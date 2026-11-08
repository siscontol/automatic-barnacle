"""Page model for EMS: finding course rows, seat status, enrolment status and buttons.

Design rule: *fail closed*. If anything is missing, ambiguous or unexpected, report
UNKNOWN / AMBIGUOUS / NEEDS_HUMAN rather than guess. Only clicks that change your
enrolment go through `perform()`, which never clicks in dry-run mode.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from playwright.sync_api import Dialog, Error as PWError, Locator, Page, Response
from playwright.sync_api import TimeoutError as PWTimeout

from .config import Config, Course, normalise
from .errors import BrowserClosed, CaptchaDetected, PortalError, RateLimited, SessionExpired
from .session import BrowserSession, _is_closed_error

log = logging.getLogger("ems_swap")


class SeatState(str, Enum):
    AVAILABLE = "AVAILABLE"
    FULL = "FULL"
    UNKNOWN = "UNKNOWN"
    NOT_FOUND = "NOT FOUND"
    AMBIGUOUS = "AMBIGUOUS"


class EnrolState(str, Enum):
    ENROLLED = "ENROLLED"
    WAITLISTED = "WAITLISTED"
    PENDING = "PENDING"
    NOT_ENROLLED = "NOT ENROLLED"
    AMBIGUOUS = "AMBIGUOUS"


class Outcome(str, Enum):
    SUCCESS = "SUCCESS"
    CLASH = "CLASH"
    CAP = "CREDIT/CAP LIMIT"
    SEAT_GONE = "SEAT GONE"
    ERROR = "ERROR"
    NEEDS_HUMAN = "NEEDS HUMAN"
    UNKNOWN = "UNKNOWN"
    DRY_RUN = "DRY RUN"
    INCOMPLETE = "INCOMPLETE"


@dataclass
class SeatStatus:
    course: Course
    state: SeatState
    seats: Optional[int] = None
    detail: str = ""

    def __str__(self) -> str:
        if self.state == SeatState.AVAILABLE:
            return f"AVAILABLE ({self.seats} seat{'s' if self.seats != 1 else ''})" if self.seats else "AVAILABLE"
        if self.state == SeatState.FULL and self.seats == 0:
            return "FULL (0 seats)"
        if self.state in (SeatState.UNKNOWN, SeatState.AMBIGUOUS) and self.detail:
            return f"{self.state.value} [{self.detail[:80]}]"
        return self.state.value


@dataclass
class EnrolStatus:
    course: Course
    state: EnrolState
    detail: str = ""


Enrolments = Dict[str, EnrolStatus]


@dataclass
class Located:
    """A button we might click, plus enough context to describe it."""
    course: Optional[Course]
    button: Locator
    label: str
    row_text: str
    page_label: str
    url: str

    def describe(self) -> str:
        where = f" in the row '{self.row_text[:90]}'" if self.row_text else ""
        return f"'{self.label}'{where} on the {self.page_label} ({self.url})"


@dataclass
class ActionResult:
    outcome: Outcome
    message: str = ""
    clicked: bool = False


@dataclass
class _DialogContext:
    acting_on: Course
    forbid: Sequence[Course]
    rejected: Optional[Tuple[Outcome, str]] = None
    seen: List[str] = field(default_factory=list)


class Portal:
    def __init__(self, session: BrowserSession, cfg: Config, dry_run: bool) -> None:
        self.s = session
        self.cfg = cfg
        self.dry_run = dry_run
        self._dialog_ctx: Optional[_DialogContext] = None
        self._rate_limited = False
        self._nav_count = 0
        self._fresh: Optional[Tuple[tuple, float, str]] = None  # (page key, loaded at, url)
        session.add_page_hook(self._attach)

    @property
    def page(self) -> Page:
        assert self.s.page is not None
        return self.s.page

    # ------------------------------------------------------------------ events
    def _attach(self, page: Page) -> None:
        page.on("dialog", self._on_dialog)
        page.on("response", self._on_response)
        page.on("framenavigated", lambda frame: self._on_nav(page, frame))

    def _on_nav(self, page: Page, frame) -> None:
        if frame == page.main_frame:
            self._nav_count += 1

    def _on_response(self, resp: Response) -> None:
        try:
            host = (urlparse(resp.url).hostname or "").lower()
            if host == self.cfg.portal_host and resp.status in (429, 503):
                self._rate_limited = True
        except Exception:
            pass

    def _on_dialog(self, dialog: Dialog) -> None:
        """Native alert()/confirm() boxes."""
        msg = dialog.message or ""
        ctx = self._dialog_ctx
        try:
            if ctx is None or self.dry_run:
                log.warning("Unexpected browser dialog dismissed: %r", msg[:200])
                dialog.dismiss()
                return
            ctx.seen.append(msg)
            if dialog.type == "confirm":
                verdict = self.judge_prompt(msg, ctx)
                if verdict is None:
                    log.info("Confirm box: %r -> OK", msg[:200])
                    dialog.accept()
                else:
                    log.warning("Confirm box: %r -> CANCEL (%s)", msg[:200], verdict[0].value)
                    ctx.rejected = verdict
                    dialog.dismiss()
            elif dialog.type == "beforeunload":
                dialog.accept()
            elif dialog.type == "alert":
                log.info("Alert box: %r", msg[:200])
                dialog.accept()
            else:  # prompt() - never expected
                ctx.rejected = (Outcome.NEEDS_HUMAN, f"Unexpected prompt dialog: {msg[:200]}")
                dialog.dismiss()
        except PWError:
            pass

    # ------------------------------------------------------------------ navigation
    def _settle(self) -> None:
        try:
            self.page.wait_for_load_state("load", timeout=self.cfg.nav_timeout_ms)
        except PWTimeout:
            pass
        try:
            self.page.wait_for_load_state("networkidle", timeout=self.cfg.network_idle_ms)
        except PWTimeout:
            pass

    def load(self, url: str, nav: Sequence[str], what: str, max_age_s: float = 0.0) -> None:
        """Navigate to a page and check it's usable. With max_age_s, reuse the current
        page if we loaded exactly this page that recently and nothing was clicked since
        (saves precious seconds when a seat opens). Safety re-checks always use 0."""
        key = (url, tuple(nav))
        if (max_age_s and self._fresh and self._fresh[0] == key
                and time.monotonic() - self._fresh[1] < max_age_s and self.page.url == self._fresh[2]):
            return
        self._fresh = None
        self._rate_limited = False
        try:
            resp = self.page.goto(url, wait_until="domcontentloaded")
        except PWTimeout as e:
            raise PortalError(f"timed out loading the {what}") from e
        except PWError as e:
            if _is_closed_error(e):
                raise BrowserClosed() from e
            raise PortalError(f"could not load the {what}: {str(e).splitlines()[0][:160]}") from e
        self._settle()
        self.check_page(resp, what)
        for name in nav:
            rx = re.compile(rf"^\s*{re.escape(name)}\s*$", re.IGNORECASE)
            target = self.page.get_by_role("link", name=rx).or_(self.page.get_by_role("tab", name=rx)).or_(
                self.page.get_by_role("button", name=rx))
            visible = [el for el in target.all() if el.is_visible()]
            if not visible:
                raise PortalError(f"navigation item {name!r} not found on the {what}")
            visible[0].click()
            self._settle()
            self.check_page(None, what)
        self._fresh = (key, time.monotonic(), self.page.url)

    def check_page(self, resp: Optional[Response], what: str) -> None:
        if self.s.on_login_page():
            raise SessionExpired(f"redirected to a login page while loading the {what}")
        status = resp.status if resp is not None else 200
        if status in (429, 503) or self._rate_limited:
            raise RateLimited(f"HTTP {status if status in (429, 503) else '429/503'} (rate limited) on the {what}")
        if self.captcha_visible():
            raise CaptchaDetected(f"CAPTCHA / bot check on the {what}")
        if status >= 400:
            raise PortalError(f"HTTP {status} on the {what}")

    def captcha_visible(self) -> bool:
        try:
            if any(el.is_visible() for el in self.page.locator(self.cfg.sel_captcha).all()):
                return True
            title = normalise(self.page.title())
            if self.cfg.txt["captcha"].search(title):
                return True
            body = self.page.locator("body").inner_text(timeout=3000)
            # Challenge pages are short; avoid false positives on normal content pages.
            return len(body) < 3000 and bool(self.cfg.txt["captcha"].search(normalise(body)))
        except PWError:
            return False

    def load_availability(self) -> None:
        self.load(self.cfg.availability_url, self.cfg.availability_nav, "availability page")

    def load_enrolments(self) -> None:
        self.load(self.cfg.enrolments_url, self.cfg.enrolments_nav, "enrolments page")

    @property
    def enrolments_same_as_availability(self) -> bool:
        return (self.cfg.enrolments_url == self.cfg.availability_url
                and self.cfg.enrolments_nav == self.cfg.availability_nav)

    # ------------------------------------------------------------------ rows
    def _scope(self, region_selector: str) -> Locator:
        if region_selector:
            region = self.page.locator(region_selector)
            if region.count() == 0:
                raise PortalError(f"region selector {region_selector!r} matched nothing on {self.page.url}")
            return region
        return self.page.locator("body")

    def rows(self, course: Course, scope: Locator) -> List[Locator]:
        """Innermost visible row containers whose text contains all the course keywords."""
        pats = course.keyword_patterns()
        rows = scope.locator(self.cfg.sel_row)
        nested = self.page.locator(self.cfg.sel_row)
        for p in pats:
            rows = rows.filter(has_text=p)
            nested = nested.filter(has_text=p)
        rows = rows.filter(has_not=nested)
        out = []
        for r in rows.all():
            try:
                if r.is_visible():
                    out.append(r)
            except PWError:
                continue
        return out

    @staticmethod
    def text_of(loc: Locator) -> str:
        try:
            return normalise(loc.inner_text(timeout=5000))
        except PWError:
            return ""

    @staticmethod
    def label_of(el: Locator) -> str:
        try:
            return re.sub(r"\s+", " ", el.evaluate(
                "e => (e.getAttribute('aria-label') || e.innerText || e.value || e.title || e.textContent || '')"
            )).strip()
        except PWError:
            return "?"

    def buttons(self, scope: Locator, kind: str) -> List[Locator]:
        """Visible, enabled buttons/links in scope whose accessible name matches buttons.<kind>."""
        rx = self.cfg.btn[kind]
        cand = scope.get_by_role("button", name=rx).or_(scope.get_by_role("link", name=rx))
        out = []
        for el in cand.all():
            try:
                if not el.is_visible() or not el.is_enabled():
                    continue
                if (el.get_attribute("aria-disabled") or "").lower() == "true":
                    continue
                if re.search(r"\bdisabled\b", el.get_attribute("class") or ""):
                    continue
            except PWError:
                continue
            label = self.label_of(el)
            if kind == "add" and (self.cfg.btn["drop"].search(label) or self.cfg.btn["swap"].search(label)):
                continue  # e.g. an "Add/Drop" menu link
            if kind == "confirm" and self.cfg.btn["cancel"].search(label):
                continue
            out.append(el)
        return out

    # ------------------------------------------------------------------ seat status
    def seat_status(self, course: Course) -> SeatStatus:
        scope = self._scope(self.cfg.sel_availability_region)
        rows = self.rows(course, scope)
        if not rows:
            return SeatStatus(course, SeatState.NOT_FOUND)
        if len(rows) > 1:
            texts = [self.text_of(r) for r in rows]
            parsed = {self.parse_seats(course, t, r).state for t, r in zip(texts, rows)}
            if len(parsed) == 1:  # duplicates agree (e.g. desktop + mobile layout)
                return self.parse_seats(course, texts[0], rows[0])
            return SeatStatus(course, SeatState.AMBIGUOUS, detail=f"{len(rows)} rows match: " + " || ".join(texts)[:200])
        return self.parse_seats(course, self.text_of(rows[0]), rows[0])

    def parse_seats(self, course: Course, text: str, row: Optional[Locator]) -> SeatStatus:
        for rx in self.cfg.seats_available:
            m = rx.search(text)
            if m:
                n = int(m.group(1))
                return SeatStatus(course, SeatState.AVAILABLE if n > 0 else SeatState.FULL, n, text)
        if any(rx.search(text) for rx in self.cfg.full):
            return SeatStatus(course, SeatState.FULL, None, text)
        if self.cfg.add_button_means_available and row is not None and self.buttons(row, "add"):
            return SeatStatus(course, SeatState.AVAILABLE, None, text)
        return SeatStatus(course, SeatState.UNKNOWN, None, text)

    # ------------------------------------------------------------------ enrolments
    def read_enrolments(self, require_some: bool = False) -> Enrolments:
        self.load_enrolments()
        return self.enrolments_on_current_page(require_some)

    def enrolments_on_current_page(self, require_some: bool = False) -> Enrolments:
        """require_some: used at start-up to catch a wrong pages.enrolments setting (you
        should at least see Helsinki). Later an empty list is legitimate, e.g. right
        after dropping Helsinki in the drop-then-add path."""
        if self.cfg.enrolments_marker is not None:
            body = normalise(self.page.locator("body").inner_text())
            if not self.cfg.enrolments_marker.search(body):
                raise PortalError("enrolments page marker not found - is pages.enrolments right?")
        scope = self._scope(self.cfg.sel_enrolled_region)
        result: Enrolments = {}
        found_any = False
        for course in self.cfg.all_courses:
            rows = self.rows(course, scope)
            found_any = found_any or bool(rows)
            if not rows:
                result[course.name] = EnrolStatus(course, EnrolState.NOT_ENROLLED)
                continue
            states = [self.row_enrol_state(r) for r in rows]
            detail = " || ".join(self.text_of(r) for r in rows)[:200]
            unique = set(states)
            if len(unique) == 1:
                state = states[0]
            elif unique == {EnrolState.ENROLLED, EnrolState.NOT_ENROLLED}:
                state = EnrolState.ENROLLED  # e.g. "My enrolments" list + catalogue row on one page
            else:
                state = EnrolState.AMBIGUOUS
            result[course.name] = EnrolStatus(course, state, detail)
        if require_some and not found_any and self.cfg.enrolments_marker is None:
            raise PortalError(
                "none of the configured courses appear on the enrolments page - "
                "check pages.enrolments / selectors.enrolled_region (or set pages.enrolments_marker)")
        return result

    def row_enrol_state(self, row: Locator) -> EnrolState:
        text = self.text_of(row)
        if self.cfg.txt["waitlisted"].search(text):
            return EnrolState.WAITLISTED
        if self.cfg.txt["pending"].search(text):
            return EnrolState.PENDING
        if self.cfg.txt["enrolled"].search(text) or self.buttons(row, "drop"):
            return EnrolState.ENROLLED
        return EnrolState.NOT_ENROLLED

    # ------------------------------------------------------------------ locating actions
    def _places(self, kind: str) -> List[Tuple[str, List[str], str, str]]:
        c = self.cfg
        avail = (c.availability_url, c.availability_nav, c.sel_availability_region, "availability page")
        enrol = (c.enrolments_url, c.enrolments_nav, c.sel_enrolled_region, "enrolments page")
        places = []
        if kind == "add":
            places = [avail]
        elif kind == "drop":
            places = [enrol, avail]
        elif kind == "swap":
            if c.swap_url:
                places.append((c.swap_url, [], "", "swap page"))
            places += [enrol, avail]
        seen, out = set(), []
        for p in places:  # de-duplicate identical pages
            key = (p[0], tuple(p[1]), p[2])
            if key not in seen:
                seen.add(key)
                out.append(p)
        return out

    def locate(self, courses: Sequence[Course], kind: str) -> Optional[Located]:
        """Find exactly one enabled <kind> button in the row of one of `courses`."""
        for url, nav, region, label in self._places(kind):
            self.load(url, nav, label, max_age_s=5)
            scope = self._scope(region)
            for course in courses:
                hits = []
                for row in self.rows(course, scope):
                    btns = self.buttons(row, kind)
                    if btns:
                        hits.append((row, btns))
                if len(hits) > 1:
                    raise PortalError(f"ambiguous: {len(hits)} rows for {course.name!r} have a {kind} button on the {label}")
                if hits:
                    row, btns = hits[0]
                    labels = {self.label_of(b) for b in btns}
                    if len(labels) > 1:
                        raise PortalError(f"ambiguous: several {kind} buttons {sorted(labels)} for {course.name!r}")
                    return Located(course, btns[0], labels.pop(), self.text_of(row), label, self.page.url)
                if self.cfg.follow_course_link:
                    found = self._locate_via_course_link(course, scope, kind, label)
                    if found:
                        return found
                    self.load(url, nav, label)
                    scope = self._scope(region)
        return None

    def _locate_via_course_link(self, course: Course, scope: Locator, kind: str, label: str) -> Optional[Located]:
        rows = self.rows(course, scope)
        if len(rows) != 1:
            return None
        links = [l for l in rows[0].get_by_role("link").all()
                 if l.is_visible() and course.mentioned_in(self.label_of(l))]
        if len(links) != 1:
            return None
        links[0].click()
        self._settle()
        self.check_page(None, f"{course.name} page")
        body = self.page.locator("body")
        if not course.mentioned_in(self.text_of(body)):
            return None
        btns = self.buttons(body, kind)
        labels = {self.label_of(b) for b in btns}
        if len(labels) != 1:
            return None
        return Located(course, btns[0], labels.pop(), "", f"{course.name} page", self.page.url)

    def find_add(self, course: Course) -> Optional[Located]:
        return self.locate([course], "add")

    def find_drop(self, course: Course) -> Optional[Located]:
        return self.locate([course], "drop")

    def find_swap(self, target: Course) -> Optional[Located]:
        found = self.locate([self.cfg.drop, target], "swap")
        if found is None and self.cfg.swap_url:
            # A dedicated swap page may have one page-level button rather than per-row ones.
            self.load(self.cfg.swap_url, [], "swap page")
            body = self.page.locator("body")
            btns = self.buttons(body, "swap")
            if len(btns) == 1 and target.mentioned_in(self.text_of(body)):
                return Located(None, btns[0], self.label_of(btns[0]), "", "swap page", self.page.url)
        return found

    # ------------------------------------------------------------------ classification
    def judge_prompt(self, text: str, ctx: _DialogContext) -> Optional[Tuple[Outcome, str]]:
        """For an 'Are you sure?' style prompt: None = OK to confirm, else (outcome, reason)."""
        t = normalise(text)
        if self.cfg.txt["clash"].search(t):
            return (Outcome.CLASH, text)
        if self.cfg.txt["cap"].search(t):
            return (Outcome.CAP, text)
        for other in ctx.forbid:
            if other.mentioned_in(t):
                return (Outcome.NEEDS_HUMAN,
                        f"confirmation for {ctx.acting_on.name} unexpectedly mentions {other.name}: {text[:200]}")
        return None

    def classify(self, text: str) -> Outcome:
        t = normalise(text)
        if not t:
            return Outcome.UNKNOWN
        tx = self.cfg.txt
        if tx["clash"].search(t):
            return Outcome.CLASH
        if tx["cap"].search(t):
            return Outcome.CAP
        if tx["seat_gone"].search(t):
            return Outcome.SEAT_GONE
        if tx["error"].search(t):
            return Outcome.ERROR
        if tx["success"].search(t):
            return Outcome.SUCCESS
        return Outcome.UNKNOWN

    def visible_dialog(self) -> Optional[Locator]:
        try:
            for el in self.page.locator(self.cfg.sel_dialog).all():
                if el.is_visible():
                    return el
        except PWError:
            pass
        return None

    def messages(self) -> str:
        texts = []
        try:
            for el in self.page.locator(self.cfg.sel_messages).all():
                if el.is_visible():
                    t = el.inner_text().strip()
                    if t and t not in texts:
                        texts.append(t)
        except PWError:
            pass
        return re.sub(r"\s+", " ", " | ".join(texts))[:500]

    # ------------------------------------------------------------------ clicking
    def perform(self, located: Located, kind: str, acting_on: Course, forbid: Sequence[Course]) -> ActionResult:
        """Click a button that changes enrolment, then handle confirmations and read the result.

        The result is only a hint: callers must re-read the enrolment page to confirm.
        """
        if self.dry_run:
            log.info("[DRY RUN] would click %s", located.describe())
            return ActionResult(Outcome.DRY_RUN, located.describe())
        ctx = _DialogContext(acting_on, forbid)
        self._dialog_ctx = ctx
        self._fresh = None
        try:
            log.info("Clicking %s", located.describe())
            located.button.click()
            outcome, message = self._after_click(ctx, kind, acting_on)
        except PWError as e:
            if _is_closed_error(e):
                raise BrowserClosed() from e
            if self.s.on_login_page():
                raise SessionExpired("session expired during an action") from e
            outcome, message = Outcome.UNKNOWN, f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"
        finally:
            self._dialog_ctx = None
        log.info("Result of %s: %s%s", kind, outcome.value, f" - {message}" if message else "")
        return ActionResult(outcome, message, clicked=True)

    def _after_click(self, ctx: _DialogContext, kind: str, acting_on: Course,
                     handled_limit: int = 3) -> Tuple[Outcome, str]:
        nav_before = self._nav_count
        deadline = time.monotonic() + self.cfg.settle_ms / 1000
        handled = 0
        quiet_since: Optional[float] = None
        info_outcome: Optional[Tuple[Outcome, str]] = None
        while time.monotonic() < deadline:
            self.page.wait_for_timeout(250)
            if ctx.rejected:
                return ctx.rejected
            if self.s.on_login_page():
                raise SessionExpired("redirected to login after clicking")
            dlg = self.visible_dialog()
            if dlg is None or handled >= handled_limit:
                # Done early once the page has navigated (or shows a message) and no
                # dialog has appeared for a short grace period.
                now = time.monotonic()
                quiet_since = quiet_since or now
                if now - quiet_since >= 1.0 and (self._nav_count > nav_before or self.messages()):
                    break
                continue
            quiet_since = None
            text = self.text_of(dlg)
            ctx.seen.append(text)
            confirm = self.buttons(dlg, "confirm")
            cancel = self.buttons(dlg, "cancel")
            is_prompt = bool(confirm) and bool(cancel)
            if is_prompt:
                verdict = self.judge_prompt(text, ctx)
                if verdict is not None:
                    log.warning("Dialog %r -> CANCEL (%s)", text[:200], verdict[0].value)
                    self._dismiss(dlg, cancel)
                    return verdict
                log.info("Dialog %r -> confirming with '%s'", text[:200], self.label_of(confirm[0]))
                confirm[0].click()
            else:
                # Informational (single OK / Close): read it, then close it.
                info_outcome = (self.classify(text), text[:300])
                log.info("Dialog %r (%s)", text[:200], info_outcome[0].value)
                self._dismiss(dlg, confirm or cancel)
            handled += 1
            self._settle()
            deadline = time.monotonic() + self.cfg.settle_ms / 1000
        self._settle()
        if ctx.rejected:
            return ctx.rejected
        if self.s.on_login_page():
            raise SessionExpired("redirected to login after clicking")
        msg = self.messages()
        native = " | ".join(m for m in ctx.seen if m)
        for candidate in (msg, native):
            if candidate:
                o = self.classify(candidate)
                if o != Outcome.UNKNOWN:
                    return o, candidate[:300]
        if info_outcome:
            return info_outcome
        return Outcome.UNKNOWN, msg or native

    def _dismiss(self, dlg: Locator, buttons: List[Locator]) -> None:
        try:
            if buttons:
                buttons[0].click()
            else:
                self.page.keyboard.press("Escape")
        except PWError:
            pass

    def choose_in_form(self, course: Course) -> bool:
        """For a native swap form: pick `course` in a <select> or radio group. True if chosen."""
        scope = self.visible_dialog() or self.page.locator("body")
        for sel in scope.locator("select").all():
            try:
                if not sel.is_visible():
                    continue
                options = sel.locator("option").all_inner_texts()
            except PWError:
                continue
            matches = [o for o in options if course.mentioned_in(o)]
            if len(matches) == 1:
                sel.select_option(label=matches[0])
                log.info("Selected %r in the swap form", matches[0].strip())
                return True
        lookahead = "".join(f"(?=.*{p.pattern})" for p in course.keyword_patterns())
        radios = [r for r in scope.get_by_role("radio", name=re.compile(lookahead, re.IGNORECASE)).all() if r.is_visible()]
        if len(radios) == 1:
            radios[0].check()
            return True
        return False

    def perform_swap(self, located: Located, target: Course, forbid: Sequence[Course]) -> ActionResult:
        """Use EMS's own swap/replace feature (atomic on the server side)."""
        if self.dry_run:
            log.info("[DRY RUN] would click native swap control %s, choose %s and confirm",
                     located.describe(), target.name if located.course != target else self.cfg.drop.name)
            return ActionResult(Outcome.DRY_RUN, located.describe())
        other = self.cfg.drop if located.course == target else target
        ctx = _DialogContext(target, forbid)
        self._dialog_ctx = ctx
        self._fresh = None
        try:
            log.info("Native swap: clicking %s", located.describe())
            located.button.click()
            self._settle()
            if ctx.rejected:
                return ActionResult(ctx.rejected[0], ctx.rejected[1], clicked=True)
            if self.s.on_login_page():
                raise SessionExpired("redirected to login after clicking swap")
            scope = self.visible_dialog() or self.page.locator("body")
            scope_text = self.text_of(scope)
            chosen = self.choose_in_form(other)
            if not chosen and not other.mentioned_in(scope_text):
                log.warning("Native swap form: couldn't find %s to select - cancelling.", other.name)
                self._dismiss(scope, self.buttons(scope, "cancel") if self.visible_dialog() else [])
                return ActionResult(Outcome.INCOMPLETE, "could not complete the swap form", clicked=True)
            confirm = [b for b in self.buttons(scope, "confirm") + self.buttons(scope, "swap")
                       if not self.cfg.btn["cancel"].search(self.label_of(b))]
            if not confirm:
                self._dismiss(scope, self.buttons(scope, "cancel") if self.visible_dialog() else [])
                return ActionResult(Outcome.INCOMPLETE, "no confirm button in the swap form", clicked=True)
            verdict = self.judge_prompt(scope_text, ctx)
            if verdict is not None:
                self._dismiss(scope, self.buttons(scope, "cancel"))
                return ActionResult(verdict[0], verdict[1], clicked=True)
            log.info("Native swap: confirming with '%s'", self.label_of(confirm[0]))
            confirm[0].click()
            outcome, message = self._after_click(ctx, "swap", target)
            return ActionResult(outcome, message, clicked=True)
        except PWError as e:
            if _is_closed_error(e):
                raise BrowserClosed() from e
            if self.s.on_login_page():
                raise SessionExpired("session expired during swap") from e
            return ActionResult(Outcome.UNKNOWN, f"{type(e).__name__}: {str(e).splitlines()[0][:160]}", clicked=True)
        finally:
            self._dialog_ctx = None
