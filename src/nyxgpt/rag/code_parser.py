"""Code repository parsing and indexing for RAG.

This module provides functionality to:
- Parse source code files
- Extract docstrings and comments
- Respect .gitignore rules
- Perform language-aware chunking
"""

import re
from pathlib import Path
from typing import Any

# Language-specific comment and docstring patterns
LANGUAGE_PATTERNS = {
    # Python
    ".py": {
        "single_line": r"#.*$",
        "docstring": r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'',
        "supports_classes": True,
        "supports_functions": True,
    },
    # JavaScript/TypeScript
    ".js": {
        "single_line": r"//.*$",
        "multi_line": r"/\*[\s\S]*?\*/",
        "supports_classes": True,
        "supports_functions": True,
    },
    ".jsx": {
        "single_line": r"//.*$",
        "multi_line": r"/\*[\s\S]*?\*/",
        "supports_classes": True,
        "supports_functions": True,
    },
    ".ts": {
        "single_line": r"//.*$",
        "multi_line": r"/\*[\s\S]*?\*/",
        "supports_classes": True,
        "supports_functions": True,
    },
    ".tsx": {
        "single_line": r"//.*$",
        "multi_line": r"/\*[\s\S]*?\*/",
        "supports_classes": True,
        "supports_functions": True,
    },
    # Java/C/C++/C#
    ".java": {
        "single_line": r"//.*$",
        "multi_line": r"/\*[\s\S]*?\*/",
        "supports_classes": True,
        "supports_functions": True,
    },
    ".c": {
        "single_line": r"//.*$",
        "multi_line": r"/\*[\s\S]*?\*/",
        "supports_classes": False,
        "supports_functions": True,
    },
    ".cpp": {
        "single_line": r"//.*$",
        "multi_line": r"/\*[\s\S]*?\*/",
        "supports_classes": True,
        "supports_functions": True,
    },
    ".h": {
        "single_line": r"//.*$",
        "multi_line": r"/\*[\s\S]*?\*/",
        "supports_classes": False,
        "supports_functions": True,
    },
    ".cs": {
        "single_line": r"//.*$",
        "multi_line": r"/\*[\s\S]*?\*/",
        "supports_classes": True,
        "supports_functions": True,
    },
    # Go
    ".go": {
        "single_line": r"//.*$",
        "multi_line": r"/\*[\s\S]*?\*/",
        "supports_classes": False,
        "supports_functions": True,
    },
    # Ruby
    ".rb": {
        "single_line": r"#.*$",
        "multi_line": r"=begin[\s\S]*?=end",
        "supports_classes": True,
        "supports_functions": True,
    },
    # Rust
    ".rs": {
        "single_line": r"//.*$",
        "multi_line": r"/\*[\s\S]*?\*/",
        "supports_classes": False,
        "supports_functions": True,
    },
    # Shell
    ".sh": {
        "single_line": r"#.*$",
        "supports_classes": False,
        "supports_functions": True,
    },
    # PHP
    ".php": {
        "single_line": r"//.*$|#.*$",
        "multi_line": r"/\*[\s\S]*?\*/",
        "supports_classes": True,
        "supports_functions": True,
    },
}


