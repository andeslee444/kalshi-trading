"""Direct tests for the extracted execution.order_monitor module."""

import time
from unittest.mock import MagicMock

from execution.order_monitor import OrderMonitor


def test_track_adds_order():
    mock_client = MagicMock()
    monitor = OrderMonitor(mock_client)

    monitor.track("order-1", "TICK-1", "yes", 50, 2)

    assert monitor.get_pending_count() == 1


def test_get_pending_capital():
    mock_client = MagicMock()
    monitor = OrderMonitor(mock_client)
    monitor.track("order-1", "TICK-1", "yes", 50, 2)
    monitor.track("order-2", "TICK-2", "no", 30, 3)

    assert monitor.get_pending_capital() == 190


def test_check_orders_removes_filled():
    mock_client = MagicMock()
    mock_client.get.return_value = {"orders": []}
    monitor = OrderMonitor(mock_client, check_interval=0)
    monitor.track("order-1", "TICK-1", "yes", 50, 1)

    changes = monitor.check_orders()

    assert changes["order-1"]["status"] == "filled_or_canceled"
    assert monitor.get_pending_count() == 0


def test_check_orders_cancels_stale():
    mock_client = MagicMock()
    mock_client.get.return_value = {"orders": [{"order_id": "order-1"}]}
    monitor = OrderMonitor(mock_client, max_age_seconds=0, check_interval=0)
    monitor.track("order-1", "TICK-1", "yes", 50, 1)
    monitor._pending["order-1"]["placed_at"] = time.time() - 100

    changes = monitor.check_orders()

    assert changes["order-1"]["action"] == "canceled"
    mock_client.delete.assert_called_once_with("/portfolio/orders/order-1")
    assert monitor.get_pending_count() == 0


def test_check_orders_keeps_fresh_resting():
    mock_client = MagicMock()
    mock_client.get.return_value = {"orders": [{"order_id": "order-1"}]}
    monitor = OrderMonitor(mock_client, max_age_seconds=600, check_interval=0)
    monitor.track("order-1", "TICK-1", "yes", 50, 1)

    assert monitor.check_orders() == {}
    assert monitor.get_pending_count() == 1


def test_cancel_order_failure():
    mock_client = MagicMock()
    mock_client.delete.side_effect = Exception("API error")
    monitor = OrderMonitor(mock_client)

    assert monitor.cancel_order("order-1") is False
