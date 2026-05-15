from spec_to_pr.models import CircuitBreaker, TripReason


def test_breaker_not_tripped_initially():
    cb = CircuitBreaker(max_attempts=3)
    assert not cb.tripped
    assert cb.trip_reason is None
    assert cb.attempt_count == 0


def test_breaker_trips_on_max_attempts():
    cb = CircuitBreaker(max_attempts=3)
    cb.record_attempt(error_fingerprint="abc", progress_score=0.5)
    cb.record_attempt(error_fingerprint="def", progress_score=0.3)
    cb.record_attempt(error_fingerprint="ghi", progress_score=0.1)
    assert cb.tripped
    assert cb.trip_reason == TripReason.MAX_ATTEMPTS_REACHED


def test_breaker_trips_on_repeated_error():
    cb = CircuitBreaker(max_attempts=5)
    cb.record_attempt(error_fingerprint="abc", progress_score=0.5)
    cb.record_attempt(error_fingerprint="abc", progress_score=0.4)
    assert cb.tripped
    assert cb.trip_reason == TripReason.REPEATED_ERROR


def test_breaker_trips_on_no_progress():
    cb = CircuitBreaker(max_attempts=5)
    cb.record_attempt(error_fingerprint="abc", progress_score=0.30)
    cb.record_attempt(error_fingerprint="def", progress_score=0.30)
    assert cb.tripped
    assert cb.trip_reason == TripReason.NO_PROGRESS


def test_breaker_allows_retry_with_progress():
    cb = CircuitBreaker(max_attempts=3)
    cb.record_attempt(error_fingerprint="abc", progress_score=0.3)
    assert not cb.tripped


def test_breaker_different_errors_with_progress_no_trip():
    cb = CircuitBreaker(max_attempts=5)
    cb.record_attempt(error_fingerprint="aaa", progress_score=0.2)
    cb.record_attempt(error_fingerprint="bbb", progress_score=0.5)
    assert not cb.tripped


def test_breaker_no_further_evaluation_after_trip():
    cb = CircuitBreaker(max_attempts=3)
    cb.record_attempt("x", 0.5)
    cb.record_attempt("x", 0.5)
    assert cb.tripped
    assert cb.trip_reason == TripReason.REPEATED_ERROR
    # Further calls should not change trip_reason
    cb.record_attempt("x", 0.5)
    assert cb.trip_reason == TripReason.REPEATED_ERROR


def test_breaker_attempt_count():
    cb = CircuitBreaker(max_attempts=5)
    cb.record_attempt("a", 0.1)
    cb.record_attempt("b", 0.5)
    assert cb.attempt_count == 2
