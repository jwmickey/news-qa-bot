# news-qa-bot

Monitors news sites for spelling, grammar, and editorial errors, and keeps a
durable record of what it found — including whether anyone ever fixed it.

Currently watches [WRAL](https://www.wral.com). Adding a source means appending
to `news_qa/sources.py`; nothing else is source-specific.

## Why it works this way

The goal is a short list of errors worth acting on, not a long list of names.
Three design decisions do most of that work:

- **Extraction never loses a space.** Stripping `<a>` tags used to weld words
  together (`See thecounty reportfor details`), inventing a misspelling for
  every link in every article. Text is now extracted with explicit separators
  and repaired punctuation, and there is no fall back to scraping every `<p>`
  on the page — an article that can't be confidently extracted is recorded as a
  failure instead of checked as garbage.
- **Names are suppressed by two independent layers.** spaCy NER catches names
  from context (`Renata Okonkwo`); a dictionary catches what NER misses (`WRAL`
  is not tagged as an entity by the small model). Suppressed issues are kept
  and labelled, not deleted, so you can see what was hidden and why.
- **Issues have content-based identity.** An issue is identified by a hash of
  the rule, the matched text, and its surrounding paragraph context — not by a
  character offset, which shifts whenever anything above it is edited. That is
  what lets a dismissal survive a rescan, and what lets a correction be
  detected as an issue's *disappearance*.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m spacy download en_core_web_sm
```

LanguageTool runs as a local Java server, so a JRE (17+) must be on your PATH.
The spaCy model is optional — without it the dictionary layer still works, the
output is just noisier.

## Usage

```bash
# Check new articles from the feed
python -m news_qa scan
python -m news_qa scan --limit 5 --dry-run     # run it all, write nothing

# Re-check known articles and record what got fixed
python -m news_qa rescan --since 14

# Review
python -m news_qa issues                        # open, error-severity
python -m news_qa issues --severity style
python -m news_qa issues --include-hidden       # show suppressed, with reasons
python -m news_qa report                        # grouped by article
python -m news_qa report --format html
python -m news_qa scans                         # scan history
python -m news_qa stats                         # counts and time-to-fix

# Triage
python -m news_qa dismiss 46 --reason brand-name --add-to-dictionary
python -m news_qa dict add "Fuquay-Varina" "GreenWise"
python -m news_qa dict list
```

Everything lives in `data/news_qa.db` (SQLite, gitignored).

## Web UI

```bash
python -m news_qa.web        # http://127.0.0.1:8000, runs in the foreground
```

Or manage it as a background service, which is usually what you want, since
uvicorn runs without `--reload` and code changes need a restart either way:

```bash
scripts/server.sh start|stop|restart|status|logs
```

`start` detaches and logs to `data/web.log`; `stop` also kills the LanguageTool
Java server, which is a separate process that outlives the web one and would
otherwise linger for days. It comes back on the next scan.

The same database, in a browser: scan state per source, one-click triggers,
issues shown in the article they came from with the flagged span marked, and
dismissal without leaving the list. It binds to loopback — the database is
unauthenticated local state. `NEWS_QA_WEB_HOST` / `NEWS_QA_WEB_PORT` override.

The interface borrows the copy desk's vocabulary because that is what it is.
Errors are marked in red pencil. **Stet** — the proofreader's "let it stand" —
is the dismiss action, and stetted marks are drawn in *non-photo blue*, the
pencil whose marks deliberately don't reproduce. That is not decoration: it is
the same rule the scanner follows, where a suppression is labelled rather than
deleted. Nothing you stet disappears, and *Restore mark* puts it back with the
original judgement kept and stamped rather than erased.

Scans run on one background worker, one at a time — they share a rate limiter
and a LanguageTool server, so running two would cost more and finish no sooner.
The worker commits after every article rather than at the end, which is what
keeps the rest of the UI usable while a scan is in flight. An interrupted run is
marked failed on the next start, not left reading "running" forever.

Adding a source is still an append to `news_qa/sources.py`; every view is
already scoped by source and the UI deliberately offers no source editing,
since `db.seed()` would overwrite it on the next run.

### Detecting fixes

`rescan` re-fetches articles that have open issues, using `If-None-Match` /
`If-Modified-Since` so unchanged pages cost a 304. When an article's text
changes, issues still present keep their row and their dismissal state; issues
that have vanished are marked `resolved` with a timestamp. `stats` turns those
timestamps into an average time-to-fix — a more useful number to bring to an
editor than a raw error count.

Two things never happen: a failed fetch or a failed extraction resolves
nothing, and a *dismissed* issue is never overwritten by resolution.

## Scheduling

The daily run is local, since the database lives on your machine:

```bash
cp scripts/com.jody.newsqa.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.jody.newsqa.plist
```

It runs `scripts/daily-scan.sh` (scan, then rescan, then report) at 07:00 daily
and logs to `data/scan.log`.

The GitHub Actions workflow is now **manual only** (`workflow_dispatch`). Its
schedule was removed because scan state cannot survive between Actions runs —
the old `actions/cache` key never rotated, so `seen_articles.json` froze after
the first successful run.

## Being a good citizen

This exists to help a newsroom, not to burden it. All traffic goes through one
session that identifies itself honestly with a contact address, obeys
`robots.txt`, waits ~1.5s between requests to a host, backs off on 429/5xx, and
sends conditional GETs so unchanged articles aren't re-downloaded.

Set `NEWS_QA_CONTACT` to your own email address.

## Email

Set `EMAIL_FROM`, `EMAIL_TO`, and `EMAIL_PASSWORD` (a Gmail app password), then:

```bash
python -m news_qa scan --email
python -m news_qa report --email
```

## Tests

```bash
python -m pytest tests/ -q
```

The suite covers the things most likely to break silently: extraction spacing,
fingerprint stability under edits, filter behaviour (including what must *not*
be suppressed), and the full scan → rescan → resolve lifecycle.

## Layout

```
news_qa/
  config.py        tunables and paths
  db.py            schema, migrations, helpers
  sources.py       feed and selector definitions
  fetch.py         polite HTTP
  extract.py       HTML -> paragraph-structured text
  fingerprint.py   stable issue identity
  textnorm.py      shared normalization
  detect/
    grammar.py     LanguageTool
    duplicates.py  repeated-paragraph detection
    filters.py     NER + dictionary + rule suppression
  scan.py          orchestration and persistence
  report.py        text/HTML rendering, email
  cli.py           command line
  web/             FastAPI review UI
    app.py         assembly, startup, entry point
    jobs.py        the background scan queue
    queries.py     read SQL, returning dicts
    actions.py     dismiss / reopen / dictionary / rules
    highlight.py   marks spliced into article text
    routes/        one module per section
    templates/     Jinja2 + HTMX
```

## Next

Per-article LLM adjudication, as an on-demand action from the review UI;
`issues.review_json` is reserved for it and the detector chain in
`news_qa/detect/__init__.py` is where it would slot in.
