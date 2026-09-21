import unittest
from unittest.mock import patch, MagicMock
from screener.web_verifier import (
    WebSearchVerifier, 
    RED_FLAG_PATTERNS, 
    TURNAROUND_CATALYST_PATTERNS,
    detect_market,
    fetch_realtime_news
)

class TestWebSearchVerifier(unittest.TestCase):
    def setUp(self):
        self.verifier = WebSearchVerifier(api_key=None)

    def test_detect_market(self):
        self.assertEqual(detect_market("QTCOM.HE"), "FI")
        self.assertEqual(detect_market("NOKIA.HE"), "FI")
        self.assertEqual(detect_market("EVO.ST"), "SE")
        self.assertEqual(detect_market("SINCH.ST"), "SE")
        self.assertEqual(detect_market("AAPL"), "US")
        self.assertEqual(detect_market("NVDA"), "US")
        self.assertEqual(detect_market("TEST", market_override="FI"), "FI")
        self.assertEqual(detect_market("TEST", market_override="SE"), "SE")
        self.assertEqual(detect_market("TEST", market_override="US"), "US")

    def test_rule_based_check_clean(self):
        snippets = [
            "Company XYZ reports 45% revenue growth in Q3 2024.",
            "Strong customer demand drives record ARR and operating margin.",
            "Management reiterates positive guidance for the full year."
        ]
        res = self.verifier.rule_based_check(snippets)
        self.assertTrue(res["passed_web_check"])
        self.assertTrue("clean" in res["reason"].lower() or "no obvious" in res["reason"].lower())

    def test_rule_based_check_reverse_split(self):
        snippets = [
            "Shareholders approve 1-for-20 reverse stock split to maintain Nasdaq listing.",
            "Company announces reverse split effective next Monday."
        ]
        res = self.verifier.rule_based_check(snippets)
        self.assertTrue(res["passed_web_check"])
        self.assertTrue(res["has_warning"])
        self.assertIn("reverse stock split", res["warnings_found"])

    def test_rule_based_check_finnish_red_flags(self):
        snippets_warn = [
            "Yhtiö ilmoittaa käynnistävänsä merkittävän suunnatun osakeannin velkojen maksamiseksi.",
            "Hallitus esittää osakkeiden yhdistämistä ja käänteistä splittiä."
        ]
        res_warn = self.verifier.rule_based_check(snippets_warn)
        self.assertTrue(res_warn["passed_web_check"])
        self.assertTrue(res_warn["has_warning"])
        self.assertTrue(any("osakeann" in f or "käänteis" in f or "yhdistämis" in f for f in res_warn["warnings_found"]))

        snippets_fatal = [
            "Käräjäoikeus on asettanut yhtiön konkurssiin ja yrityssaneeraus on rauennut."
        ]
        res_fatal = self.verifier.rule_based_check(snippets_fatal)
        self.assertFalse(res_fatal["passed_web_check"])
        self.assertTrue(any("konkurssi" in f or "saneeraus" in f for f in res_fatal["fatal_flags_found"]))

    def test_rule_based_check_finnish_turnaround_catalyst(self):
        snippets = [
            "Nokian Renkaat julkisti merkittävän 50 miljoonan euron suurtilauksen Pohjois-Amerikasta.",
            "Toimitusjohtajan katsaus: voimakas kannattavuuskäänne ja tilauskannan kasvu jatkuu."
        ]
        res = self.verifier.rule_based_check(snippets)
        self.assertTrue(res["passed_web_check"])
        self.assertTrue(res["turnaround_catalyst_detected"])
        self.assertTrue(any("suurtilau" in f for f in res["positive_catalysts_found"]))
        self.assertIn("kannattavuuskäänne", res["positive_catalysts_found"])

    def test_rule_based_check_swedish_red_flags(self):
        snippets = [
            "Styrelsen upprättar kontrollbalansräkning efter fortsatta förluster.",
            "Bolaget beslutar om företrädesemission med kraftig utspädning för befintliga aktieägare."
        ]
        res = self.verifier.rule_based_check(snippets)
        self.assertFalse(res["passed_web_check"])
        self.assertTrue(any(f in res["red_flags_found"] for f in ["kontrollbalansräkning", "företrädesemission", "utspädning"]))

    def test_rule_based_check_swedish_turnaround_catalyst(self):
        snippets = [
            "Bolaget tecknar stororder värd 120 MSEK med internationell fordonstillverkare.",
            "Styrelsen presenterar ny vd och lämnar positiv vinstvarning för helåret."
        ]
        res = self.verifier.rule_based_check(snippets)
        self.assertTrue(res["passed_web_check"])
        self.assertTrue(res["turnaround_catalyst_detected"])
        self.assertIn("stororder", res["positive_catalysts_found"])
        self.assertIn("positiv vinstvarning", res["positive_catalysts_found"])

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

    @patch.object(WebSearchVerifier, "fetch_snippets")
    def test_verify_end_to_end_reject(self, mock_fetch):
        mock_fetch.return_value = [
            "PMGC Holdings announces toxic convertible note financing and impending reverse split."
        ]
        res = self.verifier.verify("ELAB", "PMGC Holdings")
        self.assertFalse(res["passed_web_check"])

    @patch.object(WebSearchVerifier, "fetch_snippets")
    def test_verify_end_to_end_pass(self, mock_fetch):
        mock_fetch.return_value = [
            "Qt Group announces record quarterly revenue and EBIT growth in automotive."
        ]
        res = self.verifier.verify("QTCOM.HE", "Qt Group")
        self.assertTrue(res["passed_web_check"])

    def test_rule_based_check_turnaround_catalyst(self):
        snippets = [
            "Company announces major contract win with global automotive tier-1 supplier.",
            "Debt restructuring agreement finalized with creditors, eliminating 80% of outstanding debt.",
            "Board appoints new CEO appointed with proven turnaround track record."
        ]
        res = self.verifier.rule_based_check(snippets)
        self.assertTrue(res["passed_web_check"])
        self.assertTrue(res["turnaround_catalyst_detected"])
        self.assertGreaterEqual(len(res["positive_catalysts_found"]), 2)

    @patch.object(WebSearchVerifier, "fetch_snippets")
    def test_verify_turnaround_catalyst_end_to_end(self, mock_fetch):
        mock_fetch.return_value = [
            "XYZ Corp signs massive contract with European logistics giant and secures debt reduction agreement."
        ]
        res = self.verifier.verify("XYZ", "XYZ Corp")
        self.assertTrue(res["passed_web_check"])
        self.assertTrue(res["turnaround_catalyst_detected"])

    @patch.object(WebSearchVerifier, "fetch_snippets")
    def test_verify_empty_snippets_graceful(self, mock_fetch):
        mock_fetch.return_value = []
        res = self.verifier.verify("UNKNOWN", "Unknown Corp")
        self.assertTrue(res["passed_web_check"])
        self.assertFalse(res["turnaround_catalyst_detected"])

    @patch.object(WebSearchVerifier, "fetch_snippets")
    def test_fetch_realtime_news_function(self, mock_fetch):
        mock_fetch.return_value = ["Headlines for Nokia Oyj"]
        snippets = fetch_realtime_news("NOKIA.HE", "Nokia Oyj")
        self.assertEqual(snippets, ["Headlines for Nokia Oyj"])
        mock_fetch.assert_called_once_with(ticker="NOKIA.HE", company_name="Nokia Oyj", market=None)

if __name__ == "__main__":
    unittest.main()

