"""End-to-end flows against the mock portal (headless Chromium)."""

import os

import pytest

from ems_swap.monitor import EXIT_DECLINED, EXIT_FAILED, EXIT_OK
from ems_swap.portal import EnrolState, Outcome, Portal
from ems_swap.swap import Result, Swapper
from mock_ems import HEL, HK, LIS, VN, Scenario

MUTATIONS = ("add:", "drop:", "swap:")


def mutations(h):
    return [a for a in h.mock.actions if a.startswith(MUTATIONS)]


def test_dry_run_never_clicks(harness, caplog):
    h = harness(Scenario(open_after={HK: 2}))
    code = h.monitor(dry_run=True, max_checks=4).run()
    assert code == EXIT_OK
    assert mutations(h) == []
    assert h.mock.sc.enrolled == {HEL}
    text = caplog.text
    assert "SEAT OPEN (dry run)" in text
    assert "ADD: click 'Enrol'" in text and "DROP: click 'Drop'" in text


def test_add_then_drop(harness):
    h = harness(Scenario(open_after={HK: 2}))
    assert h.monitor(max_checks=8).run() == EXIT_OK
    assert mutations(h) == [f"add:{HK}", f"drop:{HEL}"]  # ADD strictly before DROP
    assert h.mock.sc.enrolled == {HK}


def test_vietnam_also_works_with_native_confirm(harness):
    h = harness(Scenario(open_after={VN: 3}, add_confirm="native"))
    assert h.monitor(max_checks=8).run() == EXIT_OK
    assert mutations(h) == [f"add:{VN}", f"drop:{HEL}"]
    assert h.mock.sc.enrolled == {VN}


def test_native_swap_preferred(harness):
    h = harness(Scenario(open_after={HK: 2}, native_swap=True))
    assert h.monitor(max_checks=8).run() == EXIT_OK
    assert mutations(h) == [f"swap:{HEL}->{HK}"]
    assert h.mock.sc.enrolled == {HK}


def test_already_enrolled_does_nothing(harness, caplog):
    h = harness(Scenario(enrolled={HEL, VN}, seats={HK: 3, VN: 0, HEL: 0, LIS: 1}))
    assert h.monitor(max_checks=5).run() == EXIT_OK
    assert mutations(h) == []
    assert h.mock.sc.enrolled == {HEL, VN}
    assert "Already enrolled in Global Experience – Vietnam" in caplog.text


def test_clash_asks_and_declines(harness):
    h = harness(Scenario(open_after={HK: 2}, clash_with={HK: HEL}))
    h.answers = ["no thanks"]
    assert h.monitor(max_checks=8).run() == EXIT_DECLINED
    assert len(h.prompts) == 1
    assert mutations(h) == [f"add:{HK}"]  # tried the add, never dropped
    assert h.mock.sc.enrolled == {HEL}


def test_clash_tries_other_target_before_asking(harness):
    h = harness(Scenario(seats={HK: 1, VN: 1, HEL: 0, LIS: 0}, clash_with={HK: HEL}))
    assert h.monitor(max_checks=4).run() == EXIT_OK
    assert h.prompts == []
    assert mutations(h) == [f"add:{HK}", f"add:{VN}", f"drop:{HEL}"]
    assert h.mock.sc.enrolled == {VN}


def test_clash_consent_drop_then_add(harness):
    h = harness(Scenario(open_after={HK: 2}, clash_with={HK: HEL}))
    h.answers = ["DROP HELSINKI"]
    assert h.monitor(max_checks=8).run() == EXIT_OK
    assert mutations(h) == [f"add:{HK}", f"drop:{HEL}", f"add:{HK}"]
    assert h.mock.sc.enrolled == {HK}


def test_add_dialog_mentioning_helsinki_is_cancelled(harness):
    h = harness(Scenario(open_after={HK: 2},
                         add_confirm_text=f"Enrolling will replace {HEL}. Continue?"))
    assert h.monitor(max_checks=8).run() == EXIT_FAILED
    assert mutations(h) == []  # cancelled at the dialog; nothing submitted
    assert h.mock.sc.enrolled == {HEL}


def test_unconfirmed_add_never_drops(harness):
    """EMS says 'enrolled' but the enrolment page disagrees -> Helsinki must stay."""
    h = harness(Scenario(seats={HK: 1, VN: 0, HEL: 0, LIS: 0}, add_fake_success=True))
    code = h.monitor(max_checks=10).run()
    assert code == EXIT_FAILED  # gives up after max_failed_add_attempts
    assert all(not a.startswith("drop:") for a in mutations(h))
    assert mutations(h).count(f"add:{HK}") == 3
    assert h.mock.sc.enrolled == {HEL}


def test_waitlisted_is_not_confirmed(harness):
    h = harness(Scenario(seats={HK: 1, VN: 0, HEL: 0, LIS: 0}, add_puts_on_waitlist=True))
    assert h.monitor(max_checks=4).run() == EXIT_FAILED
    assert mutations(h) == [f"add:{HK}"]
    assert h.mock.sc.enrolled == {HEL}


