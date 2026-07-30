import subprocess
import unittest
from unittest.mock import MagicMock, patch

from utils.browser_utils import ManagedChromeWindow


class ManagedChromeWindowTests(unittest.TestCase):
    @patch("utils.browser_utils.shutil.rmtree")
    @patch("utils.browser_utils.tempfile.mkdtemp", return_value=r"C:\temp\app-profile")
    @patch("utils.browser_utils.subprocess.Popen")
    @patch("utils.browser_utils.get_chrome_binary_path", return_value=r"C:\Chrome\chrome.exe")
    def test_managed_window_uses_isolated_profile_and_closes_only_its_process(
        self, _chrome_path, popen, _mkdtemp, rmtree
    ):
        process = MagicMock()
        process.poll.return_value = None
        popen.return_value = process
        window = ManagedChromeWindow()

        self.assertTrue(window.open("http://127.0.0.1:58220/"))
        args = popen.call_args.args[0]
        self.assertEqual(args[0], r"C:\Chrome\chrome.exe")
        self.assertIn(r"--user-data-dir=C:\temp\app-profile", args)
        self.assertIn("--new-window", args)
        self.assertIn("--disable-background-mode", args)
        self.assertEqual(args[-1], "http://127.0.0.1:58220/")

        window.close()

        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=5)
        rmtree.assert_called_once_with(r"C:\temp\app-profile", ignore_errors=True)

    @patch("utils.browser_utils.webbrowser.open")
    @patch("utils.browser_utils.get_chrome_binary_path", return_value=None)
    def test_default_browser_fallback_is_reported_as_unmanaged(
        self, _chrome_path, browser_open
    ):
        window = ManagedChromeWindow()

        self.assertFalse(window.open("http://127.0.0.1:58220/"))
        browser_open.assert_called_once_with("http://127.0.0.1:58220/")

    @patch("utils.browser_utils.shutil.rmtree")
    @patch("utils.browser_utils.tempfile.mkdtemp", return_value=r"C:\temp\app-profile")
    @patch("utils.browser_utils.subprocess.Popen")
    @patch("utils.browser_utils.get_chrome_binary_path", return_value=r"C:\Chrome\chrome.exe")
    def test_close_kills_a_window_that_does_not_exit_cleanly(
        self, _chrome_path, popen, _mkdtemp, _rmtree
    ):
        process = MagicMock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("chrome", 5), None]
        popen.return_value = process
        window = ManagedChromeWindow()
        window.open("http://127.0.0.1:58220/")

        window.close()

        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)


if __name__ == "__main__":
    unittest.main()
