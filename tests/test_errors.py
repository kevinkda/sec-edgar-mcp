"""Unit tests for sec_edgar_mcp.errors."""

from __future__ import annotations

import pytest

from sec_edgar_mcp.errors import (
    Form4ParseError,
    SecConfigurationError,
    SecError,
    SecNotFoundError,
    SecRateLimitError,
    SecTransientError,
    SecValidationError,
    ThirteenFParseError,
    redact_email,
)


class TestRedactEmail:
    def test_redacts_simple_email(self) -> None:
        out = redact_email("contact me at user@example.com please")
        assert "user@example.com" not in out
        assert "REDACTED" in out

    def test_redacts_multiple_emails(self) -> None:
        out = redact_email("a@x.com and b@y.com")
        assert "a@x.com" not in out
        assert "b@y.com" not in out

    def test_idempotent(self) -> None:
        once = redact_email("user@example.com")
        twice = redact_email(once)
        assert once == twice

    def test_no_email_unchanged(self) -> None:
        text = "no addresses here"
        assert redact_email(text) == text


class TestSecValidationError:
    def test_round_trip(self) -> None:
        err = SecValidationError(field="cik", reason="bad input from user@example.com")
        assert err.field == "cik"
        assert "user@example.com" not in str(err)
        assert "REDACTED" in err.reason

    def test_field_must_be_str(self) -> None:
        with pytest.raises(TypeError):
            SecValidationError(field=123, reason="x")  # type: ignore[arg-type]

    def test_reason_must_be_str(self) -> None:
        with pytest.raises(TypeError):
            SecValidationError(field="x", reason=123)  # type: ignore[arg-type]


class TestSecNotFoundError:
    def test_round_trip(self) -> None:
        err = SecNotFoundError(resource="cik:0000320193", hint="no such filing")
        assert err.resource == "cik:0000320193"
        assert "no such filing" in str(err)

    def test_resource_must_be_str(self) -> None:
        with pytest.raises(TypeError):
            SecNotFoundError(resource=1, hint="x")  # type: ignore[arg-type]

    def test_hint_must_be_str(self) -> None:
        with pytest.raises(TypeError):
            SecNotFoundError(resource="x", hint=1)  # type: ignore[arg-type]


class TestSecRateLimitError:
    def test_round_trip(self) -> None:
        err = SecRateLimitError(retry_after_seconds=5, current_window_used=10)
        assert err.retry_after_seconds == 5
        assert "5s" in str(err)

    def test_int_required(self) -> None:
        with pytest.raises(TypeError):
            SecRateLimitError(retry_after_seconds="5", current_window_used=10)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            SecRateLimitError(retry_after_seconds=5, current_window_used="10")  # type: ignore[arg-type]


class TestSecTransientError:
    def test_round_trip(self) -> None:
        err = SecTransientError(status_code=503, attempt=2, hint="upstream busy")
        assert err.status_code == 503
        assert err.attempt == 2

    def test_types(self) -> None:
        with pytest.raises(TypeError):
            SecTransientError(status_code="503", attempt=1, hint="x")  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            SecTransientError(status_code=503, attempt="1", hint="x")  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            SecTransientError(status_code=503, attempt=1, hint=1)  # type: ignore[arg-type]


class TestSecConfigurationError:
    def test_round_trip(self) -> None:
        err = SecConfigurationError(hint="set SEC_EDGAR_USER_AGENT in .env")
        assert "SEC_EDGAR_USER_AGENT" in str(err)

    def test_hint_redacts_email(self) -> None:
        err = SecConfigurationError(hint="contact user@example.com for help")
        assert "user@example.com" not in str(err)

    def test_hint_must_be_str(self) -> None:
        with pytest.raises(TypeError):
            SecConfigurationError(hint=123)  # type: ignore[arg-type]


def test_secerror_is_exception_subclass() -> None:
    assert issubclass(SecError, Exception)
    assert issubclass(SecValidationError, SecError)
    assert issubclass(SecNotFoundError, SecError)
    assert issubclass(SecRateLimitError, SecError)
    assert issubclass(SecTransientError, SecError)
    assert issubclass(SecConfigurationError, SecError)
    assert issubclass(Form4ParseError, SecError)


class TestForm4ParseError:
    def test_round_trip(self) -> None:
        err = Form4ParseError(
            accession_number="0000320193-24-000010",
            reason="malformed XML at line 7",
        )
        assert err.accession_number == "0000320193-24-000010"
        assert "malformed XML" in str(err)
        assert "0000320193-24-000010" in str(err)

    def test_reason_redacts_email(self) -> None:
        err = Form4ParseError(
            accession_number="x",
            reason="contact ops@example.com to debug",
        )
        assert "ops@example.com" not in str(err)
        assert "REDACTED" in err.reason

    def test_accession_number_must_be_str(self) -> None:
        with pytest.raises(TypeError):
            Form4ParseError(accession_number=123, reason="x")  # type: ignore[arg-type]

    def test_reason_must_be_str(self) -> None:
        with pytest.raises(TypeError):
            Form4ParseError(accession_number="x", reason=123)  # type: ignore[arg-type]


class TestThirteenFParseError:
    def test_round_trip(self) -> None:
        err = ThirteenFParseError(
            accession_number="0001067983-24-000020",
            reason="expected root <informationTable>, got <foo>",
        )
        assert err.accession_number == "0001067983-24-000020"
        assert "informationTable" in str(err)
        assert "0001067983-24-000020" in str(err)

    def test_reason_redacts_email(self) -> None:
        err = ThirteenFParseError(
            accession_number="x",
            reason="contact ops@example.com to debug",
        )
        assert "ops@example.com" not in str(err)
        assert "REDACTED" in err.reason

    def test_accession_number_must_be_str(self) -> None:
        with pytest.raises(TypeError):
            ThirteenFParseError(accession_number=123, reason="x")  # type: ignore[arg-type]

    def test_reason_must_be_str(self) -> None:
        with pytest.raises(TypeError):
            ThirteenFParseError(accession_number="x", reason=123)  # type: ignore[arg-type]

    def test_is_secerror_subclass(self) -> None:
        assert issubclass(ThirteenFParseError, SecError)
