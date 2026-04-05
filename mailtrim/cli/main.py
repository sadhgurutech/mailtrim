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


def _get_ai():
    from mailtrim.core.mock_ai import get_ai_engine

    return get_ai_engine()


def _print_ai_data_notice(what: str) -> None:
    """Print a visible, per-command disclosure of what data is sent to Anthropic."""
    console.print(
        f"[bold yellow][AI][/bold yellow] Sending to Anthropic: {what}. "
        "[dim]No full email bodies. Details: PRIVACY.md[/dim]"
    )


def _get_account_email(client) -> str:
    return client.get_email_address()


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
    """Authenticate with Gmail (opens browser for OAuth consent)."""
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
    json_output: bool = typer.Option(False, "--json", help="Output as JSON (for scripting)."),
    scope: str = typer.Option(
        "inbox",
        "--scope",
        help="Mail scope to scan: 'inbox' (default) or 'anywhere' (includes archived, sent, all mail).",
    ),
):
    """
    Inbox decision engine — reclaimable space, confidence-scored recommendations, top senders.

    Tells you exactly what to delete, why it's safe, and how long it will take.
    No AI required.

    Examples:
      mailtrim stats
      mailtrim stats --sort size
      mailtrim stats --share   # get a copyable one-liner to share
    """
    import json as json_lib
    import time as _time

    from mailtrim.core.sender_stats import (
        compute_confidence_score,
        confidence_reason,
        confidence_safety_label,
        fetch_sender_groups,
        format_time_estimate,
        generate_headline_insight,
        generate_insights,
        generate_recommendations,
        generate_viral_share_text,
        group_by_domain,
        impact_label,
        quick_win,
        reclaimable_mb,
        reclaimable_pct,
        risk_tier_icon,
    )

    if scope == "anywhere":
        mail_query = "in:anywhere -in:trash -in:spam"
        scope_label = "all mail"
    else:
        mail_query = "in:inbox"
        scope_label = "inbox"

    scan_start = _time.time()
    client = _get_client()

    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as p:
        t = p.add_task(f"Scanning {scope_label}…", total=None)
        profile = client.get_profile()
        p.update(t, description="Fetching top senders…")
        groups = fetch_sender_groups(
            client,
            query=mail_query,
            max_messages=2000,
            min_count=1,
            top_n=top_n,
            sort_by=sort_by if sort_by in ("score", "count", "size", "oldest") else "score",
        )
        p.update(t, description="Scoring recommendations…")
        domain_groups = group_by_domain(groups)
        insights = generate_insights(groups, domain_groups)
        recommendations = generate_recommendations(groups, top_n=3)
        win = quick_win(recommendations)
        total_reclaimable = reclaimable_mb(recommendations)
        reclaim_pct = reclaimable_pct(total_reclaimable, insights.total_size_mb)

    scan_elapsed = int(_time.time() - scan_start)
    total_messages = profile.get("messagesTotal", 0)
    total_threads = profile.get("threadsTotal", 0)
    account_email = profile.get("emailAddress", "")

    # ── Share mode ────────────────────────────────────────────────────────────
    if share:
        rec_email_count = sum(rec.sender.count for rec in recommendations)
        viral = generate_viral_share_text(
            freed_mb=total_reclaimable,
            sender_count=len(recommendations),
            email_count=rec_email_count,
            reclaim_pct=reclaim_pct,
            elapsed_seconds=scan_elapsed,
        )
        console.print()
        console.print(
            Panel(
                viral,
                title="[bold green]Share mailtrim",
                border_style="green",
                padding=(0, 2),
            )
        )
        console.print(
            "[dim]  Copy the text above — paste it to Twitter, Slack, or a team chat.[/dim]\n"
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

    first_run = _is_first_stats_run()
    console.print()
    if first_run:
        console.print(
            f"  [bold cyan]✨ First scan complete — analyzing your inbox patterns[/bold cyan]  "
            f"[dim]({insights.total_scanned:,} emails · {insights.unique_senders} senders)[/dim]"
        )
    else:
        console.print(
            f"[dim]  ✨ Scan complete — analyzed {insights.total_scanned:,} emails "
            f"across {insights.unique_senders} senders in {scan_elapsed}s[/dim]"
        )
    console.print()

    # ── Headline Insight ──────────────────────────────────────────────────────
    headline = generate_headline_insight(
        insights=insights,
        reclaim_pct=reclaim_pct,
        rec_count=len(recommendations),
        reclaimable_mb_val=total_reclaimable,
    )
    console.print(f"  {headline}")
    console.print()

    # ── Reclaimable Space Banner ──────────────────────────────────────────────
    if total_reclaimable > 0:
        pct_str = f" ({reclaim_pct}% of scanned inbox)" if reclaim_pct > 0 else ""
        total_rec_emails = sum(rec.sender.count for rec in recommendations)
        time_est = format_time_estimate(total_rec_emails)
        console.print(
            Panel(
                f"[bold green]You can safely free ~{total_reclaimable} MB{pct_str}[/bold green]\n"
                f"[dim]from your top {len(recommendations)} sender(s)  ·  "
                f"Each cleanup takes {time_est}  ·  All deletions go to Trash — undo anytime[/dim]",
                title="[bold]TOTAL RECLAIMABLE SPACE",
                border_style="green",
                padding=(0, 2),
            )
        )
        console.print()

    # ── Section 1: Account Summary ────────────────────────────────────────────
    console.rule("[bold cyan]=== ACCOUNT SUMMARY ===", align="left")
    console.print(
        f"  [bold]{account_email}[/bold]\n"
        f"  [dim]Total messages:[/dim]  {total_messages:,}   "
        f"[dim]Total threads:[/dim] {total_threads:,}\n"
        f"  [dim]Inbox scanned:[/dim]   {insights.total_scanned:,} messages  ·  "
        f"[bold]{insights.total_size_mb} MB[/bold]\n"
        f"  [dim]Unique senders:[/dim]  {insights.unique_senders}   "
        f"[dim]Unique domains:[/dim] {insights.unique_domains}   "
        f"[dim]Oldest email:[/dim] {insights.oldest_email_days}d ago"
    )
    console.print()

    # ── Section 2: Key Insights ───────────────────────────────────────────────
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

    # ── Quick Win ─────────────────────────────────────────────────────────────
    if win and win.actions:
        first_action = win.actions[0]
        safety = confidence_safety_label(win.confidence)
        icon = risk_tier_icon(win.confidence)
        reason = confidence_reason(win.sender)
        time_est = format_time_estimate(win.sender.count)
        safety_color = (
            "green" if win.confidence >= 70 else "yellow" if win.confidence >= 40 else "red"
        )
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
                f"[dim]{win.sender.count} emails · {win.sender.total_size_mb} MB[/dim]\n\n"
                + savings_line
                + f"\n  [cyan]{first_action.command}[/cyan]\n\n"
                f"  {icon} Confidence: [bold]{win.confidence}%[/bold]  ·  "
                f"[{safety_color}]{safety}[/{safety_color}]"
                + (f"\n  [dim]Why: {reason}[/dim]" if reason != "limited signals" else ""),
                title="[bold yellow]⚡ QUICK WIN",
                border_style="yellow",
                padding=(0, 2),
            )
        )
        console.print()

    # ── Section 3: Top Senders ────────────────────────────────────────────────
    console.rule("[bold cyan]=== TOP SENDERS ===", align="left")

    if groups:
        table = Table(border_style="dim", show_header=True, header_style="bold", pad_edge=False)
        table.add_column("#", width=3, style="dim")
        table.add_column("Impact", width=12)
        table.add_column("Sender", min_width=28)
        table.add_column("Emails", justify="right", style="red bold", width=7)
        table.add_column("Size", justify="right", width=9)
        table.add_column("Oldest", width=12)
        table.add_column("Risk", width=15)
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
            conf = compute_confidence_score(g)
            risk_icon = risk_tier_icon(conf)
            safety = confidence_safety_label(conf)
            risk_color = "green" if conf >= 70 else "yellow" if conf >= 40 else "red"
            risk_cell = f"{risk_icon} [{risk_color}]{safety}[/{risk_color}]"
            unsub = "[green]✓[/green]" if g.has_unsubscribe else "[dim]–[/dim]"
            table.add_row(
                str(i), score_cell, name, str(g.count), size_str, g.age_str, risk_cell, unsub
            )

        console.print(table)
        console.print(
            "[dim]  Impact = 60% storage + 40% volume (0–100)  ·  "
            "Risk: 🟢 Safe  🟡 Low risk  🔴 Review first  ·  Unsub = List-Unsubscribe header[/dim]"
        )
    else:
        console.print("  [dim]No senders found matching the query.[/dim]")
    console.print()

    # ── Section 4: Recommended Actions ───────────────────────────────────────
    console.rule("[bold cyan]=== RECOMMENDED ACTIONS ===", align="left")

    if recommendations:
        for i, rec in enumerate(recommendations, 1):
            g = rec.sender
            safety = confidence_safety_label(rec.confidence)
            safety_color = (
                "green" if rec.confidence >= 70 else "yellow" if rec.confidence >= 40 else "red"
            )
            icon = risk_tier_icon(rec.confidence)
            reason = confidence_reason(g)
            reason_hint = f" [dim]({reason})[/dim]" if reason != "limited signals" else ""
            console.print(
                f"  [bold]{i}. {g.display_name[:40]}[/bold]  "
                f"[dim]{g.count} emails · {g.total_size_mb} MB · {g.age_str}[/dim]\n"
                f"  {icon} Confidence: [bold]{rec.confidence}%[/bold]  "
                f"[{safety_color}]{safety}[/{safety_color}]{reason_hint}"
            )
            for action in rec.actions:
                savings_str = (
                    f"[green]~{action.savings_mb} MB freed[/green]"
                    if action.savings_mb > 0
                    else "[dim]no storage change[/dim]"
                )
                tilde = "" if action.savings_exact else "~"
                time_est = format_time_estimate(g.count)
                console.print(
                    f"    [yellow]→ {action.label}[/yellow]  "
                    f"{tilde}{savings_str}  [dim]takes {time_est}[/dim]"
                )
                console.print(f"      [cyan]{action.command}[/cyan]")
            console.print()
        console.print("[dim]  All deletions go to Trash — undo anytime with: mailtrim undo[/dim]\n")
    else:
        console.print("  [dim]Not enough data for recommendations.[/dim]\n")

    console.print(
        "[dim]  Or explore interactively:[/dim]\n"
        "  [cyan]mailtrim purge[/cyan]              — pick senders to delete interactively\n"
        "  [cyan]mailtrim stats --sort size[/cyan]  — re-sort by storage\n"
        "  [cyan]mailtrim stats --share[/cyan]      — copy a shareable summary\n"
        "  [cyan]mailtrim triage[/cyan]             — AI-powered inbox classification\n"
    )


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
    """Sync mail metadata to local database."""
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
    """AI-powered inbox triage with explanations for every decision."""
    from mailtrim.core.avoidance import AvoidanceDetector

    client = _get_client()
    account_email = _get_account_email(client)
    ai = _get_ai()
    detector = AvoidanceDetector(client, account_email, ai)

    with console.status("Fetching inbox..."):
        ids = client.list_message_ids(query="in:inbox is:unread", max_results=limit)
        if not ids:
            console.print("[yellow]No unread messages in inbox.[/yellow]")
            return
        messages = client.get_messages_batch(ids)

    _print_ai_data_notice(f"{len(messages)} email subjects + snippets (≤300 chars each)")
    with console.status(f"Classifying {len(messages)} messages with AI..."):
        classified = ai.classify_emails(messages)

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
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Preview without making changes."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
):
    """
    Execute a bulk operation using natural language.

    Examples:
      mailtrim bulk "archive all newsletters I haven't opened in 60 days"
      mailtrim bulk "delete all emails from noreply@* older than 1 year"
      mailtrim bulk "label as 'receipts' everything from order confirmation senders"
    """
    from mailtrim.core.bulk_engine import BulkEngine

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
    yes: bool = typer.Option(False, "--yes", "-y"),
):
    """Undo a bulk operation within the 30-day window."""
    from mailtrim.core.bulk_engine import BulkEngine
    from mailtrim.core.storage import UndoLogRepo, get_session

    client = _get_client()
    account_email = _get_account_email(client)
    engine = BulkEngine(client, account_email)

    if log_id is None:
        # Show recent undo log
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

    with console.status("Reversing operation..."):
        count = engine.undo(log_id)

    console.print(f"[green]Restored {count} messages.[/green]")

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
    sync_replies: bool = typer.Option(False, "--sync", "-s", help="Sync reply detection."),
):
    """
    Track an email for follow-up. Only reminds you if they haven't replied.

    Examples:
      mailtrim follow-up 18bca72... --days 5
      mailtrim follow-up --list
      mailtrim follow-up --sync
    """
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
        None, "--process", "-p", help="Gmail ID to process (archive/delete)."
    ),
    action: str = typer.Option("archive", "--action", "-a", help="Action: archive or delete."),
    no_insights: bool = typer.Option(False, "--no-insights", help="Skip AI insight generation."),
):
    """
    Show emails you've been putting off — viewed multiple times but never acted on.
    AI explains why you might be avoiding them and suggests one action.
    """
    from mailtrim.core.avoidance import AvoidanceDetector

    client = _get_client()
    account_email = _get_account_email(client)
    detector = AvoidanceDetector(client, account_email, _get_ai())

    if process:
        detector.process(process, action)
        console.print(f"[green]{action.capitalize()}d.[/green] Removed from avoidance list.")
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
            f"\n[dim]mailtrim avoid --process {ae.record.gmail_id} --action archive[/dim]"
        )

        console.print(Panel(panel_content, border_style="red", expand=False))


