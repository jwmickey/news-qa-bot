import unittest
from unittest.mock import MagicMock

from wral_grammar_checker import (
    IGNORE_RULE_IDS,
    build_report,
    check_text,
    get_rule_id,
)


class MockMatchV3:
    """Mock representing language_tool_python v3.x Match object."""

    def __init__(self, rule_id, message="Test message", context="Some test context"):
        self.rule_id = rule_id
        self.message = message
        self.context = context


class MockMatchV2:
    """Mock representing language_tool_python v2.x Match object."""

    def __init__(self, rule_id, message="Test message", context="Some test context"):
        self.ruleId = rule_id
        self.message = message
        self.context = context


class MockMatchRuleDict:
    """Mock representing match with a rule dictionary."""

    def __init__(self, rule_id, message="Test message", context="Some test context"):
        self.rule = {"id": rule_id}
        self.message = message
        self.context = context


class MockMatchRuleObj:
    """Mock representing match with a rule sub-object."""

    def __init__(self, rule_id, message="Test message", context="Some test context"):
        rule_obj = MagicMock()
        rule_obj.id = rule_id
        self.rule = rule_obj
        self.message = message
        self.context = context


class TestGetRuleId(unittest.TestCase):
    def test_v3_match_with_rule_id(self):
        match = MockMatchV3("MORFOLOGIK_RULE_EN_US")
        self.assertEqual(get_rule_id(match), "MORFOLOGIK_RULE_EN_US")

    def test_v2_match_with_rule_id(self):
        match = MockMatchV2("EN_QUOTES")
        self.assertEqual(get_rule_id(match), "EN_QUOTES")

    def test_match_with_rule_dict(self):
        match = MockMatchRuleDict("WHITESPACE_RULE")
        self.assertEqual(get_rule_id(match), "WHITESPACE_RULE")

    def test_match_with_rule_object(self):
        match = MockMatchRuleObj("UPPERCASE_SENTENCE_START")
        self.assertEqual(get_rule_id(match), "UPPERCASE_SENTENCE_START")

    def test_match_without_rule_id(self):
        match = object()
        self.assertIsNone(get_rule_id(match))


class TestCheckText(unittest.TestCase):
    def test_check_text_filters_ignore_rules_v3(self):
        tool = MagicMock()
        tool.check.return_value = [
            MockMatchV3("EN_QUOTES"),
            MockMatchV3("MORFOLOGIK_RULE_EN_US"),
            MockMatchV3("WHITESPACE_RULE"),
        ]
        result = check_text(tool, "Some text")
        self.assertEqual(len(result), 1)
        self.assertEqual(get_rule_id(result[0]), "MORFOLOGIK_RULE_EN_US")

    def test_check_text_filters_ignore_rules_v2(self):
        tool = MagicMock()
        tool.check.return_value = [
            MockMatchV2("EN_QUOTES"),
            MockMatchV2("MORFOLOGIK_RULE_EN_US"),
            MockMatchV2("WHITESPACE_RULE"),
        ]
        result = check_text(tool, "Some text")
        self.assertEqual(len(result), 1)
        self.assertEqual(get_rule_id(result[0]), "MORFOLOGIK_RULE_EN_US")

    def test_check_text_with_real_match_object(self):
        import language_tool_python.match as m

        attrib = {
            "message": "Possible spelling mistake found.",
            "shortMessage": "Spelling mistake",
            "replacements": [{"value": "test"}],
            "offset": 0,
            "length": 4,
            "context": {"text": "tset", "offset": 0, "length": 4},
            "sentence": "tset",
            "type": {"typeName": "Other"},
            "rule": {
                "id": "MORFOLOGIK_RULE_EN_US",
                "description": "Possible spelling mistake",
                "issueType": "misspelling",
                "category": {"id": "TYPOS", "name": "Possible Typo"},
            },
            "ignoreForIncompleteSentence": False,
            "contextForSureMatch": 0,
        }
        real_match = m.Match(attrib, "tset")
        tool = MagicMock()
        tool.check.return_value = [real_match]
        result = check_text(tool, "tset")
        self.assertEqual(len(result), 1)
        self.assertEqual(get_rule_id(result[0]), "MORFOLOGIK_RULE_EN_US")


class TestBuildReport(unittest.TestCase):
    def test_build_report_with_v3_match(self):
        issue = MockMatchV3("MORFOLOGIK_RULE_EN_US", "Spelling mistake", "tset context")
        results = {
            "articles_checked": 1,
            "flagged": [
                {
                    "title": "Article Title",
                    "url": "https://example.com/1",
                    "issues": [issue],
                    "duplicates": [],
                    "fuzzy_duplicates": [],
                }
            ],
        }
        report = build_report(results)
        self.assertIn("[MORFOLOGIK_RULE_EN_US]", report)
        self.assertIn("Spelling mistake", report)

    def test_build_report_with_v2_match(self):
        issue = MockMatchV2("MORFOLOGIK_RULE_EN_US", "Spelling mistake", "tset context")
        results = {
            "articles_checked": 1,
            "flagged": [
                {
                    "title": "Article Title",
                    "url": "https://example.com/1",
                    "issues": [issue],
                    "duplicates": [],
                    "fuzzy_duplicates": [],
                }
            ],
        }
        report = build_report(results)
        self.assertIn("[MORFOLOGIK_RULE_EN_US]", report)
        self.assertIn("Spelling mistake", report)

    def test_build_report_no_issues(self):
        results = {"articles_checked": 5, "flagged": []}
        report = build_report(results)
        self.assertIn("No spelling or grammar issues found today", report)


if __name__ == "__main__":
    unittest.main()
