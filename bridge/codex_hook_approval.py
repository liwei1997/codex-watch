#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"
BRIDGE_URL = os.getenv("CODEX_WATCH_BASE_URL", "http://127.0.0.1:8765").rstrip("/")


def load_token():
    token = os.getenv("CODEX_WATCH_TOKEN")
    if token:
        return token.strip()
    if not ENV_FILE.exists():
        raise RuntimeError("Bridge .env file not found")
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].strip()
        if line.startswith("CODEX_WATCH_TOKEN="):
            value = line.split("=", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if value:
                return value
    raise RuntimeError("CODEX_WATCH_TOKEN not configured")


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            return 0
        thread_id = str(payload.get("session_id") or "").strip()
        if not thread_id:
            return 0
        body = json.dumps({
            "thread_id": thread_id,
            "cwd": str(payload.get("cwd") or ""),
        }).encode("utf-8")
        request = Request(
            BRIDGE_URL + "/v1/threads/observe",
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer " + load_token(),
                "Content-Type": "application/json",
            },
        )
        with urlopen(request, timeout=3):
            pass
    except (Exception, HTTPError, URLError) as exc:
        print("[Codex Watch] Thread registration failed: " + str(exc), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
