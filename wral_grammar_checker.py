import json
import os
import smtplib
from datetime import datetime
from email.mime.text import MIMEText

import feedparser
import language_tool_python
import requests
from bs4 import BeautifulSoup

RSS_FEED_URL = "https://www.wral.com/news/rss/48/"
SEEN_ARTICLES_FILE = os.getenv("SEEN_ARTICLES_FILE", "seen_articles.json")
MAX_ARTICLES_PER_RUN = int(os.getenv("MAX_ARTICLES_PER_RUN", "20"))

EMAIL_FROM = os.getenv("EMAIL_FROM", "")
EMAIL_TO = os.getenv("EMAIL_TO", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
EMAIL_ENABLED = all([EMAIL_FROM, EMAIL_TO, EMAIL_PASSWORD])

IGNORE_RULE_IDS = {
    "EN_QUOTES",
    "WHITESPACE_RULE",
}


def load_seen_articles():
    if os.path.exists(SEEN_ARTICLES_FILE):
        with open(SEEN_ARTICLES_FILE, "r", encoding="utf-8") as file:
            return set(json.load(file))
    return set()


def save_seen_articles(seen):
    with open(SEEN_ARTICLES_FILE, "w", encoding="utf-8") as file:
        json.dump(sorted(seen), file)


def get_article_links():
    feed = feedparser.parse(RSS_FEED_URL)
    return [(entry.title, entry.link) for entry in feed.entries[:MAX_ARTICLES_PER_RUN]]


def find_duplicate_paragraphs(text, min_words=8, fuzzy_threshold=0.85):
    import difflib
    import re
    from collections import defaultdict

    paragraphs = [paragraph.strip() for paragraph in text.split("\n") if paragraph.strip()]

    def normalize(paragraph):
        normalized = re.sub(r"\s+", " ", paragraph.lower())
        normalized = re.sub(r"[^\w\s]", "", normalized)
        return normalized

    candidates = []
    for index, paragraph in enumerate(paragraphs):
        normalized = normalize(paragraph)
        if len(normalized.split()) < min_words:
            continue
        candidates.append((index, paragraph, normalized))

    seen_positions = defaultdict(list)
    for index, paragraph, normalized in candidates:
        seen_positions[normalized].append((index, paragraph))

    exact_dupes = []
    exact_matched_indices = set()
    for occurrences in seen_positions.values():
        if len(occurrences) > 1:
            positions = [position for position, _ in occurrences]
            exact_dupes.append(
                {
                    "text": occurrences[0][1],
                    "count": len(occurrences),
                    "positions": positions,
                    "type": "exact",
                }
            )
            exact_matched_indices.update(positions)

    exact_group_of = {}
    for group_id, dupe in enumerate(exact_dupes):
        for position in dupe["positions"]:
            exact_group_of[position] = group_id

    fuzzy_dupes = []
    fuzzy_seen_pairs = set()

    for left in range(len(candidates)):
        for right in range(left + 1, len(candidates)):
            index_one, paragraph_one, normalized_one = candidates[left]
            index_two, paragraph_two, normalized_two = candidates[right]

            if (
                index_one in exact_group_of
                and index_two in exact_group_of
                and exact_group_of[index_one] == exact_group_of[index_two]
            ):
                continue

            pair_key = tuple(sorted((index_one, index_two)))
            if pair_key in fuzzy_seen_pairs:
                continue

            ratio = difflib.SequenceMatcher(None, normalized_one, normalized_two).ratio()
            if ratio >= fuzzy_threshold:
                fuzzy_seen_pairs.add(pair_key)
                fuzzy_dupes.append(
                    {
                        "text": paragraph_one,
                        "text2": paragraph_two,
                        "similarity": round(ratio, 2),
                        "positions": [index_one, index_two],
                        "type": "fuzzy",
                    }
                )

    return exact_dupes, fuzzy_dupes


def extract_article_text(url):
    headers = {"User-Agent": "Mozilla/5.0 (compatible; GrammarBot/1.0)"}
    response = requests.get(url, headers=headers, timeout=15)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    content_div = soup.find("div", class_="article-content") or soup.find("article")
    paragraphs = content_div.find_all("p") if content_div else soup.find_all("p")

    return "\n".join(
        paragraph.get_text(strip=True)
        for paragraph in paragraphs
        if paragraph.get_text(strip=True)
    )


def check_text(tool, text):
    matches = tool.check(text)
    return [match for match in matches if match.ruleId not in IGNORE_RULE_IDS]


def build_report(results):
    if not results["flagged"]:
        return "No spelling or grammar issues found today. WRAL is on their A-game."

    lines = [f"WRAL Daily Grammar Report — {datetime.now().strftime('%Y-%m-%d')}\n"]
    lines.append(
        f"Checked {results['articles_checked']} articles, found issues in {len(results['flagged'])}.\n"
    )

    for item in results["flagged"]:
        lines.append(f"\n📰 {item['title']}")
        lines.append(f"   {item['url']}")

        for dupe in item.get("duplicates", []):
            lines.append(
                f"   - [DUPLICATE PARAGRAPH] appears {dupe['count']}x "
                f"(paragraphs {', '.join(str(position) for position in dupe['positions'])})"
            )
            preview = dupe["text"][:120] + ("..." if len(dupe["text"]) > 120 else "")
            lines.append(f'     "{preview}"')

        for fuzzy_dupe in item.get("fuzzy_duplicates", []):
            pos_one, pos_two = fuzzy_dupe["positions"]
            lines.append(
                f"   - [POSSIBLE DUPLICATE ⚠️] paragraphs {pos_one} & {pos_two} are "
                f"{int(fuzzy_dupe['similarity'] * 100)}% similar — please verify"
            )
            preview_one = fuzzy_dupe["text"][:100] + (
                "..." if len(fuzzy_dupe["text"]) > 100 else ""
            )
            preview_two = fuzzy_dupe["text2"][:100] + (
                "..." if len(fuzzy_dupe["text2"]) > 100 else ""
            )
            lines.append(f'     [{pos_one}] "{preview_one}"')
            lines.append(f'     [{pos_two}] "{preview_two}"')

        for issue in item["issues"][:10]:
            lines.append(f"   - [{issue.ruleId}] {issue.message}")
            lines.append(f"     ...{issue.context.strip()}...")

    return "\n".join(lines)


def send_email(report_text):
    msg = MIMEText(report_text)
    msg["Subject"] = f"WRAL Grammar Report — {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.send_message(msg)


def main():
    print("Starting WRAL grammar check...")
    seen = load_seen_articles()
    tool = language_tool_python.LanguageTool("en-US")

    articles = get_article_links()
    new_articles = [(title, url) for title, url in articles if url not in seen]
    results = {"articles_checked": 0, "flagged": []}

    for title, url in new_articles:
        try:
            text = extract_article_text(url)
            if not text:
                continue

            issues = check_text(tool, text)
            exact_dupes, fuzzy_dupes = find_duplicate_paragraphs(text)
            results["articles_checked"] += 1

            if issues or exact_dupes or fuzzy_dupes:
                results["flagged"].append(
                    {
                        "title": title,
                        "url": url,
                        "issues": issues,
                        "duplicates": exact_dupes,
                        "fuzzy_duplicates": fuzzy_dupes,
                    }
                )

            seen.add(url)
        except Exception as error:
            print(f"Failed to process {url}: {error}")

    save_seen_articles(seen)
    tool.close()

    report = build_report(results)
    print(f"\n{report}")

    if EMAIL_ENABLED:
        send_email(report)
        print("\nReport emailed.")
    else:
        print("\nEmail delivery disabled; set EMAIL_FROM, EMAIL_TO, and EMAIL_PASSWORD to enable it.")


if __name__ == "__main__":
    main()
