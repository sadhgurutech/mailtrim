"""Tests for SSRF protection in the unsubscribe URL validator."""


def test_safe_public_https_url(monkeypatch):
    import socket

    from mailtrim.core.unsubscribe import _is_safe_url

    # Stub DNS: resolve to a known public IP (93.184.216.34 = example.com)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **kw: [(None, None, None, None, ("93.184.216.34", 0))],
    )
    safe, reason = _is_safe_url("https://unsubscribe.example.com/opt-out?token=abc")
    assert safe is True
    assert reason == ""


def test_safe_public_http_url(monkeypatch):
    import socket

    from mailtrim.core.unsubscribe import _is_safe_url

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **kw: [(None, None, None, None, ("93.184.216.34", 0))],
    )
    safe, reason = _is_safe_url("http://unsubscribe.example.com/remove")
    assert safe is True


def test_blocks_aws_metadata_endpoint():
    from mailtrim.core.unsubscribe import _is_safe_url

    safe, reason = _is_safe_url("http://169.254.169.254/latest/meta-data/")
    assert safe is False
    assert "private" in reason or "reserved" in reason


def test_blocks_loopback():
    from mailtrim.core.unsubscribe import _is_safe_url

    safe, reason = _is_safe_url("http://127.0.0.1/admin")
    assert safe is False


def test_blocks_loopback_localhost():
    from mailtrim.core.unsubscribe import _is_safe_url

    safe, reason = _is_safe_url("http://localhost/admin")
    assert safe is False


def test_blocks_rfc1918_10_range():
    from mailtrim.core.unsubscribe import _is_safe_url

    safe, reason = _is_safe_url("http://10.0.0.1/internal")
    assert safe is False


def test_blocks_rfc1918_192168_range():
    from mailtrim.core.unsubscribe import _is_safe_url

    safe, reason = _is_safe_url("http://192.168.1.100/service")
    assert safe is False


def test_blocks_rfc1918_172_range():
    from mailtrim.core.unsubscribe import _is_safe_url

    safe, reason = _is_safe_url("http://172.16.0.1/internal")
    assert safe is False


def test_blocks_non_http_scheme_file():
    from mailtrim.core.unsubscribe import _is_safe_url

    safe, reason = _is_safe_url("file:///etc/passwd")
    assert safe is False
    assert "scheme" in reason


def test_blocks_non_http_scheme_ftp():
    from mailtrim.core.unsubscribe import _is_safe_url

    safe, reason = _is_safe_url("ftp://example.com/file")
    assert safe is False
    assert "scheme" in reason


def test_blocks_missing_hostname():
    from mailtrim.core.unsubscribe import _is_safe_url

    safe, reason = _is_safe_url("http:///path/only")
    assert safe is False


def test_blocks_unresolvable_hostname():
    from mailtrim.core.unsubscribe import _is_safe_url

    safe, reason = _is_safe_url("https://this-domain-does-not-exist.invalid/unsub")
    assert safe is False
    assert "resolve" in reason


def test_blocks_malformed_url():
    from mailtrim.core.unsubscribe import _is_safe_url

    safe, reason = _is_safe_url("not a url at all !!!")
    assert safe is False


def test_one_click_post_blocked_for_private_url(monkeypatch):
    """_one_click_post must not make any HTTP call when URL is unsafe."""
    import httpx

    from mailtrim.core.unsubscribe import UnsubscribeEngine

    calls = []
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: calls.append((a, kw)))

    engine = UnsubscribeEngine.__new__(UnsubscribeEngine)
    result = engine._one_click_post("http://169.254.169.254/latest/", "bad@evil.com")

    assert result.success is False
    assert "Blocked" in result.message
    assert calls == []  # httpx.post must never have been called


def test_url_unsubscribe_blocked_for_loopback(monkeypatch):
    """_url_unsubscribe must not make any HTTP call when URL is unsafe."""
    import httpx

    from mailtrim.core.unsubscribe import UnsubscribeEngine

    calls = []
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: calls.append((a, kw)))

    engine = UnsubscribeEngine.__new__(UnsubscribeEngine)
    result = engine._url_unsubscribe("http://127.0.0.1:8080/admin", "bad@evil.com")

    assert result.success is False
    assert "Blocked" in result.message
    assert calls == []
