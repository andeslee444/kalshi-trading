"""Tests for webhook alerting (notify_webhook)."""

import os
import time
import pytest
from unittest.mock import patch, MagicMock

from kalshi_auth import notify_webhook, _reset_webhook_rate_limiter


class TestNotifyWebhook:

    def setup_method(self):
        _reset_webhook_rate_limiter()

    def teardown_method(self):
        _reset_webhook_rate_limiter()

    def test_returns_false_without_url(self):
        """No ALERT_WEBHOOK_URL -> returns False immediately."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("ALERT_WEBHOOK_URL", None)
            assert notify_webhook("test message") is False

    @patch("kalshi_auth.requests.post")
    def test_slack_format(self, mock_post):
        """Slack URLs should use {"text": ...} format."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp
        with patch.dict(os.environ, {"ALERT_WEBHOOK_URL": "https://hooks.slack.com/services/T/B/X"}):
            result = notify_webhook("test message", level="info")
        assert result is True
        call_kwargs = mock_post.call_args
        payload = call_kwargs[1]["json"] if "json" in call_kwargs[1] else call_kwargs[0][1] if len(call_kwargs[0]) > 1 else None
        # Should use "text" key for Slack
        assert "text" in payload
        assert "content" not in payload

    @patch("kalshi_auth.requests.post")
    def test_discord_format(self, mock_post):
        """Discord URLs should use {"content": ...} format."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp
        with patch.dict(os.environ, {"ALERT_WEBHOOK_URL": "https://discord.com/api/webhooks/123/abc"}):
            result = notify_webhook("test message", level="warning")
        assert result is True
        call_kwargs = mock_post.call_args
        payload = call_kwargs[1]["json"] if "json" in call_kwargs[1] else None
        assert "content" in payload
        assert "text" not in payload

    @patch("kalshi_auth.requests.post")
    def test_rate_limiting_dedup(self, mock_post):
        """Same message within 30 minutes should be rate-limited."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp
        with patch.dict(os.environ, {"ALERT_WEBHOOK_URL": "https://hooks.slack.com/services/T/B/X"}):
            result1 = notify_webhook("Circuit breaker OPEN", level="critical")
            result2 = notify_webhook("Circuit breaker OPEN", level="critical")
        assert result1 is True
        assert result2 is False
        assert mock_post.call_count == 1

    @patch("kalshi_auth.requests.post")
    def test_different_messages_not_rate_limited(self, mock_post):
        """Different messages should not be rate-limited against each other."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp
        with patch.dict(os.environ, {"ALERT_WEBHOOK_URL": "https://hooks.slack.com/services/T/B/X"}):
            result1 = notify_webhook("Circuit breaker OPEN", level="critical")
            result2 = notify_webhook("Daily loss limit reached", level="warning")
        assert result1 is True
        assert result2 is True
        assert mock_post.call_count == 2

    @patch("kalshi_auth.requests.post")
    def test_http_error_handling(self, mock_post):
        """HTTP errors should return False gracefully."""
        import requests as req
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.raise_for_status.side_effect = req.exceptions.HTTPError(response=mock_resp)
        mock_post.return_value = mock_resp
        with patch.dict(os.environ, {"ALERT_WEBHOOK_URL": "https://hooks.slack.com/services/T/B/X"}):
            result = notify_webhook("test error", level="critical")
        assert result is False

    @patch("kalshi_auth.requests.post")
    def test_connection_error_handling(self, mock_post):
        """Connection errors should return False gracefully."""
        import requests as req
        mock_post.side_effect = req.exceptions.ConnectionError("refused")
        with patch.dict(os.environ, {"ALERT_WEBHOOK_URL": "https://hooks.slack.com/services/T/B/X"}):
            result = notify_webhook("test error", level="critical")
        assert result is False

    @patch("kalshi_auth.requests.post")
    def test_level_emoji_prefix(self, mock_post):
        """Messages should include level-appropriate emoji."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp
        with patch.dict(os.environ, {"ALERT_WEBHOOK_URL": "https://hooks.slack.com/services/T/B/X"}):
            notify_webhook("test", level="critical")
        payload = mock_post.call_args[1]["json"]
        assert "[CRITICAL]" in payload["text"]
