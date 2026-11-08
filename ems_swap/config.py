"""Configuration: course names, page URLs, selectors and text patterns.

Everything that depends on how the EMS pages actually look lives here (and in
ems_config.toml), so the selectors can be tuned after running `inspect`
without touching the swap logic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # Python 3.11+
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # Python 3.9 / 3.10
    import tomli as tomllib  # type: ignore[no-redef]


class ConfigError(Exception):
    pass


# Dash characters that might appear in course names (hyphen, en dash, em dash, minus...).
_DASHES = "-‐‑‒–—―−"


def normalise(text: str) -> str:
    """Lower-case, unify dashes and collapse whitespace (incl. non-breaking spaces)."""
    text = text.replace(" ", " ")
    for d in _DASHES:
        text = text.replace(d, "-")
    return re.sub(r"\s+", " ", text).strip().lower()


@dataclass
class Course:
    name: str
    keywords: List[str]

    @classmethod
    def from_value(cls, value: Any) -> "Course":
        if isinstance(value, str):
            return cls(name=value, keywords=default_keywords(value))
        if isinstance(value, dict) and "name" in value:
            kws = value.get("keywords") or default_keywords(value["name"])
            return cls(name=value["name"], keywords=[normalise(k) for k in kws])
        raise ConfigError(f"Bad course entry: {value!r}")

    def keyword_patterns(self) -> List[re.Pattern]:
        """One case-insensitive regex per keyword; all must match a row's text."""
        pats = []
        for kw in self.keywords:
            parts = [re.escape(p) for p in kw.split()]
            # JS-compatible (Playwright sends has_text regexes to the browser).
            pats.append(re.compile(r"\s+".join(parts), re.IGNORECASE))
        return pats

    def mentioned_in(self, text: str) -> bool:
        t = normalise(text)
        return all(re.search(r"\s+".join(map(re.escape, kw.split())), t) for kw in self.keywords)


def default_keywords(name: str) -> List[str]:
    """'Global Experience – Hong Kong' -> ['global experience', 'hong kong']."""
    parts = [p.strip() for p in re.split(f"[{_DASHES}:|]", normalise(name)) if p.strip()]
    return parts or [normalise(name)]


