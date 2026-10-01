"""Paths, tunables, and environment-driven configuration."""

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
DATA_DIR = Path(os.getenv("NEWS_QA_DATA_DIR", PROJECT_ROOT / "data"))
DB_PATH = Path(os.getenv("NEWS_QA_DB", DATA_DIR / "news_qa.db"))
SEED_DICTIONARY_PATH = PACKAGE_DIR / "data" / "seed_dictionary.txt"

# --- Fetching -------------------------------------------------------------
# Deliberately polite: this tool exists to help a newsroom, not to hammer it.
CONTACT_EMAIL = os.getenv("NEWS_QA_CONTACT", "jody.mickey@gmail.com")
USER_AGENT = os.getenv(
    "NEWS_QA_USER_AGENT",
    f"NewsQABot/0.2 (editorial quality monitor; +mailto:{CONTACT_EMAIL})",
)
REQUEST_TIMEOUT = float(os.getenv("NEWS_QA_TIMEOUT", "20"))
MIN_REQUEST_INTERVAL = float(os.getenv("NEWS_QA_MIN_INTERVAL", "1.5"))
MAX_ARTICLES_PER_RUN = int(os.getenv("MAX_ARTICLES_PER_RUN", "20"))
RESPECT_ROBOTS = os.getenv("NEWS_QA_RESPECT_ROBOTS", "1") != "0"

# --- Detection ------------------------------------------------------------
LANGUAGETOOL_LANG = os.getenv("NEWS_QA_LANGUAGE", "en-US")
SPACY_MODEL = os.getenv("NEWS_QA_SPACY_MODEL", "en_core_web_sm")
# Entity labels whose spans should never be reported as misspellings.
NAME_ENTITY_LABELS = {
    "PERSON",
    "ORG",
    "GPE",
    "LOC",
    "FAC",
    "NORP",
    "EVENT",
    "WORK_OF_ART",
}
# Characters of context stored either side of a match. Also the window used to
# build issue fingerprints, so changing it invalidates existing fingerprints.
CONTEXT_WINDOW = 60

DUPLICATE_MIN_WORDS = int(os.getenv("NEWS_QA_DUPE_MIN_WORDS", "8"))
# Near-duplicates need a higher bar than exact ones. Short formulaic lines --
# 7-day forecast rows, scorelines, list items -- are legitimately similar to
# each other, and flagging them buries the real finding: an editor accidentally
# pasting a full paragraph twice, which is invariably long.
DUPLICATE_FUZZY_MIN_WORDS = int(os.getenv("NEWS_QA_DUPE_FUZZY_MIN_WORDS", "15"))
DUPLICATE_FUZZY_THRESHOLD = float(os.getenv("NEWS_QA_DUPE_THRESHOLD", "0.88"))

# --- Web UI ---------------------------------------------------------------
# Loopback by default: the database is unauthenticated local state.
WEB_HOST = os.getenv("NEWS_QA_WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("NEWS_QA_WEB_PORT", "8000"))
# How long a request waits for the scan worker's write lock before giving up.
WEB_BUSY_TIMEOUT_MS = int(os.getenv("NEWS_QA_WEB_BUSY_TIMEOUT_MS", "10000"))

# --- Email ----------------------------------------------------------------
EMAIL_FROM = os.getenv("EMAIL_FROM", "")
EMAIL_TO = os.getenv("EMAIL_TO", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
EMAIL_ENABLED = all([EMAIL_FROM, EMAIL_TO, EMAIL_PASSWORD])


def ensure_data_dir() -> Path:
    """Create the data directory on demand and return it."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return DB_PATH.parent
