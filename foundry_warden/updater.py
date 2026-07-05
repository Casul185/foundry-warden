"""Self-update against public GitHub releases — pure stdlib (urllib), no auth.

Design: **check-and-notify by default.** `check_update()` is read-only. `apply_update()`
only runs on explicit `update` and NEVER overwrites the user's `config.json`. The
current install is backed up first and restored on any failure.

Limits (honest): updates come from public GitHub over TLS; there is **no code
signing yet** — you are trusting github.com + the repo owner. Review the diff on
GitHub if that matters to you.
"""
from __future__ import annotations

import io
import json
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

REPO = "casul185/foundry-warden"
_API = "https://api.github.com/repos/%s"
_UA = {"User-Agent": "foundry-warden-updater", "Accept": "application/vnd.github+json"}


def _parse_version(s: str) -> tuple:
    s = (s or "").lstrip("vV").strip()
    parts = []
    for chunk in s.split("."):
        num = "".join(c for c in chunk if c.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts) or (0,)


def _http_json(url: str, timeout: float = 10.0):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_latest(repo: str = REPO) -> tuple[str | None, str | None]:
    """Return (tag, tarball_url) for the highest-version tag.

    Uses /tags and picks the semver-max (robust: not every tag is a formal GitHub
    Release, and Release ordering can lag). Falls back to /releases/latest.
    """
    try:
        tags = _http_json(_API % repo + "/tags")
        if tags:
            best = max(tags, key=lambda t: _parse_version(t.get("name", "")))
            return best.get("name"), best.get("tarball_url")
    except Exception:
        pass
    try:
        rel = _http_json(_API % repo + "/releases/latest")
        return rel.get("tag_name"), rel.get("tarball_url")
    except Exception:
        pass
    return None, None


def check_update(current: str, repo: str = REPO) -> dict:
    """Read-only: compare the running version to the latest tag."""
    latest, url = get_latest(repo)
    if latest is None:
        return {"current": current, "latest": None, "update_available": False,
                "error": "could not reach GitHub"}
    available = _parse_version(latest) > _parse_version(current)
    return {"current": current, "latest": latest, "update_available": available,
            "tarball_url": url}


def apply_update(current: str, project_root: Path, repo: str = REPO) -> tuple[bool, str]:
    """Download the latest release and update in place. Backs up first; preserves config.json.

    Returns (changed, message). Only proceeds if a newer version exists.
    """
    info = check_update(current, repo)
    if info.get("latest") is None:
        return False, "could not reach GitHub — no change."
    if not info["update_available"]:
        return False, f"already up to date ({current})."

    url = info.get("tarball_url")
    if not url:
        return False, "no downloadable release asset found."

    backup = project_root.with_name(project_root.name + f".backup-{current}")
    try:
        # 1. download tarball to memory
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()

        # 2. extract to a temp dir (GitHub tarballs have a single top-level folder)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
                tf.extractall(tmp)  # noqa: S202 - trusted GitHub archive over TLS
            roots = [p for p in tmp.iterdir() if p.is_dir()]
            if not roots:
                return False, "unexpected archive layout."
            src = roots[0]

            # 3. back up the current install (config.json + logs stay in place)
            if backup.exists():
                shutil.rmtree(backup)
            shutil.copytree(project_root, backup,
                            ignore=shutil.ignore_patterns("logs", "__pycache__", ".git"))

            # 4. copy new files in — NEVER touch config.json or logs/
            for item in src.rglob("*"):
                if item.is_dir():
                    continue
                rel = item.relative_to(src)
                if rel.parts and rel.parts[0] in ("logs",) or rel.name == "config.json":
                    continue
                dest = project_root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dest)
        return True, f"updated {current} -> {info['latest']}. Backup: {backup.name}"
    except Exception as exc:
        # rollback
        try:
            if backup.exists():
                for item in backup.rglob("*"):
                    if item.is_file():
                        dest = project_root / item.relative_to(backup)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(item, dest)
        except Exception:
            pass
        return False, f"update failed ({exc}); rolled back from {backup.name}."
