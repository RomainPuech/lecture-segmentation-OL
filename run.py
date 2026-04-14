#!/usr/bin/env python3
"""
Startup script for the Slide Deck Segmenter.

Usage:
    python run.py
"""

import subprocess
import sys
import os
from pathlib import Path


def install_requirements():
    req = Path(__file__).parent / "requirements.txt"
    print("Installing / verifying dependencies…")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "-r", str(req), "--quiet"]
    )


def main():
    install_requirements()

    app_dir = Path(__file__).parent / "app"
    print("\n" + "=" * 55)
    print("  Slide Deck Segmenter")
    print("  Open http://127.0.0.1:8000 in your browser")
    print("=" * 55 + "\n")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(app_dir)

    subprocess.call(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
            "--app-dir",
            str(app_dir),
        ],
        env=env,
    )


if __name__ == "__main__":
    main()
