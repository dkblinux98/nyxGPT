"""Unit tests for tools_fs module.

Tests cover the filesystem tools: ls, cat, and grep.
These tests verify functionality, error handling, and edge cases.

Related: tools_fs.py implementation
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from nyxgpt.tools_fs import cat, grep, ls

pytestmark = pytest.mark.unit


def _capture_output(fn, *args, **kwargs) -> tuple[int, str, str]:
    """Capture stdout and stderr from a function call."""
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = fn(*args, **kwargs)
    return int(rc), out.getvalue(), err.getvalue()


# ----------------------------
# list_directory (ls) tests
# ----------------------------


def test_ls_lists_files_in_directory(tmp_path: Path) -> None:
    """Test ls lists files in a directory."""
    # Create test files
    (tmp_path / "file1.txt").write_text("content1")
    (tmp_path / "file2.txt").write_text("content2")
    (tmp_path / "subdir").mkdir()

    rc, out, err = _capture_output(ls, tmp_path)

    assert rc == 0
    assert err == ""
    lines = out.strip().split("\n")
    assert "file1.txt" in lines
    assert "file2.txt" in lines
    assert "subdir/" in lines  # Directories should have trailing slash


def test_ls_with_single_file(tmp_path: Path) -> None:
    """Test ls with a single file returns just that file."""
    test_file = tmp_path / "test.txt"
    test_file.write_text("content")

    rc, out, err = _capture_output(ls, test_file)

    assert rc == 0
    assert err == ""
    assert str(test_file) in out


def test_ls_nonexistent_directory(tmp_path: Path) -> None:
    """Test ls with nonexistent directory returns error."""
    nonexistent = tmp_path / "nonexistent"

    rc, out, err = _capture_output(ls, nonexistent)

    assert rc == 1
    assert "No such path" in err


def test_ls_empty_directory(tmp_path: Path) -> None:
    """Test ls with empty directory returns successfully."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    rc, out, err = _capture_output(ls, empty_dir)

    assert rc == 0
    assert err == ""
    assert out.strip() == ""


def test_ls_sorts_case_insensitive(tmp_path: Path) -> None:
    """Test ls sorts files case-insensitively."""
    (tmp_path / "zebra.txt").write_text("z")
    (tmp_path / "Apple.txt").write_text("a")
    (tmp_path / "banana.txt").write_text("b")

    rc, out, err = _capture_output(ls, tmp_path)

    assert rc == 0
    lines = out.strip().split("\n")
    assert lines == ["Apple.txt", "banana.txt", "zebra.txt"]


def test_ls_with_hidden_files(tmp_path: Path) -> None:
    """Test ls includes hidden files (starting with .)."""
    (tmp_path / ".hidden").write_text("hidden content")
    (tmp_path / "visible.txt").write_text("visible")

    rc, out, err = _capture_output(ls, tmp_path)

    assert rc == 0
    lines = out.strip().split("\n")
    assert ".hidden" in lines
    assert "visible.txt" in lines


# ----------------------------
# cat_file (cat) tests
# ----------------------------


def test_cat_reads_file(tmp_path: Path) -> None:
    """Test cat reads entire file content."""
    test_file = tmp_path / "test.txt"
    content = "Line 1\nLine 2\nLine 3"
    test_file.write_text(content)

    rc, out, err = _capture_output(cat, test_file)

    assert rc == 0
    assert err == ""
    assert out.strip() == content


def test_cat_with_head_limit(tmp_path: Path) -> None:
    """Test cat with head parameter limits output."""
    test_file = tmp_path / "test.txt"
    test_file.write_text("Line 1\nLine 2\nLine 3\nLine 4\nLine 5")

    rc, out, err = _capture_output(cat, test_file, head=3)

    assert rc == 0
    assert err == ""
    lines = out.strip().split("\n")
    assert len(lines) == 3
    assert lines == ["Line 1", "Line 2", "Line 3"]


def test_cat_with_tail_limit(tmp_path: Path) -> None:
    """Test cat with tail parameter shows last lines."""
    test_file = tmp_path / "test.txt"
    test_file.write_text("Line 1\nLine 2\nLine 3\nLine 4\nLine 5")

    rc, out, err = _capture_output(cat, test_file, tail=2)

    assert rc == 0
    assert err == ""
    lines = out.strip().split("\n")
    assert len(lines) == 2
    assert lines == ["Line 4", "Line 5"]


