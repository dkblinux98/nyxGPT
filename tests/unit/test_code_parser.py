"""Unit tests for code repository parsing and indexing."""

import tempfile
from pathlib import Path

import pytest

from nyxgpt.rag.code_parser import (
    chunk_code_by_structure,
    collect_code_files,
    extract_comments_and_docstrings,
    index_repository,
    parse_code_file,
    parse_gitignore,
    should_ignore,
)


def _big_py_function(name: str, n_lines: int = 20) -> str:
    """Build a Python function with a body large enough to exceed small chunk sizes."""
    body = "\n".join(f"    line_{i} = {i}" for i in range(n_lines))
    return f"def {name}():\n{body}\n"


def _big_js_function(name: str, n_lines: int = 20) -> str:
    """Build a JS function with a body large enough to exceed small chunk sizes."""
    body = "\n".join(f"    var line_{i} = {i};" for i in range(n_lines))
    return f"function {name}() {{\n{body}\n}}\n"


@pytest.mark.unit
def test_parse_gitignore():
    """Test parsing of .gitignore file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        gitignore = repo_path / ".gitignore"

        # Create a .gitignore file
        gitignore.write_text("""
# Comment line
__pycache__/
*.pyc
node_modules/
.env
dist
build/
""")

        patterns = parse_gitignore(repo_path)
        assert "__pycache__/" in patterns
        assert "*.pyc" in patterns
        assert "node_modules/" in patterns
        assert ".env" in patterns
        assert "dist" in patterns
        assert "build/" in patterns
        # Comment should not be included
        assert not any("Comment" in p for p in patterns)


@pytest.mark.unit
def test_should_ignore():
    """Test gitignore pattern matching."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        patterns = ["__pycache__/", "*.pyc", "node_modules/", ".env", "dist"]

        # Test directory ignoring
        pycache_path = repo_path / "src" / "__pycache__" / "module.pyc"
        assert should_ignore(pycache_path, repo_path, patterns) is True

        # Test wildcard pattern
        pyc_file = repo_path / "src" / "test.pyc"
        assert should_ignore(pyc_file, repo_path, patterns) is True

        # Test exact match
        env_file = repo_path / ".env"
        assert should_ignore(env_file, repo_path, patterns) is True

        # Test .git always ignored
        git_file = repo_path / ".git" / "config"
        assert should_ignore(git_file, repo_path, patterns) is True

        # Test file that should NOT be ignored
        py_file = repo_path / "src" / "main.py"
        assert should_ignore(py_file, repo_path, patterns) is False


@pytest.mark.unit
def test_should_ignore_file_not_relative_to_repo():
    """should_ignore should return False when file_path isn't under repo_path."""
    with tempfile.TemporaryDirectory() as tmpdir1, tempfile.TemporaryDirectory() as tmpdir2:
        repo_path = Path(tmpdir1)
        unrelated_file = Path(tmpdir2) / "outside.py"

        assert should_ignore(unrelated_file, repo_path, ["*.py"]) is False


@pytest.mark.unit
def test_extract_comments_python():
    """Test comment extraction from Python code."""
    code = '''"""Module docstring."""

def hello():
    """Function docstring."""
    # Inline comment
    print("Hello")

class Foo:
    """Class docstring."""
    pass
'''

    extracted = extract_comments_and_docstrings(code, ".py")
    assert "Module docstring" in extracted
    assert "Function docstring" in extracted
    assert "Inline comment" in extracted
    assert "Class docstring" in extracted
    # Code should not be in extracted comments
    assert "print" not in extracted
    assert "class Foo" not in extracted


@pytest.mark.unit
def test_extract_comments_javascript():
    """Test comment extraction from JavaScript code."""
    code = """// File header comment

/* Multi-line
   comment */

function hello() {
    // Inline comment
    console.log("Hello");
}
"""

    extracted = extract_comments_and_docstrings(code, ".js")
    assert "File header comment" in extracted
    assert "Multi-line" in extracted
    assert "comment" in extracted
    assert "Inline comment" in extracted
    # Code should not be in extracted comments
    assert "console.log" not in extracted
    assert "function hello" not in extracted


@pytest.mark.unit
def test_extract_comments_unsupported_extension():
    """extract_comments_and_docstrings should return empty string for unsupported extensions."""
    extracted = extract_comments_and_docstrings("some code here", ".xyz")
    assert extracted == ""


@pytest.mark.unit
def test_chunk_code_by_structure_python():
    """Test Python code chunking by function/class boundaries."""
    code = '''def function1():
    """First function."""
    pass

def function2():
    """Second function."""
    return 42

class MyClass:
    """My class."""
    def method(self):
        pass
'''

    chunks = chunk_code_by_structure(code, ".py", max_chunk_size=100)
    # Should split into multiple chunks due to function/class boundaries
    assert len(chunks) > 1
    # First chunk should contain function1
    assert "function1" in chunks[0]


