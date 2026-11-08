import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ems_swap.alerts import Alerts, setup_logging  # noqa: E402
from ems_swap.backoff import Backoff  # noqa: E402
from ems_swap.config import build_config  # noqa: E402
from ems_swap.monitor import Monitor  # noqa: E402
from ems_swap.session import LOGGED_IN_MARKER, BrowserSession  # noqa: E402
from mock_ems import MockEMS, Scenario  # noqa: E402

BROWSER_PATH = os.environ.get("EMS_BROWSER_PATH") or (
    "/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None)


class Harness:
    def __init__(self, tmp_path: Path, scenario: Scenario):
        self.tmp = tmp_path
        self.mock = MockEMS(scenario).start()
        self.cfg = build_config({
            "pages": {
                "home": f"{self.mock.portal}/Student/Home",
                "availability": f"{self.mock.portal}/Student/Electives",
                "enrolments": f"{self.mock.portal}/Student/Enrolments",
            },
            "auth": {"portal_host": "127.0.0.1", "relogin_alert_every_s": 30},
            "timeouts": {"settle_ms": 1500, "navigation_ms": 10000, "action_ms": 5000},
        })
        self.profile = tmp_path / "profile"
        self.answers = []
        self.prompts = []
        self.sessions = []

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answers.pop(0) if self.answers else ""

    def session(self, headless: bool = True, logged_in_before: bool = True) -> BrowserSession:
        if logged_in_before:
            self.profile.mkdir(parents=True, exist_ok=True)
            (self.profile / LOGGED_IN_MARKER).touch()
        s = BrowserSession(self.cfg, self.profile, Alerts(sound=False), headless=headless,
                           executable_path=BROWSER_PATH)
        self.sessions.append(s)
        return s

    def seed_login(self, s: BrowserSession) -> None:
        """Skip the SSO dance: give the browser a valid portal session cookie."""
        s.context.add_cookies([{"name": "ems_session", "value": self.mock.new_session(),
                                "url": self.mock.portal}])

    def monitor(self, dry_run=False, max_checks=None, headless=True, once=False, logged_in=True) -> Monitor:
        s = self.session(headless=headless)
        s.start()
        if logged_in:
            self.seed_login(s)
        fast = Backoff(base_s=0.2, jitter_min_s=0.0, jitter_max_s=0.1, max_s=1.0)
        return Monitor(self.cfg, s, Alerts(sound=False), dry_run=dry_run, once=once, ask=self.ask,
                       backoff=fast, max_checks=max_checks)

    def close(self):
        for s in self.sessions:
            s.close()
        self.mock.stop()


@pytest.fixture
def harness(tmp_path, monkeypatch):
    setup_logging(None, verbose=False)
    monkeypatch.setenv("LBS_USER", "student@london.edu")
    monkeypatch.setenv("LBS_PASS", "correct horse")
    made = []

    def make(scenario: Scenario) -> Harness:
        h = Harness(tmp_path, scenario)
        made.append(h)
        return h

    yield make
    for h in made:
        h.close()