def test_cat_nonexistent_file(tmp_path: Path) -> None:
    """Test cat with nonexistent file returns error."""
    nonexistent = tmp_path / "nonexistent.txt"

    rc, out, err = _capture_output(cat, nonexistent)

    assert rc == 1
    assert "No such file" in err


def test_cat_directory_fails(tmp_path: Path) -> None:
    """Test cat with directory path returns error."""
    test_dir = tmp_path / "dir"
    test_dir.mkdir()

    rc, out, err = _capture_output(cat, test_dir)

    assert rc == 1
    assert "No such file" in err


def test_cat_with_both_head_and_tail_fails(tmp_path: Path) -> None:
    """Test cat with both head and tail parameters returns error."""
    test_file = tmp_path / "test.txt"
    test_file.write_text("content")

    rc, out, err = _capture_output(cat, test_file, head=5, tail=3)

    assert rc == 2
    assert "use only one of --head or --tail" in err


def test_cat_empty_file(tmp_path: Path) -> None:
    """Test cat with empty file succeeds."""
    test_file = tmp_path / "empty.txt"
    test_file.write_text("")

    rc, out, err = _capture_output(cat, test_file)

    assert rc == 0
    assert err == ""
    assert out == ""


def test_cat_with_unicode_content(tmp_path: Path) -> None:
    """Test cat handles unicode content correctly."""
    test_file = tmp_path / "unicode.txt"
    content = "Hello 世界 🌍"
    test_file.write_text(content, encoding="utf-8")

    rc, out, err = _capture_output(cat, test_file)

    assert rc == 0
    assert err == ""
    assert content in out


# ----------------------------
# grep_file (grep) tests
# ----------------------------


def test_grep_finds_pattern_in_file(tmp_path: Path) -> None:
    """Test grep finds pattern in a file."""
    test_file = tmp_path / "test.txt"
    test_file.write_text("Line 1: hello\nLine 2: world\nLine 3: hello again")

    rc, out, err = _capture_output(grep, "hello", test_file)

    assert rc == 0
    assert err == ""
    assert f"{test_file}:1: Line 1: hello" in out
    assert f"{test_file}:3: Line 3: hello again" in out


def test_grep_case_sensitive_search(tmp_path: Path) -> None:
    """Test grep is case-sensitive by default."""
    test_file = tmp_path / "test.txt"
    test_file.write_text("Hello\nhello\nHELLO")

    rc, out, err = _capture_output(grep, "hello", test_file)

    assert rc == 0
    # Should only match lowercase "hello"
    assert out.count(f"{test_file}") == 1
    assert "Line 2" in out or ":2:" in out


def test_grep_regex_pattern(tmp_path: Path) -> None:
    """Test grep supports regex patterns."""
    test_file = tmp_path / "test.txt"
    test_file.write_text("test123\ntest456\nabc789")

    rc, out, err = _capture_output(grep, r"test\d+", test_file)

    assert rc == 0
    assert "test123" in out
    assert "test456" in out
    assert "abc789" not in out


def test_grep_no_matches_returns_error(tmp_path: Path) -> None:
    """Test grep returns error when no matches found."""
    test_file = tmp_path / "test.txt"
    test_file.write_text("Line 1\nLine 2\nLine 3")

    rc, out, err = _capture_output(grep, "nonexistent", test_file)

    assert rc == 1


def test_grep_nonexistent_file(tmp_path: Path) -> None:
    """Test grep with nonexistent file returns error."""
    nonexistent = tmp_path / "nonexistent.txt"

    rc, out, err = _capture_output(grep, "pattern", nonexistent)

    assert rc == 1
    assert "No such path" in err


def test_grep_invalid_regex(tmp_path: Path) -> None:
    """Test grep with invalid regex returns error."""
    test_file = tmp_path / "test.txt"
    test_file.write_text("content")

    rc, out, err = _capture_output(grep, "[invalid(", test_file)

    assert rc == 2
    assert "Invalid regex" in err


