"""Shared browser helpers for the sender scripts."""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service


def _base_chrome_args(options: Options) -> Options:
    options.add_argument("--start-maximized")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-component-update")
    options.add_argument("--disable-default-apps")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--log-level=3")
    return options


def launch_fast_chrome(profile_dir: str | os.PathLike[str] | None = None):
    options = _base_chrome_args(Options())
    if profile_dir:
        profile_path = Path(profile_dir).resolve()
        profile_path.mkdir(parents=True, exist_ok=True)
        options.add_argument(f"--user-data-dir={profile_path}")
    service = Service(log_path=os.devnull)
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(25)
    return driver


def should_use_undetected_chrome() -> bool:
    return os.getenv("USE_UNDETECTED_CHROME", "").strip().lower() in {"1", "true", "yes", "y"}


def _chrome_candidates():
    local_app_data = os.getenv("LOCALAPPDATA", "")
    return [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(local_app_data) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]


def find_chrome_executable() -> Path:
    for candidate in _chrome_candidates():
        if candidate.exists():
            return candidate
    raise FileNotFoundError("Google Chrome executable was not found.")


def _port_is_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def launch_or_attach_real_chrome(
    profile_dir: str | os.PathLike[str],
    debugger_port: int = 9222,
    startup_timeout: int = 20,
):
    """Use a real Chrome window and attach Selenium to it.

    This is useful for sites that behave better with a normal, persistent Chrome
    session than with a freshly automated browser profile.
    """

    profile_path = Path(profile_dir).resolve()
    profile_path.mkdir(parents=True, exist_ok=True)

    if not _port_is_open("127.0.0.1", debugger_port):
        chrome_exe = find_chrome_executable()
        subprocess.Popen(
            [
                str(chrome_exe),
                f"--remote-debugging-port={debugger_port}",
                f"--user-data-dir={profile_path}",
                "--start-maximized",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        started = time.time()
        while time.time() - started < startup_timeout:
            if _port_is_open("127.0.0.1", debugger_port):
                break
            time.sleep(0.5)

    options = Options()
    options.add_experimental_option("debuggerAddress", f"127.0.0.1:{debugger_port}")
    service = Service(log_path=os.devnull)
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(25)
    return driver