def test_rate_limit_backs_off_then_recovers(harness, caplog):
    h = harness(Scenario(rate_limit_on={2, 3}))
    m = h.monitor(max_checks=5)
    intervals = []
    orig = m.backoff.next_delay

    def spy():
        intervals.append(m.backoff.interval_s)
        return orig()

    m.backoff.next_delay = spy
    assert m.run() == EXIT_OK
    assert intervals[:4] == [0.2, 0.4, 0.8, 0.2]  # doubled twice, then reset
    assert "RATE LIMITED" in caplog.text


def test_captcha_backs_off_and_alerts(harness, caplog):
    h = harness(Scenario(captcha_on={1}))
    m = h.monitor(max_checks=3)
    assert m.run() == EXIT_OK
    assert "CAPTCHA" in caplog.text
    assert mutations(h) == []


def test_drop_guard_refuses_without_confirmed_target(harness):
    from ems_swap.swap import SafetyViolation
    h = harness(Scenario())
    m = h.monitor(max_checks=1)
    m.s.ensure_logged_in()
    with pytest.raises(SafetyViolation):
        m.swapper.drop_helsinki(confirmed_target=None, consent=False)
    with pytest.raises(SafetyViolation):  # HK is not enrolled -> re-check right before click fails
        m.swapper.drop_helsinki(confirmed_target=m.cfg.targets[0], consent=False)
    assert mutations(h) == []


def test_portal_reads_enrolments(harness):
    h = harness(Scenario(enrolled={HEL}, waitlisted={VN}))
    m = h.monitor(max_checks=1)
    m.s.ensure_logged_in()
    st = m.portal.read_enrolments()
    assert st[HEL].state == EnrolState.ENROLLED
    assert st[VN].state == EnrolState.WAITLISTED
    assert st[HK].state == EnrolState.NOT_ENROLLED


@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="needs a display (run under xvfb-run)")
def test_session_expiry_relogin_with_autofill(harness, caplog):
    """Headless run; session dies; a visible window opens, credentials are typed from
    env vars, 'MFA' completes, monitoring resumes headless and the swap completes."""
    h = harness(Scenario(open_after={HK: 4}, expire_on={2}))
    m = h.monitor(max_checks=10, headless=True)
    assert m.run() == EXIT_OK
    assert h.mock.logins == 1
    assert "EMS LOGIN NEEDED" in caplog.text
    assert mutations(h) == [f"add:{HK}", f"drop:{HEL}"]
    assert m.s.headless is True  # went back to headless after login
    assert "correct horse" not in caplog.text


@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="needs a display (run under xvfb-run)")
def test_first_run_opens_window_and_logs_in(harness):
    h = harness(Scenario())
    s = h.session(headless=True, logged_in_before=False)
    assert s.headless is False  # first run forces a visible window
    s.start()
    s.ensure_logged_in()
    assert s.is_logged_in()
    assert h.mock.logins == 1


def test_judge_prompt_rules(harness):
    h = harness(Scenario())
    m = h.monitor(max_checks=1)
    p: Portal = m.portal
    from ems_swap.portal import _DialogContext
    hk, vn, hel = m.cfg.targets[0], m.cfg.targets[1], m.cfg.drop
    add_ctx = _DialogContext(hk, [vn, hel])
    assert p.judge_prompt(f"Confirm enrolment in {HK}?", add_ctx) is None
    assert p.judge_prompt(f"This replaces {HEL}. Continue?", add_ctx)[0] == Outcome.NEEDS_HUMAN
    assert p.judge_prompt("This overlaps with another course. Continue?", add_ctx)[0] == Outcome.CLASH
    drop_ctx = _DialogContext(hel, [hk, vn])
    assert p.judge_prompt(f"Are you sure you want to drop {HEL}?", drop_ctx) is None
    assert p.judge_prompt(f"Are you sure you want to drop {HK}?", drop_ctx)[0] == Outcome.NEEDS_HUMAN
    assert isinstance(m.swapper, Swapper) and Result.SUCCESS.value == "SUCCESS"


def test_inspect_capture(harness, tmp_path):
    from ems_swap.inspect_dom import capture
    h = harness(Scenario(seats={HK: 2, VN: 0, HEL: 0, LIS: 1}))
    m = h.monitor(max_checks=1)
    m.s.ensure_logged_in()
    m.portal.load_availability()
    out = tmp_path / "captures"
    capture(m.s.page, m.s, m.portal, m.cfg, out, "courses")
    folder = next(out.iterdir())
    summary = (folder / "summary.txt").read_text()
    assert "AVAILABLE (2 seats)" in summary
    assert "add   buttons: ['Enrol']" in summary
    html = (folder / "page.html").read_text()
    assert "SECRET-RVT" not in html and "SECRET-CSRF-TOKEN" not in html
    assert (folder / "screenshot.png").exists() and (folder / "aria.yml").exists()
