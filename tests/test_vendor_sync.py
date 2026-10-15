"""Vendoring: shared/ctf_common is the source of truth, app/<rt>/ctf_common are committed copies
(spec: ctf-agent-runtimes, "Shared code vendored" / "Drift detection")."""

from pathlib import Path

import pytest

import sync_common

REPO_ROOT = Path(__file__).resolve().parents[1]


def make_tree(root: Path, runtimes=("harness_sdk", "decider_loop")) -> Path:
    src = root / "shared" / "ctf_common"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text('"""pkg"""\n')
    (src / "payload.py").write_text("X = 1\n")
    (src / "README.md").write_text("docs only, not vendored\n")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "payload.cpython-314.pyc").write_bytes(b"\x00")
    for rt in runtimes:
        (root / "app" / rt).mkdir(parents=True)
        (root / "app" / rt / "main.py").write_text("# entrypoint\n")
    return root


@pytest.fixture
def tree(tmp_path):
    return make_tree(tmp_path)


class TestSync:
    def test_copies_python_files_into_every_existing_runtime(self, tree):
        sync_common.sync(tree)
        for rt in ("harness_sdk", "decider_loop"):
            dest = tree / "app" / rt / "ctf_common"
            assert (dest / "payload.py").read_text() == "X = 1\n"
            assert (dest / "__init__.py").exists()

    def test_readme_and_bytecode_are_not_vendored(self, tree):
        sync_common.sync(tree)
        dest = tree / "app" / "harness_sdk" / "ctf_common"
        assert not (dest / "README.md").exists()
        assert not (dest / "__pycache__").exists()

    def test_runtime_dirs_that_do_not_exist_yet_are_skipped(self, tmp_path):
        make_tree(tmp_path, runtimes=("harness_sdk",))
        report = sync_common.sync(tmp_path)
        assert [p.parent.name for p in report.targets] == ["harness_sdk"]
        assert not (tmp_path / "app" / "harness").exists()

    def test_sync_removes_stale_files(self, tree):
        sync_common.sync(tree)
        stale = tree / "app" / "harness_sdk" / "ctf_common" / "old_module.py"
        stale.write_text("gone = True\n")
        sync_common.sync(tree)
        assert not stale.exists()

    def test_sync_overwrites_hand_edits(self, tree):
        sync_common.sync(tree)
        copy = tree / "app" / "harness_sdk" / "ctf_common" / "payload.py"
        copy.write_text("X = 999\n")
        sync_common.sync(tree)
        assert copy.read_text() == "X = 1\n"

    def test_sync_is_idempotent(self, tree):
        sync_common.sync(tree)
        first = sync_common.snapshot(tree / "app" / "harness_sdk" / "ctf_common")
        sync_common.sync(tree)
        assert sync_common.snapshot(tree / "app" / "harness_sdk" / "ctf_common") == first

    def test_unrelated_runtime_files_are_untouched(self, tree):
        sync_common.sync(tree)
        assert (tree / "app" / "harness_sdk" / "main.py").read_text() == "# entrypoint\n"


class TestCheck:
    def test_clean_after_sync(self, tree):
        sync_common.sync(tree)
        assert sync_common.check(tree) == []

    def test_hand_edited_copy_is_reported_with_its_file(self, tree):
        sync_common.sync(tree)
        (tree / "app" / "decider_loop" / "ctf_common" / "payload.py").write_text("X = 2\n")
        problems = sync_common.check(tree)
        assert len(problems) == 1
        assert "decider_loop" in problems[0] and "payload.py" in problems[0] and "differs" in problems[0]

    def test_missing_vendored_file_is_reported(self, tree):
        sync_common.sync(tree)
        (tree / "app" / "harness_sdk" / "ctf_common" / "payload.py").unlink()
        problems = sync_common.check(tree)
        assert any("payload.py" in p and "missing" in p for p in problems)

    def test_extra_vendored_file_is_reported(self, tree):
        sync_common.sync(tree)
        (tree / "app" / "harness_sdk" / "ctf_common" / "extra.py").write_text("pass\n")
        assert any("extra.py" in p and "unexpected" in p for p in sync_common.check(tree))

    def test_runtime_that_was_never_synced_is_reported(self, tree):
        problems = sync_common.check(tree)
        assert len(problems) >= 2 and all("ctf_common" in p for p in problems)

    def test_source_change_makes_all_copies_stale(self, tree):
        sync_common.sync(tree)
        (tree / "shared" / "ctf_common" / "payload.py").write_text("X = 3\n")
        problems = sync_common.check(tree)
        assert sum("payload.py" in p for p in problems) == 2

    def test_bytecode_in_a_copy_does_not_count_as_drift(self, tree):
        sync_common.sync(tree)
        cache = tree / "app" / "harness_sdk" / "ctf_common" / "__pycache__"
        cache.mkdir()
        (cache / "payload.cpython-314.pyc").write_bytes(b"\x01")
        assert sync_common.check(tree) == []


class TestCli:
    def test_check_exits_1_and_names_the_file_on_drift(self, tree, capsys):
        sync_common.sync(tree)
        (tree / "app" / "harness_sdk" / "ctf_common" / "payload.py").write_text("X = 2\n")
        assert sync_common.main(["--check"], root=tree) == 1
        assert "payload.py" in capsys.readouterr().out

    def test_check_exits_0_when_clean(self, tree, capsys):
        sync_common.sync(tree)
        assert sync_common.main(["--check"], root=tree) == 0
        assert "in sync" in capsys.readouterr().out

    def test_default_mode_syncs(self, tree):
        assert sync_common.main([], root=tree) == 0
        assert sync_common.check(tree) == []

    def test_check_does_not_modify_anything(self, tree):
        sync_common.main(["--check"], root=tree)
        assert not (tree / "app" / "harness_sdk" / "ctf_common").exists()


def test_committed_vendored_copies_match_the_source_of_truth():
    """The real guard: fails on any hand edit of app/<rt>/ctf_common or a forgotten sync."""
    assert sync_common.check(REPO_ROOT) == []
