"""Tests for webhook alerting (notify_webhook)."""

import os
from unittest.mock import patch, MagicMock

from kalshi_auth import notify_webhook, _reset_webhook_rate_limiter


class TestNotifyWebhook:

    def setup_method(self):
        _reset_webhook_rate_limiter()

    def teardown_method(self):
        _reset_webhook_rate_limiter()

    @patch("ops.notifications.subprocess.run")
    def test_returns_false_without_notification_target(self, mock_run):
        """No notification target -> returns False immediately."""
        with patch.dict(os.environ, {}, clear=True):
            assert notify_webhook("test message") is False
        mock_run.assert_not_called()

    @patch("ops.notifications.subprocess.run")
    def test_routes_alerts_through_whatsapp(self, mock_run):
        """Legacy webhook alerts should route through WhatsApp."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        with patch.dict(os.environ, {"NOTIFICATION_PHONE": "+15555550123"}):
            result = notify_webhook("test message", level="info")
        assert result is True
        command = mock_run.call_args[0][0]
        assert command[4] == "+15555550123"
        assert command[6] == "ℹ️ [INFO] test message"
        assert command[8] == "whatsapp"

    @patch("ops.notifications.subprocess.run")
    def test_rate_limiting_dedup(self, mock_run):
        """Same message within 30 minutes should be rate-limited."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        with patch.dict(os.environ, {"NOTIFICATION_PHONE": "+15555550123"}):
            result1 = notify_webhook("Circuit breaker OPEN", level="critical")
            result2 = notify_webhook("Circuit breaker OPEN", level="critical")
        assert result1 is True
        assert result2 is False
        assert mock_run.call_count == 1

    @patch("ops.notifications.subprocess.run")
    def test_different_messages_not_rate_limited(self, mock_run):
        """Different messages should not be rate-limited against each other."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        with patch.dict(os.environ, {"NOTIFICATION_PHONE": "+15555550123"}):
            result1 = notify_webhook("Circuit breaker OPEN", level="critical")
            result2 = notify_webhook("Daily loss limit reached", level="warning")
        assert result1 is True
        assert result2 is True
        assert mock_run.call_count == 2

    @patch("ops.notifications.subprocess.run")
    def test_missing_openclaw_returns_false(self, mock_run):
        """Missing openclaw CLI should return False gracefully."""
        mock_run.side_effect = FileNotFoundError()
        with patch.dict(os.environ, {"NOTIFICATION_PHONE": "+15555550123"}):
            result = notify_webhook("test error", level="critical")
        assert result is False

    @patch("ops.notifications.subprocess.run")
    def test_unexpected_error_returns_false(self, mock_run):
        """Unexpected subprocess errors should return False gracefully."""
        mock_run.side_effect = RuntimeError("boom")
        with patch.dict(os.environ, {"NOTIFICATION_PHONE": "+15555550123"}):
            result = notify_webhook("test error", level="critical")
        assert result is False

    @patch("ops.notifications.subprocess.run")
    def test_level_emoji_prefix(self, mock_post):
        """Messages should include level-appropriate text prefix."""
        mock_post.return_value = MagicMock(returncode=0, stderr="")
        with patch.dict(os.environ, {"NOTIFICATION_PHONE": "+15555550123"}):
            notify_webhook("test", level="critical")
        command = mock_post.call_args[0][0]
        assert command[6] == "🚨 [CRITICAL] test"
