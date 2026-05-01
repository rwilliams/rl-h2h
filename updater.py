#!/usr/bin/env python3
"""Self-updater for rl-h2h.

Run by start.bat before launching rl_h2h.py. Checks GitHub for a newer
commit on main and applies it. Hybrid: prefers `git pull` when a .git
directory and the git binary are available; falls back to downloading
the main-branch zip from GitHub.

Design notes:
- All network calls have a 5s timeout; any failure is swallowed and
  logged to update.log. The launcher must always be able to start the
  app afterwards.
- Local edits to tracked files block the update (preserves customisations).
  Git mode uses `git status --porcelain` (already excludes gitignored
  files). Zip mode hashes tracked files against VERSION.tree.json,
  written after each successful zip update.
- Data files (config.json, matches.jsonl, players.json) are gitignored
  and never touched by either mode.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO = "Florentde29/rl-h2h"
BRANCH = "main"
# 5s for cheap probes (SHA endpoint, git --version, rev-parse), 30s for the
# operations that actually transfer data (git fetch/pull, zip download).
TIMEOUT_PROBE = 5.0
TIMEOUT_NETWORK = 30.0
# Hard cap on the total uncompressed size of the archive we'll extract.
# Defends against zip-bomb scenarios if the upstream repo is ever compromised.
MAX_ARCHIVE_SIZE = 50 * 1024 * 1024  # 50 MB

APP_DIR = Path(__file__).resolve().parent
VERSION_PATH = APP_DIR / "VERSION"
TREE_PATH = APP_DIR / "VERSION.tree.json"
LOG_PATH = APP_DIR / "update.log"
CONFIG_PATH = APP_DIR / "config.json"

# Files the updater must never overwrite or use to detect "dirty".
# Mirrors .gitignore. Anything matching these is left alone in zip mode.
PROTECTED = {
    "config.json", "config.json.tmp",
    "matches.jsonl",
    "players.json", "players.json.tmp",
    "update.log",
    "VERSION.tree.json",
}
PROTECTED_PREFIXES = ("players.corrupt-", ".git/", ".venv/", "venv/", "env/",
                       "__pycache__/", ".vscode/", ".idea/", ".playwright-mcp/")


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{ts} {msg}\n"
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def is_protected(rel_path: str) -> bool:
    rel = rel_path.replace("\\", "/")
    if rel in PROTECTED:
        return True
    return any(rel.startswith(p) for p in PROTECTED_PREFIXES)


def have_git() -> bool:
    if not (APP_DIR / ".git").exists():
        return False
    try:
        r = subprocess.run(["git", "--version"], capture_output=True,
                           timeout=TIMEOUT_PROBE, check=False)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def git_update() -> None:
    """Fast-forward to origin/main if clean. Skip if dirty or already current."""
    def git(*args: str, timeout: float = TIMEOUT_PROBE) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(APP_DIR), *args],
            capture_output=True, text=True, timeout=timeout, check=False,
        )

    fetch = git("fetch", "--quiet", "origin", BRANCH, timeout=TIMEOUT_NETWORK)
    if fetch.returncode != 0:
        log(f"git: fetch failed: {fetch.stderr.strip() or fetch.stdout.strip()}")
        return

    local = git("rev-parse", "HEAD").stdout.strip()
    upstream = git("rev-parse", f"origin/{BRANCH}").stdout.strip()
    if not local or not upstream:
        log("git: could not resolve revs")
        return
    if local == upstream:
        log("git: up to date")
        return

    status = git("status", "--porcelain")
    if status.stdout.strip():
        log(f"git: skipped — local edits ({local[:7]} → {upstream[:7]})")
        return

    pull = git("pull", "--ff-only", "--quiet", "origin", BRANCH, timeout=TIMEOUT_NETWORK)
    if pull.returncode != 0:
        log(f"git: pull failed: {pull.stderr.strip() or pull.stdout.strip()}")
        return
    log(f"git: updated {local[:7]} → {upstream[:7]}")


def http_get(url: str, accept: str | None = None, timeout: float = TIMEOUT_PROBE) -> bytes:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "rl-h2h-updater")
    if accept:
        req.add_header("Accept", accept)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def zip_update() -> None:
    """Download the main-branch zip and overlay tracked files. Bail on local edits."""
    try:
        sha_bytes = http_get(
            f"https://api.github.com/repos/{REPO}/commits/{BRANCH}",
            accept="application/vnd.github.sha",
        )
        upstream = sha_bytes.decode("ascii", errors="replace").strip()
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        log(f"zip: SHA fetch failed: {type(e).__name__}: {e}")
        return
    if len(upstream) < 7:
        log(f"zip: bad SHA response {upstream!r}")
        return

    local = VERSION_PATH.read_text(encoding="utf-8").strip() if VERSION_PATH.exists() else ""
    if local == upstream:
        log("zip: up to date")
        return

    # Hash check: if VERSION.tree.json exists, every tracked file's hash must
    # still match what we recorded last time. Mismatch = user edit → bail.
    recorded: dict[str, str] = {}
    if TREE_PATH.exists():
        try:
            recorded = json.loads(TREE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            recorded = {}
    if recorded:
        for rel, expected in recorded.items():
            local_path = APP_DIR / rel
            if not local_path.exists():
                log(f"zip: skipped — missing tracked file {rel!r}")
                return
            if sha256_file(local_path) != expected:
                log(f"zip: skipped — local edits to {rel!r}")
                return

    # Download and stage.
    try:
        zip_bytes = http_get(
            f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip",
            timeout=TIMEOUT_NETWORK,
        )
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        log(f"zip: archive download failed: {type(e).__name__}: {e}")
        return

    app_root = APP_DIR.resolve()
    new_tree: dict[str, str] = {}
    # Two-phase commit: write every new file to a sibling .tmp, then in a tight
    # second loop atomically rename them all into place. If we crash between
    # phases the working tree is untouched (only orphan .tmp files remain). The
    # rename phase is fast enough that a crash window inside it is negligible.
    staged: list[tuple[Path, Path]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            members = zf.infolist()
            # Bomb cap: reject if the total uncompressed size is suspicious.
            total_size = sum(max(0, m.file_size) for m in members)
            if total_size > MAX_ARCHIVE_SIZE:
                log(f"zip: archive too large ({total_size} bytes > {MAX_ARCHIVE_SIZE})")
                return
            # GitHub wraps everything in <repo>-<branch>/...
            roots = {m.filename.split("/", 1)[0] for m in members if "/" in m.filename}
            if len(roots) != 1:
                log(f"zip: unexpected archive layout (roots={roots})")
                return
            root = next(iter(roots)) + "/"
            for m in members:
                if m.is_dir() or not m.filename.startswith(root):
                    continue
                rel = m.filename[len(root):]
                if not rel or is_protected(rel):
                    continue
                # Zipslip guard: reject any path that resolves outside APP_DIR.
                # GitHub archives are clean today, but this is the kind of bug
                # that makes a future repo compromise an RCE.
                dest = (APP_DIR / rel).resolve()
                if app_root not in dest.parents and dest != app_root:
                    log(f"zip: skipped suspicious path {rel!r}")
                    continue
                data = zf.read(m)
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(dest.suffix + ".tmp")
                with tmp.open("wb") as f:
                    f.write(data)
                staged.append((tmp, dest))
                new_tree[rel] = sha256_bytes(data)
    except (zipfile.BadZipFile, OSError) as e:
        # Clean up any staging files we created so they don't litter the repo.
        for tmp, _ in staged:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        log(f"zip: extract failed: {type(e).__name__}: {e}")
        return

    # Phase 2: atomic per-file rename. Fast (no I/O / decompression).
    try:
        for tmp, dest in staged:
            os.replace(tmp, dest)
    except OSError as e:
        log(f"zip: rename phase failed: {type(e).__name__}: {e}")
        return

    try:
        atomic_write(VERSION_PATH, upstream + "\n")
        atomic_write(TREE_PATH, json.dumps(new_tree, indent=2, sort_keys=True))
    except OSError as e:
        log(f"zip: VERSION write failed: {e}")
        return

    short = (local[:7] if local else "(none)") + " → " + upstream[:7]
    log(f"zip: updated {short} ({len(new_tree)} files)")


def auto_update_enabled() -> bool:
    """Read config.json and check the auto_update flag. Default: off.

    The flag is toggled via the tray menu in rl_h2h.py. On a fresh install
    config.json doesn't exist yet — that's treated as off (opt-in default).
    """
    if not CONFIG_PATH.exists():
        return False
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return bool(isinstance(cfg, dict) and cfg.get("auto_update"))


def main() -> int:
    try:
        if not auto_update_enabled():
            log("disabled (auto_update is off)")
            return 0
        if have_git():
            git_update()
        else:
            zip_update()
    except Exception as e:
        log(f"updater: unexpected error: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