def parse_gitignore(repo_path: Path) -> list[str]:
    """Parse .gitignore file and return list of patterns."""
    gitignore_path = repo_path / ".gitignore"
    if not gitignore_path.exists():
        return []

    patterns = []
    with open(gitignore_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            # Skip empty lines and comments
            if not line or line.startswith("#"):
                continue
            patterns.append(line)
    return patterns


def should_ignore(file_path: Path, repo_path: Path, ignore_patterns: list[str]) -> bool:
    """Check if a file should be ignored based on .gitignore patterns.

    Args:
        file_path: Absolute path to the file
        repo_path: Absolute path to the repository root
        ignore_patterns: List of gitignore patterns

    Returns:
        True if file should be ignored, False otherwise
    """
    try:
        # Get relative path from repo root
        rel_path = file_path.relative_to(repo_path)
        rel_path_str = str(rel_path)

        # Always ignore .git directory
        if ".git" in rel_path.parts:
            return True

        # Check each pattern
        for pattern in ignore_patterns:
            # Directory pattern (ends with /)
            if pattern.endswith("/"):
                dir_pattern = pattern.rstrip("/")
                if any(part == dir_pattern for part in rel_path.parts):
                    return True
            # Wildcard pattern
            elif "*" in pattern:
                # Simple wildcard matching (basic implementation)
                regex_pattern = pattern.replace(".", r"\.").replace("*", ".*")
                if re.match(regex_pattern, rel_path_str):
                    return True
            # Exact match
            elif rel_path_str == pattern or rel_path.name == pattern:
                return True
            # Directory basename match
            elif pattern in rel_path.parts:
                return True

        return False
    except ValueError:
        # file_path is not relative to repo_path
        return False


def extract_comments_and_docstrings(code: str, file_ext: str) -> str:
    """Extract comments and docstrings from source code.

    Args:
        code: Source code as string
        file_ext: File extension (e.g., '.py', '.js')

    Returns:
        Extracted comments and docstrings
    """
    if file_ext not in LANGUAGE_PATTERNS:
        return ""

    patterns = LANGUAGE_PATTERNS[file_ext]
    extracted: list[str] = []

    # Extract single-line comments
    if "single_line" in patterns:
        single_line_pattern = patterns.get("single_line", "")
        if isinstance(single_line_pattern, str):
            single_line_regex = re.compile(single_line_pattern, re.MULTILINE)
            extracted.extend(single_line_regex.findall(code))

    # Extract multi-line comments
    if "multi_line" in patterns:
        multi_line_pattern = patterns.get("multi_line", "")
        if isinstance(multi_line_pattern, str):
            multi_line_regex = re.compile(multi_line_pattern, re.MULTILINE)
            extracted.extend(multi_line_regex.findall(code))

    # Extract docstrings (Python-specific)
    if "docstring" in patterns:
        docstring_pattern = patterns.get("docstring", "")
        if isinstance(docstring_pattern, str):
            docstring_regex = re.compile(docstring_pattern, re.MULTILINE)
            extracted.extend(docstring_regex.findall(code))

    # Clean up and join
    cleaned: list[str] = []
    for item in extracted:
        # Remove comment markers
        cleaned_item = item
        cleaned_item = re.sub(r"^#+\s*", "", cleaned_item)  # Remove leading #
        cleaned_item = re.sub(r"^//+\s*", "", cleaned_item)  # Remove leading //
        cleaned_item = re.sub(r"^/\*+\s*|\s*\*+/$", "", cleaned_item)  # Remove /* */
        cleaned_item = re.sub(
            r'^"""|\s*"""$|^\'\'\'\s*|\s*\'\'\'$', "", cleaned_item
        )  # Remove """ or '''
        cleaned_item = cleaned_item.strip()

        if cleaned_item:
            cleaned.append(cleaned_item)

    return "\n\n".join(cleaned)


def chunk_code_by_structure(
    code: str, file_ext: str, max_chunk_size: int = 1000
) -> list[str]:
    """Chunk code by language-aware structural boundaries.

    Args:
        code: Source code as string
        file_ext: File extension (e.g., '.py', '.js')
        max_chunk_size: Maximum characters per chunk

    Returns:
        List of code chunks
    """
    if file_ext not in LANGUAGE_PATTERNS:
        # Fall back to line-based chunking for unsupported languages
        lines = code.split("\n")
        chunks: list[str] = []
        current_chunk: list[str] = []
        current_size = 0

        for line in lines:
            line_size = len(line) + 1  # +1 for newline
            if current_size + line_size > max_chunk_size and current_chunk:
                chunks.append("\n".join(current_chunk))
                current_chunk = [line]
                current_size = line_size
            else:
                current_chunk.append(line)
                current_size += line_size

        if current_chunk:
            chunks.append("\n".join(current_chunk))

        return chunks

    # Language-aware chunking
    chunks = []
    patterns = LANGUAGE_PATTERNS[file_ext]

    # For Python, try to split by function/class definitions
    if file_ext == ".py" and patterns.get("supports_functions"):
        # Match function and class definitions
        func_class_pattern = re.compile(r"^(def |class |async def )", re.MULTILINE)
        matches = list(func_class_pattern.finditer(code))

        if matches:
            # Split code at function/class boundaries
            last_end = 0
            for i, match in enumerate(matches):
                if i > 0:
                    chunk = code[last_end : match.start()].strip()
                    if chunk:
                        # Further split if chunk exceeds max size
                        if len(chunk) > max_chunk_size:
                            chunks.extend(_split_large_chunk(chunk, max_chunk_size))
                        else:
                            chunks.append(chunk)
                    last_end = match.start()

            # Add last chunk
            final_chunk = code[last_end:].strip()
            if final_chunk:
                if len(final_chunk) > max_chunk_size:
                    chunks.extend(_split_large_chunk(final_chunk, max_chunk_size))
                else:
                    chunks.append(final_chunk)

            return chunks if chunks else [code]

    # For JavaScript/TypeScript, try to split by function/class definitions
    if file_ext in {".js", ".jsx", ".ts", ".tsx"} and patterns.get("supports_functions"):
        func_class_pattern = re.compile(
            r"^(function |class |const \w+ = \(|export function |export class |export const \w+ = )",
            re.MULTILINE,
        )
        matches = list(func_class_pattern.finditer(code))

        if matches:
            last_end = 0
            for i, match in enumerate(matches):
                if i > 0:
                    chunk = code[last_end : match.start()].strip()
                    if chunk:
                        if len(chunk) > max_chunk_size:
                            chunks.extend(_split_large_chunk(chunk, max_chunk_size))
                        else:
                            chunks.append(chunk)
                    last_end = match.start()

            final_chunk = code[last_end:].strip()
            if final_chunk:
                if len(final_chunk) > max_chunk_size:
                    chunks.extend(_split_large_chunk(final_chunk, max_chunk_size))
                else:
                    chunks.append(final_chunk)

            return chunks if chunks else [code]

    # Default: fall back to line-based chunking
    lines = code.split("\n")
    current_chunk = []
    current_size = 0

    for line in lines:
        line_size = len(line) + 1
        if current_size + line_size > max_chunk_size and current_chunk:
            chunks.append("\n".join(current_chunk))
            current_chunk = [line]
            current_size = line_size
        else:
            current_chunk.append(line)
            current_size += line_size

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    return chunks


def _split_large_chunk(chunk: str, max_size: int) -> list[str]:
    """Split a large chunk into smaller pieces by lines."""
    lines = chunk.split("\n")
    chunks: list[str] = []
    current_chunk: list[str] = []
    current_size = 0

    for line in lines:
        line_size = len(line) + 1
        if current_size + line_size > max_size and current_chunk:
            chunks.append("\n".join(current_chunk))
            current_chunk = [line]
            current_size = line_size
        else:
            current_chunk.append(line)
            current_size += line_size

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    return chunks


def parse_code_file(file_path: Path, extract_docs_only: bool = False) -> str:
    """Parse a single code file and extract relevant content.

    Args:
        file_path: Path to the code file
        extract_docs_only: If True, extract only comments/docstrings. If False, include full code.

    Returns:
        Parsed content as string
    """
    file_ext = file_path.suffix.lower()

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            code = f.read()
    except (UnicodeDecodeError, PermissionError):
        # Skip binary files or files we can't read
        return ""

    if extract_docs_only:
        # Extract only comments and docstrings
        docs = extract_comments_and_docstrings(code, file_ext)
        return f"[File: {file_path.name}]\n{docs}" if docs else ""
    else:
        # Include full code with file header
        return f"[File: {file_path.name}]\n{code}"


def collect_code_files(
    repo_path: Path, extensions: set[str] | None = None
) -> list[Path]:
    """Collect all code files in a repository, respecting .gitignore.

    Args:
        repo_path: Path to the repository root
        extensions: Set of file extensions to include (e.g., {'.py', '.js'}). If None, use all supported languages.

    Returns:
        List of Path objects for code files
    """
    if extensions is None:
        extensions = set(LANGUAGE_PATTERNS.keys())

    # Parse .gitignore
    ignore_patterns = parse_gitignore(repo_path)

    # Collect files
    code_files = []
    for file_path in repo_path.rglob("*"):
        if not file_path.is_file():
            continue

        file_ext = file_path.suffix.lower()
        if file_ext not in extensions:
            continue

        if should_ignore(file_path, repo_path, ignore_patterns):
            continue

        code_files.append(file_path)

    return code_files


def index_repository(
    repo_path: Path,
    extensions: set[str] | None = None,
    extract_docs_only: bool = False,
    max_chunk_size: int = 1000,
    max_files: int = 10000,
) -> dict[str, Any]:
    """Index a code repository for RAG ingestion.

    Args:
        repo_path: Path to the repository root
        extensions: Set of file extensions to include. If None, use all supported languages.
        extract_docs_only: If True, extract only comments/docstrings. If False, include full code.
        max_chunk_size: Maximum characters per chunk for language-aware chunking
        max_files: Maximum number of files to index (default: 10000)

    Returns:
        Dictionary with:
            - 'files': list of indexed file paths
            - 'chunks': list of (file_path, chunk_index, chunk_content) tuples
            - 'total_chunks': total number of chunks
            - 'truncated': boolean indicating if file limit was reached
    """
    import logging

    log = logging.getLogger(__name__)
    code_files = collect_code_files(repo_path, extensions)

    # Check if repository is too large
    file_count = len(code_files)
    truncated = False
    if file_count > max_files:
        log.warning(
            f"Repository has {file_count} files, limiting to {max_files}. "
            f"Consider using --extensions to filter specific file types."
        )
        code_files = code_files[:max_files]
        truncated = True

    all_chunks = []
    indexed_files = []

    # Process files with progress logging for large repositories
    for i, file_path in enumerate(code_files):
        # Log progress for large repositories
        if file_count > 100 and (i + 1) % 100 == 0:
            log.info(f"Indexing progress: {i + 1}/{len(code_files)} files processed")

        try:
            content = parse_code_file(file_path, extract_docs_only)
            if not content:
                continue

            file_ext = file_path.suffix.lower()
            chunks = chunk_code_by_structure(content, file_ext, max_chunk_size)

            for idx, chunk in enumerate(chunks):
                all_chunks.append((str(file_path), idx, chunk))

            indexed_files.append(str(file_path))
        except Exception as e:
            # Log error but continue processing other files
            log.warning(f"Failed to process {file_path}: {e}")
            continue

    return {
        "files": indexed_files,
        "chunks": all_chunks,
        "total_chunks": len(all_chunks),
        "truncated": truncated,
    }
