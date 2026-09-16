"""
Unit tests for send_alert function in clients/email_client.py and screener/main_controller.py.
"""

import pytest
from unittest.mock import MagicMock, patch

from clients.email_client import EmailClient, send_alert


import email

def test_send_alert_satellite_strong_buy():
    """Verify send_alert formats Subject and Body properly for SATELLITE alerts."""
    mock_client = EmailClient(
        smtp_server="smtp.example.com",
        smtp_port=587,
        username="test@example.com",
        password="secretpassword",
        email_from="tiuku@example.com",
        email_to="trader@example.com",
    )

    analysis_summary = {
        "signals": "INSIDER_BUYING",
        "reasoning": "Insider buying confirmed: Toimitusjohtaja osti 50 000 kpl osakkeita avoimilta markkinoilta.",
        "insider_buying_personal": True,
        "positive_guidance": False,
        "recommended_allocation_eur": 650.0,
        "sizing_model": "Fractional Half-Kelly (18.0% of Satellite Pool)",
        "spread_pct": 1.45,
        "bid": 8.20,
        "ask": 8.32,
        "volume": 12000,
        "total_friction_pct": 2.85,
        "min_recommended_trade_eur": 500.0,
        "link": "https://view.news.eu.nasdaq.com/view?id=123",
        "title": "Raute Oyj: Johdon liiketoimet",
    }

    with patch("smtplib.SMTP") as mock_smtp_class:
        mock_smtp = MagicMock()
        mock_smtp_class.return_value = mock_smtp

        result = send_alert(
            pipeline_type="SATELLITE",
            ticker="RAUTE.HE",
            company_name="Raute Oyj",
            analysis_summary=analysis_summary,
            email_client=mock_client,
        )

        assert result is True
        assert mock_smtp.sendmail.called
        args, kwargs = mock_smtp.sendmail.call_args
        from_addr, to_addrs, raw_msg = args

        assert from_addr == "tiuku@example.com"
        assert to_addrs == ["trader@example.com"]

        # Parse message
        msg_obj = email.message_from_string(raw_msg)
        assert msg_obj["Subject"] == "[SATELLITE ALERT] Strong Buy: RAUTE.HE"

        # 2. Verify Body contents across parts
        payloads = []
        for part in msg_obj.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                payload_data = part.get_payload(decode=True)
                if payload_data:
                    payloads.append(payload_data.decode("utf-8"))
        combined_body = " ".join(payloads)

        assert "RAUTE.HE" in combined_body
        assert "Raute Oyj" in combined_body
        assert "SATELLITE STRATEGY" in combined_body
        assert "Insider buying confirmed" in combined_body
        assert "650 EUR" in combined_body


def test_send_alert_core_value_setup():
    """Verify send_alert formats Subject and Body properly for CORE alerts."""
    mock_client = EmailClient(
        smtp_server="smtp.example.com",
        smtp_port=587,
        username="test@example.com",
        password="secretpassword",
        email_from="tiuku@example.com",
        email_to="trader@example.com",
    )

    analysis_summary = {
        "signals": "CORE_10BAGGER_QUALITY (GM: 68%, R40: 47%)",
        "reasoning": "Yhtiö täyttää kaikki 10-bagger kasvukriteerit: myyntikate 68%, toistuva liikevaihto 85%, Rule of 40 = 47%.",
        "gross_margin_pct": 68.0,
        "recurring_revenue": True,
        "rule_of_40_score": 47.0,
        "recommended_allocation_eur": 1125.0,
        "sizing_model": "Core Equal Weight (15% of Core Pool)",
        "spread_pct": 0.85,
        "bid": 15.20,
        "ask": 15.33,
        "volume": 25000,
        "total_friction_pct": 1.65,
        "min_recommended_trade_eur": 500.0,
        "link": "https://view.news.eu.nasdaq.com/view?id=456",
        "title": "Kemira Oyj: Osavuosikatsaus Q3 2026",
    }

    with patch("smtplib.SMTP") as mock_smtp_class:
        mock_smtp = MagicMock()
        mock_smtp_class.return_value = mock_smtp

        result = send_alert(
            pipeline_type="CORE",
            ticker="KEMIRA.HE",
            company_name="Kemira Oyj",
            analysis_summary=analysis_summary,
            email_client=mock_client,
        )

        assert result is True
        assert mock_smtp.sendmail.called
        args, kwargs = mock_smtp.sendmail.call_args
        _, _, raw_msg = args

        # Parse message
        msg_obj = email.message_from_string(raw_msg)
        assert msg_obj["Subject"] == "[CORE ALERT] Value Setup: KEMIRA.HE"

        # 2. Verify Body contents
        payloads = []
        for part in msg_obj.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                payload_data = part.get_payload(decode=True)
                if payload_data:
                    payloads.append(payload_data.decode("utf-8"))
        combined_body = " ".join(payloads)

        assert "KEMIRA.HE" in combined_body
        assert "Kemira Oyj" in combined_body
        assert "CORE STRATEGY" in combined_body
        assert "68.0%" in combined_body
        assert "47.0%" in combined_body
        assert "1,125 EUR" in combined_body
        assert "Yhtiö täyttää kaikki 10-bagger kasvukriteerit" in combined_body


def test_send_alert_unconfigured_skips_gracefully():
    """Verify send_alert returns False without raising when SMTP is unconfigured."""
    unconfigured_client = EmailClient(smtp_server="", email_to="")
    result = send_alert(
        pipeline_type="SATELLITE",
        ticker="TEST.HE",
        company_name="Test Oyj",
        analysis_summary={"reasoning": "Test signal"},
        email_client=unconfigured_client,
    )
    assert result is False
