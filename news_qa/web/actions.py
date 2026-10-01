"""Write operations, kept out of the route handlers.

``dismiss`` performs exactly the pair of writes ``cli.cmd_dismiss`` does, so an
issue dismissed in the browser is indistinguishable from one dismissed at the
terminal.
"""

import sqlite3
from typing import Optional

from .. import db
from ..detect import filters


def dismiss(
    conn: sqlite3.Connection,
    issue_id: int,
    reason: str = "not-an-error",
    note: Optional[str] = None,
    add_to_dictionary: bool = False,
) -> dict:
    row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
    if row is None:
        raise KeyError(f"No issue #{issue_id}")

    conn.execute("UPDATE issues SET status = 'dismissed' WHERE id = ?", (issue_id,))
    conn.execute(
        "INSERT INTO dismissals (issue_id, reason, note, created_at) VALUES (?, ?, ?, ?)",
        (issue_id, reason, note or None, db.utcnow()),
    )

    term = (row["matched_text"] or "").strip()
    status, also_hidden = (
        _add_term_for_issue(conn, issue_id, term, row["kind"])
        if add_to_dictionary
        else (None, 0)
    )

    return {
        "issue_id": issue_id,
        "term": term,
        "term_added": status == "added",
        # Distinguishes "we added it" from "it was already there" from "this
        # kind of match is not a dictionary entry", so the UI can say which.
        "dictionary": status,
        # Other open issues the new term covered, hidden without a rescan.
        "also_hidden": also_hidden,
    }


def _add_term_for_issue(
    conn: sqlite3.Connection, issue_id: int, term: str, kind: str
) -> tuple:
    """Return (why the dictionary did or did not change, issues also hidden)."""
    if not term:
        return "no-term", 0
    # Only a spelling match is a sensible dictionary entry; a grammar rule's
    # matched text is a phrase, and adding it would allowlist nothing useful.
    if kind != "spelling":
        return "not-spelling", 0
    if not db.add_dictionary_term(conn, term, note="dismissed", issue_id=issue_id):
        return "already-present", 0
    return "added", apply_dictionary_term(conn, term, exclude_issue_id=issue_id)


def apply_dictionary_term(
    conn: sqlite3.Connection, term: str, exclude_issue_id: Optional[int] = None
) -> int:
    """Hide open spelling issues a newly added term now covers.

    Scan-time filtering is what normally sets ``suppressed_reason``, which
    means the same misspelled name stays flagged on every other article until
    the next scan runs. Applying the term here closes that gap; the match test
    deliberately mirrors ``FilterChain._dictionary_hit`` so a term hides the
    same issues either way. Rows are hidden, never dismissed -- a dismissal is
    a judgement the editor made, and only the one they acted on gets it.
    """
    term = term.strip().casefold()
    if not term:
        return 0

    words = term.split()
    # Prefix match rather than equality so possessives ("Newkirk's") are
    # considered; the exact test happens below.
    prefilter = " OR ".join(["matched_text LIKE ?"] * len(words))
    candidates = conn.execute(
        f"""
        SELECT id, matched_text, context_before, context_after FROM issues
        WHERE kind = 'spelling' AND status = 'open' AND suppressed_reason IS NULL
          AND ({prefilter})
        """,
        [f"{word}%" for word in words],
    ).fetchall()

    hit_ids = [
        row["id"]
        for row in candidates
        if row["id"] != exclude_issue_id and _covers(term, words, row)
    ]
    if not hit_ids:
        return 0

    placeholders = ",".join("?" * len(hit_ids))
    conn.execute(
        f"UPDATE issues SET suppressed_reason = 'dictionary' WHERE id IN ({placeholders})",
        hit_ids,
    )
    return len(hit_ids)


def _covers(term: str, words: list, row: sqlite3.Row) -> bool:
    """Whether a dictionary term allowlists this issue's match."""
    candidate = filters.strip_possessive(row["matched_text"] or "").casefold()
    if not candidate:
        return False
    if candidate == term:
        return True
    # A multi-word entry such as "chapel hill" covers a match on just "Chapel",
    # but only where the whole phrase actually appears around the match.
    if len(words) > 1 and candidate in words:
        neighborhood = (
            f"{row['context_before'] or ''}{row['matched_text']}{row['context_after'] or ''}"
        ).casefold()
        return term in neighborhood
    return False


def reopen(conn: sqlite3.Connection, issue_id: int) -> None:
    """Undo a dismissal (or a resolution) without erasing the audit trail."""
    row = conn.execute("SELECT id FROM issues WHERE id = ?", (issue_id,)).fetchone()
    if row is None:
        raise KeyError(f"No issue #{issue_id}")
    conn.execute(
        "UPDATE issues SET status = 'open', resolved_at = NULL WHERE id = ?", (issue_id,)
    )
    conn.execute(
        "UPDATE dismissals SET revoked_at = ? WHERE issue_id = ? AND revoked_at IS NULL",
        (db.utcnow(), issue_id),
    )


def add_term(conn: sqlite3.Connection, term: str, note: str = "manual") -> bool:
    added = db.add_dictionary_term(conn, term, note=note)
    if added:
        apply_dictionary_term(conn, term)
    return added


def remove_term(conn: sqlite3.Connection, term: str) -> bool:
    removed = conn.execute("DELETE FROM dictionary WHERE term = ?", (term,)).rowcount > 0
    if removed:
        _unhide_term(conn, term)
    return removed


def _unhide_term(conn: sqlite3.Connection, term: str) -> int:
    """Re-expose issues the removed term was the only reason for hiding.

    The counterpart to ``apply_dictionary_term``: removing a term should take
    effect as immediately as adding one. A row that some other entry still
    covers stays hidden, so removing "Chapel" does not resurface a match that
    "chapel hill" also allowlists.
    """
    term = term.strip().casefold()
    words = term.split()
    if not term:
        return 0

    prefilter = " OR ".join(["matched_text LIKE ?"] * len(words))
    hidden = conn.execute(
        f"""
        SELECT id, matched_text, context_before, context_after FROM issues
        WHERE suppressed_reason = 'dictionary' AND ({prefilter})
        """,
        [f"{word}%" for word in words],
    ).fetchall()

    remaining = [(other, other.split()) for other in db.load_dictionary(conn)]
    orphans = [
        row["id"]
        for row in hidden
        if _covers(term, words, row)
        and not any(_covers(other, other_words, row) for other, other_words in remaining)
    ]
    if not orphans:
        return 0

    placeholders = ",".join("?" * len(orphans))
    conn.execute(
        f"UPDATE issues SET suppressed_reason = NULL WHERE id IN ({placeholders})", orphans
    )
    return len(orphans)


def suppress_rule(conn: sqlite3.Connection, rule_id: str, note: Optional[str] = None) -> bool:
    """Mute a rule globally. Takes effect on the next scan, not retroactively."""
    cursor = conn.execute(
        "INSERT INTO suppressions (rule_id, scope, note, created_at) "
        "VALUES (?, 'global', ?, ?) ON CONFLICT(rule_id, scope) DO NOTHING",
        (rule_id.strip(), note or None, db.utcnow()),
    )
    return cursor.rowcount > 0


def unsuppress_rule(conn: sqlite3.Connection, rule_id: str) -> bool:
    return (
        conn.execute(
            "DELETE FROM suppressions WHERE rule_id = ? AND scope = 'global'", (rule_id,)
        ).rowcount
        > 0
    )
