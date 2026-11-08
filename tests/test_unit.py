"""Pure-Python tests: back-off schedule, config, text parsing, redaction."""

import logging

from ems_swap.alerts import RedactSecrets
from ems_swap.backoff import Backoff
from ems_swap.config import Course, build_config, default_keywords, normalise
from ems_swap.inspect_dom import scrub_html
from ems_swap.portal import Outcome, Portal, SeatState


def test_backoff_doubles_to_cap_and_resets():
    b = Backoff(10, 1, 3, 120, rng=lambda lo, hi: 2.0)
    assert b.next_delay() == 12  # 10 + jitter
    seen = []
    for _ in range(6):
        b.failure()
        seen.append(b.interval_s)
    assert seen == [20, 40, 80, 120, 120, 120]
    assert b.next_delay() == 120  # cap includes jitter
    b.success()
    assert b.interval_s == 10 and b.next_delay() == 12


def test_backoff_jitter_range():
    b = Backoff(10, 1, 3, 120)
    for _ in range(200):
        assert 11 <= b.next_delay() <= 13


def test_course_keywords_and_dash_normalisation():
    assert default_keywords("Global Experience – Hong Kong") == ["global experience", "hong kong"]
    c = Course.from_value("Global Experience – Hong Kong")
    assert c.mentioned_in("GLOBAL EXPERIENCE - HONG KONG (Week 7)")
    assert c.mentioned_in("Global Experience: Hong Kong")
    assert not c.mentioned_in("Global Experience – Helsinki")
    assert normalise("A — B  C") == "a - b c"


def test_config_rejects_too_fast_polling():
    import pytest
    from ems_swap.config import ConfigError
    with pytest.raises(ConfigError):
        build_config({"polling": {"interval_s": 1}})


class _FakePortal(Portal):
    def __init__(self, cfg):  # no browser needed for text parsing
        self.cfg = cfg


def test_seat_parsing():
    cfg = build_config({})
    p = _FakePortal(cfg)
    c = cfg.targets[0]

    def st(text):
        return p.parse_seats(c, normalise(text), None)

    assert st("GE Hong Kong | 3 seats available | Enrol").state == SeatState.AVAILABLE
    assert st("GE Hong Kong | 3 seats available").seats == 3
    assert st("GE Hong Kong | Places remaining: 2").seats == 2
    assert st("GE Hong Kong | Available: 0").state == SeatState.FULL
    assert st("GE Hong Kong | 0 places left").state == SeatState.FULL
    assert st("GE Hong Kong | FULL | Join waitlist").state == SeatState.FULL
    assert st("GE Hong Kong | Capacity 40").state == SeatState.UNKNOWN


def test_message_classification():
    p = _FakePortal(build_config({}))
    assert p.classify("Unable to enrol: timetable clash with GE Helsinki") == Outcome.CLASH
    assert p.classify("You have exceeded your maximum number of credits") == Outcome.CAP
    assert p.classify("Sorry, this course is full") == Outcome.SEAT_GONE
    assert p.classify("You have been enrolled in GE Hong Kong") == Outcome.SUCCESS
    assert p.classify("An error occurred") == Outcome.ERROR
    assert p.classify("") == Outcome.UNKNOWN


def test_enrolled_regex_ignores_counts():
    rx = build_config({}).txt["enrolled"]
    assert rx.search(normalise("Status: Enrolled"))
    assert not rx.search(normalise("45 enrolled"))
    assert not rx.search(normalise("Enrolled: 45/50"))
    assert not rx.search(normalise("30/30 enrolled"))


def test_secrets_redacted_from_logs(monkeypatch):
    monkeypatch.setenv("LBS_PASS", "hunter2-secret")
    monkeypatch.setenv("LBS_USER", "me@london.edu")
    rec = logging.LogRecord("ems_swap", logging.INFO, __file__, 1, "typed %s / %s", ("hunter2-secret", "me@london.edu"), None)
    RedactSecrets().filter(rec)
    assert "hunter2" not in rec.getMessage() and "me@london.edu" not in rec.getMessage()


def test_scrub_html_tokens():
    html = ('<input type="hidden" name="__RequestVerificationToken" value="abc123">'
            '<input value="zzz" name="csrf_token"><meta name="csrf-token" content="qqq">'
            '<input name="course" value="Global Experience">')
    out = scrub_html(html)
    assert "abc123" not in out and "zzz" not in out and "qqq" not in out
    assert 'value="Global Experience"' in out
