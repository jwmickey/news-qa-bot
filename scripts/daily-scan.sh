#!/bin/bash
# Daily scan: check new articles, then re-check known ones to catch fixes.
# Invoked by launchd (see com.jody.newsqa.plist) or run by hand.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PROJECT_DIR}/.venv/bin/python"

cd "$PROJECT_DIR"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') ==="

# New articles from the feed.
"$PYTHON" -m news_qa scan

# Re-check articles with open issues so corrections get recorded. Limited to
# the last two weeks -- older stories are rarely touched again.
"$PYTHON" -m news_qa rescan --since 14

# Everything still open, not just what this run happened to touch.
"$PYTHON" -m news_qa report --all-open
