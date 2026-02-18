import os
import subprocess
from pathlib import Path

from src.config import get_settings

# --- Anthropic tool schemas ---

TOOL_SCHEMAS = [
    {
        "name": "list_directory",
        "description": (
            "List the contents of a directory in the repository. "
            "Returns file and directory names with type indicators (file/dir)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path within the repo. Use '.' or '' for the root.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read the contents of a file in the repository. "
            "Optionally specify a line range to read a portion of the file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to the file within the repo.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "1-based start line (inclusive). Omit to start from the beginning.",
                },
                "end_line": {
                    "type": "integer",
                    "description": "1-based end line (inclusive). Omit to read to the end.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_code",
        "description": (
            "Search for a pattern in the repository using grep. "
            "Returns matching lines with file paths and line numbers."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Search pattern (regex supported).",
                },
                "path": {
                    "type": "string",
                    "description": "Relative directory to scope the search. Defaults to repo root.",
                },
                "file_glob": {
                    "type": "string",
                    "description": "Glob to filter files, e.g. '*.py' or '*.ts'.",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "file_tree",
        "description": (
            "Get a recursive directory tree of the repository. "
            "Useful for understanding the overall project structure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to start from. Defaults to repo root.",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Maximum depth to recurse. Defaults to 3.",
                },
            },
        },
    },
]


def _resolve(path: str) -> Path:
    """Resolve a relative path against the clone dir, preventing traversal."""
    base = Path(get_settings().clone_dir).resolve()
    target = (base / path).resolve()
    if not str(target).startswith(str(base)):
        raise ValueError("Path traversal detected")
    return target


def list_directory(path: str) -> str:
    target = _resolve(path)
    if not target.is_dir():
        return f"Error: '{path}' is not a directory"
    entries = sorted(target.iterdir())
    lines = []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        kind = "dir" if entry.is_dir() else "file"
        lines.append(f"[{kind}] {entry.name}")
    return "\n".join(lines) if lines else "(empty directory)"


def read_file(path: str, start_line: int | None = None, end_line: int | None = None) -> str:
    target = _resolve(path)
    if not target.is_file():
        return f"Error: '{path}' is not a file"
    try:
        text = target.read_text(errors="replace")
    except Exception as e:
        return f"Error reading file: {e}"

    lines = text.splitlines()
    start = (start_line - 1) if start_line and start_line >= 1 else 0
    end = end_line if end_line else len(lines)
    selected = lines[start:end]

    numbered = [f"{start + i + 1:>5} | {line}" for i, line in enumerate(selected)]
    return "\n".join(numbered)


def search_code(pattern: str, path: str | None = None, file_glob: str | None = None) -> str:
    base = Path(get_settings().clone_dir).resolve()
    search_dir = _resolve(path) if path else base

    cmd = [
        "grep", "-rn",
        "--include", file_glob or "*",
        "--exclude-dir=.git",
        "--exclude-dir=node_modules",
        "--exclude-dir=__pycache__",
        "--exclude-dir=.venv",
        "--exclude-dir=venv",
        "-e", pattern, str(search_dir),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return "Error: search timed out"

    output = result.stdout
    # Make paths relative to the repo root
    output = output.replace(str(base) + os.sep, "")
    lines = output.strip().splitlines()
    if len(lines) > 100:
        lines = lines[:100]
        lines.append(f"... ({len(lines)} results, truncated)")
    return "\n".join(lines) if lines else "No matches found"


def file_tree(path: str | None = None, max_depth: int | None = None) -> str:
    base = _resolve(path or ".")
    depth = max_depth or 3
    lines: list[str] = []

    def _walk(dir_path: Path, prefix: str, current_depth: int):
        if current_depth > depth:
            return
        entries = sorted(dir_path.iterdir())
        entries = [e for e in entries if not e.name.startswith(".")]
        for i, entry in enumerate(entries):
            is_last = i == len(entries) - 1
            connector = "└── " if is_last else "├── "
            if entry.is_dir():
                lines.append(f"{prefix}{connector}{entry.name}/")
                extension = "    " if is_last else "│   "
                _walk(entry, prefix + extension, current_depth + 1)
            else:
                lines.append(f"{prefix}{connector}{entry.name}")

    lines.append(f"{base.name}/")
    _walk(base, "", 1)
    return "\n".join(lines)


TOOL_HANDLERS = {
    "list_directory": lambda args: list_directory(args["path"]),
    "read_file": lambda args: read_file(
        args["path"], args.get("start_line"), args.get("end_line")
    ),
    "search_code": lambda args: search_code(
        args["pattern"], args.get("path"), args.get("file_glob")
    ),
    "file_tree": lambda args: file_tree(args.get("path"), args.get("max_depth")),
}
