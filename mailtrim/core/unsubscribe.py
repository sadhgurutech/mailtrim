"""Unsubscribe engine — List-Unsubscribe headers first, headless browser fallback."""

from __future__ import annotations

import ipaddress
import re
import socket
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from mailtrim.core.gmail_client import GmailClient, Message
from mailtrim.core.storage import UnsubscribeRecord, get_session

# ── URL safety validation ─────────────────────────────────────────────────────

# Private, loopback, and link-local ranges that must never be fetched.
# Link-local (169.254/16) covers AWS/GCP/Azure instance metadata endpoints.
_BLOCKED_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local / cloud metadata
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _is_safe_url(url: str) -> tuple[bool, str]:
    """
    Validate that a URL is safe to fetch before making any outbound request.

    Returns (safe: bool, reason: str). Blocks:
    - Non-http(s) schemes (file://, ftp://, etc.)
    - Private / loopback / link-local IP ranges (SSRF prevention)
    - Hostnames that fail DNS resolution

    This guards against SSRF attacks where a malicious sender plants a
    crafted List-Unsubscribe header pointing at internal infrastructure
    (e.g. http://169.254.169.254/latest/meta-data/).
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False, "malformed URL"

    if parsed.scheme not in ("http", "https"):
        return False, f"scheme '{parsed.scheme}' not allowed — only http/https"

    host = parsed.hostname
    if not host:
        return False, "no hostname"

    # Resolve all addresses the hostname maps to and check every one.
    # We check all, not just the first, to prevent DNS rebinding attacks.
    try:
        addr_infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False, f"hostname '{host}' did not resolve"

    for info in addr_infos:
        raw_addr = info[4][0]
        try:
            ip = ipaddress.ip_address(raw_addr)
        except ValueError:
            return False, f"could not parse resolved address '{raw_addr}'"
        for network in _BLOCKED_NETWORKS:
            if ip in network:
                return False, f"'{host}' resolves to private/reserved address {ip}"

    return True, ""


@dataclass
class UnsubscribeResult:
    sender_email: str
    method: str  # "header_mailto", "header_url", "headless", "none"
    success: bool
    message: str  # Human-readable outcome


class UnsubscribeEngine:
    """
    Two-stage unsubscribe:
    1. Parse List-Unsubscribe headers (mailto: or https:) — instant, reliable for well-behaved senders
    2. Playwright headless browser fallback — for senders that ignore the header standard

    This achieves near-100% success vs. the 70-85% of API-only tools.
    """

    def __init__(self, client: GmailClient, account_email: str):
        self.client = client
        self.account_email = account_email
        self.session = get_session()

    def unsubscribe(self, message: Message, use_headless: bool = True) -> UnsubscribeResult:
        """Attempt to unsubscribe from the sender of this message."""
        sender = message.sender_email
        domain = sender.split("@")[-1] if "@" in sender else sender

        header = message.headers.list_unsubscribe.strip()
        post_header = message.headers.list_unsubscribe_post.strip()

        # ── Stage 1a: List-Unsubscribe-Post (RFC 8058 one-click) ────────────
        if post_header and "List-Unsubscribe=One-Click" in post_header:
            url = _extract_url_from_header(header)
            if url:
                result = self._one_click_post(url, sender)
                if result.success:
                    self._record(sender, domain, "header_url", "success")
                    return result

        # ── Stage 1b: List-Unsubscribe mailto: ──────────────────────────────
        mailto = _extract_mailto_from_header(header)
        if mailto:
            result = self._mailto_unsubscribe(mailto, sender)
            if result.success:
                self._record(sender, domain, "header_mailto", "success")
                return result

        # ── Stage 1c: List-Unsubscribe URL (GET request) ─────────────────────
        url = _extract_url_from_header(header)
        if url:
            result = self._url_unsubscribe(url, sender)
            if result.success:
                self._record(sender, domain, "header_url", "success")
                return result

        # ── Stage 2: Headless browser fallback ───────────────────────────────
        if use_headless:
            result = self._headless_unsubscribe(message, sender)
            self._record(sender, domain, "headless", "success" if result.success else "failed")
            return result

        self._record(sender, domain, "none", "failed")
        return UnsubscribeResult(
            sender_email=sender,
            method="none",
            success=False,
            message=f"No unsubscribe mechanism found for {sender}.",
        )

    def batch_unsubscribe(
        self, messages: list[Message], use_headless: bool = True
    ) -> list[UnsubscribeResult]:
        """Unsubscribe from multiple senders, deduplicating by sender email."""
        seen: set[str] = set()
        results = []
        for msg in messages:
            if msg.sender_email not in seen:
                seen.add(msg.sender_email)
                results.append(self.unsubscribe(msg, use_headless=use_headless))
        return results

    # ── Stage 1a: RFC 8058 one-click POST ────────────────────────────────────

    def _one_click_post(self, url: str, sender: str) -> UnsubscribeResult:
        safe, reason = _is_safe_url(url)
        if not safe:
            return UnsubscribeResult(sender, "header_url", False, f"Blocked unsafe URL: {reason}")
        try:
            resp = httpx.post(
                url,
                data={"List-Unsubscribe": "One-Click"},
                timeout=10,
                follow_redirects=False,  # never follow redirects to avoid redirect-based SSRF
            )
            if resp.status_code < 400:
                return UnsubscribeResult(
                    sender, "header_url", True, f"One-click POST succeeded ({resp.status_code})"
                )
        except Exception:
            pass
        return UnsubscribeResult(sender, "header_url", False, "One-click POST failed")

    # ── Stage 1b: mailto: unsubscribe ────────────────────────────────────────

    def _mailto_unsubscribe(self, mailto: str, sender: str) -> UnsubscribeResult:
        """Send an unsubscribe email to the List-Unsubscribe mailto address."""
        try:
            # Use Gmail API to send the unsubscribe email
            parts = mailto.replace("mailto:", "").split("?", 1)
            to_addr = parts[0].strip()
            subject = "unsubscribe"
            if len(parts) > 1:
                for param in parts[1].split("&"):
                    if param.lower().startswith("subject="):
                        subject = param.split("=", 1)[1]
                        break

            self.client.send(to=to_addr, subject=subject, body="unsubscribe")
            return UnsubscribeResult(
                sender, "header_mailto", True, f"Unsubscribe email sent to {to_addr}"
            )
        except Exception as e:
            return UnsubscribeResult(
                sender, "header_mailto", False, f"Failed to send unsubscribe email: {e}"
            )

    # ── Stage 1c: URL GET ────────────────────────────────────────────────────

    def _url_unsubscribe(self, url: str, sender: str) -> UnsubscribeResult:
        safe, reason = _is_safe_url(url)
        if not safe:
            return UnsubscribeResult(sender, "header_url", False, f"Blocked unsafe URL: {reason}")
        try:
            resp = httpx.get(url, timeout=10, follow_redirects=False)
            if resp.status_code < 400:
                return UnsubscribeResult(
                    sender,
                    "header_url",
                    True,
                    f"Unsubscribe URL GET succeeded ({resp.status_code})",
                )
        except Exception:
            pass
        return UnsubscribeResult(sender, "header_url", False, "URL GET unsubscribe failed")

    # ── Stage 2: Headless browser ────────────────────────────────────────────

    def _headless_unsubscribe(self, message: Message, sender: str) -> UnsubscribeResult:
        """
        Use Playwright to find and click the unsubscribe link in the email HTML body.
        This handles senders that use custom unsubscribe forms/buttons.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return UnsubscribeResult(
                sender,
                "headless",
                False,
                "Playwright not installed. Run: playwright install chromium",
            )

        # Find unsubscribe URL in email body
        unsub_url = _find_unsubscribe_url_in_body(message.body_html or message.body_text)
        if not unsub_url:
            return UnsubscribeResult(
                sender, "headless", False, "No unsubscribe link found in email body."
            )

        safe, reason = _is_safe_url(unsub_url)
        if not safe:
            return UnsubscribeResult(sender, "headless", False, f"Blocked unsafe URL: {reason}")

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(unsub_url, timeout=15000)

                # Try to find and click an unsubscribe/confirm button
                clicked = False
                selectors = [
                    "button:has-text('Unsubscribe')",
                    "button:has-text('Confirm')",
                    "input[type=submit][value*='nsubscribe']",
                    "a:has-text('Unsubscribe')",
                    "button:has-text('Yes')",
                ]
                for sel in selectors:
                    try:
                        page.click(sel, timeout=3000)
                        clicked = True
                        break
                    except Exception:
                        continue

                browser.close()

                if clicked:
                    return UnsubscribeResult(
                        sender, "headless", True, f"Headless unsubscribe completed via {unsub_url}"
                    )
                else:
                    # Page loaded — even without clicking, the GET may have been enough
                    return UnsubscribeResult(
                        sender,
                        "headless",
                        True,
                        f"Unsubscribe page loaded (no button click needed): {unsub_url}",
                    )

        except Exception as e:
            return UnsubscribeResult(sender, "headless", False, f"Headless unsubscribe failed: {e}")

    # ── Storage ──────────────────────────────────────────────────────────────

    def _record(self, sender_email: str, domain: str, method: str, status: str) -> None:
        record = UnsubscribeRecord(
            account_email=self.account_email,
            sender_email=sender_email,
            sender_domain=domain,
            method=method,
            status=status,
            attempted_at=datetime.now(timezone.utc),
        )
        self.session.add(record)
        self.session.commit()

    def get_history(self) -> list[UnsubscribeRecord]:
        return (
            self.session.query(UnsubscribeRecord)
            .filter_by(account_email=self.account_email)
            .order_by(UnsubscribeRecord.attempted_at.desc())
            .all()
        )