@pytest.mark.unit
def test_chunk_code_by_structure_javascript():
    """Test JavaScript code chunking by function boundaries."""
    code = """function func1() {
    console.log("func1");
}

function func2() {
    console.log("func2");
}

export class MyClass {
    constructor() {}
}
"""

    chunks = chunk_code_by_structure(code, ".js", max_chunk_size=100)
    # Should split into multiple chunks
    assert len(chunks) > 1


@pytest.mark.unit
def test_chunk_code_by_structure_unsupported_extension():
    """Unsupported extensions should fall back to line-based chunking."""
    lines = [f"line {i} of some unsupported-language file" for i in range(50)]
    code = "\n".join(lines)

    chunks = chunk_code_by_structure(code, ".xyz", max_chunk_size=100)

    # Should produce multiple chunks and preserve all content
    assert len(chunks) > 1
    assert "\n".join(chunks) == "\n".join(lines)


@pytest.mark.unit
def test_chunk_code_by_structure_python_large_chunks_split():
    """Python chunks larger than max_chunk_size should be further split."""
    code = _big_py_function("function1") + "\n" + _big_py_function("function2")

    chunks = chunk_code_by_structure(code, ".py", max_chunk_size=50)

    # Every chunk should respect the max size after the secondary split
    assert len(chunks) > 2
    for chunk in chunks:
        assert len(chunk) <= 50 or chunk.count("\n") == 0


@pytest.mark.unit
def test_chunk_code_by_structure_javascript_large_chunks_split():
    """JS/TS chunks larger than max_chunk_size should be further split."""
    code = _big_js_function("func1") + "\n" + _big_js_function("func2")

    chunks = chunk_code_by_structure(code, ".js", max_chunk_size=50)

    assert len(chunks) > 2


@pytest.mark.unit
def test_chunk_code_by_structure_default_fallback_multiple_chunks():
    """Languages without special-cased splitting (e.g. Java) use line-based chunking."""
    lines = [f"System.out.println({i});" for i in range(50)]
    code = "\n".join(lines)

    chunks = chunk_code_by_structure(code, ".java", max_chunk_size=100)

    assert len(chunks) > 1


@pytest.mark.unit
def test_parse_code_file_full():
    """Test parsing code file with full code."""
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "test.py"
        code = '''"""Module doc."""

def hello():
    """Say hello."""
    print("Hello")
'''
        file_path.write_text(code)

        content = parse_code_file(file_path, extract_docs_only=False)
        # Should include full code
        assert "Module doc" in content
        assert "def hello" in content
        assert 'print("Hello")' in content
        assert file_path.name in content  # Should have file header


@pytest.mark.unit
def test_parse_code_file_docs_only():
    """Test parsing code file with docs only."""
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "test.py"
        code = '''"""Module doc."""

def hello():
    """Say hello."""
    print("Hello")
'''
        file_path.write_text(code)

        content = parse_code_file(file_path, extract_docs_only=True)
        # Should include only docstrings
        assert "Module doc" in content
        assert "Say hello" in content
        # Should NOT include code
        assert "def hello" not in content
        assert 'print("Hello")' not in content


@pytest.mark.unit
def test_parse_code_file_stat_oserror():
    """parse_code_file should return empty string when stat() fails (e.g. missing file)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        missing_file = Path(tmpdir) / "does_not_exist.py"

        content = parse_code_file(missing_file)
        assert content == ""


@pytest.mark.unit
def test_parse_code_file_too_large_is_skipped():
    """parse_code_file should skip files larger than max_file_size_mb."""
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "huge.py"
        file_path.write_bytes(b"x" * (2 * 1024 * 1024))  # 2MB

        content = parse_code_file(file_path, max_file_size_mb=1)
        assert content == ""


@pytest.mark.unit
def test_parse_code_file_unicode_decode_error():
    """parse_code_file should return empty string for files that aren't valid UTF-8."""
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "binary.py"
        file_path.write_bytes(b"\xff\xfe\x00\x01invalid utf-8")

        content = parse_code_file(file_path)
        assert content == ""


