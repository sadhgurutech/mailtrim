"""mailtrim CLI — Rich terminal UI for world-class inbox management."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from mailtrim import __version__
from mailtrim.config import CREDENTIALS_PATH, DATA_DIR, get_settings

app = typer.Typer(
    name="mailtrim",
    help="Privacy-first, AI-powered Gmail inbox management.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
console = Console()


# ── Lazy imports to keep startup fast ────────────────────────────────────────


def _get_client():
    from mailtrim.core.gmail_client import GmailClient, authenticate

    creds = authenticate()
    return GmailClient(creds)


def _get_provider(
    provider: str = "gmail",
    imap_server: str = "",
    imap_user: str = "",
    imap_password: str = "",
    imap_port: int = 993,
    imap_folder: str = "INBOX",
):
    """Construct the EmailProvider selected by --provider."""
    from mailtrim.core.providers.factory import get_provider

    return get_provider(
        provider=provider,
        imap_server=imap_server,
        imap_user=imap_user,
        imap_password=imap_password,
        imap_port=imap_port,
        imap_folder=imap_folder,
    )


def _get_ai_client_opt(backend: str, url: str, model: str):
    """Return an AIClient override, or None to use the llm.py default."""
    if backend == "llama" and not url:
        return None  # default client already configured in llm.py
    from mailtrim.core.ai.client import get_ai_client

    return get_ai_client(backend=backend, url=url, model=model)


def _get_ai():
    from mailtrim.core.mock_ai import get_ai_engine

    return get_ai_engine()


def _print_ai_data_notice(what: str) -> None:
    """Print a visible, per-command disclosure of what data is sent to Anthropic."""
    console.print(
        f"[bold yellow][AI][/bold yellow] Sending to Anthropic: {what}. "
        "[dim]No full email bodies. Details: PRIVACY.md[/dim]"
    )


def _cloud_ai_warning() -> None:
    """Print a prominent warning before any cloud AI call that may send email data."""
    from rich.panel import Panel

    console.print(
        Panel(
            "[bold yellow]⚠  Cloud AI is enabled[/bold yellow]\n"
            "[dim]This command will send email subjects and/or snippets to Anthropic's servers.\n"
            "No full email bodies are transmitted. See PRIVACY.md for details.\n\n"
            "To disable:  [cyan]mailtrim config ai-mode off[/cyan][/dim]",
            border_style="yellow",
            padding=(0, 2),
        )
    )


def _get_account_email(client) -> str:
    return client.get_email_address()


def _action_explanation(label: str, domain: str) -> str:
    """Return a one-line plain-English explanation of what an action command does."""
    lbl = label.lower()
    if "review manually" in lbl:
        return f"Previews what would be moved from {domain} — nothing is changed"
    if "older than 90" in lbl:
        return f"Moves emails older than 90 days from {domain} to Trash (recoverable)"
    if "older than 30" in lbl:
        return f"Moves emails older than 30 days from {domain} to Trash (recoverable)"
    if "keep last" in lbl or "keep latest" in lbl:
        parts = label.split()
        n = parts[-1] if parts else "5"
        return f"Moves all but the {n} most recent emails from {domain} to Trash (recoverable)"
    if "mark" in lbl and "read" in lbl:
        return f"Marks all emails from {domain} as read"
    if "delete all" in lbl:
        return f"Moves all emails from {domain} to Trash (recoverable)"
    return ""


def _is_first_stats_run() -> bool:
    """Returns True (and marks seen) the very first time stats completes successfully."""
    sentinel = DATA_DIR / ".stats_seen"
    if sentinel.exists():
        return False
    try:
        sentinel.touch()
    except OSError:
        pass
    return True


def _handle_error(exc: Exception, verbose: bool = False) -> None:
    """Translate an exception to a friendly message and exit."""
    from mailtrim.core.ai.mode import AIModeError
    from mailtrim.core.errors import friendly_error

    if isinstance(exc, AIModeError):
        lines = str(exc).strip().splitlines()
        console.print(f"\n[bold yellow]AI blocked:[/bold yellow] {lines[0]}")
        for line in lines[1:]:
            console.print(f"  [dim]{line}[/dim]")
        raise typer.Exit(1)

    human_msg, fix_hint = friendly_error(exc)
    console.print(f"\n[red]Error:[/red] {human_msg}")
    if fix_hint:
        console.print(f"  [cyan]{fix_hint}[/cyan]")
    if verbose:
        console.print(f"\n[dim]Details: {exc}[/dim]")
    else:
        console.print("[dim]  Add --verbose for technical details.[/dim]")
    raise typer.Exit(1)


def _record(command: str) -> None:
    """Record command run in local usage stats (best-effort, never raises)."""
    try:
        from mailtrim.core.usage_stats import record_run

        record_run(command)
    except Exception:
        pass


def _require_gmail(command_name: str) -> None:
    """
    Exit with a clear message if the user configured an IMAP provider during setup.

    IMAP users would otherwise hit the Gmail OAuth flow unexpectedly.
    This guard fires only when the persisted provider is "imap" — Gmail users
    are never affected.
    """
    try:
        if get_settings().provider == "imap":
            console.print(
                f"\n[yellow]'{command_name}' currently requires Gmail.[/yellow]\n"
                "  This command uses Gmail-specific features (labels, OAuth) "
                "not yet available over IMAP.\n\n"
                "  Available for all providers:\n"
                "    [cyan]mailtrim stats[/cyan]   — inbox analysis\n"
                "    [cyan]mailtrim purge[/cyan]   — clean by sender or domain\n"
                "    [cyan]mailtrim undo[/cyan]    — reverse a purge\n\n"
                "  [dim]IMAP support for this command is planned. "
                "Follow progress at github.com/sadhgurutech/mailtrim[/dim]"
            )
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception:
        pass  # settings unreadable — let the command proceed and fail naturally


def _resolve_imap_settings(
    provider: str = "",
    imap_server: str = "",
    imap_user: str = "",
    imap_port: int = 993,
    imap_folder: str = "INBOX",
) -> tuple[str, str, str, int, str]:
    """
    Merge CLI flag values with persisted settings from ~/.mailtrim/.env.

    CLI values take precedence when they differ from the hardcoded defaults.
    Falls back to persisted settings so commands work with zero flags after setup.

    Returns (provider, imap_server, imap_user, imap_port, imap_folder).
    """
    try:
        s = get_settings()
    except Exception:
        s = None

    resolved_provider = provider or (s.provider if s else "gmail")
    resolved_server = imap_server or (s.imap_server if s else "")
    resolved_user = imap_user or (s.imap_user if s else "")
    # For port/folder, treat CLI defaults (993/"INBOX") as "not specified" so
    # settings values configured during setup take precedence.
    resolved_port = imap_port if (imap_port != 993 or not s or not s.imap_port) else s.imap_port
    resolved_folder = (
        imap_folder if (imap_folder != "INBOX" or not s or not s.imap_folder) else s.imap_folder
    )

    return resolved_provider, resolved_server, resolved_user, resolved_port, resolved_folder


# ── setup ────────────────────────────────────────────────────────────────────


@app.command()
def setup():
    """
    Interactive first-time setup — connect your account and run your first inbox scan.

    Guides you through provider selection, authentication, health checks, and
    surfaces your first safe cleanup in ~1–2 minutes.
    """
    _record("setup")

    from mailtrim.core.diagnostics import run_all
    from mailtrim.core.sender_stats import (
        best_next_step,
        classify_sender_risk,
        compute_confidence_score,
        fetch_sender_groups,
        generate_recommendations,
        group_by_domain,
        reclaimable_mb,
    )

    # ── Welcome ───────────────────────────────────────────────────────────────
    console.print()
    console.print(
        Panel.fit(
            "[bold cyan]Welcome to mailtrim[/bold cyan]  ·  This takes [bold]~1–2 minutes[/bold].\n"
            "[dim]Nothing is deleted without your explicit command.[/dim]",
            border_style="cyan",
        )
    )
    console.print()

    # ── Step 1: Provider selection ────────────────────────────────────────────
    console.print("[bold]Step 1 of 3[/bold]  ·  Choose your email provider")
    console.print()
    console.print("  [bold]G[/bold]  Gmail via OAuth  [dim](recommended — opens browser)[/dim]")
    console.print("  [bold]I[/bold]  IMAP  [dim](Outlook, Yahoo, custom server)[/dim]")
    console.print()

    provider_choice = Prompt.ask(
        "  Provider", choices=["G", "g", "I", "i"], default="G", show_choices=False
    ).upper()
    console.print()

    # ── Step 2: Authentication ────────────────────────────────────────────────
    console.print("[bold]Step 2 of 3[/bold]  ·  Connect your account")
    console.print()

    _client = None  # set on success; used for the quickstart scan

    if provider_choice == "G":
        # Gmail OAuth path
        if not CREDENTIALS_PATH.exists():
            console.print(
                "  [yellow]⚠  credentials.json not found[/yellow] at "
                f"[dim]{CREDENTIALS_PATH}[/dim]\n"
            )
            console.print("  To get it:")
            console.print("    1. Go to [cyan]https://console.cloud.google.com[/cyan]")
            console.print("    2. Create a project  →  Enable the Gmail API")
            console.print("    3. Create OAuth 2.0 credentials  (Desktop app type)")
            console.print(f"    4. Download and save as [cyan]{CREDENTIALS_PATH}[/cyan]")
            console.print()
            console.print("  [dim]Then re-run:[/dim]  [bold cyan]mailtrim setup[/bold cyan]")
            console.print()
            raise typer.Exit(1)

        console.print("  [dim]Opening browser for Google OAuth consent…[/dim]")
        try:
            from mailtrim.core.gmail_client import GmailClient, authenticate

            creds = authenticate(credentials_path=CREDENTIALS_PATH)
            _client = GmailClient(creds)
            email = _client.get_email_address()
            console.print(f"  [green]✓[/green]  Authenticated as [bold]{email}[/bold]")
            # Persist Gmail as the active provider, clearing any stale IMAP settings.
            # Without this a previous IMAP setup would leave MAILTRIM_IMAP_USER in
            # .env and every subsequent command would prompt for an IMAP password.
            _env_path = DATA_DIR / ".env"
            try:
                _env_lines = _env_path.read_text().splitlines() if _env_path.exists() else []
                _imap_prefixes = {
                    "MAILTRIM_PROVIDER=",
                    "MAILTRIM_IMAP_SERVER=",
                    "MAILTRIM_IMAP_USER=",
                    "MAILTRIM_IMAP_PORT=",
                    "MAILTRIM_IMAP_FOLDER=",
                }
                _env_lines = [
                    ln for ln in _env_lines if not any(ln.startswith(p) for p in _imap_prefixes)
                ]
                _env_lines.append("MAILTRIM_PROVIDER=gmail")
                _env_path.write_text("\n".join(_env_lines) + "\n")
            except OSError:
                pass
        except Exception as exc:
            console.print(f"  [red]✗  Authentication failed:[/red] {str(exc)[:100]}")
            console.print()
            console.print(
                "  Check that your credentials.json is valid, then retry:\n"
                "  [cyan]mailtrim auth[/cyan]  →  [cyan]mailtrim setup[/cyan]"
            )
            console.print()
            raise typer.Exit(1)

    else:
        # IMAP path
        console.print("  You'll need: server hostname, username, and password.")
        console.print()
        imap_server = Prompt.ask("  IMAP server", default="imap.gmail.com")
        imap_user = Prompt.ask("  Username / email")
        imap_password = Prompt.ask("  Password", password=True)
        imap_port_str = Prompt.ask("  Port", default="993")
        try:
            imap_port = int(imap_port_str)
        except ValueError:
            imap_port = 993

        console.print()
        console.print("  [dim]Testing IMAP connection…[/dim]")
        try:
            from mailtrim.core.providers.factory import get_provider

            _client = get_provider(
                provider="imap",
                imap_server=imap_server,
                imap_user=imap_user,
                imap_password=imap_password,
                imap_port=imap_port,
            )
            _client.get_profile()  # verifies connectivity
            console.print(
                f"  [green]✓[/green]  Connected to [bold]{imap_server}[/bold] as [bold]{imap_user}[/bold]"
            )
            # Persist all IMAP settings so future commands work with zero flags
            _env_path = DATA_DIR / ".env"
            try:
                _env_lines = _env_path.read_text().splitlines() if _env_path.exists() else []
                _prefixes_to_remove = {
                    "MAILTRIM_PROVIDER=",
                    "MAILTRIM_IMAP_SERVER=",
                    "MAILTRIM_IMAP_USER=",
                    "MAILTRIM_IMAP_PORT=",
                    "MAILTRIM_IMAP_FOLDER=",
                }
                _env_lines = [
                    ln
                    for ln in _env_lines
                    if not any(ln.startswith(p) for p in _prefixes_to_remove)
                ]
                _env_lines.extend(
                    [
                        "MAILTRIM_PROVIDER=imap",
                        f"MAILTRIM_IMAP_SERVER={imap_server}",
                        f"MAILTRIM_IMAP_USER={imap_user}",
                        f"MAILTRIM_IMAP_PORT={imap_port}",
                        "MAILTRIM_IMAP_FOLDER=INBOX",
                    ]
                )
                _env_path.write_text("\n".join(_env_lines) + "\n")
            except OSError:
                pass  # non-fatal — commands still accept explicit flags
        except Exception as exc:
            console.print(f"  [red]✗  IMAP connection failed:[/red] {str(exc)[:100]}")
            console.print()
            console.print(
                "  Double-check your server, username, and password, then retry:\n"
                "  [cyan]mailtrim setup[/cyan]"
            )
            console.print()
            raise typer.Exit(1)

    console.print()

    # ── Step 3: Health check (inline doctor — required checks only) ────────────
    console.print("[bold]Step 3 of 3[/bold]  ·  System check")
    console.print()

    # For IMAP setups skip Gmail-specific checks (token/connection checks need OAuth)
    if provider_choice == "I":
        from mailtrim.core.diagnostics import (
            check_config,
            check_data_dir,
            check_dependencies,
            check_undo_storage,
        )

        check_fns = [check_dependencies, check_config, check_data_dir, check_undo_storage]
        results = []
        for fn in check_fns:
            try:
                results.append(fn())
            except Exception as exc:
                from mailtrim.core.diagnostics import CheckResult

                results.append(CheckResult(fn.__name__, ok=False, message=str(exc)))
    else:
        results = run_all(include_optional=False)

    failed = []
    for r in results:
        icon = "[green]✓[/green]" if r.ok else "[red]✗[/red]"
        console.print(f"  {icon}  {r.name}")
        if not r.ok:
            console.print(f"     [dim]{r.message}[/dim]")
            if r.fix:
                console.print(f"     [cyan]→ {r.fix}[/cyan]")
            failed.append(r)

    console.print()

    if failed:
        console.print(
            f"  [red]{len(failed)} check(s) failed.[/red]  Fix the issues above, then re-run:"
        )
        console.print("  [cyan]mailtrim setup[/cyan]")
        console.print()
        raise typer.Exit(1)

    console.print("  [green]All checks passed.[/green]  Scanning your inbox…")
    console.print()

    # ── Quickstart scan (inline — reuses same logic as quickstart command) ─────
    try:
        groups = fetch_sender_groups(
            _client,
            query="in:inbox",
            max_messages=500,
            min_count=2,
            top_n=50,
            sort_by="score",
        )
        domain_groups = group_by_domain(groups)
        domain_map_lookup = {d.domain: d for d in domain_groups}
        recommendations = generate_recommendations(groups, top_n=10, domain_map=domain_map_lookup)
        bns = best_next_step(recommendations)
        total_reclaimable = reclaimable_mb(recommendations)
    except Exception as exc:
        console.print(f"  [yellow]⚠  Scan failed:[/yellow] {str(exc)[:80]}")
        console.print(
            "\n  You're still set up correctly — try manually:\n  [cyan]mailtrim quickstart[/cyan]"
        )
        console.print()
        return  # not a fatal error — auth + doctor passed

    total_emails = sum(g.count for g in groups)
    safe_count = len(
        [
            r
            for r in recommendations
            if classify_sender_risk(r.sender) != "sensitive"
            and compute_confidence_score(r.sender) >= 50
        ]
    )

    console.print(
        f"  Scanned [bold]{total_emails:,}[/bold] emails  ·  "
        f"[bold]{safe_count}[/bold] safe senders to clean"
        + (
            f"  ·  [green]~{total_reclaimable} MB[/green] reclaimable"
            if total_reclaimable > 0
            else ""
        )
    )

    if (
        bns
        and bns.actions
        and classify_sender_risk(bns.sender) != "sensitive"
        and compute_confidence_score(bns.sender) >= 50
    ):
        g = bns.sender
        action = bns.actions[0]
        d = domain_map_lookup.get(g.domain)
        email_count = d.count if d else g.count
        size_mb = (d.total_size_mb if d else g.total_size_mb) if action.savings_mb > 0 else 0
        size_str = f"  ·  {size_mb} MB" if size_mb > 0 else ""
        console.print()
        console.print(
            f"  [bold]Best first action[/bold]  "
            f"[dim]{g.display_name[:45]} — {email_count:,} emails{size_str}[/dim]"
        )
        console.print(f"    [bold cyan]{action.command}[/bold cyan]")
    elif not recommendations:
        console.print("  [green]Inbox looks clean![/green]  Nothing obvious to remove right now.")

    console.print()
    console.print("  [dim]Undo anytime:[/dim]  mailtrim undo")

    # ── Done ──────────────────────────────────────────────────────────────────
    console.print()
    console.print(
        Panel(
            "[green]You're all set![/green]\n\n"
            "  [cyan]mailtrim quickstart[/cyan]   — guided first cleanup\n"
            "  [cyan]mailtrim stats[/cyan]         — full inbox analysis\n"
            "  [cyan]mailtrim doctor[/cyan]        — re-run health checks anytime",
            title="[bold green]Setup complete",
            border_style="green",
            padding=(0, 2),
        )
    )
    console.print()


# ── auth ─────────────────────────────────────────────────────────────────────


@app.command()
def auth(
    credentials: Path = typer.Option(
        CREDENTIALS_PATH,
        "--credentials",
        "-c",
        help="Path to OAuth credentials JSON downloaded from Google Cloud Console.",
    ),
):
    """
    Authenticate with Gmail (opens browser for OAuth consent).

    Examples:
      mailtrim auth
      mailtrim auth --credentials ~/Downloads/client_secret.json
    """
    from mailtrim.core.gmail_client import authenticate

    console.print(
        Panel.fit(
            "[bold]MailTrim — Authentication[/bold]\n\n"
            "You'll be redirected to Google to grant access.\n"
            "Your token is stored locally at [cyan]~/.mailtrim/token.json[/cyan].\n"
            "[dim]Nothing is stored on external servers.[/dim]",
            border_style="blue",
        )
    )

    if not credentials.exists():
        console.print(f"[red]Credentials file not found:[/red] {credentials}")
        console.print(
            "\n[yellow]To get credentials:[/yellow]\n"
            "1. Go to [link]https://console.cloud.google.com[/link]\n"
            "2. Create a project → Enable Gmail API\n"
            "3. Create OAuth 2.0 credentials (Desktop app)\n"
            "4. Download and save as [cyan]~/.mailtrim/credentials.json[/cyan]"
        )
        raise typer.Exit(1)

    with console.status("Opening browser for OAuth consent..."):
        creds = authenticate(credentials_path=credentials)
        client = __import__("mailtrim.core.gmail_client", fromlist=["GmailClient"]).GmailClient(
            creds
        )
        email = client.get_email_address()

    console.print(f"\n[green]Authenticated as:[/green] [bold]{email}[/bold]")
    console.print("[dim]Token saved to ~/.mailtrim/token.json[/dim]")


# ── stats ────────────────────────────────────────────────────────────────────


@app.command()
def stats(
    sort_by: str = typer.Option(
        "score", "--sort", "-s", help="Sort top senders: score|count|size|oldest"
    ),
    top_n: int = typer.Option(15, "--top", help="Number of top senders to display."),
    share: bool = typer.Option(False, "--share", help="Print a copyable share summary and exit."),
    share_format: str = typer.Option(
        "twitter",
        "--format",
        help="Share format: twitter (emoji, ≤280 chars) or plain (no emoji).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON (for scripting)."),
    scope: str = typer.Option(
        "inbox",
        "--scope",
        help="Mail scope to scan: 'inbox' (default) or 'anywhere' (includes archived, sent, all mail).",
    ),
    max_scan: int = typer.Option(
        1000,
        "--max-scan",
        help="Max emails to scan (default 1000). Raise to 5000+ for large mailboxes.",
    ),
    use_ai: bool = typer.Option(
        False,
        "--ai",
        help="[EXPERIMENTAL] Enrich sender insights with local AI (requires llama.cpp at localhost:8080).",
    ),
    ai_debug: bool = typer.Option(
        False,
        "--ai-debug",
        help="Print AI call details, raw responses, and parse results.",
        hidden=True,
    ),
    provider: str = typer.Option(
        "",
        "--provider",
        help="Email provider: gmail or imap. Defaults to configured provider.",
    ),
    imap_server: str = typer.Option("", "--imap-server", help="IMAP server hostname."),
    imap_user: str = typer.Option("", "--imap-user", help="IMAP login username."),
    imap_port: int = typer.Option(993, "--imap-port", help="IMAP SSL port (default 993)."),
    imap_folder: str = typer.Option(
        "INBOX", "--imap-folder", help="IMAP folder to scan (default INBOX)."
    ),
    ai_backend: str = typer.Option(
        "llama",
        "--ai-backend",
        help="Local AI backend: llama (llama.cpp, default) or ollama.",
    ),
    ai_url: str = typer.Option("", "--ai-url", help="Override local AI server URL."),
    ai_model: str = typer.Option("phi3", "--ai-model", help="Model name (Ollama only)."),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show full account summary and detailed insights."
    ),
    simple: bool = typer.Option(
        False, "--simple", help="Plain-language output — no scores or tables."
    ),
    since: str = typer.Option(
        "",
        "--since",
        help="Only scan emails received within the last N days. Format: 30d, 7d.",
    ),
):
    """
    Inbox decision engine — reclaimable space, confidence-scored recommendations, top senders.

    Tells you exactly what to move to Trash, why it's safe, and how long it will take.
    No AI required. Works with Gmail and IMAP. All deletions go to Trash — recoverable for 30 days.

    Examples:
      mailtrim stats
      mailtrim stats --sort size
      mailtrim stats --scope anywhere   # include archived and sent mail
      mailtrim stats --max-scan 5000    # scan more of a large mailbox
      mailtrim stats --since 30d        # only emails from the last 30 days
      mailtrim stats --share            # twitter-style summary (≤280 chars)
    """
    _record("stats")
    import json as json_lib
    import time as _time

    from mailtrim.core.sender_stats import (
        best_next_step,
        classify_sender_risk,
        confidence_description,
        confidence_reason,
        confidence_safety_label,
        fetch_sender_groups,
        format_time_estimate,
        generate_headline_insight,
        generate_insights,
        generate_recommendations,
        generate_stats_share_text,
        group_by_domain,
        impact_label,
        quick_win,
        reclaimable_mb,
        reclaimable_pct,
        sender_risk_tier,
        sender_risk_tier_from_conf,
    )

    if scope == "anywhere":
        mail_query = "in:anywhere -in:trash -in:spam"
        scope_label = "all mail"
    else:
        mail_query = "in:inbox"
        scope_label = "inbox"

    from mailtrim.core.validation import validate_since

    since_days: int | None = None
    if since:
        try:
            since_days = validate_since(since)
        except typer.BadParameter as exc:
            console.print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(1)
        mail_query += f" newer_than:{since_days}d"
        scope_label += f" · last {since_days}d"

    scan_start = _time.time()

    # Resolve provider + IMAP settings (CLI flags override persisted settings)
    provider, imap_server, imap_user, imap_port, imap_folder = _resolve_imap_settings(
        provider, imap_server, imap_user, imap_port, imap_folder
    )

    # Resolve IMAP password: env var → interactive prompt (never CLI flag)
    import os as _os

    imap_password = _os.environ.get("MAILTRIM_IMAP_PASSWORD", "")
    if provider == "imap" and imap_user and not imap_password:
        imap_password = typer.prompt(f"IMAP password for {imap_user}", hide_input=True, default="")

    try:
        client = _get_provider(
            provider=provider,
            imap_server=imap_server,
            imap_user=imap_user,
            imap_password=imap_password,
            imap_port=imap_port,
            imap_folder=imap_folder,
        )
    except Exception as exc:
        _handle_error(exc, verbose=verbose)

    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as p:
        t = p.add_task(f"Scanning {scope_label}…", total=None)
        try:
            profile = client.get_profile()
        except Exception as exc:
            _handle_error(exc, verbose=verbose)
        p.update(t, description="Fetching top senders…")
        groups = fetch_sender_groups(
            client,
            query=mail_query,
            max_messages=max_scan,
            min_count=1,
            top_n=top_n,
            sort_by=sort_by if sort_by in ("score", "count", "size", "oldest") else "score",
        )
        p.update(t, description="Scoring recommendations…")
        domain_groups = group_by_domain(groups)
        domain_map_lookup = {d.domain: d for d in domain_groups}
        insights = generate_insights(groups, domain_groups)
        recommendations = generate_recommendations(groups, top_n=5, domain_map=domain_map_lookup)
        bns = best_next_step(recommendations)
        win = quick_win(recommendations)
        total_reclaimable = reclaimable_mb(recommendations)
        reclaim_pct = reclaimable_pct(total_reclaimable, insights.total_size_mb)

    scan_elapsed = int(_time.time() - scan_start)
    total_messages = profile.get("messagesTotal", 0)
    total_threads = profile.get("threadsTotal", 0)
    account_email = profile.get("emailAddress", "")

    # ── Share mode ────────────────────────────────────────────────────────────
    if share:
        if share_format not in ("twitter", "plain"):
            console.print(
                f"[red]Unknown --format '{share_format}'.[/red]  "
                "Valid values: [bold]twitter[/bold], [bold]plain[/bold]."
            )
            raise typer.Exit(1)

        rec_email_count = sum(
            (domain_map_lookup.get(rec.sender.domain) or rec.sender).count
            for rec in recommendations
        )
        # Top 3 safe/review domains by impact score — no personal data
        top_domains = [
            rec.sender.domain
            for rec in recommendations
            if classify_sender_risk(rec.sender) != "sensitive"
        ][:3]

        share_text = generate_stats_share_text(
            reclaimable_mb_val=total_reclaimable,
            sender_count=len(recommendations),
            email_count=rec_email_count,
            top_domains=top_domains,
            scan_seconds=scan_elapsed,
            fmt=share_format,
        )

        console.print()
        # Colored terminal preview
        console.print(
            Panel(
                share_text,
                title="[bold green]Share mailtrim[/bold green]"
                + (" [dim](twitter)[/dim]" if share_format == "twitter" else " [dim](plain)[/dim]"),
                border_style="green",
                padding=(0, 2),
            )
        )
        # Copy-ready block — raw text, no markup
        console.print("[dim]── copy-ready ──────────────────────────────────[/dim]")
        console.print(share_text)
        console.print("[dim]───────────────────────────────────────────────[/dim]")
        console.print(
            f"\n[dim]{len(share_text)} chars"
            + (" · fits Twitter/X" if len(share_text) <= 280 else " · over 280 chars")
            + "[/dim]\n"
        )
        return

    if json_output:
        data = {
            "account": account_email,
            "total_messages": total_messages,
            "total_threads": total_threads,
            "scanned": insights.total_scanned,
            "scanned_size_mb": insights.total_size_mb,
            "reclaimable_mb": total_reclaimable,
            "reclaimable_pct": reclaim_pct,
            "unique_senders": insights.unique_senders,
            "unique_domains": insights.unique_domains,
            "oldest_email_days": insights.oldest_email_days,
            "top_n_coverage_pct": insights.top_n_coverage_pct,
            "senders": [
                {
                    "email": g.sender_email,
                    "name": g.sender_name,
                    "domain": g.domain,
                    "count": g.count,
                    "size_mb": g.total_size_mb,
                    "impact_score": g.impact_score,
                    "impact_label": impact_label(g.impact_score),
                    "oldest_days": g.inbox_days,
                    "has_unsubscribe": g.has_unsubscribe,
                }
                for g in groups
            ],
            "domains": [
                {
                    "domain": d.domain,
                    "count": d.count,
                    "size_mb": d.total_size_mb,
                    "impact_score": d.impact_score,
                    "sender_count": len(d.senders),
                }
                for d in domain_groups[:10]
            ],
            "recommendations": [
                {
                    "sender": rec.sender.sender_email,
                    "confidence": rec.confidence,
                    "confidence_reason": confidence_reason(rec.sender),
                    "safety": confidence_safety_label(rec.confidence),
                    "actions": [
                        {
                            "label": a.label,
                            "savings_mb": a.savings_mb,
                            "exact": a.savings_exact,
                            "time_estimate": format_time_estimate(rec.sender.count),
                            "command": a.command,
                        }
                        for a in rec.actions
                    ],
                }
                for rec in recommendations
            ],
        }
        console.print_json(json_lib.dumps(data))
        return

    from mailtrim.core.ai.mode import ai_status_line

    ai_label, ai_note, ai_color = ai_status_line(get_settings().ai_mode)

    first_run = _is_first_stats_run()
    console.print()
    if first_run:
        console.print(
            f"  [bold cyan]✨ First scan complete — analyzing your inbox patterns[/bold cyan]  "
            f"[dim]({insights.total_scanned:,} emails · {insights.unique_senders} senders)[/dim]"
        )
    else:
        _since_note = f" · last {since_days}d" if since_days else ""
        console.print(
            f"[dim]  ✨ Scan complete — analyzed {insights.total_scanned:,} emails "
            f"across {insights.unique_senders} senders in {scan_elapsed}s{_since_note}[/dim]"
        )
    console.print(f"  [{ai_color}]AI: {ai_label}[/{ai_color}]  [dim]{ai_note}[/dim]")
    console.print()

    # ── Headline Insight ──────────────────────────────────────────────────────
    headline = generate_headline_insight(
        insights=insights,
        reclaim_pct=reclaim_pct,
        rec_count=len(recommendations),
        reclaimable_mb_val=total_reclaimable,
        recommendations=recommendations,
    )
    console.print(f"  {headline}")
    console.print()

    # ── Reclaimable Space Banner ──────────────────────────────────────────────
    if total_reclaimable > 0:
        total_rec_emails = sum(rec.sender.count for rec in recommendations)
        time_est = format_time_estimate(total_rec_emails)
        if total_reclaimable < 10:
            banner_lead = (
                f"[bold green]Clean {total_rec_emails:,} unnecessary emails quickly[/bold green]"
            )
        else:
            pct_str = f" ({reclaim_pct}% of scanned inbox)" if reclaim_pct > 0 else ""
            banner_lead = (
                f"[bold green]You can safely free ~{total_reclaimable} MB{pct_str}[/bold green]"
            )
        console.print(
            Panel(
                f"{banner_lead}\n"
                f"[dim]from your top {len(recommendations)} sender(s)  ·  "
                f"Each cleanup takes {time_est}  ·  All deletions go to Trash — undo anytime[/dim]",
                title="[bold]TOTAL RECLAIMABLE SPACE",
                border_style="green",
                padding=(0, 2),
            )
        )
        console.print()

    # ── Best Next Step ────────────────────────────────────────────────────────
    if bns and bns.actions:
        _bns_name = bns.sender.display_name[:40]
        _bns_domain_grp = domain_map_lookup.get(bns.sender.domain)
        _bns_count = _bns_domain_grp.count if _bns_domain_grp else bns.sender.count
        _bns_action = bns.actions[0]
        _bns_risk_label, _bns_icon, _bns_color = sender_risk_tier(bns.sender)
        _bns_conf_desc = confidence_description(min(95, bns.confidence))
        _bns_expl = _action_explanation(_bns_action.label, bns.sender.domain)
        # Emphasise count over MB when savings are small (< 5 MB feels underwhelming)
        if _bns_action.savings_mb >= 5:
            _bns_value = f"[green]~{_bns_action.savings_mb} MB freed[/green]"
        elif _bns_count > 0:
            _bns_value = f"[green]Clean {_bns_count:,} low-value emails quickly[/green]"
        else:
            _bns_value = ""
        console.print(
            Panel(
                f"[bold]{_bns_name}[/bold]  [dim]{_bns_count} emails[/dim]\n"
                + (f"  {_bns_value}\n" if _bns_value else "")
                + f"  [cyan]{_bns_action.command}[/cyan]\n"
                + (f"  [dim]{_bns_expl}[/dim]\n" if _bns_expl else "")
                + f"  {_bns_icon} [{_bns_color}]{_bns_risk_label}[/{_bns_color}]  "
                f"[dim]{_bns_conf_desc} ({min(95, bns.confidence)}%)[/dim]",
                title="[bold green]=== BEST NEXT STEP ===",
                border_style="green",
                padding=(0, 2),
            )
        )
        console.print()
    elif recommendations:
        _top_rec = max(recommendations, key=lambda r: r.sender.impact_score)
        console.print(
            Panel(
                f"⚠ [bold]{_top_rec.sender.display_name[:45]}[/bold]  "
                f"[dim]{_top_rec.sender.count} emails[/dim]\n"
                "  Largest reclaimable item [yellow]needs review[/yellow] before deleting.",
                title="[bold yellow]=== BEST NEXT STEP ===",
                border_style="yellow",
                padding=(0, 2),
            )
        )
        console.print()

    # ── Section 1: Account Summary (verbose only) ─────────────────────────────
    if verbose:
        console.rule("[bold cyan]=== ACCOUNT SUMMARY ===", align="left")
        console.print(
            f"  [bold]{account_email}[/bold]\n"
            f"  [dim]Total messages:[/dim]  {total_messages:,}   "
            f"[dim]Total threads:[/dim] {total_threads:,}\n"
            f"  [dim]{scope_label.capitalize()} scanned:[/dim]  {insights.total_scanned:,} messages  ·  "
            f"[bold]{insights.total_size_mb} MB[/bold]\n"
            f"  [dim]Unique senders:[/dim]  {insights.unique_senders}   "
            f"[dim]Unique domains:[/dim] {insights.unique_domains}   "
            f"[dim]Oldest email:[/dim] {insights.oldest_email_days}d ago"
        )
    if max_scan > 1000:
        console.print(
            f"\n  [dim]⚠ max-scan set to {max_scan:,} — large scans may take longer.[/dim]"
        )
    if verbose:
        if insights.total_scanned >= max_scan:
            console.print(
                f"\n  [yellow]⚠ Scan capped at {max_scan:,} — your mailbox may have more. "
                f"Run [bold]mailtrim stats --max-scan 5000[/bold] to analyze further.[/yellow]"
            )
        if total_messages > 10_000:
            console.print(
                "\n  [dim]📬 Large mailbox detected — scanning recent inbox for quick wins[/dim]"
            )
        if scope == "inbox" and total_messages > insights.total_scanned * 3:
            console.print(
                f"\n  [dim]💡 {total_messages:,} total messages in your account vs "
                f"{insights.total_scanned:,} scanned. Run [bold]mailtrim stats --scope anywhere[/bold] "
                f"to include archived and sent mail.[/dim]"
            )
        console.print()

    # ── Section 2: Key Insights (verbose only) ───────────────────────────────
    if verbose:
        console.rule("[bold cyan]=== KEY INSIGHTS ===", align="left")

        if insights.top_storage:
            g = insights.top_storage
            console.print(
                f"  [bold red]🔥 Top storage hog:[/bold red]  {g.display_name[:40]}  "
                f"[bold]{g.total_size_mb} MB[/bold] across {g.count} emails"
            )

        if insights.top_volume:
            g = insights.top_volume
            console.print(
                f"  [bold yellow]📮 Most frequent:[/bold yellow]  {g.display_name[:40]}  "
                f"[bold]{g.count} emails[/bold]  ·  {g.total_size_mb} MB"
            )

        if insights.oldest:
            g = insights.oldest
            console.print(
                f"  [bold blue]📅 Oldest clutter:[/bold blue]  {g.display_name[:40]}  "
                f"sitting in inbox since [bold]{g.age_str}[/bold]"
            )

        if insights.multi_sender_domains:
            top_multi = insights.multi_sender_domains[:3]
            domain_parts = ", ".join(
                f"[bold]{d.domain}[/bold] ({len(d.senders)} senders, {d.count} emails)"
                for d in top_multi
            )
            console.print(f"  [bold green]🌐 Domain patterns:[/bold green]  {domain_parts}")

        pct = insights.top_n_coverage_pct
        top5_mb = insights.top_n_size_mb
        console.print(
            f"\n  [dim]Top 5 senders account for[/dim] [bold]{pct}%[/bold] "
            f"[dim]of scanned mail[/dim] ([bold]{top5_mb} MB[/bold])"
        )
        console.print()

    # ── Quick Win — only show when it differs from BEST NEXT STEP ───────────
    if win and win is bns:
        win = None  # suppress duplicate — same sender already shown above
    if win and win.actions:
        first_action = win.actions[0]
        _win_conf = min(95, win.confidence)
        _win_label, _win_icon, _win_color = sender_risk_tier(win.sender)
        reason = confidence_reason(win.sender)
        _win_domain = domain_map_lookup.get(win.sender.domain)
        _win_count = _win_domain.count if _win_domain else win.sender.count
        _win_size_mb = _win_domain.total_size_mb if _win_domain else win.sender.total_size_mb
        time_est = format_time_estimate(_win_count)
        _win_conf_desc = confidence_description(_win_conf)
        savings_line = (
            f"  [yellow]→ {first_action.label}[/yellow]  "
            + (
                f"[green]~{first_action.savings_mb} MB freed[/green]  "
                if first_action.savings_mb > 0
                else ""
            )
            + f"[dim]takes {time_est}[/dim]"
        )
        console.print(
            Panel(
                f"[bold]{win.sender.display_name[:45]}[/bold]  "
                f"[dim]{_win_count} emails · {_win_size_mb} MB[/dim]\n\n"
                + savings_line
                + f"\n  [cyan]{first_action.command}[/cyan]\n\n"
                f"  {_win_icon} [{_win_color}]{_win_label}[/{_win_color}]  "
                f"[dim]{_win_conf_desc} ({_win_conf}%)[/dim]"
                + (f"\n  [dim]Why: {reason}[/dim]" if reason != "limited signals" else ""),
                title="[bold yellow]⚡ QUICK WIN — largest savings",
                border_style="yellow",
                padding=(0, 2),
            )
        )
        console.print()
    elif recommendations:
        # All recommendations need review — surface the largest as a warning
        _needs_review = max(recommendations, key=lambda r: r.sender.impact_score)
        console.print(
            Panel(
                f"⚠ [bold]{_needs_review.sender.display_name[:45]}[/bold]  "
                f"[dim]{_needs_review.sender.count} emails[/dim]\n"
                "  Largest reclaimable item [yellow]needs review[/yellow] before deleting.",
                title="[bold yellow]⚡ QUICK WIN",
                border_style="yellow",
                padding=(0, 2),
            )
        )
        console.print()

    # ── Section 3: Top Senders (verbose only) ────────────────────────────────
    if verbose:
        console.rule("[bold cyan]=== TOP SENDERS ===", align="left")

        if groups:
            table = Table(border_style="dim", show_header=True, header_style="bold", pad_edge=False)
            table.add_column("#", width=3, style="dim")
            table.add_column("Impact", width=12)
            table.add_column("Sender", min_width=28)
            table.add_column("Emails", justify="right", style="red bold", width=7)
            table.add_column("Size", justify="right", width=9)
            table.add_column("Oldest", width=12)
            table.add_column("Risk", width=22)
            table.add_column("Unsub", width=5, justify="center")

            for i, g in enumerate(groups, 1):
                ilabel = impact_label(g.impact_score)
                label_color = {"High": "red", "Medium": "yellow", "Low": "dim"}.get(ilabel, "dim")
                score_cell = f"[{label_color}]{g.impact_score} ({ilabel})[/{label_color}]"
                name = g.display_name[:32]
                size_str = (
                    f"{g.total_size_mb}MB"
                    if g.total_size_mb >= 0.1
                    else f"{g.total_size_bytes // 1024}KB"
                )
                _t_label, _t_icon, _t_color = sender_risk_tier(g)
                risk_cell = f"{_t_icon} [{_t_color}]{_t_label}[/{_t_color}]"
                unsub = "[green]✓[/green]" if g.has_unsubscribe else "[dim]–[/dim]"
                table.add_row(
                    str(i), score_cell, name, str(g.count), size_str, g.age_str, risk_cell, unsub
                )

            console.print(table)
            console.print(
                "[dim]  Impact = 60% storage + 40% volume (0–100)  ·  "
                "Risk: 🟢 Safe to clean  🟡 Needs review  🔴 Sensitive / personal  ·  Unsub = List-Unsubscribe header[/dim]"
            )
        else:
            console.print("  [dim]No senders found matching the query.[/dim]")
        console.print()

    # ── Simple mode — plain-language output, no scores ────────────────────────
    if simple:
        if not recommendations:
            console.print("  Your inbox looks clean — nothing obvious to remove right now.\n")
        else:
            console.print("  [bold]Here are 3 safe things you can do right now:[/bold]\n")
            for i, rec in enumerate(recommendations[:3], 1):
                g = rec.sender
                _rec_domain = domain_map_lookup.get(g.domain)
                _rec_count = _rec_domain.count if _rec_domain else g.count
                risk_class = classify_sender_risk(g)
                safety_note = (
                    "safe to clean"
                    if risk_class == "safe"
                    else "worth a quick look first"
                    if risk_class == "review"
                    else "review manually — may be important"
                )
                if rec.actions:
                    action = rec.actions[0]
                    console.print(
                        f"  [bold]{i}. {g.display_name[:45]}[/bold]  "
                        f"[dim]({_rec_count} emails)[/dim]\n"
                        f"     {safety_note}\n"
                        f"     [cyan]{action.command}[/cyan]\n"
                    )
            console.print(
                "[dim]  All emails moved to Trash — undo anytime with: mailtrim undo[/dim]\n"
            )
        console.print(
            "[dim]  Add --verbose for full details · mailtrim purge — interactive cleanup[/dim]\n"
        )
        return

    # ── Section 4: Recommended Actions ───────────────────────────────────────
    console.rule("[bold cyan]=== RECOMMENDED ACTIONS ===", align="left")

    # Optional local-AI enrichment — always runs on all top recommendations.
    ai_insights: dict[str, dict] = {}
    if use_ai and recommendations:
        from mailtrim.core.ai.mode import require_local

        require_local(get_settings().ai_mode)
        from mailtrim.core.llm import (
            analyze_batch,
            confidence_delta,
        )

        # Always run AI on all top recommendations — they are exactly the senders where
        # a second opinion matters most. The old should_analyze filter was blocking all of
        # them because high-confidence senders were being skipped.
        eligible = recommendations

        if ai_debug:
            import logging as _logging

            _ai_handler = _logging.StreamHandler()
            _ai_handler.setFormatter(_logging.Formatter("[AI DEBUG] %(name)s — %(message)s"))
            for _mod in ("mailtrim.core.ai.client", "mailtrim.core.llm"):
                _log = _logging.getLogger(_mod)
                _log.setLevel(_logging.DEBUG)
                _log.addHandler(_ai_handler)

            console.print(
                f"[dim][AI debug] Running AI on {len(eligible)} sender(s): "
                + ", ".join(r.sender.sender_email for r in eligible)
                + "[/dim]"
            )
            for rec in eligible:
                _text = f"From: {rec.sender.sender_name or rec.sender.sender_email}\n" + "\n".join(
                    rec.sender.sample_subjects[:3]
                )
                console.print(
                    f"[dim][AI debug] prompt for {rec.sender.sender_email}:\n{_text}[/dim]"
                )

        with console.status("[dim][AI] Analyzing senders via local model…[/dim]"):
            texts = [
                f"From: {rec.sender.sender_name or rec.sender.sender_email}\n"
                + "\n".join(rec.sender.sample_subjects[:3])
                for rec in eligible
            ]
            keys = [rec.sender.sender_email for rec in eligible]
            _ai_client_override = _get_ai_client_opt(ai_backend, ai_url, ai_model)
            results = analyze_batch(texts, cache_keys=keys, ai_client=_ai_client_override)

        for rec, result in zip(eligible, results):
            if ai_debug:
                console.print(
                    f"[dim][AI debug] {rec.sender.sender_email}: "
                    f"{'parsed OK → ' + str(result) if result else 'no result (parse failed or backend unavailable)'}[/dim]"
                )
            ai_insights[rec.sender.sender_email] = result

        if any(ai_insights.values()):
            from mailtrim.core.llm import apply_impact_nudge

            apply_impact_nudge(groups, ai_insights)
        else:
            _expected = ai_url or (
                "http://localhost:11434" if ai_backend == "ollama" else "http://localhost:8080"
            )
            console.print(
                f"\n[yellow]⚠ Local AI unavailable[/yellow] — results shown without AI enrichment.\n"
                f"  Is [bold]{'Ollama' if ai_backend == 'ollama' else 'llama.cpp'}[/bold] running?"
                f" Expected at {_expected}\n"
                "  [dim]Results are still accurate — AI only adjusts confidence scores.[/dim]\n"
            )

    if recommendations:
        from mailtrim.core.llm import format_ai_line  # always available

        # AI summary block — one concise insight line per sender with AI data
        if use_ai and ai_insights:
            _AI_CAT_DESC: dict[str, str] = {
                "promo": "looks promotional",
                "spam": "appears spam-like",
                "update": "appears to be automated updates",
                "important": "may contain important content",
            }
            _ai_lines = []
            for rec in recommendations:
                _ai = ai_insights.get(rec.sender.sender_email, {})
                _cat = _ai.get("category", "")
                _desc = _AI_CAT_DESC.get(_cat, "")
                if not _desc:
                    continue
                # Sensitive senders get a stronger warning regardless of AI category
                if classify_sender_risk(rec.sender) == "sensitive":
                    _desc = "may be important — review before deleting"
                _ai_lines.append(f"  [dim][AI] {rec.sender.display_name[:30]} {_desc}[/dim]")
            for _line in _ai_lines[:3]:
                console.print(_line)
            if _ai_lines:
                console.print()

        for i, rec in enumerate(recommendations[:3], 1):
            g = rec.sender
            _rec_domain = domain_map_lookup.get(g.domain)
            _rec_count = _rec_domain.count if _rec_domain else g.count
            _rec_size_mb = _rec_domain.total_size_mb if _rec_domain else g.total_size_mb
            ai = ai_insights.get(g.sender_email, {})
            rule_conf = rec.confidence

            # Compute adjusted confidence without mutating rec.confidence.
            # Cap at 95 — 100% implies certainty we never have.
            delta = confidence_delta(ai) if (use_ai and ai) else 0
            display_confidence = max(0, min(95, rule_conf + delta))

            safety, icon, safety_color = sender_risk_tier_from_conf(g, display_confidence)
            conf_desc = confidence_description(display_confidence)

            # Reason hint: prefer AI category phrase, fall back to rule-based.
            if use_ai and ai:
                ai_cat = ai.get("category", "")
                _cat_phrase = {
                    "promo": "promo pattern",
                    "spam": "spam signals",
                    "update": "automated updates",
                    "important": "important mail",
                }.get(ai_cat, "")
                rule_reason = confidence_reason(g)
                if _cat_phrase and rule_reason != "limited signals":
                    reason_text = f"{_cat_phrase} + {rule_reason}"[:50]
                elif _cat_phrase:
                    reason_text = _cat_phrase
                else:
                    reason_text = rule_reason
            else:
                reason_text = confidence_reason(g)
            reason_hint = f" [dim]({reason_text})[/dim]" if reason_text != "limited signals" else ""

            # Confidence line: show arrow + delta when AI adjusted it.
            if use_ai and ai and delta != 0:
                sign = "+" if delta > 0 else ""
                conf_str = (
                    f"[dim]{rule_conf}%[/dim] → [bold]{display_confidence}%[/bold] "
                    f"[dim]({sign}{delta}% AI)[/dim]"
                )
            else:
                conf_str = f"[bold]{display_confidence}%[/bold]"

            console.print(
                f"  [bold]{i}. {g.display_name[:40]}[/bold]  "
                f"[dim]{_rec_count} emails · {_rec_size_mb} MB · {g.age_str}[/dim]\n"
                f"  {icon} [{safety_color}]{safety}[/{safety_color}]  "
                f"Confidence: {conf_str} [dim]— {conf_desc}[/dim]{reason_hint}"
            )
            if use_ai and ai:
                console.print(f"  [dim]{format_ai_line(ai)}[/dim]")
            for action in rec.actions:
                if action.savings_mb > 0:
                    savings_str = f"[green]~{action.savings_mb} MB freed[/green]"
                elif "review" in action.label.lower():
                    savings_str = "[yellow]Review manually before deciding[/yellow]"
                else:
                    savings_str = "[dim]Preview items safely before deleting[/dim]"
                tilde = "" if action.savings_exact else "~"
                time_est = format_time_estimate(_rec_count)
                console.print(
                    f"    [yellow]→ {action.label}[/yellow]  "
                    f"{tilde}{savings_str}  [dim]takes {time_est}[/dim]"
                )
                console.print(f"      [cyan]{action.command}[/cyan]")
                _expl = _action_explanation(action.label, g.domain)
                if _expl:
                    console.print(f"      [dim]{_expl}[/dim]")
            console.print()
        console.print(
            "[dim]  All emails moved to Trash (recoverable) — undo anytime with: mailtrim undo[/dim]\n"
        )
    else:
        console.print("  [dim]Not enough data for recommendations.[/dim]\n")

    console.print(
        "[dim]  Next steps:[/dim]\n"
        "  [cyan]mailtrim purge[/cyan]              — pick senders to move to Trash interactively\n"
        "  [cyan]mailtrim stats --sort size[/cyan]  — re-sort by storage\n"
        "  [cyan]mailtrim stats --verbose[/cyan]    — full account summary + all senders\n"
        "  [cyan]mailtrim stats --simple[/cyan]     — plain-language view, no scores\n"
        "  [cyan]mailtrim quickstart[/cyan]         — guided first cleanup\n"
    )


# ── quickstart ───────────────────────────────────────────────────────────────


@app.command()
def quickstart(
    provider: str = typer.Option(
        "", "--provider", help="Email provider: gmail or imap. Defaults to configured provider."
    ),
    imap_server: str = typer.Option("", "--imap-server", help="IMAP server hostname."),
    imap_user: str = typer.Option("", "--imap-user", help="IMAP login username."),
    imap_port: int = typer.Option(993, "--imap-port", help="IMAP SSL port (default 993)."),
    imap_folder: str = typer.Option(
        "INBOX", "--imap-folder", help="IMAP folder to scan (default INBOX)."
    ),
):
    """
    Guided first cleanup — checks auth, scans inbox, and suggests your first safe action.

    Perfect for first-time users. Run this before anything else.
    Works with Gmail and IMAP (Outlook, Yahoo, custom servers).
    All cleanups go to Trash — undo anytime with: mailtrim undo

    After running `mailtrim setup`, no flags are needed:
      mailtrim quickstart

    First-time IMAP setup (then no flags required afterwards):
      mailtrim quickstart --provider imap --imap-server imap.example.com --imap-user you@example.com
    """
    import os as _os

    from mailtrim.core.sender_stats import (
        best_next_step,
        classify_sender_risk,
        compute_confidence_score,
        fetch_sender_groups,
        generate_recommendations,
        group_by_domain,
        reclaimable_mb,
    )

    # Resolve provider + IMAP settings (CLI flags override persisted settings)
    provider, imap_server, imap_user, imap_port, imap_folder = _resolve_imap_settings(
        provider, imap_server, imap_user, imap_port, imap_folder
    )

    # Step 1: Check auth / connectivity
    console.print()
    imap_password = ""
    if provider == "imap":
        imap_password = _os.environ.get("MAILTRIM_IMAP_PASSWORD", "")
        if imap_user and not imap_password:
            imap_password = typer.prompt(
                f"IMAP password for {imap_user}", hide_input=True, default=""
            )
    try:
        client = _get_provider(
            provider=provider,
            imap_server=imap_server,
            imap_user=imap_user,
            imap_password=imap_password,
            imap_port=imap_port,
            imap_folder=imap_folder,
        )
        account_email = _get_account_email(client)
        console.print(f"[green]✓[/green] Connected as [bold]{account_email}[/bold]")
    except Exception:
        if provider == "imap":
            console.print(
                "[red]✗ IMAP connection failed.[/red]  "
                "Re-run [cyan]mailtrim setup[/cyan] to reconfigure."
            )
        else:
            console.print("[red]✗ Not authenticated.[/red]  Run [cyan]mailtrim auth[/cyan] first.")
        raise typer.Exit(1)

    # Step 2: Scan — fetch up to 500 messages from inbox
    _SCAN_LIMIT = 500
    with console.status(f"  Scanning up to {_SCAN_LIMIT} emails…"):
        groups = fetch_sender_groups(
            client,
            query="in:inbox",
            max_messages=_SCAN_LIMIT,
            min_count=2,
            top_n=50,
            sort_by="score",
        )
        domain_groups = group_by_domain(groups)
        domain_map_lookup = {d.domain: d for d in domain_groups}
        recommendations = generate_recommendations(groups, top_n=10, domain_map=domain_map_lookup)
        bns = best_next_step(recommendations)
        total_reclaimable = reclaimable_mb(recommendations)

    total_emails = sum(g.count for g in groups)
    # Safe candidates: non-sensitive recs with confidence >= 50
    safe_candidates = [
        r
        for r in recommendations
        if classify_sender_risk(r.sender) != "sensitive"
        and compute_confidence_score(r.sender) >= 50
    ]

    console.print(
        f"  Scanned [bold]{total_emails:,}[/bold] emails · "
        f"[bold]{len(safe_candidates)}[/bold] safe senders to clean"
        + (
            f" · [green]~{total_reclaimable} MB[/green] reclaimable"
            if total_reclaimable > 0
            else ""
        )
    )

    from mailtrim.core.ai.mode import ai_status_line

    _qs_ai_label, _qs_ai_note, _qs_ai_color = ai_status_line(get_settings().ai_mode)
    console.print(
        f"  [{_qs_ai_color}]AI: {_qs_ai_label}[/{_qs_ai_color}]  [dim]{_qs_ai_note}[/dim]"
    )
    console.print()

    # Step 3: Surface the single best safe action
    # Only show a command when we have a safe or high-confidence pick
    if (
        bns
        and bns.actions
        and (
            classify_sender_risk(bns.sender) != "sensitive"
            and compute_confidence_score(bns.sender) >= 50
        )
    ):
        g = bns.sender
        action = bns.actions[0]
        d = domain_map_lookup.get(g.domain)
        email_count = d.count if d else g.count
        size_mb = (d.total_size_mb if d else g.total_size_mb) if action.savings_mb > 0 else 0

        size_str = f" · {size_mb} MB" if size_mb > 0 else ""
        console.print(
            f"  [bold]Best first action[/bold]  "
            f"[dim]{g.display_name[:45]} — {email_count:,} emails{size_str}[/dim]"
        )
        console.print(f"    [bold cyan]{action.command}[/bold cyan]")
        console.print()
        console.print("  [dim]Undo anytime:[/dim]  mailtrim undo")
        console.print("  [dim]Full analysis:[/dim]  mailtrim stats")
    elif recommendations:
        # Recs exist but nothing is high-confidence enough for an auto-suggest
        console.print(
            "  Found senders worth reviewing — run [cyan]mailtrim stats[/cyan] to see them."
        )
        console.print("  [dim]Undo anytime:[/dim]  mailtrim undo")
    else:
        console.print("  [green]Inbox looks clean.[/green]  Nothing to remove right now.")
        console.print("  [dim]Full analysis:[/dim]  mailtrim stats")

    console.print()


# ── sync ─────────────────────────────────────────────────────────────────────


@app.command()
def sync(
    limit: int = typer.Option(200, "--limit", "-n", help="Number of messages to sync."),
    query: str = typer.Option("in:inbox", "--query", "-q", help="Gmail query to sync."),
    scope: str = typer.Option(
        "inbox",
        "--scope",
        help="Mail scope: 'inbox' (default) or 'anywhere' (includes archived, sent, all mail).",
    ),
):
    """
    Sync mail metadata to local database for fast repeated queries.

    Run before stats or triage when you want to avoid re-fetching from Gmail.

    Examples:
      mailtrim sync
      mailtrim sync --limit 500
      mailtrim sync --query "in:inbox is:unread"
      mailtrim sync --scope anywhere
    """
    _require_gmail("sync")
    from mailtrim.core.storage import EmailRecord, EmailRepo, get_session

    if scope == "anywhere" and query == "in:inbox":
        query = "in:anywhere -in:trash -in:spam"
    elif scope == "anywhere":
        query = f"in:anywhere -in:trash -in:spam {query}"

    client = _get_client()
    account_email = _get_account_email(client)
    session = get_session()
    repo = EmailRepo(session)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Fetching message IDs...", total=None)
        ids = client.list_message_ids(query=query, max_results=limit)
        progress.update(task, description=f"Fetching {len(ids)} messages...", total=len(ids))

        messages = []
        chunk_size = 50
        for i in range(0, len(ids), chunk_size):
            chunk = client.get_messages_batch(ids[i : i + chunk_size])
            messages.extend(chunk)
            progress.update(task, advance=len(chunk))

    records = []
    for msg in messages:
        rec = EmailRecord(
            account_email=account_email,
            gmail_id=msg.id,
            thread_id=msg.thread_id,
            subject=msg.headers.subject,
            sender_email=msg.sender_email,
            sender_name=msg.sender_name,
            snippet=msg.snippet,
            label_ids_json=__import__("json").dumps(msg.label_ids),
            internal_date=msg.internal_date,
            size_estimate=msg.size_estimate,
            is_unread=msg.is_unread,
            is_inbox=msg.is_inbox,
            list_unsubscribe=msg.headers.list_unsubscribe,
        )
        records.append(rec)

    repo.upsert_many(records)
    console.print(f"[green]Synced {len(records)} messages[/green] for [bold]{account_email}[/bold]")

    # Housekeeping: purge expired undo log entries silently
    from mailtrim.core.storage import UndoLogRepo

    purged = UndoLogRepo(session).purge_expired()
    if purged:
        console.print(f"[dim]Cleaned up {purged} expired undo log entries.[/dim]")


# ── triage ────────────────────────────────────────────────────────────────────


@app.command()
def triage(
    limit: int = typer.Option(30, "--limit", "-n", help="Number of inbox messages to classify."),
    show_actions: bool = typer.Option(
        True, "--actions/--no-actions", help="Show suggested actions."
    ),
):
    """
    AI-powered inbox triage — classifies each unread email with priority, category, and a one-line reason.

    Requires ai_mode=cloud. Sends email subjects and snippets (≤300 chars) to Anthropic — never full body.
    Run: mailtrim config ai-mode cloud

    Examples:
      mailtrim triage
      mailtrim triage --limit 50
      mailtrim triage --no-actions
    """
    _require_gmail("triage")
    from mailtrim.core.ai.mode import require_cloud

    try:
        require_cloud(get_settings().ai_mode)
    except Exception as exc:
        _handle_error(exc)
    _cloud_ai_warning()
    from mailtrim.core.avoidance import AvoidanceDetector

    client = _get_client()
    account_email = _get_account_email(client)

    with console.status("Fetching inbox..."):
        ids = client.list_message_ids(query="in:inbox is:unread", max_results=limit)
        if not ids:
            console.print("[yellow]No unread messages in inbox.[/yellow]")
            return
        messages = client.get_messages_batch(ids)

    # Try Anthropic first; fall back to local LLM when key is absent or invalid.
    # Use AIEngine directly (not _get_ai()) so a missing key raises ValueError
    # instead of silently returning MockAIEngine.
    classified = None
    try:
        import anthropic

        from mailtrim.core.ai_engine import AIEngine

        ai = AIEngine()  # raises ValueError if ANTHROPIC_API_KEY is unset
        _print_ai_data_notice(f"{len(messages)} email subjects + snippets (≤300 chars each)")
        with console.status(f"Classifying {len(messages)} messages with Anthropic AI..."):
            classified = ai.classify_emails(messages)
    except (
        ValueError,
        anthropic.AuthenticationError,
        anthropic.RateLimitError,
        anthropic.APIStatusError,
        anthropic.APIConnectionError,
    ) as exc:
        # ValueError              → ANTHROPIC_API_KEY not set
        # AuthenticationError     → key present but invalid
        # RateLimitError          → quota exceeded, fall back gracefully
        # APIStatusError          → server-side error (5xx)
        # APIConnectionError      → network unreachable
        reason = "no API key set" if isinstance(exc, ValueError) else str(exc)
        console.print(
            f"[yellow]Anthropic unavailable ({reason}) — falling back to local AI "
            f"(localhost:8080).[/yellow]"
        )
        from mailtrim.core.llm import classify_for_triage
        from mailtrim.core.mock_ai import MockAIEngine

        with console.status(f"Classifying {len(messages)} messages with local AI..."):
            classified = classify_for_triage(messages)
        ai = MockAIEngine()  # avoidance detector only calls record_view here

    detector = AvoidanceDetector(client, account_email, ai)

    # Record views for avoidance tracking
    for msg in messages:
        detector.record_view(msg.id)

    # Build display table
    table = Table(
        title=f"Inbox Triage — {account_email}",
        show_header=True,
        header_style="bold cyan",
        border_style="dim",
        expand=True,
    )
    table.add_column("Priority", width=8)
    table.add_column("From", width=22, no_wrap=True)
    table.add_column("Subject", min_width=30)
    table.add_column("Category", width=14)
    table.add_column("Why", min_width=30)
    if show_actions:
        table.add_column("Action", width=12)

    priority_colors = {"high": "red", "medium": "yellow", "low": "dim"}
    category_icons = {
        "action_required": "⚡",
        "conversation": "💬",
        "newsletter": "📰",
        "notification": "🔔",
        "receipt": "🧾",
        "calendar": "📅",
        "social": "👥",
        "spam": "🚫",
        "other": "•",
    }

    msg_map = {m.id: m for m in messages}

    for cls in sorted(classified, key=lambda c: {"high": 0, "medium": 1, "low": 2}[c.priority]):
        msg = msg_map.get(cls.gmail_id)
        if not msg:
            continue

        color = priority_colors[cls.priority]
        icon = category_icons.get(cls.category, "•")
        deadline = f" [red]({cls.deadline_hint})[/red]" if cls.deadline_hint else ""

        row = [
            f"[{color}]{cls.priority.upper()}[/{color}]",
            Text(msg.sender_name[:20] or msg.sender_email[:20], overflow="ellipsis"),
            f"{msg.headers.subject[:60]}{deadline}",
            f"{icon} {cls.category.replace('_', ' ')}",
            cls.explanation[:80],
        ]
        if show_actions:
            row.append(f"[cyan]{cls.suggested_action}[/cyan]")

        table.add_row(*row)

    console.print(table)
    console.print(
        "\n[dim]Tip: Use [bold]mailtrim avoid[/bold] to see emails you've been putting off.[/dim]"
    )


# ── bulk ─────────────────────────────────────────────────────────────────────


@app.command()
def bulk(
    instruction: str = typer.Argument(
        ..., help='Natural language instruction, e.g. "archive all newsletters older than 30 days"'
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without making changes."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
):
    """
    Execute a bulk operation using natural language.

    All destructive actions move email to Trash (recoverable for 30 days).
    Requires ai_mode=cloud. Run: mailtrim config ai-mode cloud

    Examples:
      mailtrim bulk "archive all newsletters I haven't opened in 60 days"
      mailtrim bulk "move to Trash all emails from noreply@* older than 1 year"
      mailtrim bulk "label as 'receipts' everything from order confirmation senders"
      mailtrim bulk "archive LinkedIn notifications" --dry-run   # preview first
    """
    _require_gmail("bulk")
    from mailtrim.core.ai.mode import require_cloud
    from mailtrim.core.bulk_engine import BulkEngine

    try:
        require_cloud(get_settings().ai_mode)
    except Exception as exc:
        _handle_error(exc)
    _cloud_ai_warning()
    client = _get_client()
    account_email = _get_account_email(client)
    engine = BulkEngine(client, account_email, _get_ai())

    _print_ai_data_notice("your instruction text only (no email content)")
    with console.status("Translating your instruction with AI..."):
        preview = engine.preview(instruction)

    op = preview.operation
    console.print(
        Panel(
            f"[bold]Operation:[/bold] {op.explanation}\n"
            f"[bold]Gmail query:[/bold] [cyan]{op.gmail_query}[/cyan]\n"
            f"[bold]Action:[/bold] [yellow]{op.action}[/yellow]\n"
            f"[bold]Messages matched:[/bold] [red]{preview.total_count}[/red]\n"
            f"[bold]Estimated size:[/bold] {preview.estimated_size_mb} MB",
            title="Bulk Operation Preview",
            border_style="yellow",
        )
    )

    if preview.sample_messages:
        console.print("\n[bold]Sample messages:[/bold]")
        for msg in preview.sample_messages[:5]:
            console.print(f"  • [dim]{msg.sender_email}[/dim] — {msg.headers.subject[:70]}")

    if preview.total_count == 0:
        console.print("[yellow]No messages matched. Nothing to do.[/yellow]")
        return

    if dry_run:
        console.print("\n[dim]Dry run — no changes made. Remove --dry-run to execute.[/dim]")
        return

    if not yes:
        confirmed = Confirm.ask(f"\nProceed with {op.action} on {preview.total_count} messages?")
        if not confirmed:
            console.print("[dim]Cancelled.[/dim]")
            return

    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as progress:
        t = progress.add_task(f"Executing {op.action}...", total=None)
        result = engine.execute(preview)
        progress.update(t, description="Done.")

    console.print(
        f"\n[green]Done.[/green] {result.affected_count} messages {op.action}d. "
        f"Undo log ID: [bold]{result.undo_log_id}[/bold] "
        f"(undo within {get_settings().undo_window_days} days with [cyan]mailtrim undo {result.undo_log_id}[/cyan])"
    )


# ── undo ─────────────────────────────────────────────────────────────────────


@app.command()
def undo(
    log_id: Optional[int] = typer.Argument(
        None, help="Undo log ID. Omit to see recent operations."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
    provider: str = typer.Option(
        "", "--provider", help="Email provider: gmail or imap. Defaults to configured provider."
    ),
    imap_server: str = typer.Option("", "--imap-server", help="IMAP server hostname."),
    imap_user: str = typer.Option("", "--imap-user", help="IMAP login username."),
    imap_port: int = typer.Option(993, "--imap-port", help="IMAP SSL port (default 993)."),
    imap_folder: str = typer.Option("INBOX", "--imap-folder", help="IMAP folder (default INBOX)."),
):
    """
    Undo a bulk operation within the 30-day window.

    Restores emails from Trash back to Inbox. Works with Gmail and IMAP providers.
    After `mailtrim setup`, the correct provider is used automatically.

    Examples:
      mailtrim undo          # list all undoable operations
      mailtrim undo 3        # restore operation #3
      mailtrim undo 3 --yes  # restore without prompting
    """
    import os as _os

    from mailtrim.core.storage import UndoLogRepo, get_session

    # Resolve provider + IMAP settings (CLI flags override persisted settings)
    provider, imap_server, imap_user, imap_port, imap_folder = _resolve_imap_settings(
        provider, imap_server, imap_user, imap_port, imap_folder
    )

    # Resolve credentials and account identity
    _gmail_client = None
    imap_password = ""

    if provider == "imap":
        if not imap_user:
            console.print("[red]--imap-user is required for provider=imap.[/red]")
            raise typer.Exit(1)
        imap_password = _os.environ.get("MAILTRIM_IMAP_PASSWORD", "")
        if not imap_password:
            imap_password = typer.prompt(
                f"IMAP password for {imap_user}", hide_input=True, default=""
            )
        account_email = imap_user
    else:
        try:
            _gmail_client = _get_client()
            account_email = _get_account_email(_gmail_client)
        except Exception:
            console.print("[red]✗ Not authenticated.[/red]  Run [cyan]mailtrim auth[/cyan] first.")
            raise typer.Exit(1)

    if log_id is None:
        # Show recent undo log — no connection to mail server needed
        repo = UndoLogRepo(get_session())
        entries = repo.list_recent(account_email)
        if not entries:
            console.print("[yellow]No recent undoable operations.[/yellow]")
            return

        table = Table(title="Recent Operations (undoable)", border_style="dim")
        table.add_column("ID", width=6)
        table.add_column("Action", width=10)
        table.add_column("Messages", width=10)
        table.add_column("Description")
        table.add_column("Executed At", width=20)
        table.add_column("Expires", width=12)

        now = datetime.now(timezone.utc)
        for entry in entries:
            expires_in = (entry.expires_at.replace(tzinfo=timezone.utc) - now).days
            table.add_row(
                str(entry.id),
                entry.operation,
                str(len(entry.message_ids)),
                entry.description[:60],
                entry.executed_at.strftime("%Y-%m-%d %H:%M"),
                f"{expires_in}d",
            )
        console.print(table)
        console.print("\nRun [cyan]mailtrim undo <ID>[/cyan] to reverse an operation.")
        return

    entry = UndoLogRepo(get_session()).get(log_id)
    if not entry:
        console.print(f"[red]Undo log entry {log_id} not found.[/red]")
        raise typer.Exit(1)

    console.print(
        f"Undo: [yellow]{entry.operation}[/yellow] on [bold]{len(entry.message_ids)}[/bold] messages\n"
        f"[dim]{entry.description}[/dim]"
    )

    if not yes:
        if not Confirm.ask("Proceed?"):
            console.print("[dim]Cancelled.[/dim]")
            return

    count = 0
    if provider == "imap":
        # IMAP undo: only "trash" operations are supported
        if entry.operation != "trash":
            console.print(
                f"[yellow]Undo for '{entry.operation}' is not supported for IMAP providers.[/yellow]\n"
                "  Only purge (trash) operations can be undone via IMAP."
            )
            raise typer.Exit(1)
        _imap_provider = _get_provider(
            provider="imap",
            imap_server=imap_server,
            imap_user=imap_user,
            imap_password=imap_password,
            imap_port=imap_port,
            imap_folder=imap_folder,
        )
        with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as prog:
            t = prog.add_task("Restoring emails…", total=None)
            count = _imap_provider.batch_untrash(entry.message_ids)
            prog.update(t, description="Done.")
        UndoLogRepo(get_session()).mark_undone(log_id)
        total = len(entry.message_ids)
        skipped = total - count
        if count == total:
            console.print(f"[green]✓ Restored {count} email(s).[/green]")
        elif count > 0:
            console.print(
                f"[yellow]⚠ Partial restore:[/yellow] "
                f"[green]{count}[/green] restored · "
                f"[yellow]{skipped}[/yellow] skipped\n"
                "[dim]IMAP UIDs are folder-specific — they may change after a MOVE.\n"
                "Check your Trash folder manually and drag any remaining emails back to Inbox.[/dim]"
            )
        else:
            console.print(
                "[red]✗ Restore failed[/red] — no emails could be moved from Trash.\n"
                "[dim]IMAP UIDs may have changed. Open your mail client and move emails from Trash manually.[/dim]"
            )
    else:
        # Gmail: use BulkEngine (handles archive, label, mark_read undo too)
        from mailtrim.core.bulk_engine import BulkEngine

        engine = BulkEngine(_gmail_client, account_email)
        with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as prog:
            t = prog.add_task("Restoring emails…", total=None)
            count = engine.undo(log_id)
            prog.update(t, description="Done.")
        console.print(f"[green]✓ Restored {count} email(s).[/green]")

    try:
        from mailtrim.core.usage_stats import record_undo

        record_undo(restored=count)
    except Exception:
        pass

    # Offer to protect the senders so this doesn't happen again
    senders = entry.op_metadata.get("senders", [])
    if senders and not yes:
        protect_them = Confirm.ask(
            f"Protect {len(senders)} sender(s) from future purges?",
            default=False,
        )
        if protect_them:
            from mailtrim.core.storage import BlocklistRepo

            repo = BlocklistRepo(get_session())
            for s in senders:
                repo.add(account_email, s, reason="undo_feedback")
            console.print(
                f"[green]Protected {len(senders)} sender(s).[/green] "
                "Manage with [cyan]mailtrim protect --list[/cyan]"
            )


# ── follow-up ─────────────────────────────────────────────────────────────────


@app.command(name="follow-up")
def follow_up(
    message_id: Optional[str] = typer.Argument(None, help="Gmail message ID to track."),
    days: int = typer.Option(3, "--days", "-d", help="Remind in N days if no reply."),
    unconditional: bool = typer.Option(False, "--always", help="Remind even if they reply."),
    list_due: bool = typer.Option(False, "--list", "-l", help="Show due follow-ups."),
    sync_replies: bool = typer.Option(False, "--sync", help="Sync reply detection."),
):
    """
    [EXPERIMENTAL] Track an email for follow-up. Only reminds you if they haven't replied.

    Examples:
      mailtrim follow-up 18bca72... --days 5
      mailtrim follow-up --list
      mailtrim follow-up --sync
    """
    _require_gmail("follow-up")
    from mailtrim.core.follow_up import FollowUpTracker

    client = _get_client()
    account_email = _get_account_email(client)
    tracker = FollowUpTracker(client, account_email)

    if sync_replies:
        with console.status("Checking for replies..."):
            count = tracker.sync_replies()
        console.print(f"[green]Reply sync complete.[/green] {count} new replies detected.")
        return

    if list_due or message_id is None:
        due = tracker.get_due_follow_ups()
        if not due:
            console.print("[green]No follow-ups due.[/green]")
            stats = tracker.get_stats()
            console.print(
                f"[dim]Tracking {stats['pending']} threads | {stats['replied']} replied[/dim]"
            )
            return

        table = Table(title="Due Follow-ups", border_style="dim")
        table.add_column("ID", width=6)
        table.add_column("To", width=25)
        table.add_column("Subject")
        table.add_column("Sent", width=12)
        table.add_column("Note")

        for fu in due:
            table.add_row(
                str(fu.id),
                fu.to_email[:24],
                fu.subject[:50],
                fu.sent_at.strftime("%b %d"),
                fu.note[:40] or "[dim]—[/dim]",
            )
        console.print(table)
        console.print(
            "\n[dim]Options: dismiss with [cyan]mailtrim follow-up --dismiss <ID>[/cyan][/dim]"
        )
        return

    # Track a specific message
    with console.status(f"Fetching message {message_id}..."):
        msg = client.get_message(message_id)

    remind_type = "unconditionally" if unconditional else "only if no reply"
    fu = tracker.track(
        msg,
        remind_in_days=days,
        remind_only_if_no_reply=not unconditional,
    )
    console.print(
        f"[green]Tracking follow-up:[/green] [bold]{msg.headers.subject[:60]}[/bold]\n"
        f"Reminder in {days} days ({remind_type}). ID: [dim]{fu.id}[/dim]"
    )


# ── avoid ─────────────────────────────────────────────────────────────────────


@app.command()
def avoid(
    process: Optional[str] = typer.Option(
        None, "--process", "-p", help="Gmail message ID to act on."
    ),
    action: str = typer.Option(
        "archive",
        "--action",
        "-a",
        help="Action when processing: archive or trash (move to Trash — recoverable).",
    ),
    no_insights: bool = typer.Option(False, "--no-insights", help="Skip AI insight generation."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would happen without acting."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
):
    """
    [EXPERIMENTAL] Show emails you've been putting off — viewed multiple times but never acted on.
    AI explains why you might be avoiding them and suggests one action.

    Requires ai_mode=cloud. Run: mailtrim config ai-mode cloud
    All Trash actions are recoverable for 30 days.

    Examples:
      mailtrim avoid
      mailtrim avoid --no-insights              # faster, no AI
      mailtrim avoid --process <id> --action archive
      mailtrim avoid --process <id> --action trash  # move to Trash
    """
    _require_gmail("avoid")
    from mailtrim.core.ai.mode import require_cloud
    from mailtrim.core.avoidance import AvoidanceDetector

    try:
        require_cloud(get_settings().ai_mode)
    except Exception as exc:
        _handle_error(exc)
    _cloud_ai_warning()
    client = _get_client()
    account_email = _get_account_email(client)
    detector = AvoidanceDetector(client, account_email, _get_ai())

    if process:
        action_display = "Move to Trash" if action == "trash" else action.capitalize()
        if dry_run:
            console.print(f"[dim]Dry run — would {action_display.lower()} message {process}.[/dim]")
            return
        if not yes:
            if not Confirm.ask(f"{action_display} this email?"):
                console.print("[dim]Cancelled.[/dim]")
                return
        effective_action = "delete" if action == "trash" else action
        detector.process(process, effective_action)
        result_msg = "Moved to Trash." if action == "trash" else f"{action.capitalize()}d."
        console.print(f"[green]✓ {result_msg}[/green] Removed from avoidance list.")
        return

    if not no_insights:
        _print_ai_data_notice("email subjects + snippets (≤300 chars each) for insight generation")
    with console.status("Finding avoided emails..."):
        avoided = detector.get_avoided_emails(with_insights=not no_insights)

    if not avoided:
        console.print("[green]No avoided emails detected.[/green] You're on top of things.")
        return

    console.print(f"\n[bold red]Emails you've been avoiding[/bold red] ({len(avoided)} found)\n")

    for ae in avoided:
        panel_content = (
            f"[bold]{ae.record.subject[:70]}[/bold]\n"
            f"From: [cyan]{ae.record.sender_name or ae.record.sender_email}[/cyan]\n"
            f"[dim]Viewed {ae.view_count}× · {ae.days_in_inbox:.0f} days in inbox[/dim]\n\n"
        )
        if ae.ai_insight:
            panel_content += f"[yellow]{ae.ai_insight}[/yellow]\n"
        panel_content += (
            f"\n[dim]mailtrim avoid --process {ae.record.gmail_id} --action archive[/dim]\n"
            f"[dim]mailtrim avoid --process {ae.record.gmail_id} --action trash  "
            "(move to Trash — recoverable)[/dim]"
        )

        console.print(Panel(panel_content, border_style="red", expand=False))


# ── unsubscribe ───────────────────────────────────────────────────────────────


@app.command()
def unsubscribe(
    sender: Optional[str] = typer.Argument(None, help="Sender email to unsubscribe from."),
    from_query: Optional[str] = typer.Option(
        None, "--from-query", "-q", help="Gmail query to find senders to unsubscribe from."
    ),
    no_headless: bool = typer.Option(
        False, "--no-headless", help="Skip Playwright headless fallback."
    ),
    list_history: bool = typer.Option(False, "--history", help="Show unsubscribe history."),
    limit: int = typer.Option(10, "--limit", "-n", help="Max senders to process (default 10)."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be unsubscribed without acting."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip per-sender confirmation prompt."),
):
    """
    Unsubscribe from email senders via List-Unsubscribe headers and headless browser fallback.

    Uses RFC 8058 one-click unsubscribe when available; falls back to mailto and Playwright.
    This action is irreversible — unsubscribing cannot be undone.

    Examples:
      mailtrim unsubscribe newsletters@example.com
      mailtrim unsubscribe --from-query "label:newsletters" --limit 20
      mailtrim unsubscribe --from-query "label:newsletters" --dry-run   # preview first
      mailtrim unsubscribe --history
    """
    _require_gmail("unsubscribe")
    from mailtrim.core.unsubscribe import UnsubscribeEngine

    client = _get_client()
    account_email = _get_account_email(client)
    engine = UnsubscribeEngine(client, account_email)

    if list_history:
        history = engine.get_history()
        table = Table(title="Unsubscribe History", border_style="dim")
        table.add_column("Sender", min_width=30)
        table.add_column("Method", width=16)
        table.add_column("Status", width=10)
        table.add_column("Date", width=12)
        for rec in history[:50]:
            color = "green" if rec.status == "success" else "red"
            table.add_row(
                rec.sender_email,
                rec.method,
                f"[{color}]{rec.status}[/{color}]",
                rec.attempted_at.strftime("%Y-%m-%d") if rec.attempted_at else "",
            )
        console.print(table)
        return

    messages: list = []
    if sender:
        from mailtrim.core.validation import validate_sender_email

        sender = validate_sender_email(sender)
        with console.status(f"Finding emails from {sender}..."):
            ids = client.list_message_ids(query=f"from:{sender}", max_results=1)
            if ids:
                messages = client.get_messages_batch(ids[:1])
    elif from_query:
        with console.status(f"Finding senders matching: {from_query}..."):
            ids = client.list_message_ids(query=from_query, max_results=limit * 3)
            if ids:
                all_msgs = client.get_messages_batch(ids[: limit * 3])
                # Deduplicate by sender
                seen_senders: set[str] = set()
                for msg in all_msgs:
                    if msg.sender_email not in seen_senders and len(messages) < limit:
                        seen_senders.add(msg.sender_email)
                        messages.append(msg)
    else:
        console.print("[red]Error:[/red] Provide a sender email or --from-query.")
        console.print("  [dim]Example: mailtrim unsubscribe newsletters@example.com[/dim]")
        raise typer.Exit(1)

    if not messages:
        console.print("[yellow]No messages found.[/yellow]")
        return

    if dry_run:
        console.print(f"[dim]Dry run — would unsubscribe from {len(messages)} sender(s):[/dim]")
        for msg in messages:
            console.print(f"  [dim]· {msg.sender_email}[/dim]")
        console.print("[dim]Remove --dry-run to execute.[/dim]")
        return

    for msg in messages:
        if not yes:
            if not Confirm.ask(f"Unsubscribe from [bold]{msg.sender_email}[/bold]?"):
                console.print("[dim]Skipped.[/dim]")
                continue
        console.print(f"Unsubscribing from [bold]{msg.sender_email}[/bold]...", end=" ")
        result = engine.unsubscribe(msg, use_headless=not no_headless)
        status = "[green]success[/green]" if result.success else "[red]failed[/red]"
        console.print(f"{status} [dim]({result.method})[/dim]")
        console.print(f"  [dim]{result.message}[/dim]")


# ── rules ─────────────────────────────────────────────────────────────────────


@app.command()
def rules(
    add: Optional[str] = typer.Option(None, "--add", "-a", help="Add a rule in natural language."),
    run: bool = typer.Option(False, "--run", "-r", help="Run all active rules now."),
    list_rules: bool = typer.Option(False, "--list", "-l", help="List all active rules."),
    remove_id: Optional[int] = typer.Option(None, "--remove", help="Deactivate a rule by ID."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview rule execution without making changes."
    ),
):
    """
    Manage recurring automation rules defined in natural language.

    Examples:
      mailtrim rules --add "archive LinkedIn notifications older than 7 days"
      mailtrim rules --add "label as 'receipts' anything from order@* or receipt@*"
      mailtrim rules --run
      mailtrim rules --list
    """
    _require_gmail("rules")
    from mailtrim.core.bulk_engine import BulkEngine
    from mailtrim.core.storage import RuleRepo, get_session

    client = _get_client()
    account_email = _get_account_email(client)
    engine = BulkEngine(client, account_email, _get_ai())
    repo = RuleRepo(get_session())

    if add:
        _print_ai_data_notice("your rule text only (no email content)")
        with console.status("Translating rule with AI..."):
            rule = engine.create_rule(add)

        console.print(
            Panel(
                f"[bold]{rule.name}[/bold]\n\n"
                f"[dim]Gmail query:[/dim] [cyan]{rule.gmail_query}[/cyan]\n"
                f"[dim]Action:[/dim] [yellow]{rule.action}[/yellow]\n"
                f"[dim]Explanation:[/dim] {rule.ai_explanation}",
                title=f"Rule created (ID: {rule.id})",
                border_style="green",
            )
        )
        return

    if remove_id:
        repo.deactivate(remove_id)
        console.print(f"[yellow]Rule {remove_id} deactivated.[/yellow]")
        return

    if list_rules:
        active = repo.list_active(account_email)
        if not active:
            console.print("[yellow]No active rules.[/yellow]")
            return
        table = Table(title="Active Rules", border_style="dim")
        table.add_column("ID", width=5)
        table.add_column("Rule", min_width=40)
        table.add_column("Action", width=12)
        table.add_column("Runs", width=6)
        table.add_column("Last run", width=14)
        for r in active:
            table.add_row(
                str(r.id),
                r.natural_language[:60],
                r.action,
                str(r.run_count),
                r.last_run_at.strftime("%Y-%m-%d") if r.last_run_at else "never",
            )
        console.print(table)
        return

    if run:
        active = repo.list_active(account_email)
        if not active:
            console.print("[yellow]No active rules to run.[/yellow]")
            return

        with Progress(
            SpinnerColumn(), TextColumn("{task.description}"), console=console
        ) as progress:
            t = progress.add_task("Running rules...", total=len(active))
            results = engine.run_rules(dry_run=dry_run)
            progress.update(t, completed=len(active))

        for rule_id, result in results.items():
            prefix = "[dim][DRY RUN][/dim] " if dry_run else ""
            console.print(
                f"{prefix}Rule {rule_id}: [green]{result.affected_count}[/green] messages affected"
            )
        return

    console.print("Use --add, --list, --run, or --remove. See --help for details.")


# ── digest ────────────────────────────────────────────────────────────────────


@app.command()
def digest():
    """
    [EXPERIMENTAL] Generate your weekly inbox digest — insights, action items, and one cleanup suggestion.

    Requires ai_mode=cloud. Sends inbox counts and sender names to Anthropic — no subjects or body content.
    Run: mailtrim config ai-mode cloud

    Examples:
      mailtrim digest
    """
    _require_gmail("digest")
    from mailtrim.core.ai.mode import require_cloud

    try:
        require_cloud(get_settings().ai_mode)
    except Exception as exc:
        _handle_error(exc)
    _cloud_ai_warning()
    from collections import Counter

    from mailtrim.core.avoidance import AvoidanceDetector
    from mailtrim.core.follow_up import FollowUpTracker
    from mailtrim.core.storage import EmailRepo, get_session

    client = _get_client()
    account_email = _get_account_email(client)
    ai = _get_ai()

    session = get_session()
    email_repo = EmailRepo(session)
    tracker = FollowUpTracker(client, account_email)
    detector = AvoidanceDetector(client, account_email, ai)

    with console.status("Gathering inbox data..."):
        records = email_repo.get_inbox(account_email, limit=500)

    # Stats
    unread = sum(1 for r in records if r.is_unread)
    top_senders = [
        {"sender": k, "count": v}
        for k, v in Counter(r.sender_email for r in records).most_common(5)
    ]

    # Follow-ups
    due_fus = tracker.get_due_follow_ups()
    fu_data = [
        {"to": f.to_email, "subject": f.subject, "sent": str(f.sent_at.date())} for f in due_fus[:5]
    ]

    # Avoidance
    avoided_count = detector.get_stats()["total_avoided"]

    inbox_summary = {
        "total_in_inbox": len(records),
        "unread": unread,
        "senders": len(set(r.sender_email for r in records)),
    }

    _print_ai_data_notice("inbox counts + sender names only (no subjects or snippets)")
    with console.status("Generating digest with AI..."):
        summary = ai.generate_digest(inbox_summary, fu_data, avoided_count, top_senders)

    console.print(
        Panel(
            summary,
            title=f"Weekly Digest — {account_email}",
            border_style="blue",
            padding=(1, 2),
        )
    )


# ── Shared post-cleanup output ───────────────────────────────────────────────


def _print_cleanup_complete(
    console: Console,
    freed_mb: float,
    email_count: int,
    sender_names: list[str],
    elapsed_seconds: int,
    permanent: bool,
    undo_id: "int | None",
    share: bool,
    is_gmail: bool = True,
) -> None:
    """
    Print a celebratory completion panel after any purge operation,
    then optionally append a copyable share line.
    """
    from mailtrim.core.sender_stats import generate_share_text

    names_str = ", ".join(sender_names[:3])
    if len(sender_names) > 3:
        names_str += f" + {len(sender_names) - 3} more"

    if permanent:
        body = (
            f"[bold red]Permanently deleted {email_count:,} emails[/bold red] "
            f"from {names_str}\n"
            f"[dim]Freed ~{freed_mb} MB  ·  took {elapsed_seconds}s  ·  No undo possible.[/dim]"
        )
        border = "red"
        title = "🗑  Cleanup Complete"
    else:
        undo_line = (
            f"\n  [bold]Undo anytime:[/bold] [cyan]mailtrim undo {undo_id}[/cyan]"
            if undo_id is not None
            else "\n  [cyan]mailtrim undo[/cyan] — see recent operations"
        )
        gmail_note = (
            "\n[dim]Gmail Trash shows threads, not messages — visible count there will be lower.[/dim]"
            if is_gmail
            else ""
        )
        body = (
            f"[green]✓ Moved {email_count:,} emails to Trash[/green]  ·  "
            f"freed [bold green]~{freed_mb} MB[/bold green]  ·  took [bold]{elapsed_seconds}s[/bold]\n"
            f"Senders: [dim]{names_str}[/dim]" + gmail_note + undo_line
        )
        border = "green"
        title = "🎉  Cleanup Complete"

    console.print()
    console.print(Panel(body, title=f"[bold]{title}", border_style=border, padding=(0, 2)))

    if share:
        share_text = generate_share_text(
            freed_mb=freed_mb,
            sender_count=len(sender_names),
            email_count=email_count,
            elapsed_seconds=elapsed_seconds,
        )
        console.print(f"\n[bold green]Share this:[/bold green]\n  {share_text}\n")


# ── purge ─────────────────────────────────────────────────────────────────────


@app.command()
def purge(
    query: str = typer.Option(
        "category:promotions OR label:newsletters",
        "--query",
        "-q",
        help="Gmail query to scan for unwanted mail.",
    ),
    domain: Optional[str] = typer.Option(
        None,
        "--domain",
        "-d",
        help="Target a specific domain (e.g. linkedin.com). Skips interactive selection.",
    ),
    keep: Optional[int] = typer.Option(
        None,
        "--keep",
        help="Keep the last N emails per sender; move the rest to Trash (recoverable).",
    ),
    older_than: Optional[int] = typer.Option(
        None,
        "--older-than",
        help="Only move to Trash emails older than this many days.",
    ),
    max_scan: int = typer.Option(2000, "--max-scan", help="Max emails to scan."),
    top: int = typer.Option(30, "--top", "-n", help="How many top offenders to show."),
    min_count: int = typer.Option(2, "--min", help="Minimum emails to appear on list."),
    sort_by: str = typer.Option("count", "--sort", "-s", help="Sort by: count | oldest | size"),
    also_unsubscribe: bool = typer.Option(
        False, "--unsub", help="Also unsubscribe from selected senders."
    ),
    permanent: bool = typer.Option(
        False,
        "--permanent",
        help="Permanently delete (skip Trash). IRREVERSIBLE.",
        hidden=True,
    ),
    i_understand_permanent: bool = typer.Option(
        False,
        "--i-understand-permanent",
        help="Required second flag when using --permanent.",
        hidden=True,
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Output sender list as JSON and exit (no deletion)."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
    share: bool = typer.Option(
        False, "--share", help="Print a copyable share summary after deletion."
    ),
    scope: str = typer.Option(
        "inbox",
        "--scope",
        help="Mail scope to scan: 'inbox' (default) or 'anywhere' (includes archived, sent, all mail).",
    ),
    use_ai: bool = typer.Option(
        False,
        "--ai",
        help="[EXPERIMENTAL] Enrich confidence scores with local AI (requires llama.cpp at localhost:8080).",
    ),
    provider: str = typer.Option(
        "", "--provider", help="Email provider: gmail or imap. Defaults to configured provider."
    ),
    imap_server: str = typer.Option("", "--imap-server", help="IMAP server hostname."),
    imap_user: str = typer.Option("", "--imap-user", help="IMAP login username."),
    imap_port: int = typer.Option(993, "--imap-port", help="IMAP SSL port (default 993)."),
    imap_folder: str = typer.Option(
        "INBOX", "--imap-folder", help="IMAP folder to scan (default INBOX)."
    ),
    ai_backend: str = typer.Option(
        "llama",
        "--ai-backend",
        help="Local AI backend: llama (llama.cpp, default) or ollama.",
    ),
    ai_url: str = typer.Option("", "--ai-url", help="Override local AI server URL."),
    ai_model: str = typer.Option("phi3", "--ai-model", help="Model name (Ollama only)."),
    since: str = typer.Option(
        "",
        "--since",
        help="Only scan emails received within the last N days. Format: 30d, 7d.",
    ),
):
    """
    Move top email senders to Trash — with a 30-day undo window.

    Scans your promotions/newsletters, ranks senders, lets you pick which
    ones to move to Trash. Works with Gmail and IMAP providers.
    All deletions are recoverable for 30 days with: mailtrim undo

    Examples:
      mailtrim purge
      mailtrim purge --domain linkedin.com --yes
      mailtrim purge --domain linkedin.com --keep 10
      mailtrim purge --domain linkedin.com --older-than 90
      mailtrim purge --scope anywhere            # scan all mail, not just inbox
      mailtrim purge --since 30d                 # only emails from the last 30 days
    """
    _record("purge")
    import os as _os
    import time as _time

    from mailtrim.core.sender_stats import compute_confidence_score, fetch_sender_groups
    from mailtrim.core.storage import UndoLogRepo, get_session
    from mailtrim.core.unsubscribe import UnsubscribeEngine
    from mailtrim.core.validation import validate_domain, validate_older_than, validate_since

    # Guard: --permanent requires the explicit confirmation flag to prevent accidents.
    if permanent and not i_understand_permanent:
        console.print(
            Panel(
                "[bold red]--permanent requires --i-understand-permanent[/bold red]\n\n"
                "Permanent deletion bypasses Trash and cannot be undone.\n"
                "If you really mean it, add [bold]--i-understand-permanent[/bold] to your command.\n\n"
                "[dim]Tip: omit --permanent to move to Trash instead (recoverable for 30 days).[/dim]",
                border_style="red",
            )
        )
        raise typer.Exit(1)

    # Resolve provider + IMAP settings (CLI flags override persisted settings)
    provider, imap_server, imap_user, imap_port, imap_folder = _resolve_imap_settings(
        provider, imap_server, imap_user, imap_port, imap_folder
    )

    # Resolve IMAP password: env var → interactive prompt (never CLI flag)
    imap_password = _os.environ.get("MAILTRIM_IMAP_PASSWORD", "")
    if provider == "imap" and imap_user and not imap_password:
        imap_password = typer.prompt(f"IMAP password for {imap_user}", hide_input=True, default="")

    client = _get_provider(
        provider=provider,
        imap_server=imap_server,
        imap_user=imap_user,
        imap_password=imap_password,
        imap_port=imap_port,
        imap_folder=imap_folder,
    )
    account_email = _get_account_email(client)

    # Validate user-supplied values before embedding them in Gmail queries.
    if domain:
        domain = validate_domain(domain)
    if older_than is not None:
        older_than = validate_older_than(older_than)

    since_days: int | None = None
    if since:
        try:
            since_days = validate_since(since)
        except typer.BadParameter as exc:
            console.print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(1)

    # --scope prepends a scope filter to the query unless --query is explicitly set
    if scope == "anywhere" and query == "category:promotions OR label:newsletters":
        query = "in:anywhere -in:trash -in:spam"
    elif scope == "anywhere":
        query = f"in:anywhere -in:trash -in:spam {query}"

    # --since appends a date lower bound to whatever query is in use
    if since_days:
        query += f" newer_than:{since_days}d"

    # --domain builds a targeted query and routes to non-interactive mode.
    # Scope filter is always applied so the count matches what stats showed.
    if domain:
        if scope == "anywhere":
            effective_query = f"in:anywhere -in:trash -in:spam from:{domain}"
        else:
            effective_query = f"in:inbox from:{domain}"
        if older_than:
            effective_query += f" older_than:{older_than}d"
        if since_days:
            effective_query += f" newer_than:{since_days}d"
        query = effective_query

    scope_label = "all mail" if scope == "anywhere" else "inbox"

    # ── Step 1: scan and rank ────────────────────────────────────────────────
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        t = progress.add_task(f"Scanning up to {max_scan} emails in {scope_label}…", total=None)
        valid_sorts = ("count", "oldest", "size")
        if sort_by not in valid_sorts:
            console.print(
                f"[red]Invalid --sort value '{sort_by}'. Choose: {', '.join(valid_sorts)}[/red]"
            )
            raise typer.Exit(1)
        groups = fetch_sender_groups(
            client,
            query=query,
            max_messages=max_scan,
            min_count=min_count if not domain else 1,
            top_n=top,
            sort_by=sort_by,
        )
        progress.update(t, description=f"Found {len(groups)} senders.")

    # Filter protected senders before displaying anything
    from mailtrim.core.storage import BlocklistRepo
    from mailtrim.core.storage import get_session as _get_session

    _blocked = BlocklistRepo(_get_session()).blocked_emails(account_email)
    if _blocked:
        before = len(groups)
        groups = [g for g in groups if g.sender_email not in _blocked]
        filtered_count = before - len(groups)
        if filtered_count:
            console.print(
                f"[dim]({filtered_count} protected sender(s) hidden — "
                "mailtrim protect --list to manage)[/dim]"
            )

    if not groups:
        console.print("[yellow]No matching emails found.[/yellow]")
        if scope == "inbox":
            console.print(
                "[dim]Tip: run [bold]mailtrim purge --scope anywhere[/bold] "
                "to include archived and sent mail.[/dim]"
            )
        return

    # Coverage hints — shown once, before the table
    scanned_count = sum(g.count for g in groups)
    if scanned_count >= max_scan:
        console.print(
            f"[yellow]⚠ Scan capped at {max_scan:,} emails. "
            f"Run with [bold]--max-scan 5000[/bold] to surface more senders.[/yellow]"
        )
    if scope == "inbox":
        console.print(
            "[dim]Scoped to inbox — use [bold]--scope anywhere[/bold] "
            "to include archived and sent mail.[/dim]"
        )

    # ── Domain mode: --keep N trims to last N per sender ─────────────────────
    if domain and keep is not None:
        trimmed_groups = []
        for g in groups:
            if g.count <= keep:
                continue  # already within limit
            # Sort message_ids by internal_date desc (latest first) via metadata we have
            # We have message_ids but not dates per-id — trim by total count heuristic:
            # keep the last `keep` message_ids (they were accumulated in order of API response)
            to_delete = g.message_ids[:-keep] if keep > 0 else g.message_ids
            if to_delete:
                from dataclasses import replace

                trimmed_groups.append(
                    replace(
                        g,
                        message_ids=to_delete,
                        count=len(to_delete),
                    )
                )
        if not trimmed_groups:
            console.print(
                f"[yellow]All senders from {domain} already have ≤{keep} emails.[/yellow]"
            )
            return
        groups = trimmed_groups

    # JSON output mode — print data and exit without interactive deletion
    if json_output:
        import json as json_lib

        data = [
            {
                "rank": i,
                "sender_email": g.sender_email,
                "sender_name": g.sender_name,
                "count": g.count,
                "size_mb": g.total_size_mb,
                "oldest_email": g.earliest_date.isoformat(),
                "latest_email": g.latest_date.isoformat(),
                "has_unsubscribe": g.has_unsubscribe,
                "sample_subjects": g.sample_subjects,
            }
            for i, g in enumerate(groups, 1)
        ]
        console.print_json(json_lib.dumps(data))
        return

    total_msgs = sum(g.count for g in groups)
    total_mb = round(sum(g.total_size_bytes for g in groups) / (1024 * 1024), 1)

    # ── Domain mode: skip table + interactive selection, auto-select all ──────
    if domain:
        keep_note = f" (keeping last {keep})" if keep is not None else ""
        console.print(
            f"\n[bold]Domain target:[/bold] [cyan]{domain}[/cyan]  "
            f"[dim]{total_msgs} emails · {total_mb} MB{keep_note}[/dim]"
        )

        # Show a compact per-sender breakdown so the user sees exactly which
        # addresses (including subdomains) will be affected before confirming.
        # Gmail's from: query is a suffix match, so mail.domain.com is included.
        unique_domains = sorted({g.domain for g in groups})
        if len(unique_domains) > 1:
            console.print(f"[dim]  Includes subdomains: {', '.join(unique_domains)}[/dim]")
        for g in sorted(groups, key=lambda x: x.count, reverse=True)[:10]:
            size_str = (
                f"{g.total_size_mb} MB"
                if g.total_size_mb >= 0.1
                else f"{g.total_size_bytes // 1024} KB"
            )
            console.print(
                f"  [dim]{g.sender_email}[/dim]  [red]{g.count}[/red] emails · {size_str}"
            )
        if len(groups) > 10:
            console.print(f"  [dim]… and {len(groups) - 10} more sender(s)[/dim]")
        console.print()

        selected = groups
        sel_msgs = total_msgs
        sel_mb = total_mb
        # Jump directly to Step 4 (confirm + execute)
        if not yes:
            confirmed = Confirm.ask(
                f"Delete {sel_msgs} emails from {domain}? "
                f"(undo available for {get_settings().undo_window_days} days)"
            )
            if not confirmed:
                console.print("[dim]Cancelled.[/dim]")
                return
        all_ids = [mid for g in selected for mid in g.message_ids]
        _t0 = _time.monotonic()
        with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as pr:
            t2 = pr.add_task(f"Deleting {sel_msgs} emails…", total=len(selected))
            for g in selected:
                client.batch_trash(
                    g.message_ids
                ) if not permanent else client.batch_delete_permanent(g.message_ids)
                pr.advance(t2)
        elapsed = round(_time.monotonic() - _t0)

        domain_undo_id: int | None = None
        if not permanent:
            _undo_repo = UndoLogRepo(get_session())
            _undo_entry = _undo_repo.record(
                account_email=account_email,
                operation="trash",
                message_ids=all_ids,
                description=f"Purge domain: {domain}",
                metadata={"senders": [g.sender_email for g in selected]},
            )
            domain_undo_id = _undo_entry.id

        _print_cleanup_complete(
            console=console,
            freed_mb=sel_mb,
            email_count=sel_msgs,
            sender_names=[domain],
            elapsed_seconds=elapsed,
            permanent=permanent,
            undo_id=domain_undo_id,
            share=share,
            is_gmail=(provider == "gmail"),
        )
        return

    # ── Step 2: render offender table ────────────────────────────────────────

    # Optional local-AI enrichment — only for senders where AI adds signal.
    purge_ai_insights: dict[str, dict] = {}
    if use_ai:
        from mailtrim.core.ai.mode import require_local

        require_local(get_settings().ai_mode)
        from mailtrim.core.llm import (
            analyze_batch,
            confidence_delta,
            should_analyze,
        )

        eligible_groups = [
            g
            for g in groups
            if should_analyze(compute_confidence_score(g), g.count, g.sender_email)
        ]
        if eligible_groups:
            with console.status("[dim][AI] Analyzing senders via local model…[/dim]"):
                texts = [
                    f"From: {g.sender_name or g.sender_email}\n" + "\n".join(g.sample_subjects[:3])
                    for g in eligible_groups
                ]
                keys = [g.sender_email for g in eligible_groups]
                _purge_ai_client = _get_ai_client_opt(ai_backend, ai_url, ai_model)
                ai_results = analyze_batch(texts, cache_keys=keys, ai_client=_purge_ai_client)
            purge_ai_insights = {g.sender_email: r for g, r in zip(eligible_groups, ai_results)}

        if any(purge_ai_insights.values()):
            from mailtrim.core.llm import apply_impact_nudge

            apply_impact_nudge(groups, purge_ai_insights)
        elif eligible_groups:
            _expected = ai_url or (
                "http://localhost:11434" if ai_backend == "ollama" else "http://localhost:8080"
            )
            console.print(
                f"\n[yellow]⚠ Local AI unavailable[/yellow] — confidence scores not adjusted.\n"
                f"  Is [bold]{'Ollama' if ai_backend == 'ollama' else 'llama.cpp'}[/bold] running?"
                f" Expected at {_expected}\n"
            )

    console.print()
    table = Table(
        title=f"Top Email Offenders  [dim]({total_msgs} emails · {total_mb} MB)[/dim]",
        show_header=True,
        header_style="bold cyan",
        border_style="dim",
        show_lines=False,
        expand=True,
    )
    table.add_column("#", width=4, style="dim")
    table.add_column("Sender", min_width=28, no_wrap=True)
    table.add_column("Emails", width=8, justify="right", style="red bold")
    table.add_column("Size", width=8, justify="right")
    # Date column header and value depend on sort mode
    if sort_by == "oldest":
        date_header, date_style = "Oldest email", "yellow"
    elif sort_by == "size":
        date_header, date_style = "Latest", "dim"
    else:
        date_header, date_style = "Latest", "dim"
    table.add_column(date_header, width=14)
    table.add_column("Sample subject", min_width=30)
    table.add_column("Unsub?", width=7)
    if use_ai:
        table.add_column("AI", width=12)

    for i, g in enumerate(groups, 1):
        name = g.display_name[:30]
        if g.sender_email not in name:
            name += f" [dim]<{g.sender_email[:28]}>[/dim]"
        size_str = (
            f"{g.total_size_mb}MB" if g.total_size_mb >= 0.1 else f"{g.total_size_bytes // 1024}KB"
        )

        if sort_by == "oldest":
            # Show how long ago the first email arrived + the actual date
            days = g.inbox_days
            age = f"{days}d ago" if days < 365 else f"{days // 365}y {days % 365 // 30}m ago"
            date_str = f"[{date_style}]{age}[/{date_style}]"
        else:
            date_str = f"[{date_style}]{g.latest_date.strftime('%b %d')}[/{date_style}]"

        subject = g.sample_subjects[0][:55] if g.sample_subjects else "—"
        unsub = "[green]yes[/green]" if g.has_unsubscribe else "[dim]no[/dim]"

        if use_ai:
            ai = purge_ai_insights.get(g.sender_email, {})
            if ai:
                from mailtrim.core.llm import CATEGORY_ICON as _ICON

                cat = ai.get("category", "")
                action = ai.get("action", "")
                rule_conf = compute_confidence_score(g)
                delta = confidence_delta(ai)
                adj_conf = max(0, min(95, rule_conf + delta))
                sign = "+" if delta > 0 else ""
                delta_str = f"{sign}{delta}%" if delta != 0 else ""
                ai_cell = (
                    f"{_ICON.get(cat, '')} {cat} → {action}\n"
                    f"[dim]{rule_conf}→{adj_conf}% {delta_str}[/dim]"
                )
            else:
                ai_cell = "[dim]—[/dim]"
            table.add_row(str(i), name, str(g.count), size_str, date_str, subject, unsub, ai_cell)
        else:
            table.add_row(str(i), name, str(g.count), size_str, date_str, subject, unsub)

    console.print(table)

    # ── Step 3: interactive selection ────────────────────────────────────────
    console.print(
        "\n[bold]Select senders to delete.[/bold] "
        "Enter numbers separated by commas, ranges (e.g. [cyan]1-5[/cyan]), "
        "[cyan]all[/cyan], or [cyan]q[/cyan] to quit.\n"
    )

    raw = Prompt.ask("Your selection").strip()
    if raw.lower() in ("q", "quit", ""):
        console.print("[dim]Cancelled.[/dim]")
        return

    selected_indices = _parse_selection(raw, len(groups))
    if not selected_indices:
        console.print("[yellow]No valid selection. Cancelled.[/yellow]")
        return

    selected = [groups[i] for i in selected_indices]
    sel_msgs = sum(g.count for g in selected)
    sel_mb = round(sum(g.total_size_bytes for g in selected) / (1024 * 1024), 1)

    # ── Step 4: confirm ──────────────────────────────────────────────────────
    console.print(
        f"\n[bold]Selected {len(selected)} sender(s) — {sel_msgs} emails ({sel_mb} MB):[/bold]"
    )
    for g in selected:
        unsub_note = " [dim]+unsubscribe[/dim]" if also_unsubscribe and g.has_unsubscribe else ""
        console.print(
            f"  [red]✕[/red] {g.display_name[:40]} [dim]({g.count} emails)[/dim]{unsub_note}"
        )

    if permanent:
        console.print(
            Panel(
                f"[bold red]⚠  PERMANENT DELETION — THIS CANNOT BE UNDONE  ⚠[/bold red]\n\n"
                f"You are about to [bold]permanently erase {sel_msgs} emails[/bold] "
                f"from {len(selected)} sender(s).\n"
                f"They will [bold]NOT[/bold] go to Trash. There is [bold]no recovery, no undo[/bold].\n\n"
                f"[dim]Omit --permanent to move to Trash instead (recoverable for 30 days).[/dim]",
                border_style="red",
                title="[bold red]IRREVERSIBLE ACTION",
            )
        )
        if not yes:
            # Require typing "DELETE FOREVER" to confirm — not just Enter
            answer = Prompt.ask(
                '[bold red]Type "DELETE FOREVER" to confirm, or anything else to cancel[/bold red]'
            )
            if answer.strip() != "DELETE FOREVER":
                console.print("[dim]Cancelled.[/dim]")
                return
    else:
        if not yes:
            confirmed = Confirm.ask(
                f"\nMove {sel_msgs} emails to Trash? "
                f"(undo available for {get_settings().undo_window_days} days)"
            )
            if not confirmed:
                console.print("[dim]Cancelled.[/dim]")
                return

    # ── Step 5: execute ──────────────────────────────────────────────────────
    all_ids = [mid for g in selected for mid in g.message_ids]
    sender_desc = (
        f"{', '.join(g.sender_email for g in selected[:5])}{'...' if len(selected) > 5 else ''}"
    )

    deleted = 0
    _t0 = _time.monotonic()
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        t = progress.add_task("Deleting…", total=len(selected))
        for g in selected:
            if permanent:
                client.batch_delete_permanent(g.message_ids)
            else:
                client.batch_trash(g.message_ids)
            deleted += g.count
            progress.advance(t)
    elapsed = round(_time.monotonic() - _t0)

    undo_id: int | None = None
    if not permanent:
        undo_repo = UndoLogRepo(get_session())
        undo_entry = undo_repo.record(
            account_email=account_email,
            operation="trash",
            message_ids=all_ids,
            description=f"Purge: {sender_desc}",
            metadata={"senders": [g.sender_email for g in selected]},
        )
        undo_id = undo_entry.id

    freed_mb = round(sum(g.total_size_bytes for g in selected) / (1024 * 1024), 1)
    sender_names = [g.display_name[:30] for g in selected[:3]]

    if not permanent:
        try:
            from mailtrim.core.usage_stats import record_emails_trashed

            record_emails_trashed(deleted)
        except Exception:
            pass

    _print_cleanup_complete(
        console=console,
        freed_mb=freed_mb,
        email_count=deleted,
        sender_names=sender_names,
        elapsed_seconds=elapsed,
        permanent=permanent,
        undo_id=undo_id,
        share=share,
        is_gmail=(provider == "gmail"),
    )

    # ── Step 6: optional unsubscribe ─────────────────────────────────────────
    if also_unsubscribe:
        unsub_engine = UnsubscribeEngine(client, account_email)
        to_unsub = [g for g in selected if g.has_unsubscribe]
        if to_unsub:
            console.print(f"\n[bold]Unsubscribing from {len(to_unsub)} sender(s)…[/bold]")
            for g in to_unsub:
                # Get one message from this sender to extract the header
                ids = client.list_message_ids(query=f"from:{g.sender_email}", max_results=1)
                if ids:
                    msg = client.get_message(ids[0])
                    result = unsub_engine.unsubscribe(msg)
                    status = "[green]ok[/green]" if result.success else "[red]failed[/red]"
                    console.print(f"  {status} {g.sender_email} [dim]({result.method})[/dim]")


# ── protect ───────────────────────────────────────────────────────────────────


@app.command()
def protect(
    sender: Optional[str] = typer.Argument(None, help="Sender email to protect from purge."),
    remove: Optional[str] = typer.Option(
        None, "--remove", "-r", help="Remove a sender from the protected list."
    ),
    list_protected: bool = typer.Option(False, "--list", "-l", help="List all protected senders."),
):
    """
    Protect a sender from future purge operations.

    Protected senders are hidden from the purge list entirely.
    Add a sender after an accidental purge (undo also prompts you).

    Examples:
      mailtrim protect invoices@mybank.com
      mailtrim protect --list
      mailtrim protect --remove invoices@mybank.com
    """
    from mailtrim.core.storage import BlocklistRepo, get_session

    client = _get_client()
    account_email = _get_account_email(client)
    repo = BlocklistRepo(get_session())

    if list_protected:
        entries = repo.list_all(account_email)
        if not entries:
            console.print("[yellow]No protected senders.[/yellow]")
            console.print("[dim]Add one with: mailtrim protect <email>[/dim]")
            return
        table = Table(title="Protected Senders", border_style="dim")
        table.add_column("Sender", min_width=35)
        table.add_column("Reason", width=18)
        table.add_column("Protected since", width=16)
        for e in entries:
            table.add_row(
                e.sender_email,
                e.reason.replace("_", " "),
                e.created_at.strftime("%Y-%m-%d"),
            )
        console.print(table)
        console.print("\nRemove with [cyan]mailtrim protect --remove <email>[/cyan]")
        return

    if remove:
        removed = repo.remove(account_email, remove)
        if removed:
            console.print(f"[green]Removed[/green] {remove} from the protected list.")
        else:
            console.print(f"[yellow]{remove} was not in the protected list.[/yellow]")
        return

    if not sender:
        console.print("Provide a sender email, or use --list / --remove. See --help.")
        raise typer.Exit(1)

    repo.add(account_email, sender)
    console.print(
        f"[green]Protected:[/green] [bold]{sender}[/bold]\n"
        "[dim]This sender will no longer appear in purge lists.[/dim]"
    )


# ── doctor ────────────────────────────────────────────────────────────────────


@app.command()
def doctor(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show full error details."),
    ai: bool = typer.Option(False, "--ai", help="Also check local AI endpoint."),
    provider: str = typer.Option(
        "",
        "--provider",
        help="Email provider to check: gmail or imap. Defaults to configured provider.",
    ),
    imap_server: str = typer.Option("", "--imap-server", help="IMAP server hostname."),
    imap_user: str = typer.Option("", "--imap-user", help="IMAP login username."),
    imap_port: int = typer.Option(993, "--imap-port", help="IMAP SSL port (default 993)."),
):
    """
    Check that mailtrim is configured correctly and ready to use.

    Verifies auth, connection, storage, and optional AI endpoint.
    Uses the persisted provider from setup — no flags required after first run.

    Examples:
      mailtrim doctor
      mailtrim doctor --ai        # also check local AI endpoint
      mailtrim doctor --provider imap   # force IMAP checks (reads persisted server/user)
    """
    import os as _os

    from mailtrim.core.diagnostics import run_all, run_imap_checks

    # Resolve provider + IMAP settings (CLI flags override persisted settings)
    provider, imap_server, imap_user, imap_port, _ = _resolve_imap_settings(
        provider, imap_server, imap_user, imap_port
    )

    console.print()
    console.print(
        Panel.fit(
            f"[bold cyan]mailtrim doctor[/bold cyan]  [dim]v{__version__}[/dim]\n"
            f"[dim]Running system checks… (provider: {provider})[/dim]",
            border_style="cyan",
        )
    )
    console.print()

    if provider == "imap":
        imap_password = _os.environ.get("MAILTRIM_IMAP_PASSWORD", "")
        if imap_user and not imap_password:
            imap_password = typer.prompt(
                f"IMAP password for {imap_user}", hide_input=True, default=""
            )
        results = run_imap_checks(
            server=imap_server,
            user=imap_user,
            password=imap_password,
            port=imap_port,
        )
    else:
        results = run_all(include_optional=ai)

    required_ok = 0
    required_fail = 0
    optional_warn = 0

    for r in results:
        if r.ok:
            icon = "[green]✓[/green]"
            if not r.optional:
                required_ok += 1
        elif r.optional:
            icon = "[yellow]⚠[/yellow]"
            optional_warn += 1
        else:
            icon = "[red]✗[/red]"
            required_fail += 1

        console.print(f"  {icon}  {r.name}")
        if not r.ok:
            console.print(f"     [dim]{r.message}[/dim]")
            if r.fix:
                console.print(f"     [cyan]Fix: {r.fix}[/cyan]")
        elif verbose:
            console.print(f"     [dim]{r.message}[/dim]")

    console.print()

    from mailtrim.core.ai.mode import ai_status_line

    _dr_ai_label, _dr_ai_note, _dr_ai_color = ai_status_line(get_settings().ai_mode)
    console.print(
        f"  [{_dr_ai_color}]AI: {_dr_ai_label}[/{_dr_ai_color}]  [dim]{_dr_ai_note}[/dim]"
    )
    console.print()

    if required_fail == 0:
        status_text = "[bold green]Ready[/bold green]"
        border = "green"
        hint = "You're all set — try: mailtrim quickstart"
    else:
        status_text = "[bold red]Needs Attention[/bold red]"
        border = "red"
        hint = "Fix the issues above, then re-run: mailtrim doctor"

    if optional_warn:
        note = f"  [dim]{optional_warn} optional check(s) not passing — these won't block usage.[/dim]\n"
    else:
        note = ""

    console.print(
        Panel(
            f"Overall status: {status_text}\n{note}  [dim]{hint}[/dim]",
            border_style=border,
            padding=(0, 2),
        )
    )
    console.print()

    if required_fail > 0:
        raise typer.Exit(1)


# ── version ───────────────────────────────────────────────────────────────────


@app.command(name="config")
def config_cmd(
    key: str = typer.Argument(..., help="Config key to set. Currently supported: ai-mode"),
    value: str = typer.Argument(..., help="Value to set. For ai-mode: off | local | cloud"),
):
    """
    Set a persistent configuration value.

    Examples:
      mailtrim config ai-mode off     # disable all AI (default, privacy-safe)
      mailtrim config ai-mode local   # allow local AI only (Ollama, llama.cpp)
      mailtrim config ai-mode cloud   # allow Anthropic cloud AI
    """
    if key != "ai-mode":
        console.print(f"[red]Unknown config key '[bold]{key}[/bold]'.[/red]")
        console.print("  Supported keys: ai-mode")
        raise typer.Exit(1)

    from mailtrim.core.ai.mode import validate_mode

    try:
        validate_mode(value)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    # Persist to ~/.mailtrim/.env which Settings already reads.
    env_path = DATA_DIR / ".env"
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    key_line = f"MAILTRIM_AI_MODE={value}"
    updated = [line for line in lines if not line.startswith("MAILTRIM_AI_MODE=")]
    updated.append(key_line)
    env_path.write_text("\n".join(updated) + "\n")

    if value == "cloud":
        console.print(
            Panel(
                "[green]ai_mode set to [bold]cloud[/bold][/green]\n\n"
                "[yellow]Warning:[/yellow] Cloud AI sends email subjects and snippets\n"
                "to Anthropic's servers. See anthropic.com/privacy for details.\n\n"
                "Requires [bold]ANTHROPIC_API_KEY[/bold] to be set.",
                border_style="yellow",
            )
        )
    elif value == "local":
        console.print(
            Panel(
                "[green]ai_mode set to [bold]local[/bold][/green]\n\n"
                "Local AI runs entirely on your machine — nothing leaves it.\n"
                "Requires Ollama or llama.cpp to be running.\n\n"
                "Try:  mailtrim stats --ai",
                border_style="green",
            )
        )
    else:
        console.print(
            Panel(
                "[green]ai_mode set to [bold]off[/bold][/green]\n\n"
                "All AI features are disabled. Core commands (stats, purge, undo)\n"
                "work fully without AI.",
                border_style="dim",
            )
        )


@app.command()
def privacy():
    """Show what data mailtrim stores and whether any leaves your machine."""
    from mailtrim.config import (
        CREDENTIALS_PATH,
        DATA_DIR,
        DB_PATH,
        TOKEN_PATH,
        UNDO_LOG_DIR,
    )
    from mailtrim.core.usage_stats import get_stats

    settings = get_settings()
    ai_mode = settings.ai_mode

    # ── AI mode line ──────────────────────────────────────────────────────────
    if ai_mode == "off":
        ai_label = "[green]OFF[/green]"
        ai_note = "No email data leaves your machine."
        ai_color = "green"
    elif ai_mode == "local":
        ai_label = "[cyan]LOCAL[/cyan]"
        ai_note = "Processed locally — nothing sent externally."
        ai_color = "cyan"
    else:  # cloud
        ai_label = "[yellow]CLOUD[/yellow]"
        ai_note = "May send email subjects and snippets to Anthropic."
        ai_color = "yellow"

    # ── Usage stats ───────────────────────────────────────────────────────────
    stats = get_stats()
    usage_enabled = (DATA_DIR / "usage.json").exists()
    runs = stats.get("total_runs", 0)
    trashed = stats.get("emails_trashed", 0)

    console.print()
    console.print(
        Panel.fit(
            "[bold]mailtrim privacy report[/bold]",
            border_style="cyan",
        )
    )

    console.print()
    console.print("[bold]What is stored locally[/bold]")
    console.print()

    rows = [
        ("OAuth token", TOKEN_PATH, "Lets mailtrim access Gmail. Never shared."),
        ("OAuth credentials", CREDENTIALS_PATH, "Your Google Cloud app credentials. Never shared."),
        ("Email database", DB_PATH, "Local cache of metadata (no email body stored)."),
        ("Undo logs", UNDO_LOG_DIR, "History of cleanup operations for undo."),
        ("Usage stats", DATA_DIR / "usage.json", "Local run counts. Never uploaded."),
        ("Config / env", DATA_DIR / ".env", "Your settings (API keys, ai_mode, etc.)."),
    ]

    for label, path, note in rows:
        exists = "[green]exists[/green]" if Path(path).exists() else "[dim]not created yet[/dim]"
        console.print(f"  [bold]{label:<20}[/bold]  {exists}")
        console.print(f"  [dim]  {path}[/dim]")
        console.print(f"  [dim]  {note}[/dim]")
        console.print()

    console.print("[bold]AI mode[/bold]")
    console.print()
    console.print(f"  Current mode:  {ai_label}")
    console.print(f"  [{ai_color}]{ai_note}[/{ai_color}]")
    console.print()
    console.print("  Change with:  [cyan]mailtrim config ai-mode [off|local|cloud][/cyan]")

    console.print()
    console.print("[bold]Usage stats (local only)[/bold]")
    console.print()
    if usage_enabled:
        console.print(f"  [green]Enabled[/green] — stored in {DATA_DIR / 'usage.json'}")
        console.print(f"  {runs:,} total runs · {trashed:,} emails trashed")
        console.print("  [dim]Never uploaded. Delete the file to reset.[/dim]")
    else:
        console.print("  [dim]Not yet created (runs after first command).[/dim]")

    console.print()
    console.print("[bold]What never happens[/bold]")
    console.print()
    console.print("  [green]✓[/green]  No telemetry or analytics")
    console.print("  [green]✓[/green]  No email body content ever stored or sent")
    console.print("  [green]✓[/green]  No account data shared with mailtrim project")
    console.print("  [green]✓[/green]  OAuth token stored locally at chmod 600")
    console.print()


@app.command()
def version():
    """Print version."""
    console.print(f"mailtrim {__version__}")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_selection(raw: str, max_index: int) -> list[int]:
    """
    Parse a selection string like "1,3,5-8,all" into 0-based indices.
    Input numbers are 1-based (as displayed to the user).
    """
    indices: set[int] = set()
    if raw.lower() == "all":
        return list(range(max_index))

    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            try:
                lo, hi = part.split("-", 1)
                for n in range(int(lo), int(hi) + 1):
                    if 1 <= n <= max_index:
                        indices.add(n - 1)
            except ValueError:
                pass
        else:
            try:
                n = int(part)
                if 1 <= n <= max_index:
                    indices.add(n - 1)
            except ValueError:
                pass

    return sorted(indices)


# ── Entry point ───────────────────────────────────────────────────────────────


if __name__ == "__main__":
    app()
