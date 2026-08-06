import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from services import event_log_service


class EventTimezoneTests(unittest.TestCase):
    def _driver_log_with_system_info(self, timezone_text):
        temp_dir = tempfile.TemporaryDirectory()
        log_path = os.path.join(temp_dir.name, "driver.log")
        info_path = os.path.join(temp_dir.name, "system_info.txt")
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write("sample\n")
        with open(info_path, "w", encoding="utf-8") as handle:
            json.dump({"System Time Zone": timezone_text}, handle)
        return temp_dir, log_path

    def test_customer_timezone_accepts_current_gmt_compact_shape(self):
        temp_dir, log_path = self._driver_log_with_system_info(
            "Central Standard Time (GMT-0500)"
        )
        self.addCleanup(temp_dir.cleanup)

        self.assertEqual(
            event_log_service.find_capture_utc_offset_minutes(log_path), -300
        )

    def test_customer_timezone_still_accepts_utc_colon_shape(self):
        temp_dir, log_path = self._driver_log_with_system_info(
            "(UTC+08:00) Taipei"
        )
        self.addCleanup(temp_dir.cleanup)

        self.assertEqual(
            event_log_service.find_capture_utc_offset_minutes(log_path), 480
        )

    @patch("services.event_log_service.local_utc_offset_minutes", return_value=480)
    @patch("services.event_log_service.peek_event_log_utc_datetime")
    @patch("services.event_log_service.find_capture_utc_offset_minutes", return_value=-300)
    def test_wifi_aligns_events_to_engineer_local_not_customer(
        self, _customer_offset, event_time, _local_offset
    ):
        event_time.return_value = datetime(2026, 6, 2, 21, 45, tzinfo=timezone.utc)

        result = event_log_service.resolve_event_time_alignment(
            "wifi.log", "System.evt", "wifi"
        )

        self.assertEqual(result["offset_min"], 480)
        self.assertEqual(result["basis"], "engineer_local")
        self.assertEqual(result["customer_offset_min"], -300)

    @patch("services.event_log_service.find_capture_utc_offset_minutes", return_value=-300)
    def test_bt_aligns_events_and_hci_to_customer_timezone(self, _customer_offset):
        result = event_log_service.resolve_event_time_alignment(
            "capture.hci.txt", "System.evt", "bt"
        )

        self.assertEqual(result["offset_min"], -300)
        self.assertEqual(result["basis"], "customer")

    @patch("services.event_log_service.peek_event_log_utc_datetime")
    def test_date_anchor_is_converted_before_crossing_midnight(self, event_time):
        event_time.return_value = datetime(2026, 6, 2, 21, 45, tzinfo=timezone.utc)

        self.assertEqual(
            event_log_service.peek_event_log_date("System.evt", 480).isoformat(),
            "2026-06-03",
        )


if __name__ == "__main__":
    unittest.main()
