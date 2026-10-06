import pytest

from app.core.state_machines import CLUB_DAY, ENROLMENT, InvalidTransition, transition


def test_enrolment_pending_to_approved():
    assert transition(ENROLMENT, "pending", "approved") == "approved"


def test_enrolment_cannot_go_back():
    with pytest.raises(InvalidTransition):
        transition(ENROLMENT, "approved", "pending")


def test_club_day_happy_path():
    s = "planned"
    for nxt in ("approved", "announced", "open", "submissions", "evaluating", "closed"):
        s = transition(CLUB_DAY, s, nxt)
    assert s == "closed"


def test_club_day_cannot_skip_steps():
    with pytest.raises(InvalidTransition):
        transition(CLUB_DAY, "planned", "open")