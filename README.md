# news-qa-bot

Daily WRAL.com article checks for spelling, grammar, and duplicate paragraphs.

## Files

- `wral_grammar_checker.py` pulls recent WRAL RSS items, checks article text with LanguageTool, and reports potential issues.
- `.github/workflows/daily-check.yml` runs the checker every day at `11:00 UTC` and supports manual runs through GitHub Actions.

## Optional email delivery

Add these repository secrets if you want the workflow to email the report:

- `EMAIL_FROM`
- `EMAIL_TO`
- `EMAIL_PASSWORD`

If the secrets are not set, the workflow still runs and prints the report to the job log.