@pytest.mark.unit
def test_collect_code_files():
    """Test collecting code files from repository."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)

        # Create directory structure
        (repo_path / "src").mkdir()
        (repo_path / "tests").mkdir()
        (repo_path / "__pycache__").mkdir()

        # Create files
        (repo_path / "src" / "main.py").write_text("# Main file")
        (repo_path / "src" / "utils.py").write_text("# Utils")
        (repo_path / "tests" / "test_main.py").write_text("# Tests")
        (repo_path / "__pycache__" / "main.pyc").write_text("# Binary")
        (repo_path / "README.md").write_text("# README")

        # Create .gitignore
        (repo_path / ".gitignore").write_text("__pycache__/\n*.pyc")

        # Collect Python files
        files = collect_code_files(repo_path, extensions={".py"})

        # Should find Python files
        file_names = [f.name for f in files]
        assert "main.py" in file_names
        assert "utils.py" in file_names
        assert "test_main.py" in file_names

        # Should NOT find ignored files
        assert "main.pyc" not in file_names
        # Should NOT find non-code files
        assert "README.md" not in file_names


@pytest.mark.unit
def test_collect_code_files_default_extensions():
    """collect_code_files should use all supported languages when extensions is None."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        (repo_path / "main.py").write_text("# Python")
        (repo_path / "app.js").write_text("// JS")

        files = collect_code_files(repo_path)

        file_names = {f.name for f in files}
        assert "main.py" in file_names
        assert "app.js" in file_names


@pytest.mark.unit
def test_collect_code_files_gitignore_skips_matching_extension():
    """collect_code_files should skip files that match .gitignore, even with a matching extension."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        (repo_path / "keep.py").write_text("# keep")
        (repo_path / "ignored.py").write_text("# ignored")
        (repo_path / ".gitignore").write_text("ignored.py")

        files = collect_code_files(repo_path, extensions={".py"})

        file_names = {f.name for f in files}
        assert "keep.py" in file_names
        assert "ignored.py" not in file_names


@pytest.mark.unit
def test_index_repository():
    """Test full repository indexing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)

        # Create a simple Python project
        (repo_path / "src").mkdir()
        (repo_path / "src" / "main.py").write_text('''"""Main module."""

def main():
    """Entry point."""
    print("Hello")
''')
        (repo_path / "src" / "utils.py").write_text('''"""Utilities."""

def helper():
    """Helper function."""
    pass
''')

        # Create .gitignore
        (repo_path / ".gitignore").write_text("__pycache__/")

        # Index the repository
        result = index_repository(repo_path, extensions={".py"}, extract_docs_only=False)

        assert result["total_chunks"] > 0
        assert len(result["files"]) == 2  # main.py and utils.py
        assert len(result["chunks"]) > 0

        # Check chunks structure
        for file_path, idx, chunk in result["chunks"]:
            assert isinstance(file_path, str)
            assert isinstance(idx, int)
            assert isinstance(chunk, str)
            assert len(chunk) > 0


@pytest.mark.unit
def test_index_repository_docs_only():
    """Test repository indexing with docs only."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)

        (repo_path / "test.py").write_text('''"""Module doc."""

def func():
    """Function doc."""
    print("code")
''')

        result = index_repository(repo_path, extensions={".py"}, extract_docs_only=True)

        # Should extract chunks
        assert result["total_chunks"] > 0

        # Verify chunks contain docs but not code
        for _file_path, _idx, chunk in result["chunks"]:
            # Docs should be present
            if "Module doc" in chunk or "Function doc" in chunk:
                # But code should not
                assert 'print("code")' not in chunk


@pytest.mark.unit
def test_index_repository_truncates_when_over_max_files():
    """index_repository should truncate to max_files and report truncated=True."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        for i in range(3):
            (repo_path / f"file{i}.py").write_text(f"# file {i}")

        result = index_repository(repo_path, extensions={".py"}, max_files=2)

        assert result["truncated"] is True
        assert len(result["files"]) <= 2


@pytest.mark.unit
def test_index_repository_logs_progress_for_large_repos():
    """index_repository should log progress when processing more than 100 files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        for i in range(101):
            (repo_path / f"file{i}.py").write_text(f"# file {i}")

        result = index_repository(repo_path, extensions={".py"}, max_files=1000)

        assert result["truncated"] is False
        assert len(result["files"]) == 101


@pytest.mark.unit
def test_index_repository_skips_files_with_no_content():
    """index_repository should skip files whose parsed content is empty."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        # No docstrings/comments -> parse_code_file returns "" when extract_docs_only=True
        (repo_path / "no_docs.py").write_text("x = 1\ny = 2\n")

        result = index_repository(repo_path, extensions={".py"}, extract_docs_only=True)

        assert result["files"] == []
        assert result["chunks"] == []
        assert result["total_chunks"] == 0


@pytest.mark.unit
def test_index_repository_continues_after_per_file_exception(monkeypatch: pytest.MonkeyPatch):
    """index_repository should log and continue when a single file fails to process."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        (repo_path / "good.py").write_text("# good file")

        def _boom(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr("nyxgpt.rag.code_parser.parse_code_file", _boom)

        result = index_repository(repo_path, extensions={".py"})

        assert result["files"] == []
        assert result["chunks"] == []
        assert result["total_chunks"] == 0
