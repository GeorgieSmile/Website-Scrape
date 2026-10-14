"""Offline regression checks for browser handoff, concurrency, and safe failures."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from requests.cookies import RequestsCookieJar

import scrape
from browser_access import (
    BrowserCredentials,
    BrowserVerification,
    BrowserVerificationError,
    VerifiedSession,
    capture_browser_credentials,
)

URL = "https://38rat.nstru.ac.th/backend/ajax/public/innovation.php"
CHALLENGE = "<html><title>One moment, please...</title></html>"


def response(text: str) -> requests.Response:
    result = requests.Response()
    result.status_code = 200
    result._content = text.encode()
    result._content_consumed = True
    return result


def credentials(value: str = "verified") -> BrowserCredentials:
    cookies = RequestsCookieJar()
    cookies.set("wssplashchk", value, domain="38rat.nstru.ac.th", path="/", secure=True)
    return BrowserCredentials("actual-browser-agent", cookies)


class SessionVerificationTests(unittest.TestCase):
    def test_cli_help_renders_browser_options_and_removal_guard(self) -> None:
        help_text = scrape.build_parser().format_help()
        self.assertIn("--chrome-path", help_text)
        self.assertIn("--browser-timeout", help_text)
        self.assertIn("--browser ", help_text)
        self.assertIn("25%", help_text)

    def test_unblocked_request_does_not_launch_chrome(self) -> None:
        bootstrap = Mock(return_value=credentials())
        session = scrape.create_session(
            "collector", BrowserVerification(bootstrap=bootstrap)
        )
        with patch.object(
            requests.Session, "request", return_value=response('{"ok":true}')
        ):
            self.assertEqual(session.get(URL).json(), {"ok": True})
        bootstrap.assert_not_called()
        self.assertEqual(session.headers["User-Agent"], "collector")
        session.close()

    def test_title_inside_valid_json_does_not_trigger_browser(self) -> None:
        bootstrap = Mock(return_value=credentials())
        payload = {"description": "<title>One moment, please...</title>"}
        with (
            scrape.create_session(
                "collector", BrowserVerification(bootstrap=bootstrap)
            ) as session,
            patch.object(
                requests.Session, "request", return_value=response(json.dumps(payload))
            ),
        ):
            self.assertEqual(session.get(URL).json(), payload)
        bootstrap.assert_not_called()

    def test_challenge_retries_same_payload_with_browser_credentials(self) -> None:
        bootstrap = Mock(return_value=credentials())
        access = BrowserVerification(bootstrap=bootstrap)
        session = scrape.create_session("collector", access)
        payload = {"action": "fetch_innovationAll_JSON"}
        calls = []

        def request(current, method, url, **kwargs):
            calls.append((method, url, kwargs))
            if len(calls) == 1:
                return response(CHALLENGE)
            self.assertEqual(current.headers["User-Agent"], "actual-browser-agent")
            self.assertEqual(current.headers["Origin"], "https://38rat.nstru.ac.th")
            self.assertEqual(current.headers["Referer"], "https://38rat.nstru.ac.th/")
            cookie = next(iter(current.cookies))
            self.assertEqual(
                (cookie.domain, cookie.path, cookie.secure),
                ("38rat.nstru.ac.th", "/", True),
            )
            return response('{"ok":true}')

        with patch.object(requests.Session, "request", new=request):
            self.assertTrue(session.post(URL, json=payload, timeout=45).json()["ok"])
        self.assertEqual(calls[0], calls[1])
        bootstrap.assert_called_once()
        session.close()

    def test_workers_get_independent_sessions_and_cookie_jars(self) -> None:
        access = BrowserVerification(bootstrap=lambda: credentials())
        access.recover(0)
        provider = scrape.thread_session_provider("collector", access)
        barrier = threading.Barrier(4)

        def worker(_):
            session = provider()
            assert isinstance(session, VerifiedSession)
            access.apply(session)
            barrier.wait(timeout=5)
            self.assertIs(provider(), session)
            return session

        with ThreadPoolExecutor(max_workers=4) as pool:
            sessions = list(pool.map(worker, range(4)))
        self.assertEqual(len({id(s) for s in sessions}), 4)
        self.assertEqual(len({id(s.cookies) for s in sessions}), 4)
        sessions[0].cookies.clear()
        for session in sessions[1:]:
            self.assertEqual(session.cookies.get("wssplashchk"), "verified")
            self.assertEqual(session.headers["User-Agent"], "actual-browser-agent")
        for session in sessions:
            session.close()

    def test_simultaneously_blocked_workers_launch_only_one_browser(self) -> None:
        bootstrap = Mock(return_value=credentials())
        access = BrowserVerification(bootstrap=bootstrap)
        barrier = threading.Barrier(4)

        def request(current, method, url, **kwargs):
            if not current.cookies:
                barrier.wait(timeout=5)
                return response(CHALLENGE)
            return response('{"ok":true}')

        def worker(_):
            with scrape.create_session("collector", access) as session:
                return session.get(URL).json()

        with (
            patch.object(requests.Session, "request", new=request),
            ThreadPoolExecutor(max_workers=4) as pool,
        ):
            results = list(pool.map(worker, range(4)))
        self.assertEqual(results, [{"ok": True}] * 4)
        bootstrap.assert_called_once()

    def test_expired_credentials_refresh_once_across_workers(self) -> None:
        bootstrap = Mock(side_effect=[credentials("old"), credentials("new")])
        access = BrowserVerification(bootstrap=bootstrap)
        access.recover(0)
        barrier = threading.Barrier(4)

        def request(current, method, url, **kwargs):
            if current.cookies.get("wssplashchk") == "old":
                barrier.wait(timeout=5)
                return response(CHALLENGE)
            self.assertEqual(current.cookies.get("wssplashchk"), "new")
            return response('{"ok":true}')

        def worker(_):
            with scrape.create_session("collector", access) as session:
                return session.get(URL).json()

        with (
            patch.object(requests.Session, "request", new=request),
            ThreadPoolExecutor(max_workers=4) as pool,
        ):
            self.assertEqual(list(pool.map(worker, range(4))), [{"ok": True}] * 4)
        self.assertEqual(bootstrap.call_count, 2)

    def test_persistent_block_exhausts_budget_without_endless_retries(self) -> None:
        bootstrap = Mock(return_value=credentials())
        access = BrowserVerification(bootstrap=bootstrap)
        with (
            scrape.create_session("collector", access) as session,
            patch.object(
                requests.Session, "request", return_value=response(CHALLENGE)
            ) as request,
            self.assertRaisesRegex(BrowserVerificationError, "budget exhausted"),
        ):
            session.get(URL)
        self.assertEqual(bootstrap.call_count, 2)
        self.assertEqual(request.call_count, 3)
        with (
            scrape.create_session("collector", access) as other,
            self.assertRaises(BrowserVerificationError),
        ):
            other.get(URL)
        self.assertEqual(bootstrap.call_count, 2)

    def test_startup_failure_is_shared_not_retried_by_every_worker(self) -> None:
        bootstrap = Mock(
            side_effect=BrowserVerificationError(
                "browser verification required: timed out"
            )
        )
        access = BrowserVerification(bootstrap=bootstrap)
        for _ in range(3):
            with (
                scrape.create_session("collector", access) as session,
                patch.object(
                    requests.Session, "request", return_value=response(CHALLENGE)
                ),
                self.assertRaisesRegex(BrowserVerificationError, "timed out"),
            ):
                session.get(URL)
        bootstrap.assert_called_once()

    def test_failed_live_collection_preserves_existing_output(self) -> None:
        args = scrape.build_parser().parse_args(["--dataset", "innovations", "--quiet"])
        access = BrowserVerification(
            bootstrap=Mock(
                side_effect=BrowserVerificationError(
                    "browser verification required: timed out"
                )
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "innovations.json"
            original = b'{"data":[{"record_id":"existing"}]}\n'
            output.write_bytes(original)
            stdout = io.StringIO()
            with (
                patch.object(scrape, "OUTPUT_DIR", Path(directory)),
                patch.object(scrape, "BrowserVerification", return_value=access),
                patch.object(
                    requests.Session, "request", return_value=response(CHALLENGE)
                ),
                contextlib.redirect_stdout(stdout),
            ):
                self.assertEqual(scrape.run_scrape(args), 1)
            self.assertEqual(output.read_bytes(), original)
            result = json.loads(stdout.getvalue())["datasets"]["innovation"]
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["output_written"])


class ChromeLifecycleTests(unittest.TestCase):
    def run_capture(self, helper_result, *, stubborn=False):
        process = Mock(pid=987654)
        process.poll.return_value = None
        if stubborn:
            process.wait.side_effect = [subprocess.TimeoutExpired("chrome", 5), 0]
        profile_paths = []

        def launch(command, **kwargs):
            profile_paths.append(
                Path(
                    next(
                        a.split("=", 1)[1]
                        for a in command
                        if a.startswith("--user-data-dir=")
                    )
                )
            )
            port = int(
                next(
                    a.split("=", 1)[1]
                    for a in command
                    if a.startswith("--remote-debugging-port=")
                )
            )
            self.assertGreater(port, 0)
            self.assertTrue(profile_paths[-1].is_dir())
            self.assertTrue(kwargs["start_new_session"])
            return process

        with (
            patch("browser_access.chrome_executable", return_value="/fake/chrome"),
            patch("browser_access.shutil.which", return_value="/fake/node"),
            patch("browser_access.subprocess.Popen", side_effect=launch),
            patch(
                "browser_access.subprocess.run",
                side_effect=helper_result
                if isinstance(helper_result, Exception)
                else None,
                return_value=helper_result,
            ),
            patch("browser_access.os.killpg") as terminate,
        ):
            try:
                return capture_browser_credentials(timeout=1)
            finally:
                process.terminate.assert_called_once()
                if stubborn:
                    terminate.assert_called_once()
                    self.assertEqual(process.wait.call_count, 2)
                else:
                    terminate.assert_not_called()
                    process.wait.assert_called_once()
                self.assertTrue(profile_paths)
                self.assertFalse(profile_paths[0].exists())

    def test_helper_timeout_closes_chrome_and_removes_profile(self) -> None:
        with self.assertRaisesRegex(BrowserVerificationError, "timed out"):
            self.run_capture(subprocess.TimeoutExpired("node", 1))

    def test_unresponsive_chrome_is_force_stopped_after_grace_period(self) -> None:
        with self.assertRaisesRegex(BrowserVerificationError, "timed out"):
            self.run_capture(subprocess.TimeoutExpired("node", 1), stubborn=True)

    def test_invalid_helper_result_fails_closed_and_cleans_up(self) -> None:
        result = subprocess.CompletedProcess([], 0, stdout="not JSON", stderr="")
        with self.assertRaisesRegex(BrowserVerificationError, "invalid credentials"):
            self.run_capture(result)

    def test_success_preserves_cookie_attributes_and_cleans_up(self) -> None:
        payload = {
            "user_agent": "browser",
            "cookies": [
                {
                    "name": "wssplashchk",
                    "value": "private",
                    "domain": ".38rat.nstru.ac.th",
                    "path": "/",
                    "secure": True,
                }
            ],
        }
        result = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps(payload), stderr=""
        )
        actual = self.run_capture(result)
        self.assertEqual(actual.user_agent, "browser")
        cookie = next(iter(actual.cookies))
        self.assertEqual(
            (cookie.domain, cookie.path, cookie.secure),
            (".38rat.nstru.ac.th", "/", True),
        )

    def test_missing_chrome_is_actionable(self) -> None:
        with (
            patch("browser_access.shutil.which", return_value=None),
            self.assertRaisesRegex(BrowserVerificationError, "Chrome not found"),
        ):
            capture_browser_credentials()

    def test_missing_node_is_actionable(self) -> None:
        with (
            patch("browser_access.chrome_executable", return_value="/fake/chrome"),
            patch("browser_access.shutil.which", return_value=None),
            self.assertRaisesRegex(BrowserVerificationError, "Node.js"),
        ):
            capture_browser_credentials()


if __name__ == "__main__":
    unittest.main()
