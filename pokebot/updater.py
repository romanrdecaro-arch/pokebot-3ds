"""
In-place updates, so nobody has to re-download the zip by hand.

Two installs exist in the wild and they update differently:

  * a ``git clone`` -- updated with ``git pull --ff-only``
  * the release zip -- has no ``.git``, so it is updated by fetching
    the current zip and copying it over the tree

Both are identified by the commit sha they were built from. A clone
reads it from git; the zip carries a ``BUILD_INFO.json`` stamped in by
CI. The same file is published as its own release asset, so checking
for an update is one small HTTP GET against a stable URL -- no GitHub
API call, and therefore no 60-requests-per-hour anonymous rate limit
to trip over.

Nothing here runs on import and nothing updates silently: the launcher
checks in the background and the user presses a button to apply.
Downloading and running new code on someone's machine is not something
to do behind their back.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

OWNER = "romanrdecaro-arch"
REPO = "pokebot-3ds"

_BASE = f"https://github.com/{OWNER}/{REPO}/releases/latest/download"
BUILD_INFO_URL = f"{_BASE}/BUILD_INFO.json"
ZIP_URL = f"{_BASE}/pokebot-3ds.zip"
SHA256SUMS_URL = f"{_BASE}/SHA256SUMS.txt"

BUILD_INFO_NAME = "BUILD_INFO.json"
BACKUP_DIR = ".update-backup"

USER_AGENT = f"pokebot-3ds/{REPO}"
HTTP_TIMEOUT = 10.0
MAX_ZIP_BYTES = 256 * 1024 * 1024      # a ~8 MB zip; anything near this is wrong

# Paths an update must never touch. These are either the user's own
# settings or data the bot produced for them, and a "helpful" refresh
# that reset config.yaml would silently throw away their offsets,
# press timings and target rules.
PRESERVE_NAMES = frozenset({
    "config.yaml",
    "config.local.yaml",
    "targets",
    "logs",
    ".git",
    ".venv",
    "venv",
    BACKUP_DIR,
})
PRESERVE_SUFFIXES = (".pk6", ".pk7", ".ek6", ".ek7",
                     ".log", ".jsonl", ".sav", ".local.yaml")
PRESERVE_PREFIXES = (".pokebot_stats", ".pokebot_accessors", ".crystal_wram")

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


class UpdateError(RuntimeError):
    """Any failure to check for or apply an update."""


# ----------------------------------------------------------------------
# Version identity
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Version:
    """Where a copy of the bot came from."""
    sha: str = ""            # full commit sha, or "" if unknown
    source: str = "unknown"  # "git" | "zip" | "unknown"
    built: str = ""          # ISO-ish build timestamp, zip installs only
    subject: str = ""        # commit subject line, if known

    @property
    def short(self) -> str:
        return self.sha[:7] if self.sha else "unknown"

    def describe(self) -> str:
        if not self.sha:
            return "unknown build"
        where = {"git": "git checkout", "zip": "zip install"}.get(
            self.source, self.source)
        return f"{self.short} ({where})"


def is_git_checkout(root: Path) -> bool:
    return (Path(root) / ".git").exists()


def _run_git(root: Path, *args: str, timeout: float = 30.0):
    """Run git in ``root``. Returns CompletedProcess; never raises."""
    try:
        return subprocess.run(
            ("git", *args), cwd=str(root), timeout=timeout,
            capture_output=True, text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug(f"git {' '.join(args)} failed: {exc}")
        return None


def read_build_info(root: Path) -> Optional[dict]:
    """Parse BUILD_INFO.json from an install, or None if absent/broken."""
    path = Path(root) / BUILD_INFO_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def local_version(root: Path) -> Version:
    """Identify the installed copy.

    A git checkout wins over a stamped BUILD_INFO.json: if someone
    cloned and then pulled, git is the truth and the stamp is stale.
    """
    root = Path(root)
    if is_git_checkout(root):
        proc = _run_git(root, "rev-parse", "HEAD")
        if proc is not None and proc.returncode == 0:
            sha = proc.stdout.strip()
            if _SHA_RE.match(sha):
                subject = ""
                sub = _run_git(root, "log", "-1", "--pretty=%s")
                if sub is not None and sub.returncode == 0:
                    subject = sub.stdout.strip()
                return Version(sha=sha, source="git", subject=subject)
        # .git exists but git is unusable (not installed, shallow, …).
        return Version(source="git")

    info = read_build_info(root)
    if info:
        sha = str(info.get("sha", "")).strip().lower()
        return Version(
            sha=sha if _SHA_RE.match(sha) else "",
            source="zip",
            built=str(info.get("built", "")),
            subject=str(info.get("subject", "")),
        )
    return Version()


# ----------------------------------------------------------------------
# Talking to GitHub
# ----------------------------------------------------------------------
def _get(url: str, timeout: float = HTTP_TIMEOUT, max_bytes: int = 1 << 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # The rolling release exists but predates build stamping, or
            # no release has been published at all. Neither is the
            # user's fault and neither is a network problem, so don't
            # show them a raw 404.
            raise UpdateError(
                "the published release has no build stamp yet, so there "
                "is nothing to compare against. Update manually this "
                "once and in-app updates will work from then on."
            ) from exc
        raise UpdateError(f"GitHub returned HTTP {exc.code} for {url}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise UpdateError(f"could not reach {url}: {exc}") from exc


def fetch_remote(timeout: float = HTTP_TIMEOUT) -> Version:
    """The build currently published as the rolling 'latest' release."""
    raw = _get(BUILD_INFO_URL, timeout=timeout)
    try:
        info = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise UpdateError(f"release build info was not valid JSON: {exc}") from exc
    if not isinstance(info, dict):
        raise UpdateError("release build info was not a JSON object")

    sha = str(info.get("sha", "")).strip().lower()
    if not _SHA_RE.match(sha):
        raise UpdateError(f"release build info has no usable sha: {sha!r}")
    return Version(sha=sha, source="release",
                   built=str(info.get("built", "")),
                   subject=str(info.get("subject", "")))


@dataclass(frozen=True)
class UpdateCheck:
    local: Version
    remote: Optional[Version] = None
    available: bool = False
    detail: str = ""

    @property
    def ok(self) -> bool:
        """Did the check itself succeed?"""
        return self.remote is not None


def check(root: Path, timeout: float = HTTP_TIMEOUT) -> UpdateCheck:
    """Compare the installed build against the published one.

    Never raises: a failed check is reported in ``detail`` and must not
    be allowed to take the launcher down with it.
    """
    here = local_version(root)
    try:
        there = fetch_remote(timeout=timeout)
    except UpdateError as exc:
        return UpdateCheck(local=here, detail=str(exc))

    if not here.sha:
        return UpdateCheck(
            local=here, remote=there, available=True,
            detail="This copy is not stamped with a version, so it "
                   "cannot be compared. Updating will bring it to "
                   f"{there.short}.")
    if here.sha == there.sha:
        return UpdateCheck(local=here, remote=there, available=False,
                           detail=f"Up to date ({here.short}).")
    return UpdateCheck(
        local=here, remote=there, available=True,
        detail=f"{here.short} installed, {there.short} available.")


# ----------------------------------------------------------------------
# Applying: shared helpers
# ----------------------------------------------------------------------
def is_preserved(rel: Path) -> bool:
    """Should this repo-relative path survive an update untouched?"""
    parts = rel.parts
    if not parts:
        return True
    if any(p in PRESERVE_NAMES for p in parts):
        return True
    name = parts[-1]
    if name.endswith(PRESERVE_SUFFIXES):
        return True
    return name.startswith(PRESERVE_PREFIXES)


@dataclass(frozen=True)
class UpdateResult:
    ok: bool
    detail: str
    version: Optional[Version] = None
    changed: tuple[str, ...] = ()
    backup: str = ""

    @property
    def restart_required(self) -> bool:
        return self.ok and bool(self.changed)


Logger = Callable[[str], None]


def _noop(_msg: str) -> None:
    pass


# ----------------------------------------------------------------------
# Applying: git checkout
# ----------------------------------------------------------------------
def _update_git(root: Path, on_log: Logger) -> UpdateResult:
    dirty = _run_git(root, "status", "--porcelain")
    if dirty is None:
        return UpdateResult(False, "git is not available on PATH, so this "
                                   "checkout cannot update itself.")
    if dirty.returncode != 0:
        return UpdateResult(False, f"git status failed: "
                                   f"{dirty.stderr.strip() or dirty.returncode}")

    # Ignored files (config.yaml is tracked, but targets/ and the stats
    # files are not) never show up here, so this only catches real
    # edits to tracked files -- which a fast-forward pull would refuse
    # to clobber anyway. Report it plainly instead of forcing.
    if dirty.stdout.strip():
        # Porcelain lines are "XY PATH", and the X column is a space for
        # an unstaged edit. Stripping the block first shifts that line
        # left by one, so splitting on whitespace is the only parse that
        # survives both " M path" and "?? path".
        edited = [ln.split(None, 1)[-1]
                  for ln in dirty.stdout.splitlines() if ln.strip()][:5]
        return UpdateResult(
            False,
            "This checkout has local changes, so it was left alone: "
            + ", ".join(edited)
            + ". Commit or discard them, then update again.")

    on_log("Pulling latest changes...")
    pull = _run_git(root, "pull", "--ff-only", timeout=120.0)
    if pull is None:
        return UpdateResult(False, "git pull could not be started.")
    if pull.returncode != 0:
        return UpdateResult(
            False, f"git pull failed: "
                   f"{(pull.stderr or pull.stdout).strip()[:300]}")

    after = local_version(root)
    changed = () if "Already up to date" in pull.stdout else ("(git pull)",)
    return UpdateResult(True, pull.stdout.strip().splitlines()[-1]
                        if pull.stdout.strip() else "Updated.",
                        version=after, changed=changed)


# ----------------------------------------------------------------------
# Applying: zip install
# ----------------------------------------------------------------------
def _expected_sha256(timeout: float) -> str:
    """The published zip's sha256, or "" if the sums file is unusable."""
    try:
        text = _get(SHA256SUMS_URL, timeout=timeout).decode("utf-8", "replace")
    except UpdateError as exc:
        log.warning(f"could not fetch SHA256SUMS.txt: {exc}")
        return ""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].lstrip("*").endswith("pokebot-3ds.zip"):
            digest = parts[0].strip().lower()
            if re.fullmatch(r"[0-9a-f]{64}", digest):
                return digest
    return ""


