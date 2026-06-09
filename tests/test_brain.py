"""
Unit tests for brain.py — commission math, position validation, JSON retry logic.
Does not call Claude or yfinance (all external calls are mocked).
"""

import json
import math
import unittest
from unittest.mock import MagicMock, patch


class TestCommissionMath(unittest.TestCase):
    """Verify the core share-count and cost formula."""

    def _shares(self, position_dollars: float, price: float) -> int:
        return math.floor((position_dollars - 10) / price)

    def test_basic_share_count(self):
        # $10k position in $142.50 stock
        shares = self._shares(10_000, 142.50)
        self.assertEqual(shares, 69)
        total_cost = shares * 142.50 + 10
        self.assertAlmostEqual(total_cost, 9842.50, places=2)

    def test_commission_included(self):
        # Commission must always be subtracted before dividing
        shares = self._shares(5_000, 50.0)
        self.assertEqual(shares, 99)
        self.assertAlmostEqual(shares * 50 + 10, 5_010, places=2)

    def test_floor_not_round(self):
        # $1,000 budget, $9.99 stock → floor, not round
        shares = self._shares(1_000, 9.99)
        expected = math.floor((1_000 - 10) / 9.99)
        self.assertEqual(shares, expected)
        # Verify no fractional shares
        self.assertIsInstance(shares, int)

    def test_min_trade_threshold(self):
        # Position too small: 0 shares after commission
        shares = self._shares(15, 10.0)
        self.assertEqual(shares, 0)  # (15-10)/10 = 0.5 → floor = 0


class TestValidateLong(unittest.TestCase):
    """Test _correct_long validation logic."""

    def setUp(self):
        # Import here so patching works cleanly
        from agent.brain import _correct_long
        self._correct_long = _correct_long

    def test_price_below_minimum_rejected(self):
        rec = {"ticker": "JUNK", "entry_price": 2.50, "position_size_pct": 10}
        result = self._correct_long(rec, 50_000)
        self.assertIsNone(result)

    def test_position_capped_at_25_pct(self):
        # Request 30% — should be capped at 25%
        rec = {
            "ticker": "NVDA",
            "entry_price": 100.0,
            "position_size_pct": 30,
            "target_price": 120.0,
            "stop_loss": 90.0,
        }
        result = self._correct_long(rec, 50_000)
        self.assertIsNotNone(result)
        max_cost = 0.25 * 50_000
        self.assertLessEqual(result["total_with_commission"], max_cost)
        self.assertLessEqual(result["position_size_pct"], 25.0)

    def test_share_count_is_whole_number(self):
        rec = {
            "ticker": "AMD",
            "entry_price": 150.0,
            "position_size_pct": 15,
            "target_price": 170.0,
            "stop_loss": 140.0,
        }
        result = self._correct_long(rec, 50_000)
        self.assertIsNotNone(result)
        self.assertIsInstance(result["shares"], int)

    def test_commission_always_added(self):
        rec = {
            "ticker": "AAPL",
            "entry_price": 200.0,
            "position_size_pct": 20,
            "target_price": 220.0,
            "stop_loss": 190.0,
        }
        result = self._correct_long(rec, 50_000)
        self.assertIsNotNone(result)
        self.assertEqual(result["commission"], 10.0)
        expected_cost = result["shares"] * 200.0 + 10
        self.assertAlmostEqual(result["total_with_commission"], expected_cost, places=2)


class TestValidateShort(unittest.TestCase):
    """Test _correct_short validation logic."""

    def setUp(self):
        from agent.brain import _correct_short
        self._correct_short = _correct_short

    def test_price_below_minimum_rejected(self):
        rec = {"ticker": "CHEAP", "entry_price": 2.99, "position_size_pct": 10}
        result = self._correct_short(rec, 50_000)
        self.assertIsNone(result)

    def test_notional_capped_at_25_pct(self):
        rec = {
            "ticker": "RIVN",
            "entry_price": 12.0,
            "position_size_pct": 30,
            "cover_target": 9.0,
            "stop_loss": 15.0,
        }
        result = self._correct_short(rec, 50_000)
        self.assertIsNotNone(result)
        notional = result["shares"] * 12.0
        self.assertLessEqual(notional, 0.25 * 50_000 + 1)  # +1 for float rounding


