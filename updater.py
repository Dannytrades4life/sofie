"""Self-update on start: pull the latest code from GitHub before the bot loads.

Set UPDATE_REPO=owner/repo in .env (plus UPDATE_TOKEN for a private repo). Every start, this
downloads that repo's latest code and copies it over the bot's files, so updating is just
pressing Restart. Your .env and data/ folder are never touched. Any failure is logged and
the bot starts on the code it already has.
"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import urllib.request
import zipfile

log = logging.getLogger("updater")

ROOT = os.path.dirname(os.path.abspath(__file__))
KEEP = {".env", "data", ".local", ".cache", ".git"}  # never overwritten or read from the download
STAMP = os.path.join(ROOT, "data", "version.json")


def _get(url: str, token: str) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "sofie-updater"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def update() -> str:
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass
    repo = os.getenv("UPDATE_REPO", "").strip()
    if not repo:
        return "off"
    token, branch = os.getenv("UPDATE_TOKEN", "").strip(), os.getenv("UPDATE_BRANCH", "main").strip() or "main"
    try:
        sha = json.loads(_get(f"https://api.github.com/repos/{repo}/commits/{branch}", token))["sha"]
        try:
            with open(STAMP) as fh:
                if json.load(fh).get("sha") == sha:
                    return f"up to date ({sha[:7]})"
        except (OSError, ValueError):
            pass
        archive = zipfile.ZipFile(io.BytesIO(_get(f"https://api.github.com/repos/{repo}/zipball/{sha}", token)))
        count = 0
        for info in archive.infolist():
            parts = info.filename.split("/", 1)  # strip the "owner-repo-sha/" folder GitHub adds
            if len(parts) < 2 or not parts[1] or info.is_dir():
                continue
            rel = parts[1]
            if rel.split("/", 1)[0] in KEEP:
                continue
            dest = os.path.join(ROOT, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with archive.open(info) as src, open(dest + ".new", "wb") as out:
                shutil.copyfileobj(src, out)
            os.replace(dest + ".new", dest)
            count += 1
        os.makedirs(os.path.dirname(STAMP), exist_ok=True)
        with open(STAMP, "w") as fh:
            json.dump({"sha": sha, "repo": repo}, fh)
        return f"updated to {sha[:7]} ({count} files)"
    except Exception as exc:  # never block startup on an update problem
        return f"failed, starting with the current code: {exc}"
