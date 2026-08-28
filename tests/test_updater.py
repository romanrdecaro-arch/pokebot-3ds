"""
Tests for in-place updating.

The dangerous behaviours here are all about destroying something the
user cannot get back: overwriting an edited config.yaml, deleting
exported .pk6 targets, or extracting an archive that writes outside
the install directory. Those get the most attention. Network access is
faked throughout -- no test here touches GitHub.
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot import updater  # noqa: E402


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
SHA_A = "a" * 40
SHA_B = "b" * 40


def make_install(root: Path, *, sha: str = SHA_A, git: bool = False,
                 stamped: bool = True) -> Path:
    """A plausible pokebot install on disk."""
    root.mkdir(parents=True, exist_ok=True)
    # Bytes, not text: write_text translates \n to \r\n on Windows, which
    # unzipping never does. Writing text here would make every file look
    # modified against a LF archive and mask a real no-op update.
    (root / "launcher.py").write_bytes(b"# old launcher\n")
    (root / "run.py").write_bytes(b"# old run\n")
    (root / "config.yaml").write_bytes(b"mode: soft_reset\nMINE: yes\n")
    (root / "pokebot").mkdir(exist_ok=True)
    (root / "pokebot" / "bot.py").write_bytes(b"# old bot\n")
    (root / "targets").mkdir(exist_ok=True)
    (root / "targets" / "shiny_656.pk6").write_bytes(b"precious")
    (root / "logs").mkdir(exist_ok=True)
    (root / "logs" / "events.jsonl").write_text("{}\n", encoding="utf-8")
    (root / ".pokebot_stats.json").write_text("{}", encoding="utf-8")
    if stamped:
        (root / "BUILD_INFO.json").write_text(
            json.dumps({"sha": sha, "built": "2026-01-01", "subject": "old"}),
            encoding="utf-8")
    if git:
        (root / ".git").mkdir(exist_ok=True)
    return root


def make_zip(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def new_release_files(sha: str = SHA_B) -> dict[str, bytes]:
    return {
        "launcher.py": b"# NEW launcher\n",
        "run.py": b"# NEW run\n",
        "pokebot/bot.py": b"# NEW bot\n",
        "pokebot/updater.py": b"# NEW updater\n",
        "config.yaml": b"mode: observe\nTEMPLATE: yes\n",
        "BUILD_INFO.json": json.dumps(
            {"sha": sha, "built": "2026-08-27", "subject": "new"}).encode(),
    }


class FakeNet:
    """Stands in for GitHub. Records what was asked for."""

    def __init__(self, build_info: dict | None = None,
                 zip_bytes: bytes = b"", sums: str = ""):
        self.build_info = build_info
        self.zip_bytes = zip_bytes
        self.sums = sums
        self.requested: list[str] = []

    def get(self, url: str, timeout: float = 10.0, max_bytes: int = 1 << 20):
        self.requested.append(url)
        if url == updater.BUILD_INFO_URL:
            if self.build_info is None:
                raise updater.UpdateError("offline")
            return json.dumps(self.build_info).encode()
        if url == updater.SHA256SUMS_URL:
            return self.sums.encode()
        raise updater.UpdateError(f"unexpected url {url}")


# ----------------------------------------------------------------------
# Identifying the installed copy
# ----------------------------------------------------------------------
def test_a_zip_install_is_identified_from_its_stamp(tmp_path):
    root = make_install(tmp_path / "inst")
    v = updater.local_version(root)
    assert v.source == "zip"
    assert v.sha == SHA_A
    assert v.short == "aaaaaaa"


def test_an_unstamped_install_is_unknown_not_a_crash(tmp_path):
    root = make_install(tmp_path / "inst", stamped=False)
    v = updater.local_version(root)
    assert v.source == "unknown"
    assert v.sha == ""
    assert v.describe() == "unknown build"


def test_a_corrupt_stamp_is_treated_as_unknown(tmp_path):
    root = make_install(tmp_path / "inst", stamped=False)
    (root / "BUILD_INFO.json").write_text("{not json", encoding="utf-8")
    assert updater.local_version(root).sha == ""


def test_a_stamp_with_a_junk_sha_is_rejected(tmp_path):
    root = make_install(tmp_path / "inst", stamped=False)
    (root / "BUILD_INFO.json").write_text(
        json.dumps({"sha": "; rm -rf /"}), encoding="utf-8")
    assert updater.local_version(root).sha == ""


def test_git_wins_over_a_stale_stamp(tmp_path, monkeypatch):
    """A clone that pulled has a stamp from whenever the zip was cut."""
    root = make_install(tmp_path / "inst", sha=SHA_A, git=True)

    class P:
        returncode = 0
        stdout = SHA_B + "\n"
        stderr = ""

    monkeypatch.setattr(updater, "_run_git", lambda *a, **k: P())
    v = updater.local_version(root)
    assert v.source == "git"
    assert v.sha == SHA_B          # git's answer, not the stamp's


def test_a_checkout_without_git_installed_still_reports_git(tmp_path,
                                                            monkeypatch):
    root = make_install(tmp_path / "inst", git=True)
    monkeypatch.setattr(updater, "_run_git", lambda *a, **k: None)
    v = updater.local_version(root)
    assert v.source == "git" and v.sha == ""


# ----------------------------------------------------------------------
# Checking
# ----------------------------------------------------------------------
def test_same_sha_means_no_update(tmp_path, monkeypatch):
    root = make_install(tmp_path / "inst", sha=SHA_A)
    net = FakeNet(build_info={"sha": SHA_A})
    monkeypatch.setattr(updater, "_get", net.get)
    res = updater.check(root)
    assert res.ok and not res.available
    assert "Up to date" in res.detail


def test_a_different_sha_means_an_update(tmp_path, monkeypatch):
    root = make_install(tmp_path / "inst", sha=SHA_A)
    monkeypatch.setattr(updater, "_get", FakeNet({"sha": SHA_B}).get)
    res = updater.check(root)
    assert res.available
    assert "aaaaaaa" in res.detail and "bbbbbbb" in res.detail


def test_being_offline_is_reported_not_raised(tmp_path, monkeypatch):
    root = make_install(tmp_path / "inst")
    monkeypatch.setattr(updater, "_get", FakeNet(build_info=None).get)
    res = updater.check(root)
    assert not res.ok
    assert not res.available          # never offer an update we can't see
    assert "offline" in res.detail


def test_a_junk_response_does_not_offer_an_update(tmp_path, monkeypatch):
    root = make_install(tmp_path / "inst")

    def junk(url, timeout=10.0, max_bytes=1 << 20):
        return b"<html>404</html>"

    monkeypatch.setattr(updater, "_get", junk)
    res = updater.check(root)
    assert not res.ok and not res.available


def test_a_release_without_a_sha_is_refused(tmp_path, monkeypatch):
    root = make_install(tmp_path / "inst")
    monkeypatch.setattr(updater, "_get", FakeNet({"built": "today"}).get)
    res = updater.check(root)
    assert not res.ok


def test_an_unstamped_install_is_offered_the_update(tmp_path, monkeypatch):
    """Cannot compare, so offer rather than silently claim up-to-date."""
    root = make_install(tmp_path / "inst", stamped=False)
    monkeypatch.setattr(updater, "_get", FakeNet({"sha": SHA_B}).get)
    res = updater.check(root)
    assert res.available
    assert "not stamped" in res.detail


def test_checking_never_calls_the_rate_limited_api(tmp_path, monkeypatch):
    root = make_install(tmp_path / "inst")
    net = FakeNet({"sha": SHA_B})
    monkeypatch.setattr(updater, "_get", net.get)
    updater.check(root)
    assert all("api.github.com" not in u for u in net.requested)


# ----------------------------------------------------------------------
# What must never be replaced
# ----------------------------------------------------------------------
@pytest.mark.parametrize("rel", [
    "config.yaml",
    "config.local.yaml",
    "targets/shiny_656.pk6",
    "targets",
    "logs/events.jsonl",
    "logs",
    ".pokebot_stats.json",
    ".pokebot_stats_Azahar_Crystal.json",
    ".crystal_wram.json",
    ".pokebot_accessors.json",
    "some.log",
    ".git/config",
    ".update-backup/20260101-000000/launcher.py",
])
def test_user_data_is_preserved(rel):
    assert updater.is_preserved(Path(rel)), rel


@pytest.mark.parametrize("rel", [
    "launcher.py",
    "run.py",
    "pokebot/bot.py",
    "pokebot/updater.py",
    "docs/TUTORIAL.md",
    "requirements.txt",
    "BUILD_INFO.json",
])
def test_program_files_are_replaced(rel):
    assert not updater.is_preserved(Path(rel)), rel


# ----------------------------------------------------------------------
# Applying to a zip install
# ----------------------------------------------------------------------
def _patch_zip_update(monkeypatch, tmp_path, files, sums=""):
    """Wire _download_zip/_expected_sha256 to a local archive."""
    src = make_zip(tmp_path / "release.zip", files)

    def fake_download(dest, timeout, on_log):
        dest.write_bytes(src.read_bytes())
        return dest

    monkeypatch.setattr(updater, "_download_zip", fake_download)
    monkeypatch.setattr(updater, "_expected_sha256", lambda t: sums)
    return src


def test_applying_an_update_replaces_program_files(tmp_path, monkeypatch):
    root = make_install(tmp_path / "inst")
    _patch_zip_update(monkeypatch, tmp_path, new_release_files())

    res = updater.apply_update(root)

    assert res.ok, res.detail
    assert (root / "launcher.py").read_text() == "# NEW launcher\n"
    assert (root / "pokebot" / "bot.py").read_text() == "# NEW bot\n"
    assert res.restart_required


def test_applying_an_update_never_touches_the_users_config(tmp_path,
                                                           monkeypatch):
    root = make_install(tmp_path / "inst")
    before = (root / "config.yaml").read_text()
    _patch_zip_update(monkeypatch, tmp_path, new_release_files())

    updater.apply_update(root)

    assert (root / "config.yaml").read_text() == before
    assert "MINE" in (root / "config.yaml").read_text()


def test_applying_an_update_never_touches_caught_pokemon(tmp_path,
                                                         monkeypatch):
    root = make_install(tmp_path / "inst")
    files = new_release_files()
    files["targets/shiny_656.pk6"] = b"OVERWRITTEN"
    _patch_zip_update(monkeypatch, tmp_path, files)

    updater.apply_update(root)

    assert (root / "targets" / "shiny_656.pk6").read_bytes() == b"precious"


def test_replaced_files_are_backed_up(tmp_path, monkeypatch):
    root = make_install(tmp_path / "inst")
    _patch_zip_update(monkeypatch, tmp_path, new_release_files())

    res = updater.apply_update(root)

    backup = Path(res.backup)
    assert backup.is_dir()
    assert (backup / "launcher.py").read_text() == "# old launcher\n"


def test_the_stamp_advances_so_the_next_check_is_clean(tmp_path, monkeypatch):
    root = make_install(tmp_path / "inst", sha=SHA_A)
    _patch_zip_update(monkeypatch, tmp_path, new_release_files(SHA_B))

    updater.apply_update(root)

    assert updater.local_version(root).sha == SHA_B


def test_an_identical_release_changes_nothing(tmp_path, monkeypatch):
    root = make_install(tmp_path / "inst")
    same = {
        "launcher.py": b"# old launcher\n",
        "run.py": b"# old run\n",
        "pokebot/bot.py": b"# old bot\n",
        "BUILD_INFO.json": (root / "BUILD_INFO.json").read_bytes(),
    }
    _patch_zip_update(monkeypatch, tmp_path, same)

    res = updater.apply_update(root)

    assert res.ok
    assert res.changed == ()
    assert not res.restart_required
    assert not (root / updater.BACKUP_DIR).exists()


def test_an_archive_that_is_not_pokebot_is_refused(tmp_path, monkeypatch):
    root = make_install(tmp_path / "inst")
    _patch_zip_update(monkeypatch, tmp_path, {"totally_other.py": b"nope"})

    res = updater.apply_update(root)

    assert not res.ok
    assert "did not look like pokebot" in res.detail
    assert (root / "launcher.py").read_text() == "# old launcher\n"


def test_a_corrupt_download_is_refused(tmp_path, monkeypatch):
    root = make_install(tmp_path / "inst")

    def fake_download(dest, timeout, on_log):
        dest.write_bytes(b"this is not a zip")
        return dest

    monkeypatch.setattr(updater, "_download_zip", fake_download)
    monkeypatch.setattr(updater, "_expected_sha256", lambda t: "")

    res = updater.apply_update(root)

    assert not res.ok and "corrupt" in res.detail
    assert (root / "launcher.py").read_text() == "# old launcher\n"


def test_a_checksum_mismatch_aborts_before_touching_anything(tmp_path,
                                                            monkeypatch):
    root = make_install(tmp_path / "inst")
    _patch_zip_update(monkeypatch, tmp_path, new_release_files(),
                      sums="f" * 64)

    res = updater.apply_update(root)

    assert not res.ok
    assert "checksum mismatch" in res.detail
    assert (root / "launcher.py").read_text() == "# old launcher\n"


def test_a_matching_checksum_is_accepted(tmp_path, monkeypatch):
    import hashlib
    src = make_zip(tmp_path / "release.zip", new_release_files())
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    root = make_install(tmp_path / "inst")
    _patch_zip_update(monkeypatch, tmp_path, new_release_files(),
                      sums=digest)

    res = updater.apply_update(root)

    assert res.ok, res.detail


def test_the_published_checksum_is_parsed_from_the_sums_file(monkeypatch):
    line = "d" * 64 + "  dist/pokebot-3ds.zip\n" + "e" * 64 + "  other.txt\n"
    monkeypatch.setattr(updater, "_get",
                        lambda *a, **k: line.encode())
    assert updater._expected_sha256(1.0) == "d" * 64


def test_a_missing_sums_file_does_not_block_the_update(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise updater.UpdateError("404")

    monkeypatch.setattr(updater, "_get", boom)
    assert updater._expected_sha256(1.0) == ""


# ----------------------------------------------------------------------
# Archive safety
# ----------------------------------------------------------------------
@pytest.mark.parametrize("evil", [
    "../escaped.py",
    "../../escaped.py",
    "pokebot/../../escaped.py",
    "/etc/passwd",
])
def test_paths_that_escape_the_install_are_dropped(tmp_path, evil):
    z = make_zip(tmp_path / "evil.zip", {evil: b"pwned", "launcher.py": b"ok"})
    with zipfile.ZipFile(z) as zf:
        names = [m.filename for m in updater.safe_members(zf)]
    assert names == ["launcher.py"]


def test_an_escaping_member_cannot_write_outside_the_root(tmp_path,
                                                          monkeypatch):
    root = make_install(tmp_path / "inst")
    outside = tmp_path / "escaped.py"
    files = dict(new_release_files())
    files["../escaped.py"] = b"pwned"
    _patch_zip_update(monkeypatch, tmp_path, files)

    updater.apply_update(root)

    assert not outside.exists()


# ----------------------------------------------------------------------
# Applying to a git checkout
# ----------------------------------------------------------------------
class GitStub:
    """Scripted git. ``calls`` records the subcommands attempted."""

    def __init__(self, status_out="", pull_rc=0, pull_out="Updating a..b"):
        self.status_out = status_out
        self.pull_rc = pull_rc
        self.pull_out = pull_out
        self.calls: list[tuple] = []

    def __call__(self, root, *args, **kw):
        self.calls.append(args)

        class P:
            returncode = 0
            stdout = ""
            stderr = ""

        p = P()
        if args[:1] == ("status",):
            p.stdout = self.status_out
        elif args[:1] == ("pull",):
            p.returncode = self.pull_rc
            p.stdout = self.pull_out
            if self.pull_rc:
                p.stderr = self.pull_out
        elif args[:1] == ("rev-parse",):
            p.stdout = SHA_B
        return p


def test_a_clean_checkout_fast_forwards(tmp_path, monkeypatch):
    root = make_install(tmp_path / "inst", git=True)
    git = GitStub()
    monkeypatch.setattr(updater, "_run_git", git)

    res = updater.apply_update(root)

    assert res.ok
    assert ("pull", "--ff-only") in git.calls


def test_a_dirty_checkout_is_left_alone(tmp_path, monkeypatch):
    root = make_install(tmp_path / "inst", git=True)
    git = GitStub(status_out=" M pokebot/bot.py\n")
    monkeypatch.setattr(updater, "_run_git", git)

    res = updater.apply_update(root)

    assert not res.ok
    assert "local changes" in res.detail
    assert "pokebot/bot.py" in res.detail
    assert not any(a[:1] == ("pull",) for a in git.calls)


def test_a_failed_pull_is_reported_not_forced(tmp_path, monkeypatch):
    root = make_install(tmp_path / "inst", git=True)
    git = GitStub(pull_rc=1, pull_out="fatal: refusing to merge histories")
    monkeypatch.setattr(updater, "_run_git", git)

    res = updater.apply_update(root)

    assert not res.ok
    assert "refusing to merge" in res.detail
    assert not any("--force" in " ".join(a) for a in git.calls)


def test_git_missing_is_explained(tmp_path, monkeypatch):
    root = make_install(tmp_path / "inst", git=True)
    monkeypatch.setattr(updater, "_run_git", lambda *a, **k: None)

    res = updater.apply_update(root)

    assert not res.ok
    assert "git is not available" in res.detail


def test_an_already_current_checkout_needs_no_restart(tmp_path, monkeypatch):
    root = make_install(tmp_path / "inst", git=True)
    monkeypatch.setattr(updater, "_run_git",
                        GitStub(pull_out="Already up to date."))

    res = updater.apply_update(root)

    assert res.ok and not res.restart_required


# ----------------------------------------------------------------------
# Backups
# ----------------------------------------------------------------------
def test_old_backups_are_pruned_newest_first(tmp_path):
    root = tmp_path / "inst"
    base = root / updater.BACKUP_DIR
    for stamp in ["20260101-000000", "20260102-000000", "20260103-000000",
                  "20260104-000000", "20260105-000000"]:
        (base / stamp).mkdir(parents=True)

    removed = updater.prune_backups(root, keep=3)

    kept = sorted(d.name for d in base.iterdir())
    assert removed == 2
    assert kept == ["20260103-000000", "20260104-000000", "20260105-000000"]


def test_pruning_a_fresh_install_is_a_no_op(tmp_path):
    assert updater.prune_backups(tmp_path / "nothing-here") == 0


# ----------------------------------------------------------------------
# The startup-check opt-out
# ----------------------------------------------------------------------
@pytest.mark.parametrize("cfg", [
    None,
    {},
    {"updates": None},
    {"updates": {}},
    {"updates": "yes please"},
    {"updates": {"check_on_start": True}},
])
def test_checking_on_start_is_the_default(cfg):
    assert updater.should_check_on_start(cfg)


@pytest.mark.parametrize("cfg", [
    {"updates": {"check_on_start": False}},
    {"updates": {"check_on_start": 0}},
])
def test_checking_on_start_can_be_turned_off(cfg):
    assert not updater.should_check_on_start(cfg)
