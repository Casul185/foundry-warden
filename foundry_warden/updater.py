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
import os
import shutil
import sys
import tarfile
import tempfile
import time
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


# ---------------------------------------------------------------------------
# Passive startup update check (best-effort, fail-silent, cached 24h).
#
# This is separate from the interactive check-update/update commands above:
# it prints AT MOST one stderr line on startup and never downloads or nags.
# ---------------------------------------------------------------------------
_NAME = "foundry-warden"
_ENV_OPTOUT = "FOUNDRY_NO_UPDATE_CHECK"
_LATEST_API = "https://api.github.com/repos/%s/releases/latest"
_RELEASES_PAGE = "https://github.com/%s/releases/latest"
_UA_CHECK = "foundry-warden-update-check"
_CACHE_TTL = 24 * 60 * 60
_CHECK_TIMEOUT = 2.0


def _optout() -> bool:
    val = os.environ.get(_ENV_OPTOUT, "")
    return bool(val) and val.strip().lower() not in ("0", "false")


def _cache_file() -> str:
    base = (os.environ.get("LOCALAPPDATA")
            or os.environ.get("XDG_CACHE_HOME")
            or os.path.expanduser("~/.cache"))
    d = os.path.join(base, _NAME)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "update_check.json")


def _read_cache() -> dict:
    try:
        with open(_cache_file(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_cache(now: float, latest: str | None) -> None:
    try:
        with open(_cache_file(), "w", encoding="utf-8") as fh:
            json.dump({"last_check_epoch": now, "latest_version": latest}, fh)
    except Exception:
        pass


def _strip_v(s) -> str:
    s = str(s or "").strip()
    return s[1:] if s[:1] in ("v", "V") else s


def _is_newer(remote: str, local: str) -> bool:
    """True iff remote parses to a strictly greater semver than local.
    Malformed input parses low and yields False — never raises."""
    try:
        return _parse_version(remote) > _parse_version(local)
    except Exception:
        return False


def _fetch_latest_tag(url: str, timeout: float) -> str | None:
    """Best-effort GET of releases/latest; returns the raw tag_name or None."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA_CHECK})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("tag_name")


def notify_if_update_available(current: str, repo: str = REPO) -> None:
    """Passive startup notice: print ONE stderr line if a newer release exists.

    Fail-silent on everything (offline, timeout, rate-limit, JSON change, ...).
    Hits the network at most once per 24h; a fresh cache is compared without any
    network call. Honors the FOUNDRY_NO_UPDATE_CHECK opt-out (checked first).
    """
    try:
        if _optout():
            return
        now = time.time()
        cached = _read_cache()
        latest = cached.get("latest_version")
        fresh = (now - float(cached.get("last_check_epoch", 0) or 0)) < _CACHE_TTL
        if not fresh:
            fetched = None
            try:
                raw = _fetch_latest_tag(_LATEST_API % repo, _CHECK_TIMEOUT)
                fetched = _strip_v(raw) if raw else None
            except Exception:
                fetched = None
            if fetched:
                latest = fetched
            # Record the check time regardless, so we stay within once-per-24h
            # even when offline; keep the last known latest for the notice.
            _write_cache(now, latest)
        if latest and _is_newer(latest, _strip_v(current)):
            sys.stderr.write(
                f">> A new version of {_NAME} is available: v{_strip_v(latest)} "
                f"(you have {_strip_v(current)}) — {_RELEASES_PAGE % repo}\n"
            )
    except Exception:
        return