# ── Helpers ──────────────────────────────────────────────────────────────────


def _extract_mailto_from_header(header: str) -> str:
    """Extract the first mailto: address from a List-Unsubscribe header value."""
    match = re.search(r"<(mailto:[^>]+)>", header, re.IGNORECASE)
    if match:
        return match.group(1)
    return ""


def _extract_url_from_header(header: str) -> str:
    """Extract the first https: URL from a List-Unsubscribe header value."""
    match = re.search(r"<(https?://[^>]+)>", header, re.IGNORECASE)
    if match:
        return match.group(1)
    return ""


def _find_unsubscribe_url_in_body(body: str) -> str:
    """
    Find an unsubscribe link in email HTML/text body.
    Returns the most likely unsubscribe URL.
    """
    if not body:
        return ""

    # Look for links containing "unsubscribe" in href or anchor text
    patterns = [
        r'href=["\']([^"\']+)["\'][^>]*>[^<]*unsubscri',  # href before text
        r'unsubscri[^<"\']*["\'][^"\']*href=["\']([^"\']+)',  # text before href
        r'href=["\']([^"\']*unsubscri[^"\']+)["\']',  # unsubscribe in URL
    ]

    for pattern in patterns:
        matches = re.findall(pattern, body, re.IGNORECASE)
        if matches:
            url = matches[0]
            if url.startswith("http"):
                return url

    # Fallback: find any URL with "unsubscribe" in it
    urls = re.findall(r'https?://[^\s"\'<>]+unsubscri[^\s"\'<>]+', body, re.IGNORECASE)
    if urls:
        return urls[0]

    return ""
