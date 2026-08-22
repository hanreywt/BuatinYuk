"""Refuse to let a credential reach a commit.

Written after a real bot token was pasted into `.env.example` - a tracked file - and
swept into two commits by `git add -A`. The lesson was not "be more careful"; it was
that nothing mechanical was checking.

    python scripts/check_secrets.py            # scan tracked + staged files
    python scripts/check_secrets.py --staged   # scan only what is staged (hook mode)
    python scripts/check_secrets.py --install  # install as a pre-commit hook

Exit code 0 means clean, 1 means something looks like a secret.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = PROJECT_ROOT / ".git" / "hooks" / "pre-commit"

#: Values that are meant to appear in the template and are not secrets.
KNOWN_PLACEHOLDERS = {
    "123456789:AAExampleFakeTokenReplaceMeWithYourOwn",
    "000000000",
}

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Telegram bot token: <digits>:<35ish base64-ish chars>
    ("Telegram bot token", re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("OpenAI API key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

#: Files that legitimately discuss token shapes: this scanner and its tests.
ALLOWLIST = {
    "scripts/check_secrets.py",
    "app/utils/logging.py",
    "tests/test_secret_scan.py",
}

SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".safetensors", ".db", ".pyc"}


def git(*args: str) -> list[str]:
    result = subprocess.run(
        ["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def files_to_scan(staged_only: bool) -> list[str]:
    if staged_only:
        return git("diff", "--cached", "--name-only", "--diff-filter=ACM")
    return git("ls-files")


def scan_text(text: str) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    for label, pattern in PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(0)
            if value in KNOWN_PLACEHOLDERS:
                continue
            findings.append((label, value))
    return findings


def redact(value: str) -> str:
    """Never print a whole secret, even while reporting it."""
    if len(value) <= 10:
        return value[:2] + "..."
    return f"{value[:6]}...{value[-2:]} ({len(value)} chars)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="scan only staged changes")
    parser.add_argument("--install", action="store_true", help="install as a pre-commit hook")
    args = parser.parse_args()

    if args.install:
        return install_hook()

    problems: list[tuple[str, str, str]] = []
    for relative in files_to_scan(args.staged):
        if relative in ALLOWLIST or Path(relative).suffix.lower() in SKIP_SUFFIXES:
            continue
        path = PROJECT_ROOT / relative
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for label, value in scan_text(text):
            problems.append((relative, label, value))

    if not problems:
        print("secret scan: clean")
        return 0

    print("SECRET SCAN FAILED - these look like real credentials in tracked files:\n")
    for relative, label, value in problems:
        print(f"  {relative}: {label} {redact(value)}")
    print(
        "\nSecrets belong in .env, which is gitignored. If one of these is already\n"
        "committed, rotate it - revoke a Telegram token with @BotFather /revoke."
    )
    return 1


def install_hook() -> int:
    if not (PROJECT_ROOT / ".git").is_dir():
        print("not a git repository")
        return 1
    HOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    HOOK_PATH.write_text(
        "#!/bin/sh\n"
        "# Installed by scripts/check_secrets.py --install\n"
        'python "$(git rev-parse --show-toplevel)/scripts/check_secrets.py" --staged || exit 1\n',
        encoding="utf-8",
    )
    HOOK_PATH.chmod(0o755)
    print(f"installed pre-commit hook at {HOOK_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
