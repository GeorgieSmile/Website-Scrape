"""Bounded Chrome verification shared by otherwise independent requests sessions."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import requests
from requests.cookies import RequestsCookieJar

HOST = "38rat.nstru.ac.th"
BASE_URL = f"https://{HOST}/"
HELPER = Path(__file__).with_name("chrome_verify.mjs")


class BrowserVerificationError(RuntimeError):
    """The browser check could not be completed within the run's bounded budget."""


def is_browser_challenge(text: str) -> bool:
    # A title string inside legitimate JSON data is not a challenge document.
    if not text.lstrip("\ufeff \t\r\n").startswith("<"):
        return False
    return bool(
        re.search(
            r"<title\b[^>]*>\s*(?:one|just)\s+(?:a\s+)?moment\b",
            text,
            re.IGNORECASE,
        )
    )


def reject_browser_challenge(text: str) -> None:
    if is_browser_challenge(text):
        raise BrowserVerificationError("browser verification required by the MRU site")


@dataclass(frozen=True)
class BrowserCredentials:
    user_agent: str
    cookies: RequestsCookieJar


def chrome_executable(explicit_path: str | None) -> str:
    candidates = (
        [explicit_path]
        if explicit_path
        else [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "google-chrome",
            "chromium",
            "chromium-browser",
        ]
    )
    for candidate in candidates:
        if candidate and (resolved := shutil.which(candidate)):
            return resolved
    raise BrowserVerificationError(
        "browser verification required: Chrome not found; install Chrome or set --chrome-path"
    )


def capture_browser_credentials(
    chrome_path: str | None = None, timeout: float = 40
) -> BrowserCredentials:
    """Use a fresh visible Chrome profile; always close Chrome and remove the profile."""
    chrome = chrome_executable(chrome_path)
    node = shutil.which("node")
    if node is None:
        raise BrowserVerificationError(
            "browser verification required: install Node.js 22+ for the Chrome helper"
        )
    with TemporaryDirectory(prefix="mru-chrome-") as profile:
        # A nonzero debugging port is important: port=0 failed in the live probe.
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        try:
            process = subprocess.Popen(
                [
                    chrome,
                    f"--user-data-dir={profile}",
                    f"--remote-debugging-port={port}",
                    "--remote-debugging-address=127.0.0.1",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-background-networking",
                    "about:blank",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as error:
            raise BrowserVerificationError(
                "browser verification required: Chrome could not be launched"
            ) from error
        try:
            result = subprocess.run(
                [node, str(HELPER), str(port), str(timeout)],
                capture_output=True,
                text=True,
                timeout=timeout + 5,
                check=False,
            )
            if result.returncode:
                # Helper errors never include cookie values or page content.
                message = result.stderr.strip() or "Chrome verification failed"
                raise BrowserVerificationError(
                    f"browser verification required: {message}"
                )
            payload = json.loads(result.stdout)
            user_agent = payload["user_agent"]
            cookies = RequestsCookieJar()
            if not isinstance(user_agent, str) or not user_agent:
                raise ValueError("missing browser user agent")
            for cookie in payload["cookies"]:
                if cookie["domain"].lstrip(".") != HOST:
                    raise ValueError("unexpected cookie domain")
                cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie["domain"],
                    path=cookie["path"],
                    secure=cookie["secure"],
                )
            if not any(cookie.name == "wssplashchk" for cookie in cookies):
                raise ValueError("browser verification cookie missing")
            return BrowserCredentials(user_agent, cookies)
        except subprocess.TimeoutExpired as error:
            raise BrowserVerificationError(
                "browser verification required: Chrome verification timed out"
            ) from error
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise BrowserVerificationError(
                "browser verification required: Chrome helper returned invalid credentials"
            ) from error
        finally:
            # Let Chrome close its own workers before deleting the profile. Killing
            # the whole group immediately can race with final profile writes.
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Only our dedicated process group is touched, never personal Chrome.
                if os.name == "posix":
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait(timeout=5)


class BrowserVerification:
    """One initial verification plus one refresh per run, coordinated across workers."""

    def __init__(
        self,
        chrome_path: str | None = None,
        timeout: float = 40,
        report: Callable[[str], None] = lambda _: None,
        bootstrap: Callable[[], BrowserCredentials] | None = None,
    ) -> None:
        self._bootstrap = bootstrap or (
            lambda: capture_browser_credentials(chrome_path, timeout)
        )
        self._report = report
        self._lock = threading.Lock()
        self._credentials: BrowserCredentials | None = None
        self._generation = 0
        self._attempts = 0
        self._failure: str | None = None

    def apply(self, session: VerifiedSession) -> int:
        with self._lock:
            if self._failure:
                raise BrowserVerificationError(self._failure)
            if (
                self._credentials
                and session.verification_generation != self._generation
            ):
                session.cookies.clear()
                session.cookies.update(self._credentials.cookies.copy())
                session.headers["User-Agent"] = self._credentials.user_agent
                session.verification_generation = self._generation
            return self._generation

    def recover(self, observed_generation: int) -> None:
        with self._lock:
            if self._failure:
                raise BrowserVerificationError(self._failure)
            if observed_generation != self._generation:
                return  # Another worker already refreshed these credentials.
            if self._attempts >= 2:
                self._failure = "browser verification required: run's verification/refresh budget exhausted"
                raise BrowserVerificationError(self._failure)
            self._attempts += 1
            self._report(
                "MRU browser verification: opening temporary Chrome"
                if self._attempts == 1
                else "MRU browser verification: refreshing once"
            )
            try:
                self._credentials = self._bootstrap()
            except BrowserVerificationError as error:
                self._failure = str(error)
                raise
            except Exception as error:
                self._failure = f"browser verification required: Chrome startup failed ({type(error).__name__})"
                raise BrowserVerificationError(self._failure) from error
            self._generation += 1
            self._report(
                "MRU browser verification: cookies ready; retrying through requests"
            )


class VerifiedSession(requests.Session):
    """A requests session that obtains/reuses shared browser credentials only if blocked."""

    def __init__(self, verification: BrowserVerification) -> None:
        super().__init__()
        self.verification = verification
        self.verification_generation = 0

    def request(self, *args: Any, **kwargs: Any) -> requests.Response:
        for _ in range(3):
            generation = self.verification.apply(self)
            response = super().request(*args, **kwargs)
            if not is_browser_challenge(response.text):
                return response
            response.close()
            self.verification.recover(generation)
        raise BrowserVerificationError(
            "browser verification required: request retry budget exhausted"
        )
