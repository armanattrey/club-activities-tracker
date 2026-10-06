from app.services import checkin

SECRET = "test-secret"


def test_token_accepted_now_and_one_step_later():
    now = 1_000_000.0
    tok = checkin.make_token(SECRET, 1, checkin.current_step(now))
    assert checkin.token_valid(SECRET, 1, tok, now)
    assert checkin.token_valid(SECRET, 1, tok, now + checkin.STEP_SECONDS)


def test_token_expires_after_two_steps():
    now = 1_000_000.0
    tok = checkin.make_token(SECRET, 1, checkin.current_step(now))
    assert not checkin.token_valid(SECRET, 1, tok, now + 2 * checkin.STEP_SECONDS)


def test_token_is_bound_to_its_session():
    now = 1_000_000.0
    tok = checkin.make_token(SECRET, 1, checkin.current_step(now))
    assert not checkin.token_valid(SECRET, 2, tok, now)


def test_wrong_secret_and_garbage_rejected():
    now = 1_000_000.0
    tok = checkin.make_token(SECRET, 1, checkin.current_step(now))
    assert not checkin.token_valid("other-secret", 1, tok, now)
    assert not checkin.token_valid(SECRET, 1, "not-a-token", now)


def test_distance():
    assert checkin.distance_m(26.9, 75.8, 26.9, 75.8) == 0
    d = checkin.distance_m(26.900, 75.8, 26.901, 75.8)  # ~0.001 degree latitude
    assert 100 < d < 120