# ── unsubscribe ───────────────────────────────────────────────────────────────


@app.command()
def unsubscribe(
    sender: Optional[str] = typer.Argument(None, help="Sender email to unsubscribe from."),
    from_query: Optional[str] = typer.Option(
        None, "--from-query", "-q", help="Gmail query to find senders."
    ),
    no_headless: bool = typer.Option(
        False, "--no-headless", help="Skip Playwright headless fallback."
    ),
    list_history: bool = typer.Option(False, "--history", help="Show unsubscribe history."),
    limit: int = typer.Option(10, "--limit", "-n"),
):
    """
    Unsubscribe from email senders using List-Unsubscribe headers + headless browser fallback.
    Achieves near-100% success vs. the 70-85% of API-only tools.
    """
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
        console.print("Provide a sender email or --from-query.")
        raise typer.Exit(1)

    if not messages:
        console.print("[yellow]No messages found.[/yellow]")
        return

    for msg in messages:
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
    dry_run: bool = typer.Option(False, "--dry-run", "-n"),
):
    """
    Manage recurring automation rules defined in natural language.

    Examples:
      mailtrim rules --add "archive LinkedIn notifications older than 7 days"
      mailtrim rules --add "label as 'receipts' anything from order@* or receipt@*"
      mailtrim rules --run
      mailtrim rules --list
    """
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
    """Generate your weekly inbox digest — insights, action items, and one cleanup suggestion."""
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
        undo_hint = f"  [dim]Undo: mailtrim undo {undo_id}[/dim]" if undo_id is not None else ""
        body = (
            f"Moved [bold]{email_count:,} emails[/bold] to Trash  ·  "
            f"freed [bold green]~{freed_mb} MB[/bold green]  ·  took [bold]{elapsed_seconds}s[/bold]\n"
            f"Senders: [dim]{names_str}[/dim]\n" + undo_hint
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
        help="Keep the last N emails per sender; delete the rest.",
    ),
    older_than: Optional[int] = typer.Option(
        None,
        "--older-than",
        help="Only delete emails older than this many days.",
    ),
    max_scan: int = typer.Option(2000, "--max-scan", help="Max emails to scan."),
    top: int = typer.Option(30, "--top", "-n", help="How many top offenders to show."),
    min_count: int = typer.Option(2, "--min", help="Minimum emails to appear on list."),
    sort_by: str = typer.Option("count", "--sort", "-s", help="Sort by: count | oldest | size"),
    also_unsubscribe: bool = typer.Option(
        False, "--unsub", help="Also unsubscribe from selected senders."
    ),
    permanent: bool = typer.Option(
        False, "--permanent", help="Permanently delete (skip Trash). IRREVERSIBLE."
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
):
    """
    Show top email offenders and bulk-delete the ones you choose.

    Scans your promotions/newsletters, ranks senders, lets you pick which
    ones to delete — with a 30-day undo window.

    Examples:
      mailtrim purge
      mailtrim purge --scope anywhere            # scan all mail, not just inbox
      mailtrim purge --domain linkedin.com --yes
      mailtrim purge --domain linkedin.com --keep 10
      mailtrim purge --domain linkedin.com --older-than 90
      mailtrim purge --domain linkedin.com --yes --share
      mailtrim purge --query "category:promotions" --top 20
      mailtrim purge --unsub   # also unsubscribe while deleting
    """
    import time as _time

    from mailtrim.core.sender_stats import fetch_sender_groups
    from mailtrim.core.storage import UndoLogRepo, get_session
    from mailtrim.core.unsubscribe import UnsubscribeEngine

    client = _get_client()
    account_email = _get_account_email(client)

    # --scope prepends a scope filter to the query unless --query is explicitly set
    if scope == "anywhere" and query == "category:promotions OR label:newsletters":
        query = "in:anywhere -in:trash -in:spam"
    elif scope == "anywhere":
        query = f"in:anywhere -in:trash -in:spam {query}"

    # --domain builds a targeted query and routes to non-interactive mode
    if domain:
        effective_query = f"from:{domain}"
        if scope == "anywhere":
            effective_query = f"in:anywhere -in:trash -in:spam from:{domain}"
        if older_than:
            effective_query += f" older_than:{older_than}d"
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
        return

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
        _print_cleanup_complete(
            console=console,
            freed_mb=sel_mb,
            email_count=sel_msgs,
            sender_names=[domain],
            elapsed_seconds=elapsed,
            permanent=permanent,
            undo_id=None,
            share=share,
        )
        return

    # ── Step 2: render offender table ────────────────────────────────────────
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
                f"[bold red]WARNING — THIS CANNOT BE UNDONE[/bold red]\n\n"
                f"You are about to [bold]permanently delete {sel_msgs} emails[/bold] "
                f"from {len(selected)} sender(s).\n"
                f"They will NOT go to Trash. There is no recovery.\n\n"
                f"[dim]Remove --permanent to move to Trash instead (recoverable for 30 days).[/dim]",
                border_style="red",
            )
        )
        if not yes:
            # Require typing "delete permanently" to confirm — not just Enter
            answer = Prompt.ask(
                '[bold red]Type "delete permanently" to confirm, or anything else to cancel[/bold red]'
            )
            if answer.strip().lower() != "delete permanently":
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
    _print_cleanup_complete(
        console=console,
        freed_mb=freed_mb,
        email_count=deleted,
        sender_names=sender_names,
        elapsed_seconds=elapsed,
        permanent=permanent,
        undo_id=undo_id,
        share=share,
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


# ── version ───────────────────────────────────────────────────────────────────


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