def _download_zip(dest: Path, timeout: float, on_log: Logger) -> Path:
    on_log("Downloading update...")
    req = urllib.request.Request(ZIP_URL, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, \
                open(dest, "wb") as fh:
            total = 0
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ZIP_BYTES:
                    raise UpdateError(
                        f"update download exceeded {MAX_ZIP_BYTES} bytes; "
                        f"refusing it")
                fh.write(chunk)
    except (urllib.error.URLError, OSError) as exc:
        raise UpdateError(f"download failed: {exc}") from exc
    if not dest.exists() or dest.stat().st_size == 0:
        raise UpdateError("downloaded update was empty")
    return dest


def _verify(zip_path: Path, expected: str, on_log: Logger) -> None:
    if not expected:
        # Not fatal: the zip still has to parse as a zip and the
        # extract step rejects anything that escapes the tree. Say so
        # rather than implying it was checked.
        on_log("No published checksum found -- skipping verification.")
        return
    digest = hashlib.sha256()
    with open(zip_path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    got = digest.hexdigest()
    if got != expected:
        raise UpdateError(
            f"checksum mismatch: expected {expected[:12]}…, got {got[:12]}…. "
            f"The update was NOT applied.")
    on_log("Checksum verified.")


def safe_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Members that are safe to extract, with a common prefix stripped.

    A zip can name ``../../evil`` or an absolute path and walk straight
    out of the destination. Anything that does not stay inside is
    dropped rather than trusted.
    """
    out: list[zipfile.ZipInfo] = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ".." in Path(name).parts or ":" in name:
            log.warning(f"refusing unsafe zip member: {info.filename!r}")
            continue
        out.append(info)
    return out


def _extract(zip_path: Path, dest: Path, on_log: Logger) -> Path:
    on_log("Unpacking...")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = safe_members(zf)
            if not members:
                raise UpdateError("the update archive was empty")
            for info in members:
                zf.extract(info, dest)
    except zipfile.BadZipFile as exc:
        raise UpdateError(f"the update archive was corrupt: {exc}") from exc

    # The zip is built with `zip -r ... .` so members are already
    # repo-relative, but tolerate a single wrapping directory in case
    # the packaging ever changes.
    entries = [p for p in dest.iterdir()]
    if len(entries) == 1 and entries[0].is_dir() \
            and not (dest / "launcher.py").exists():
        return entries[0]
    return dest


def _install_tree(staged: Path, root: Path, on_log: Logger) -> UpdateResult:
    """Copy the staged tree over the install, preserving user files."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = Path(root) / BACKUP_DIR / stamp
    changed: list[str] = []

    for src in sorted(staged.rglob("*")):
        if src.is_dir():
            continue
        rel = src.relative_to(staged)
        if is_preserved(rel):
            continue
        dst = Path(root) / rel
        try:
            if dst.exists() and dst.read_bytes() == src.read_bytes():
                continue                      # identical; nothing to do
        except OSError:
            pass                              # unreadable -> replace it

        try:
            if dst.exists():
                bdst = backup / rel
                bdst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, bdst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            changed.append(str(rel).replace("\\", "/"))
        except OSError as exc:
            # Most likely Windows holding a file open. Stop rather than
            # leave the tree half-new: the backup below is the way back.
            return UpdateResult(
                False,
                f"could not replace {rel}: {exc}. "
                + (f"Files already replaced were backed up to "
                   f"{BACKUP_DIR}/{stamp}. " if changed else "")
                + "Close the bot and any editor holding these files, "
                  "then try again.",
                changed=tuple(changed),
                backup=str(backup) if changed else "")

    if not changed:
        return UpdateResult(True, "Already up to date -- nothing to replace.")

    on_log(f"Replaced {len(changed)} file(s).")
    return UpdateResult(True, f"Updated {len(changed)} file(s).",
                        version=local_version(root),
                        changed=tuple(changed),
                        backup=str(backup))


def _update_zip(root: Path, on_log: Logger, timeout: float) -> UpdateResult:
    with tempfile.TemporaryDirectory(prefix="pokebot-update-") as tmp:
        tmpdir = Path(tmp)
        try:
            zip_path = _download_zip(tmpdir / "update.zip", timeout, on_log)
            _verify(zip_path, _expected_sha256(timeout), on_log)
            staged = _extract(zip_path, tmpdir / "staged", on_log)
        except UpdateError as exc:
            return UpdateResult(False, str(exc))
        if not (staged / "launcher.py").exists():
            return UpdateResult(
                False, "the update archive did not look like pokebot-3ds "
                       "(no launcher.py); nothing was changed")
        return _install_tree(staged, Path(root), on_log)


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------
def apply_update(root: Path, on_log: Logger = _noop,
                 timeout: float = HTTP_TIMEOUT) -> UpdateResult:
    """Bring this install up to the published build.

    Dispatches on how the copy was obtained. Returns a result rather
    than raising, so a failed update leaves a usable bot and a readable
    explanation.
    """
    root = Path(root)
    if is_git_checkout(root):
        on_log("git checkout detected.")
        return _update_git(root, on_log)
    on_log("zip install detected.")
    return _update_zip(root, on_log, timeout)


def should_check_on_start(config: Optional[dict]) -> bool:
    """Is the launcher allowed to look for updates when it opens?

    Opt-out, not opt-in: an update nobody hears about is the whole
    problem this module exists to solve. A missing or malformed
    ``updates:`` section means yes; only an explicit false means no.
    """
    section = (config or {}).get("updates")
    if not isinstance(section, dict):
        return True
    return bool(section.get("check_on_start", True))


def prune_backups(root: Path, keep: int = 3) -> int:
    """Keep only the newest ``keep`` update backups. Returns how many went."""
    base = Path(root) / BACKUP_DIR
    if not base.is_dir():
        return 0
    dirs = sorted((d for d in base.iterdir() if d.is_dir()),
                  key=lambda d: d.name, reverse=True)
    removed = 0
    for old in dirs[keep:]:
        try:
            shutil.rmtree(old)
            removed += 1
        except OSError as exc:
            log.debug(f"could not remove old backup {old}: {exc}")
    return removed