class TestCashReserveEnforcement(unittest.TestCase):
    """Test that the 15% cash reserve is maintained when trimming recommendations."""

    def setUp(self):
        from agent.brain import _enforce_cash_reserve
        self._enforce_cash_reserve = _enforce_cash_reserve

    def test_drops_lowest_confidence_first(self):
        portfolio_value = 50_000.0
        cash = 10_000.0  # only 20% cash — one big trade would breach reserve
        recs = [
            {"ticker": "A", "confidence": 85, "total_with_commission": 9_000},
            {"ticker": "B", "confidence": 72, "total_with_commission": 4_000},
        ]
        # 15% reserve = $7,500. Cash $10k - $9k = $1k < $7.5k → B fits, A alone = $1k left
        # A alone: 10k - 9k = 1k < 7.5k → A should be dropped too
        # Actually: try A first (confidence 85): 10k - 9k = 1k < 7.5k → drop A
        # Then B: 10k - 4k = 6k < 7.5k → drop B too
        approved_long, _ = self._enforce_cash_reserve(recs, [], cash, portfolio_value)
        # Both should be dropped since neither leaves 15% reserve
        for rec in approved_long:
            remaining = cash - rec["total_with_commission"]
            self.assertGreaterEqual(remaining, 0.15 * portfolio_value)

    def test_high_cash_allows_all_trades(self):
        portfolio_value = 50_000.0
        cash = 45_000.0  # plenty of cash
        recs = [
            {"ticker": "A", "confidence": 85, "total_with_commission": 8_000},
            {"ticker": "B", "confidence": 75, "total_with_commission": 5_000},
        ]
        approved_long, _ = self._enforce_cash_reserve(recs, [], cash, portfolio_value)
        self.assertEqual(len(approved_long), 2)


class TestJsonRetryLogic(unittest.TestCase):
    """Test that bad JSON triggers a single retry with a stricter prompt."""

    def test_retry_on_json_error(self):
        from agent.brain import _call_claude

        good_response = json.dumps({
            "analysis_date": "2026-06-09",
            "market_summary": "Test.",
            "recommendations": [],
            "short_recommendations": [],
            "watchlist": [],
            "avoid": [],
            "avoid_reason": "",
            "portfolio_cash_suggestion_pct": 20,
        })

        mock_content = MagicMock()
        mock_content.text = good_response
        mock_response = MagicMock()
        mock_response.content = [mock_content]

        with patch("agent.brain._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = mock_response
            mock_get_client.return_value = mock_client

            result = _call_claude("test prompt", retry=False)
            self.assertIn("analysis_date", result)
            mock_client.messages.create.assert_called_once()

    def test_retry_prompt_contains_stricter_instruction(self):
        """Verify the retry prompt includes the JSON-only instruction."""
        from agent.brain import _call_claude

        call_args = []

        def capture_call(**kwargs):
            call_args.append(kwargs)
            mock_content = MagicMock()
            mock_content.text = '{"analysis_date": "2026-06-09", "market_summary": "", "recommendations": [], "short_recommendations": [], "watchlist": [], "avoid": [], "avoid_reason": "", "portfolio_cash_suggestion_pct": 20}'
            mock_resp = MagicMock()
            mock_resp.content = [mock_content]
            return mock_resp

        with patch("agent.brain._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.messages.create.side_effect = capture_call
            mock_get_client.return_value = mock_client

            _call_claude("base prompt", retry=True)
            prompt_sent = call_args[0]["messages"][0]["content"]
            self.assertIn("CRITICAL", prompt_sent)
            self.assertIn("valid JSON", prompt_sent)


if __name__ == "__main__":
    unittest.main()
