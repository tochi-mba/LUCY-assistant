"""`repos.lock` records the commit each sibling was verified at, and says when it drifts.

The lock is only worth keeping if it stays true and if it refuses to name a private
repository, so both are tested here rather than left to the generator's good intentions.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import lockrepos  # noqa: E402


def repo(root: Path, name: str, subject: str = "one") -> str:
    """A real git repository with one commit, because the script shells out to git."""
    path = root / name
    path.mkdir(parents=True)
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
    )
    run("init", "-q")
    run("config", "user.email", "test@example.invalid")
    run("config", "user.name", "Test")
    (path / "file.txt").write_text(subject, encoding="utf-8")
    run("add", ".")
    run("commit", "-qm", subject)
    return run("rev-parse", "HEAD").stdout.strip()


def family(root: Path, names: list[str]) -> dict[str, str]:
    manifest = "\n".join(f"{name} https://example.invalid/{name}.git" for name in names)
    (root / "repos.txt").write_text(f"# a comment\n\n{manifest}\n", encoding="utf-8")
    return {name: repo(root, name) for name in names}


# --- the manifest -------------------------------------------------------------------------


def test_the_manifest_skips_comments_and_blank_lines(tmp_path: Path) -> None:
    family(tmp_path, ["Alpha", "Beta"])
    assert lockrepos.read_manifest(tmp_path / "repos.txt") == ["Alpha", "Beta"]


def test_a_missing_manifest_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(lockrepos.LockError, match="missing"):
        lockrepos.read_manifest(tmp_path / "repos.txt")


# --- recording ----------------------------------------------------------------------------


def test_writing_records_every_repository_at_its_head(tmp_path: Path) -> None:
    heads = family(tmp_path, ["Alpha", "Beta"])
    lock = tmp_path / "repos.lock"
    assert lockrepos.write(["Alpha", "Beta"], tmp_path, lock) == 2
    assert lockrepos.read_lock(lock) == heads


def test_the_lock_carries_the_reason_it_exists(tmp_path: Path) -> None:
    """A generated file nobody can read is a file nobody will re-record."""
    family(tmp_path, ["Alpha"])
    lock = tmp_path / "repos.lock"
    lockrepos.write(["Alpha"], tmp_path, lock)
    text = lock.read_text(encoding="utf-8")
    assert "make lock" in text
    assert "0011-private-services-are-extensions" in text


def test_recording_a_repository_with_no_checkout_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "repos.txt").write_text("Missing https://example.invalid/m.git\n", encoding="utf-8")
    with pytest.raises(lockrepos.LockError, match="clone it first"):
        lockrepos.write(["Missing"], tmp_path, tmp_path / "repos.lock")


# --- checking -----------------------------------------------------------------------------


def test_matching_checkouts_report_no_drift(tmp_path: Path) -> None:
    heads = family(tmp_path, ["Alpha", "Beta"])
    assert lockrepos.compare(["Alpha", "Beta"], heads, tmp_path) == []


def test_a_moved_checkout_is_named_with_both_commits(tmp_path: Path) -> None:
    heads = family(tmp_path, ["Alpha"])
    stale = dict(heads, Alpha="0" * lockrepos.SHA_LENGTH)
    (drift,) = lockrepos.compare(["Alpha"], stale, tmp_path)
    assert drift.name == "Alpha"
    assert "locked 000000000000" in drift.reason
    assert heads["Alpha"][:12] in drift.reason


def test_a_repository_absent_from_the_lock_says_to_record_it(tmp_path: Path) -> None:
    family(tmp_path, ["Alpha"])
    (drift,) = lockrepos.compare(["Alpha"], {}, tmp_path)
    assert "make lock" in drift.reason


def test_a_repository_with_no_checkout_is_named(tmp_path: Path) -> None:
    (tmp_path / "repos.txt").write_text("Alpha https://example.invalid/a.git\n", encoding="utf-8")
    (drift,) = lockrepos.compare(["Alpha"], {"Alpha": "a" * lockrepos.SHA_LENGTH}, tmp_path)
    assert drift.reason == "no checkout here"


def test_a_directory_that_is_not_a_repository_reads_as_no_checkout(tmp_path: Path) -> None:
    (tmp_path / "Alpha").mkdir()
    assert lockrepos.head_of(tmp_path / "Alpha") == ""


def test_a_lock_entry_with_no_manifest_row_is_named(tmp_path: Path) -> None:
    """A repository removed from repos.txt must not linger in the lock."""
    heads = family(tmp_path, ["Alpha"])
    stale = dict(heads, Removed="b" * lockrepos.SHA_LENGTH)
    (drift,) = lockrepos.compare(["Alpha"], stale, tmp_path)
    assert drift.name == "Removed"
    assert "not in repos.txt" in drift.reason


def test_a_malformed_lock_line_is_an_error(tmp_path: Path) -> None:
    lock = tmp_path / "repos.lock"
    lock.write_text("Alpha\n", encoding="utf-8")
    with pytest.raises(lockrepos.LockError, match="expected"):
        lockrepos.read_lock(lock)


def test_a_missing_lock_is_empty_rather_than_an_error(tmp_path: Path) -> None:
    assert lockrepos.read_lock(tmp_path / "repos.lock") == {}


# --- the command ---------------------------------------------------------------------------


def test_the_command_writes_then_reports_a_match(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    family(tmp_path, ["Alpha", "Beta"])
    argv = [
        "--repos-file",
        str(tmp_path / "repos.txt"),
        "--lock-file",
        str(tmp_path / "repos.lock"),
    ]
    assert lockrepos.main([*argv, "--write"]) == 0
    assert "Recorded 2" in capsys.readouterr().out
    assert lockrepos.main(argv) == 0
    assert "All 2 repositories match" in capsys.readouterr().out


def test_the_command_exits_one_on_drift(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    family(tmp_path, ["Alpha"])
    argv = [
        "--repos-file",
        str(tmp_path / "repos.txt"),
        "--lock-file",
        str(tmp_path / "repos.lock"),
    ]
    assert lockrepos.main(argv) == 1
    assert "differ from repos.lock" in capsys.readouterr().out


def test_the_command_reports_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    family(tmp_path, ["Alpha"])
    argv = [
        "--repos-file",
        str(tmp_path / "repos.txt"),
        "--lock-file",
        str(tmp_path / "repos.lock"),
    ]
    assert lockrepos.main([*argv, "--json"]) == 1
    assert '"name": "Alpha"' in capsys.readouterr().out


def test_the_command_exits_two_on_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["--repos-file", str(tmp_path / "nothing.txt"), "--lock-file", str(tmp_path / "l.lock")]
    assert lockrepos.main(argv) == 2
    assert "missing" in capsys.readouterr().err


# --- the real files -------------------------------------------------------------------------


def test_the_committed_lock_covers_exactly_the_committed_manifest() -> None:
    """The two files agree, or somebody added a service and forgot to record it."""
    names = lockrepos.read_manifest()
    locked = lockrepos.read_lock()
    assert set(locked) == set(names), "repos.lock and repos.txt disagree; run `make lock`"


def test_every_locked_commit_is_a_full_sha() -> None:
    for name, commit in lockrepos.read_lock().items():
        assert len(commit) == lockrepos.SHA_LENGTH, f"{name} is not a full commit"
        assert all(c in "0123456789abcdef" for c in commit), name


def test_the_lock_names_no_private_repository() -> None:
    """ADR-0011: a public repository never names a private one, and a lock file is no
    exception. Only `repos.txt` is read, and `.repos.local.txt` is gitignored."""
    local = ROOT / ".repos.local.txt"
    if not local.is_file():
        pytest.skip("no private checkouts configured here")
    private = {
        line.split("#", 1)[0].strip().split()[0]
        for line in local.read_text(encoding="utf-8").splitlines()
        if line.split("#", 1)[0].strip()
    }
    assert private.isdisjoint(set(lockrepos.read_lock()))
