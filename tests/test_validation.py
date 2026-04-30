"""Tests for the input validation module (mailtrim.core.validation)."""

import pytest
import typer

from mailtrim.core.validation import validate_domain, validate_older_than, validate_sender_email

# ── validate_domain ───────────────────────────────────────────────────────────


class TestValidateDomain:
    def test_simple_domain(self):
        assert validate_domain("example.com") == "example.com"

    def test_subdomain(self):
        assert validate_domain("mail.example.com") == "mail.example.com"

    def test_deep_subdomain(self):
        assert validate_domain("a.b.c.example.co.uk") == "a.b.c.example.co.uk"

    def test_normalises_to_lowercase(self):
        assert validate_domain("LinkedIn.COM") == "linkedin.com"

    def test_strips_whitespace(self):
        assert validate_domain("  example.com  ") == "example.com"

    def test_hyphen_allowed_in_label(self):
        assert validate_domain("my-company.example.com") == "my-company.example.com"

    # ── rejection cases (query injection vectors) ────────────────────────────

    def test_rejects_query_injection_or(self):
        with pytest.raises(typer.BadParameter):
            validate_domain("evil.com OR from:other@bad.com")

    def test_rejects_query_injection_space(self):
        with pytest.raises(typer.BadParameter):
            validate_domain("linkedin.com -in:inbox")

    def test_rejects_bare_label_no_dot(self):
        """Single-label 'domains' are not valid FQDNs."""
        with pytest.raises(typer.BadParameter):
            validate_domain("localhost")

    def test_rejects_leading_hyphen(self):
        with pytest.raises(typer.BadParameter):
            validate_domain("-evil.com")

    def test_rejects_trailing_hyphen(self):
        with pytest.raises(typer.BadParameter):
            validate_domain("evil-.com")

    def test_rejects_path_traversal(self):
        with pytest.raises(typer.BadParameter):
            validate_domain("../../etc/passwd")

    def test_rejects_empty_string(self):
        with pytest.raises(typer.BadParameter):
            validate_domain("")

    def test_rejects_at_sign(self):
        with pytest.raises(typer.BadParameter):
            validate_domain("user@example.com")

    def test_rejects_parentheses(self):
        with pytest.raises(typer.BadParameter):
            validate_domain("(example.com)")


# ── validate_sender_email ─────────────────────────────────────────────────────


class TestValidateSenderEmail:
    def test_valid_email(self):
        assert validate_sender_email("user@example.com") == "user@example.com"

    def test_normalises_lowercase(self):
        assert validate_sender_email("User@EXAMPLE.COM") == "user@example.com"

    def test_strips_whitespace(self):
        assert validate_sender_email("  user@example.com  ") == "user@example.com"

    def test_rejects_no_at(self):
        with pytest.raises(typer.BadParameter):
            validate_sender_email("notanemail")

    def test_rejects_space_in_address(self):
        with pytest.raises(typer.BadParameter):
            validate_sender_email("user @example.com")

    def test_rejects_multiple_at(self):
        with pytest.raises(typer.BadParameter):
            validate_sender_email("user@@example.com")

    def test_rejects_injection_suffix(self):
        with pytest.raises(typer.BadParameter):
            validate_sender_email("user@evil.com OR from:other@bad.com")


# ── validate_older_than ───────────────────────────────────────────────────────


class TestValidateOlderThan:
    def test_valid_positive(self):
        assert validate_older_than(30) == 30

    def test_valid_one(self):
        assert validate_older_than(1) == 1

    def test_valid_large(self):
        assert validate_older_than(365) == 365

    def test_rejects_zero(self):
        with pytest.raises(typer.BadParameter):
            validate_older_than(0)

    def test_rejects_negative(self):
        with pytest.raises(typer.BadParameter):
            validate_older_than(-5)

    def test_rejects_over_100_years(self):
        with pytest.raises(typer.BadParameter):
            validate_older_than(36_501)

    def test_boundary_max_allowed(self):
        assert validate_older_than(36_500) == 36_500
