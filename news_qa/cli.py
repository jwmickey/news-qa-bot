"""Command-line interface: python -m news_qa <command>"""

import argparse
import sys
from typing import Optional

from . import config, db, report as report_module
from .scan import ScanSummary, scan


def _print_progress(result) -> None:
    icons = {"checked": "*", "unchanged": "=", "failed": "!", "skipped": "-"}
    icon = icons.get(result.outcome, "?")
    label = result.title or result.url
    detail = ""
    if result.outcome == "checked":
        visible = len(result.visible_issues)
        hidden = len(result.issues) - visible
        detail = f"{visible} issue(s)" + (f", {hidden} hidden" if hidden else "")
        if result.resolved_issues:
            detail += f", {result.resolved_issues} resolved"
    elif result.note:
        detail = result.note
    print(f"  {icon} {label[:70]:<70} {detail}")


def _print_summary(summary: ScanSummary, dry_run: bool = False) -> None:
    print()
    print(
        f"Scan #{summary.scan_id} ({summary.kind}): "
        f"{summary.articles_seen} seen, {summary.articles_checked} checked, "
        f"{summary.articles_unchanged} unchanged, {summary.articles_failed} failed."
    )
    print(f"{summary.issues_new} new issue(s), {summary.issues_resolved} resolved.")
    if dry_run:
        print("\nDry run: nothing was written to the database.")


# --- commands -------------------------------------------------------------


def cmd_scan(args) -> int:
    with db.session() as conn:
        print(f"Scanning {args.source}...")
        summary = scan(
            conn,
            source_key=args.source,
            limit=args.limit,
            use_ner=not args.no_ner,
            on_progress=_print_progress,
        )
        _print_summary(summary, args.dry_run)
        if args.dry_run:
            conn.rollback()
            return 0
        conn.commit()

        if args.email:
            built = report_module.build_report(conn, summary.scan_id)
            report_module.send_email(built)
            print("Report emailed.")
    return 0


def cmd_rescan(args) -> int:
    with db.session() as conn:
        print(f"Rescanning {args.source} for fixes...")
        summary = scan(
            conn,
            source_key=args.source,
            limit=args.limit,
            rescan=True,
            open_only=not args.all,
            since_days=args.since,
            use_ner=not args.no_ner,
            on_progress=_print_progress,
        )
        _print_summary(summary, args.dry_run)
        if args.dry_run:
            conn.rollback()
            return 0
        conn.commit()
    return 0


def cmd_report(args) -> int:
    with db.session() as conn:
        built = report_module.build_report(
            conn,
            scan_id=args.scan,
            severity=None if args.severity == "all" else args.severity,
            include_suppressed=args.include_hidden,
            all_open=args.all_open,
        )
        if built is None:
            print("No scans recorded yet. Run: python -m news_qa scan")
            return 1

        if args.format == "html":
            print(report_module.render_html(built))
        else:
            print(report_module.render_text(built))

        if args.email:
            report_module.send_email(built)
            print("\nReport emailed.", file=sys.stderr)
    return 0


def cmd_issues(args) -> int:
    filters = ["1=1"]
    params: list = []
    if args.status != "all":
        filters.append("i.status = ?")
        params.append(args.status)
    if args.severity != "all":
        filters.append("i.severity = ?")
        params.append(args.severity)
    if not args.include_hidden:
        filters.append("i.suppressed_reason IS NULL")
    if args.kind:
        filters.append("i.kind = ?")
        params.append(args.kind)

    with db.session() as conn:
        rows = conn.execute(
            f"""
            SELECT i.*, a.url, a.title FROM issues i
            JOIN articles a ON a.id = i.article_id
            WHERE {' AND '.join(filters)}
            ORDER BY i.id DESC LIMIT ?
            """,
            (*params, args.limit),
        ).fetchall()

        if not rows:
            print("No matching issues.")
            return 0

        for row in rows:
            hidden = f" [hidden: {row['suppressed_reason']}]" if row["suppressed_reason"] else ""
            quoted = " [in quote]" if row["in_quote"] else ""
            print(f"#{row['id']} {row['severity']}/{row['kind']} {row['status']}{hidden}{quoted}")
            print(f"    {row['message']}")
            print(
                f"    ...{(row['context_before'] or '')[-40:]}"
                f"[{row['matched_text']}]"
                f"{(row['context_after'] or '')[:40]}..."
            )
            print(f"    {row['title'] or ''}")
            print(f"    {row['url']}")
            print()
        print(f"{len(rows)} issue(s).")
    return 0