def test_grep_directory_search(tmp_path: Path) -> None:
    """Test grep searches recursively in directory."""
    (tmp_path / "file1.txt").write_text("match in file1")
    (tmp_path / "file2.txt").write_text("no pattern here")
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    (subdir / "file3.txt").write_text("match in subdir")

    rc, out, err = _capture_output(grep, "match", tmp_path)

    assert rc == 0
    assert "file1.txt" in out
    assert "file3.txt" in out
    assert "file2.txt" not in out


def test_grep_skips_hidden_files(tmp_path: Path) -> None:
    """Test grep skips hidden files and directories."""
    (tmp_path / "visible.txt").write_text("match visible")
    (tmp_path / ".hidden.txt").write_text("match hidden")
    hidden_dir = tmp_path / ".hidden_dir"
    hidden_dir.mkdir()
    (hidden_dir / "file.txt").write_text("match in hidden dir")

    rc, out, err = _capture_output(grep, "match", tmp_path)

    assert rc == 0
    assert "visible.txt" in out
    assert ".hidden.txt" not in out
    assert ".hidden_dir" not in out


def test_grep_max_matches_limit(tmp_path: Path) -> None:
    """Test grep respects max_matches parameter."""
    test_file = tmp_path / "test.txt"
    # Create 100 lines with matches
    content = "\n".join([f"match line {i}" for i in range(100)])
    test_file.write_text(content)

    rc, out, err = _capture_output(grep, "match", test_file, max_matches=10)

    assert rc == 0
    # Should stop at 10 matches
    lines = [line for line in out.split("\n") if line.strip()]
    assert len(lines) == 10


def test_grep_multiple_matches_per_line_counts_once(tmp_path: Path) -> None:
    """Test grep counts each line once even with multiple matches."""
    test_file = tmp_path / "test.txt"
    test_file.write_text("test test test")

    rc, out, err = _capture_output(grep, "test", test_file)

    assert rc == 0
    # Should only show one line even though pattern appears 3 times
    lines = [line for line in out.split("\n") if line.strip()]
    assert len(lines) == 1


# ----------------------------
# root confinement tests (#3195 defense in depth)
# ----------------------------


def test_ls_within_root_succeeds(tmp_path: Path) -> None:
    """Test ls succeeds for a path inside the given root."""
    (tmp_path / "file.txt").write_text("content")

    rc, out, err = _capture_output(ls, tmp_path, root=tmp_path)

    assert rc == 0
    assert err == ""
    assert "file.txt" in out


def test_ls_outside_root_rejected(tmp_path: Path) -> None:
    """Test ls rejects a path outside the given root."""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    rc, out, err = _capture_output(ls, outside, root=root)

    assert rc == 1
    assert "escapes allowed root" in err


def test_cat_within_root_succeeds(tmp_path: Path) -> None:
    """Test cat succeeds for a file inside the given root."""
    test_file = tmp_path / "test.txt"
    test_file.write_text("content")

    rc, out, err = _capture_output(cat, test_file, root=tmp_path)

    assert rc == 0
    assert out.strip() == "content"


def test_cat_outside_root_rejected(tmp_path: Path) -> None:
    """Test cat rejects a file outside the given root."""
    root = tmp_path / "root"
    root.mkdir()
    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("top secret")

    rc, out, err = _capture_output(cat, outside_file, root=root)

    assert rc == 1
    assert "escapes allowed root" in err
    assert "top secret" not in out


def test_cat_traversal_outside_root_rejected(tmp_path: Path) -> None:
    """Test cat rejects a `..`-traversal path that escapes the root."""
    root = tmp_path / "root"
    root.mkdir()
    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("top secret")

    rc, out, err = _capture_output(cat, root / ".." / "secret.txt", root=root)

    assert rc == 1
    assert "escapes allowed root" in err
    assert "top secret" not in out


def test_grep_outside_root_rejected(tmp_path: Path) -> None:
    """Test grep rejects a path outside the given root."""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file.txt").write_text("match")

    rc, out, err = _capture_output(grep, "match", outside, root=root)

    assert rc == 1
    assert "escapes allowed root" in err


def test_cat_root_itself_succeeds(tmp_path: Path) -> None:
    """Test cat succeeds when the requested path is the root itself, not just a descendant."""
    test_file = tmp_path / "test.txt"
    test_file.write_text("content")

    rc, out, err = _capture_output(cat, test_file, root=test_file)

    assert rc == 0
    assert out.strip() == "content"
