"""Browser lifecycle: persistent profile, login detection, waiting for SSO/MFA."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Callable, List, Optional
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Error as PWError, Page, Playwright, sync_playwright

from .alerts import Alerts
from .config import Config
from .errors import BrowserClosed

log = logging.getLogger("ems_swap")

LOGGED_IN_MARKER = ".ems_swap_logged_in_once"

# Generic login-form selectors (Microsoft Entra ID / ADFS / plain forms).
USER_FIELD = (
    "input[name='loginfmt'], input[type='email'], input[name='username'], input[name='UserName'], "
    "input[autocomplete='username'], input#username, input#userNameInput"
)
PASS_FIELD = "input[name='passwd'], input#passwordInput, input[type='password']"
SUBMIT_NAME = re.compile(r"^\s*(next|sign in|log ?in|continue|submit)\s*$", re.IGNORECASE)
STAY_SIGNED_IN = re.compile(r"stay signed in", re.IGNORECASE)
YES_NAME = re.compile(r"^\s*yes\s*$", re.IGNORECASE)


def _is_closed_error(e: Exception) -> bool:
    return "closed" in str(e).lower() and ("target" in str(e).lower() or "browser" in str(e).lower())


def disable_password_saving(profile_dir: Path) -> None:
    """Make sure Chromium never offers to store the LBS password in the profile."""
    prefs_path = profile_dir / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if prefs_path.exists():
        try:
            data = json.loads(prefs_path.read_text(encoding="utf-8"))
        except Exception:
            return  # don't clobber a profile we can't parse
    data["credentials_enable_service"] = False
    data.setdefault("profile", {})["password_manager_enabled"] = False
    autofill = data.setdefault("autofill", {})
    autofill["profile_enabled"] = False
    autofill["credit_card_enabled"] = False
    prefs_path.write_text(json.dumps(data), encoding="utf-8")


class BrowserSession:
    def __init__(self, cfg: Config, profile_dir: Path, alerts: Alerts, headless: bool = False,
                 channel: Optional[str] = None, executable_path: Optional[str] = None,
                 autofill: bool = True) -> None:
        self.cfg = cfg
        self.profile_dir = profile_dir.expanduser()
        self.alerts = alerts
        self.want_headless = headless
        self.channel = channel
        self.executable_path = executable_path
        self.autofill = autofill
        self._pw: Optional[Playwright] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._page_hooks: List[Callable[[Page], None]] = []
        first_run = not (self.profile_dir / LOGGED_IN_MARKER).exists()
        self.headless = headless and not first_run
        if headless and first_run:
            log.info("First run: opening a visible browser so you can complete the LBS sign-in/MFA.")

    # ---------------------------------------------------------------- lifecycle
    def add_page_hook(self, hook: Callable[[Page], None]) -> None:
        self._page_hooks.append(hook)
        if self.page is not None:
            hook(self.page)

    def start(self) -> None:
        self._pw = sync_playwright().start()
        self._launch(self.headless)

    def _launch(self, headless: bool) -> None:
        assert self._pw is not None
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.profile_dir, 0o700)  # session cookies live here
        except OSError:
            pass
        disable_password_saving(self.profile_dir)
        kwargs = dict(
            user_data_dir=str(self.profile_dir),
            headless=headless,
            viewport={"width": 1366, "height": 900},
            accept_downloads=False,
        )
        if self.channel:
            kwargs["channel"] = self.channel
        if self.executable_path:
            kwargs["executable_path"] = self.executable_path
        self.context = self._pw.chromium.launch_persistent_context(**kwargs)
        self.context.set_default_timeout(self.cfg.action_timeout_ms)
        self.context.set_default_navigation_timeout(self.cfg.nav_timeout_ms)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.headless = headless
        for hook in self._page_hooks:
            hook(self.page)
        log.debug("Browser launched (headless=%s, profile=%s)", headless, self.profile_dir)

    def _close_context(self) -> None:
        if self.context is not None:
            try:
                self.context.close()
            except PWError:
                pass
        self.context = None
        self.page = None

    def close(self) -> None:
        self._close_context()
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None

    def sleep(self, seconds: float) -> None:
        """Wait while still letting Playwright process browser events."""
        try:
            assert self.page is not None
            self.page.wait_for_timeout(seconds * 1000)
        except PWError as e:
            if _is_closed_error(e):
                raise BrowserClosed() from e
            raise

    # ---------------------------------------------------------------- login state
    def on_login_page(self, page: Optional[Page] = None) -> bool:
        page = page or self.page
        assert page is not None
        url = page.url
        if any(p.search(url) for p in self.cfg.login_url_patterns):
            return True
        host = (urlparse(url).hostname or "").lower()
        if host and host != self.cfg.portal_host and not host.endswith("." + self.cfg.portal_host):
            return True  # bounced off the portal: SSO / identity provider
        try:
            pw = page.locator("input[type='password']")
            return any(el.is_visible() for el in pw.all())
        except PWError:
            return False

    def is_logged_in(self, page: Optional[Page] = None) -> bool:
        page = page or self.page
        assert page is not None
        host = (urlparse(page.url).hostname or "").lower()
        on_portal = host == self.cfg.portal_host or host.endswith("." + self.cfg.portal_host)
        return on_portal and not self.on_login_page(page)

    def ensure_logged_in(self) -> None:
        assert self.page is not None
        try:
            self.page.goto(self.cfg.home_url, wait_until="domcontentloaded")
            self.page.wait_for_load_state("load")
        except PWError as e:
            if _is_closed_error(e):
                raise BrowserClosed() from e
            log.warning("Could not load the EMS home page yet (%s)", type(e).__name__)
        if self.is_logged_in():
            log.info("Logged in to EMS (saved session reused).")
            self._mark_logged_in()
            return
        self.relogin("No valid EMS session found.")

    def relogin(self, reason: str) -> None:
        """Pause, alert the user, let them finish SSO/MFA, then carry on."""
        self.alerts.attention(
            "EMS LOGIN NEEDED - monitoring is paused",
            f"{reason}\nFinish signing in (including MFA) in the browser window.\n"
            "Monitoring resumes automatically once you're back on EMS.",
        )
        relaunched = False
        if self.headless:
            log.info("Switching to a visible browser window so you can sign in.")
            self._close_context()
            self._launch(headless=False)
            relaunched = True
        assert self.page is not None and self.context is not None
        try:
            self.page.goto(self.cfg.home_url, wait_until="domcontentloaded")
        except PWError as e:
            if _is_closed_error(e):
                raise BrowserClosed() from e
        if self.autofill and not (os.environ.get("LBS_USER") and os.environ.get("LBS_PASS")):
            log.warning("LBS_USER / LBS_PASS not set - please type your credentials in the browser.")

        filled: set = set()
        last_alert = time.monotonic()
        while True:
            if self._any_page_logged_in():
                break
            if self.autofill:
                self._autofill(filled)
            self.sleep(1.5)
            if time.monotonic() - last_alert > self.cfg.relogin_alert_every_s:
                self.alerts.attention("Still waiting for you to sign in to EMS", "Monitoring is paused.")
                last_alert = time.monotonic()

        self._mark_logged_in()
        log.info("Signed in - resuming.")
        if relaunched and self.want_headless:
            self._close_context()
            self._launch(headless=True)
            assert self.page is not None
            self.page.goto(self.cfg.home_url, wait_until="domcontentloaded")
            if not self.is_logged_in():
                log.warning("The session didn't carry over to headless mode; staying in a visible window.")
                self._close_context()
                self._launch(headless=False)
                self.relogin("Session not kept after switching back to headless.")

    def _any_page_logged_in(self) -> bool:
        assert self.context is not None and self.page is not None
        if self.page.is_closed():
            raise BrowserClosed()
        if self.is_logged_in(self.page):
            return True
        for p in self.context.pages:
            if p is not self.page and not p.is_closed() and self.is_logged_in(p):
                self.page.goto(self.cfg.home_url, wait_until="domcontentloaded")
                return self.is_logged_in(self.page)
        return False

    def _mark_logged_in(self) -> None:
        try:
            (self.profile_dir / LOGGED_IN_MARKER).touch()
        except OSError:
            pass

    # ---------------------------------------------------------------- autofill
    def _first_visible(self, selector: str):
        assert self.page is not None
        for el in self.page.locator(selector).all():
            try:
                if el.is_visible() and el.is_editable():
                    return el
            except PWError:
                continue
        return None

    def _click_submit(self) -> None:
        assert self.page is not None
        for el in self.page.get_by_role("button", name=SUBMIT_NAME).all():
            if el.is_visible():
                el.click()
                return
        for sel in ("#idSIButton9", "input[type='submit']", "button[type='submit']"):
            el = self.page.locator(sel).first
            if el.count() and el.is_visible():
                el.click()
                return

    def _autofill(self, filled: set) -> None:
        """Type LBS_USER / LBS_PASS into the SSO form once per login; the user does MFA.

        Each step runs at most once, so a wrong password never gets retried in a loop
        (avoids account lock-outs). Values are never logged.
        """
        page = self.page
        assert page is not None
        try:
            if not self.on_login_page(page):
                return
            user, password = os.environ.get("LBS_USER"), os.environ.get("LBS_PASS")
            pw_field = self._first_visible(PASS_FIELD)
            if user and "user" not in filled:
                u = self._first_visible(USER_FIELD)
                if u is not None:
                    if not (u.input_value() or "").strip():
                        u.fill(user)
                    filled.add("user")
                    if pw_field is None:
                        self._click_submit()
                        return
            if password and "password" not in filled and pw_field is not None:
                pw_field.fill(password)
                filled.add("password")
                self._click_submit()
                log.info("Entered your username/password - approve the MFA prompt to continue.")
                return
            if "kmsi" not in filled and page.get_by_text(STAY_SIGNED_IN).count():
                for el in page.get_by_role("button", name=YES_NAME).all():
                    if el.is_visible():
                        el.click()  # "Stay signed in? Yes" keeps the saved session alive longer
                        filled.add("kmsi")
                        return
        except PWError as e:
            if _is_closed_error(e):
                raise BrowserClosed() from e
            # Deliberately not logging the exception text (could echo a typed value).
            log.debug("Autofill step skipped (%s)", type(e).__name__)