def cmd_dismiss(args) -> int:
    with db.session() as conn:
        row = conn.execute("SELECT * FROM issues WHERE id = ?", (args.issue_id,)).fetchone()
        if row is None:
            print(f"No issue #{args.issue_id}.", file=sys.stderr)
            return 1

        conn.execute("UPDATE issues SET status = 'dismissed' WHERE id = ?", (args.issue_id,))
        conn.execute(
            "INSERT INTO dismissals (issue_id, reason, note, created_at) VALUES (?, ?, ?, ?)",
            (args.issue_id, args.reason, args.note, db.utcnow()),
        )
        print(f"Dismissed issue #{args.issue_id} ({args.reason}).")

        if args.add_to_dictionary:
            term = row["matched_text"]
            if db.add_dictionary_term(conn, term, note="dismissed", issue_id=args.issue_id):
                print(f"Added {term!r} to the spelling dictionary.")
            else:
                print(f"{term!r} was already in the dictionary.")
        conn.commit()
    return 0


def cmd_dict(args) -> int:
    with db.session() as conn:
        if args.dict_command == "add":
            for term in args.terms:
                added = db.add_dictionary_term(conn, term, note="manual")
                print(f"{'Added' if added else 'Already present:'} {term!r}")
            conn.commit()
        elif args.dict_command == "remove":
            for term in args.terms:
                cursor = conn.execute("DELETE FROM dictionary WHERE term = ?", (term,))
                print(f"{'Removed' if cursor.rowcount else 'Not found:'} {term!r}")
            conn.commit()
        else:
            rows = conn.execute(
                "SELECT term, note, added_at FROM dictionary ORDER BY term COLLATE NOCASE"
            ).fetchall()
            for row in rows:
                print(f"{row['term']:<32} {row['note'] or ''}")
            print(f"\n{len(rows)} term(s).")
    return 0


def cmd_scans(args) -> int:
    with db.session() as conn:
        rows = conn.execute(
            "SELECT * FROM scans ORDER BY id DESC LIMIT ?", (args.limit,)
        ).fetchall()
        if not rows:
            print("No scans recorded yet.")
            return 0
        print(f"{'ID':>5}  {'STARTED':<20} {'KIND':<8} {'STATUS':<9} "
              f"{'CHECKED':>7} {'NEW':>4} {'FIXED':>5}")
        for row in rows:
            print(
                f"{row['id']:>5}  {row['started_at'][:19]:<20} {row['kind']:<8} "
                f"{row['status']:<9} {row['articles_checked']:>7} "
                f"{row['issues_new']:>4} {row['issues_resolved']:>5}"
            )
    return 0


def cmd_stats(args) -> int:
    """Time-to-fix is the number worth putting in front of an editor."""
    with db.session() as conn:
        counts = conn.execute(
            "SELECT status, COUNT(*) n FROM issues WHERE suppressed_reason IS NULL "
            "GROUP BY status"
        ).fetchall()
        print("Issues by status:")
        for row in counts:
            print(f"  {row['status']:<12} {row['n']}")

        row = conn.execute(
            """
            SELECT COUNT(*) n,
                   AVG(julianday(resolved_at) - julianday(created_at)) avg_days,
                   MAX(julianday(resolved_at) - julianday(created_at)) max_days
            FROM issues
            WHERE status = 'resolved' AND resolved_at IS NOT NULL
              AND suppressed_reason IS NULL
            """
        ).fetchone()
        print()
        if row["n"]:
            print(f"Resolved issues: {row['n']}")
            print(f"  Average time to fix: {row['avg_days']:.2f} days")
            print(f"  Longest time to fix: {row['max_days']:.2f} days")
        else:
            print("No resolved issues yet -- rescan after a few days to measure time-to-fix.")

        top = conn.execute(
            """
            SELECT rule_id, COUNT(*) n FROM issues
            WHERE suppressed_reason IS NULL GROUP BY rule_id ORDER BY n DESC LIMIT 10
            """
        ).fetchall()
        if top:
            print("\nMost frequent rules:")
            for entry in top:
                print(f"  {entry['n']:>4}  {entry['rule_id']}")

        hidden = conn.execute(
            "SELECT suppressed_reason, COUNT(*) n FROM issues "
            "WHERE suppressed_reason IS NOT NULL GROUP BY suppressed_reason ORDER BY n DESC"
        ).fetchall()
        if hidden:
            print("\nSuppressed (hidden) issues:")
            for entry in hidden:
                print(f"  {entry['n']:>4}  {entry['suppressed_reason']}")
    return 0