# --------------------------------------------------------------------------------------
# Defaults. Button-name patterns are matched by Playwright *in the browser*, so they must
# be JavaScript-compatible (no (?P<name>...) groups, no inline (?i) flags). All patterns
# are compiled case-insensitive. Text patterns are matched in Python against the
# whitespace-normalised, dash-normalised, lower-cased text.
# --------------------------------------------------------------------------------------
DEFAULTS: Dict[str, Any] = {
    "pages": {
        "home": "https://ems.london.edu/Student/Home",
        "availability": "",  # page listing electives + seat counts ("" = home)
        "enrolments": "",  # page listing your enrolments ("" = availability)
        "swap": "",  # page with a native swap/replace feature, if EMS has one
        # Optional: link/button/tab names to click (in order) after loading the page,
        # for portals where the list is reached by clicking rather than by URL.
        "availability_nav": [],
        "enrolments_nav": [],
        # Optional regex that must appear on the enrolments page for it to count as
        # "loaded correctly" (e.g. "my enrolments"). Leave "" to skip the check.
        "enrolments_marker": "",
    },
    "auth": {
        "portal_host": "ems.london.edu",
        # URL regexes that mean "this is a login page, not the portal".
        "login_url_patterns": [
            r"login\.microsoftonline\.com",
            r"login\.live\.com",
            r"/adfs/",
            r"/(account/)?log-?in\b",
            r"/sign-?in\b",
            r"/saml",
            r"/sso\b",
            r"/oauth2?/",
        ],
        "relogin_alert_every_s": 60,
    },
    "courses": {
        "targets": ["Global Experience – Hong Kong", "Global Experience – Vietnam"],
        "drop": "Global Experience – Helsinki",
    },
    "selectors": {
        # Containers that hold one course each. The innermost container whose text
        # contains all of a course's keywords is treated as that course's "row".
        "row": "tr, [role='row'], li, article, .card, .course, .elective, .list-group-item",
        # Optional CSS selectors that scope where to look for rows.
        "availability_region": "",
        "enrolled_region": "",
        "dialog": "[role='dialog'], [role='alertdialog'], .modal, dialog[open], .swal2-popup, .ui-dialog",
        "messages": (
            "[role='alert'], [role='status'], .alert, .validation-summary-errors, "
            ".field-validation-error, .toast, .notification, .message, .error, .success, "
            ".flash, #flash, .feedback"
        ),
        "captcha": (
            "iframe[src*='recaptcha'], iframe[src*='hcaptcha'], iframe[src*='turnstile'], "
            "iframe[src*='challenges.cloudflare.com'], .g-recaptcha, .h-captcha, #cf-challenge-running"
        ),
    },
    "buttons": {  # JS-compatible regexes matched against accessible names
        "add": r"^\s*(add|enrol|enroll|register|sign up|join|select|take)\b(?!.*wait)",
        "drop": r"^\s*(drop|remove|withdraw|unenrol|unenroll|de-?register|leave|cancel (enrol|registration))\b",
        "swap": r"\b(swap|replace|exchange|switch)\b",
        "confirm": r"^\s*(confirm|yes|ok|okay|continue|proceed|submit|add|enrol|enroll|drop|remove|withdraw|swap|replace)\b",
        "cancel": r"^\s*(cancel|no|close|back|dismiss|keep)\b|^\s*[x×]\s*$",
    },
    "text": {  # Python regexes on normalised text
        # Seat counts. Group 1 must capture the number of free seats.
        "seats_available": [
            r"(\d+)\s*(?:seats?|places?|spaces?|spots?)\s*(?:available|left|remaining|open|free)",
            r"(?:seats?|places?|spaces?)\s*(?:available|left|remaining|open|free)\s*[:\-]?\s*(\d+)",
            r"(?:availability|available|remaining|vacanc(?:y|ies))\s*[:\-]\s*(\d+)",
        ],
        "full": [
            r"\bfull\b",
            r"\bwait\s*-?list",
            r"\bno (?:seats|places|spaces) (?:available|left)",
            r"\bsold out\b",
            r"\bclosed\b",
        ],
        # Row status text meaning "you are in this course".
        # Lookarounds stop counts like "45 enrolled" or "Enrolled: 45/50" from matching.
        "enrolled": r"(?<![\d/]\s)(?<![\d/])\b(enrolled|confirmed|registered|allocated)\b(?!\s*[:(/]?\s*\d)",
        "waitlisted": r"\bwait\s*-?listed\b|\bon (?:the )?wait(?:ing)?\s*-?list\b",
        "pending": r"\bpending\b|\bawaiting\b|\bin progress\b|\bprovisional\b",
        # Messages / dialog text after an action.
        "clash": r"clash|conflict|overlap|same (?:time|week|block|dates)",
        "cap": (r"credit (?:limit|cap|maximum)|maximum (?:number of )?(?:credits|courses|electives|units)"
                r"|exceed|over (?:the |your )?(?:credit )?limit|too many|quota|allowance"),
        "seat_gone": r"\bfull\b|no (?:seats|places|spaces)|capacity (?:reached|exceeded)|no longer available|not available|sold out",
        "error": r"\berror\b|unable|cannot|can't|could not|failed|not (?:allowed|permitted|eligible)|denied|invalid|prerequisite",
        "success": r"success|has been (?:added|dropped|removed|swapped|enrolled|replaced)|you (?:are|have been) (?:now )?(?:enrolled|registered)|enrolment confirmed|added to your|swapped|dropped|removed from your",
        "captcha": r"captcha|verify you are (?:a )?human|are you a robot|unusual traffic|access denied",
    },
    "behaviour": {
        # If a row has no seat count and no "full" text, does an enabled Add/Enrol
        # button mean a seat is open? (Safe to leave on: adding never drops anything.)
        "add_button_means_available": True,
        # Click the course-name link inside the row and look for buttons on the
        # course's own page (for portals where Add/Drop live on a detail page).
        "follow_course_link": False,
        "enrolment_recheck_every": 30,  # re-read the enrolment page every N checks
        "max_failed_add_attempts": 3,  # per course, before giving up and alerting
    },
    "polling": {
        "interval_s": 10,
        "jitter_min_s": 1,
        "jitter_max_s": 3,
        "max_interval_s": 120,
    },
    "timeouts": {
        "navigation_ms": 30000,
        "action_ms": 15000,
        "settle_ms": 6000,  # max wait for dialogs/messages after a click
        "network_idle_ms": 2000,  # extra wait for background requests after a page load
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _rx(pattern: str, where: str) -> re.Pattern:
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        raise ConfigError(f"Invalid regex in {where}: {pattern!r} ({e})") from e


@dataclass
class Config:
    raw: Dict[str, Any]
    home_url: str
    availability_url: str
    enrolments_url: str
    swap_url: str
    availability_nav: List[str]
    enrolments_nav: List[str]
    enrolments_marker: Optional[re.Pattern]
    portal_host: str
    login_url_patterns: List[re.Pattern]
    relogin_alert_every_s: float
    targets: List[Course]
    drop: Course
    sel_row: str
    sel_availability_region: str
    sel_enrolled_region: str
    sel_dialog: str
    sel_messages: str
    sel_captcha: str
    btn: Dict[str, re.Pattern]
    seats_available: List[re.Pattern]
    full: List[re.Pattern]
    txt: Dict[str, re.Pattern]
    add_button_means_available: bool
    follow_course_link: bool
    enrolment_recheck_every: int
    max_failed_add_attempts: int
    interval_s: float
    jitter_min_s: float
    jitter_max_s: float
    max_interval_s: float
    nav_timeout_ms: int
    action_timeout_ms: int
    settle_ms: int
    network_idle_ms: int
    source: Optional[Path] = field(default=None)

    @property
    def all_courses(self) -> List[Course]:
        return [*self.targets, self.drop]


def build_config(data: Dict[str, Any], source: Optional[Path] = None) -> Config:
    d = _deep_merge(DEFAULTS, data)
    p, a, c, s, b, t = d["pages"], d["auth"], d["courses"], d["selectors"], d["buttons"], d["text"]
    beh, pol, tmo = d["behaviour"], d["polling"], d["timeouts"]

    targets = [Course.from_value(x) for x in c["targets"]]
    drop = Course.from_value(c["drop"])
    if not targets:
        raise ConfigError("courses.targets must list at least one course")
    for tc in targets:
        if tc.keywords == drop.keywords:
            raise ConfigError(f"Target {tc.name!r} has the same keywords as the course to drop")

    home = p["home"]
    availability = p["availability"] or home
    enrolments = p["enrolments"] or availability
    marker = _rx(p["enrolments_marker"], "pages.enrolments_marker") if p["enrolments_marker"] else None

    if pol["interval_s"] < 5:
        raise ConfigError("polling.interval_s below 5 seconds is not allowed (be kind to EMS)")

    return Config(
        raw=d,
        home_url=home,
        availability_url=availability,
        enrolments_url=enrolments,
        swap_url=p["swap"],
        availability_nav=list(p["availability_nav"]),
        enrolments_nav=list(p["enrolments_nav"]),
        enrolments_marker=marker,
        portal_host=a["portal_host"].lower(),
        login_url_patterns=[_rx(x, "auth.login_url_patterns") for x in a["login_url_patterns"]],
        relogin_alert_every_s=float(a["relogin_alert_every_s"]),
        targets=targets,
        drop=drop,
        sel_row=s["row"],
        sel_availability_region=s["availability_region"],
        sel_enrolled_region=s["enrolled_region"],
        sel_dialog=s["dialog"],
        sel_messages=s["messages"],
        sel_captcha=s["captcha"],
        btn={k: _rx(v, f"buttons.{k}") for k, v in b.items()},
        seats_available=[_rx(x, "text.seats_available") for x in t["seats_available"]],
        full=[_rx(x, "text.full") for x in t["full"]],
        txt={k: _rx(v, f"text.{k}") for k, v in t.items() if isinstance(v, str)},
        add_button_means_available=bool(beh["add_button_means_available"]),
        follow_course_link=bool(beh["follow_course_link"]),
        enrolment_recheck_every=max(1, int(beh["enrolment_recheck_every"])),
        max_failed_add_attempts=max(1, int(beh["max_failed_add_attempts"])),
        interval_s=float(pol["interval_s"]),
        jitter_min_s=float(pol["jitter_min_s"]),
        jitter_max_s=float(pol["jitter_max_s"]),
        max_interval_s=float(pol["max_interval_s"]),
        nav_timeout_ms=int(tmo["navigation_ms"]),
        action_timeout_ms=int(tmo["action_ms"]),
        settle_ms=int(tmo["settle_ms"]),
        network_idle_ms=int(tmo["network_idle_ms"]),
        source=source,
    )


def load_config(path: Optional[Path]) -> Config:
    if path is None:
        return build_config({})
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    with path.open("rb") as fh:
        try:
            data = tomllib.load(fh)
        except Exception as e:  # tomllib.TOMLDecodeError
            raise ConfigError(f"Could not parse {path}: {e}") from e
    return build_config(data, source=path)
