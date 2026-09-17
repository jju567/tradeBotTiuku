import unittest
from unittest.mock import patch, MagicMock
from screener.web_verifier import WebSearchVerifier, RED_FLAG_PATTERNS

class TestWebSearchVerifier(unittest.TestCase):
    def setUp(self):
        self.verifier = WebSearchVerifier(api_key=None)

    def test_rule_based_check_clean(self):
        snippets = [
            "Company XYZ reports 45% revenue growth in Q3 2024.",
            "Strong customer demand drives record ARR and operating margin.",
            "Management reiterates positive guidance for the full year."
        ]
        res = self.verifier.rule_based_check(snippets)
        self.assertTrue(res["passed_web_check"])
        self.assertIn("No obvious red flag patterns", res["reason"])

    def test_rule_based_check_reverse_split(self):
        snippets = [
            "Shareholders approve 1-for-20 reverse stock split to maintain Nasdaq listing.",
            "Company announces reverse split effective next Monday."
        ]
        res = self.verifier.rule_based_check(snippets)
        self.assertFalse(res["passed_web_check"])
        self.assertIn("reverse stock split", res["reason"].lower())

    def test_rule_based_check_going_concern(self):
        snippets = [
            "Auditor raises going concern warning due to recurring operating losses."
        ]
        res = self.verifier.rule_based_check(snippets)
        self.assertFalse(res["passed_web_check"])
        self.assertIn("going concern", res["reason"].lower())

    def test_rule_based_check_sec_investigation(self):
        snippets = [
            "Company confirms ongoing SEC investigation into revenue recognition practices."
        ]
        res = self.verifier.rule_based_check(snippets)
        self.assertFalse(res["passed_web_check"])
        self.assertIn("sec investigation", res["reason"].lower())

    @patch.object(WebSearchVerifier, "search_duckduckgo")
    def test_verify_end_to_end_reject(self, mock_search):
        mock_search.return_value = [
            "PMGC Holdings announces toxic convertible note financing and impending reverse split."
        ]
        res = self.verifier.verify("ELAB", "PMGC Holdings")
        self.assertFalse(res["passed_web_check"])

    @patch.object(WebSearchVerifier, "search_duckduckgo")
    def test_verify_end_to_end_pass(self, mock_search):
        mock_search.return_value = [
            "Qt Group announces record quarterly revenue and EBIT growth in automotive."
        ]
        res = self.verifier.verify("QTCOM.HE", "Qt Group")
        self.assertTrue(res["passed_web_check"])

    @patch.object(WebSearchVerifier, "search_duckduckgo")
    def test_verify_empty_snippets_graceful(self, mock_search):
        mock_search.return_value = []
        res = self.verifier.verify("UNKNOWN", "Unknown Corp")
        self.assertTrue(res["passed_web_check"])

if __name__ == "__main__":
    unittest.main()