# --- parser ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="news_qa",
        description="Monitor news sites for spelling, grammar, and editorial errors.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub):
        sub.add_argument("--source", default="wral", help="Source key (default: wral)")
        sub.add_argument("--limit", type=int, default=None, help="Max articles to process")
        sub.add_argument("--no-ner", action="store_true", help="Skip spaCy entity filtering")
        sub.add_argument(
            "--dry-run", action="store_true", help="Run the scan but write nothing"
        )

    scan_parser = subparsers.add_parser("scan", help="Fetch the feed and check new articles")
    add_common(scan_parser)
    scan_parser.add_argument("--email", action="store_true", help="Email the resulting report")
    scan_parser.set_defaults(func=cmd_scan)

    rescan_parser = subparsers.add_parser(
        "rescan", help="Re-check known articles and detect fixes"
    )
    add_common(rescan_parser)
    rescan_parser.add_argument(
        "--all", action="store_true", help="Rescan every article, not only those with open issues"
    )
    rescan_parser.add_argument(
        "--since", type=int, default=None, metavar="DAYS", help="Only articles first seen within N days"
    )
    rescan_parser.set_defaults(func=cmd_rescan)

    report_parser = subparsers.add_parser("report", help="Render a scan's report")
    report_parser.add_argument(
        "--scan", type=int, default=None, help="Scan ID (default: most recent)"
    )
    report_parser.add_argument("--format", choices=("text", "html"), default="text")
    report_parser.add_argument(
        "--severity", choices=("error", "style", "all"), default="error"
    )
    report_parser.add_argument("--include-hidden", action="store_true")
    report_parser.add_argument(
        "--all-open",
        action="store_true",
        help="Every open issue in the database, not just this scan's",
    )
    report_parser.add_argument("--email", action="store_true")
    report_parser.set_defaults(func=cmd_report)

    issues_parser = subparsers.add_parser("issues", help="List stored issues")
    issues_parser.add_argument(
        "--status", default="open", choices=("open", "dismissed", "resolved", "submitted", "all")
    )
    issues_parser.add_argument("--severity", default="error", choices=("error", "style", "all"))
    issues_parser.add_argument(
        "--kind", default=None, choices=("spelling", "grammar", "style", "duplicate")
    )
    issues_parser.add_argument("--include-hidden", action="store_true")
    issues_parser.add_argument("--limit", type=int, default=50)
    issues_parser.set_defaults(func=cmd_issues)

    dismiss_parser = subparsers.add_parser("dismiss", help="Dismiss an issue by ID")
    dismiss_parser.add_argument("issue_id", type=int)
    dismiss_parser.add_argument(
        "--reason", default="not-an-error", help="Why it was dismissed"
    )
    dismiss_parser.add_argument("--note", default=None)
    dismiss_parser.add_argument(
        "--add-to-dictionary",
        action="store_true",
        help="Also add the matched text to the spelling allowlist",
    )
    dismiss_parser.set_defaults(func=cmd_dismiss)

    dict_parser = subparsers.add_parser("dict", help="Manage the spelling allowlist")
    dict_sub = dict_parser.add_subparsers(dest="dict_command")
    dict_add = dict_sub.add_parser("add")
    dict_add.add_argument("terms", nargs="+")
    dict_remove = dict_sub.add_parser("remove")
    dict_remove.add_argument("terms", nargs="+")
    dict_sub.add_parser("list")
    dict_parser.set_defaults(func=cmd_dict, dict_command="list")

    scans_parser = subparsers.add_parser("scans", help="List recent scans")
    scans_parser.add_argument("--limit", type=int, default=20)
    scans_parser.set_defaults(func=cmd_scans)

    stats_parser = subparsers.add_parser("stats", help="Issue counts and time-to-fix")
    stats_parser.set_defaults(func=cmd_stats)

    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config.ensure_data_dir()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as error:  # noqa: BLE001 - CLI boundary
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
