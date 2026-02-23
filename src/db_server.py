"""Standalone MCP server providing read-only database access via SQLAlchemy.

Launched as a subprocess by MCPManager. Receives config via environment
variables: DATABASE_URL, DB_MAX_ROWS, DB_QUERY_TIMEOUT.
"""

from __future__ import annotations

import json
import re
import sys

# ---------------------------------------------------------------------------
# Read-only SQL validation (pure Python — no external deps)
# ---------------------------------------------------------------------------

_ALLOWED_PREFIXES = ("SELECT", "WITH", "EXPLAIN", "SHOW", "DESCRIBE", "PRAGMA")

_FORBIDDEN_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE",
    "REPLACE", "MERGE", "UPSERT", "GRANT", "REVOKE", "CALL", "EXEC",
    "EXECUTE", "RENAME", "LOCK", "UNLOCK", "SET",
    "INTO", "OUTFILE", "DUMPFILE",
}

# Handles SQL-standard doubled-quote escaping: 'it''s ok'
_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")
# SQL comments: -- line comments and /* block comments */
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _validate_readonly(sql: str) -> str | None:
    """Return an error message if the SQL is not read-only, else None."""
    stripped = sql.strip()
    if not stripped:
        return "Empty query"

    # Strip comments first (before any other checks)
    sanitised = _BLOCK_COMMENT_RE.sub(" ", stripped)
    sanitised = _LINE_COMMENT_RE.sub(" ", sanitised)
    sanitised = sanitised.strip()

    if not sanitised:
        return "Empty query (comments only)"

    # Check allowed prefixes
    first_word = sanitised.split()[0].upper()
    if first_word not in _ALLOWED_PREFIXES:
        return f"Only SELECT/WITH/EXPLAIN queries are allowed (got {first_word})"

    # Strip string literals before semicolon + keyword checks
    sanitised = _STRING_LITERAL_RE.sub("''", sanitised)

    # Reject multi-statement queries (semicolons before the end)
    body = sanitised.rstrip(";")
    if ";" in body:
        return "Multi-statement queries are not allowed"

    # Check for forbidden keywords
    tokens = set(re.findall(r"\b[A-Z_]+\b", body.upper()))
    found = tokens & _FORBIDDEN_KEYWORDS
    if found:
        return f"Forbidden keyword(s): {', '.join(sorted(found))}"

    return None


# ---------------------------------------------------------------------------
# URL normalisation (maps common URLs to SQLAlchemy dialect+driver format)
# ---------------------------------------------------------------------------

_URL_REWRITES = [
    ("postgres://", "postgresql+psycopg2://"),
    ("postgresql://", "postgresql+psycopg2://"),
    ("mysql://", "mysql+pymysql://"),
]

# Query param rewrites: provider-specific → SQLAlchemy/driver equivalent
_PARAM_REWRITES = [
    ("sslaccept=strict", "ssl_mode=REQUIRED"),
]


def _normalize_url(url: str) -> str:
    """Rewrite common DATABASE_URL formats to SQLAlchemy dialect+driver format."""
    for prefix, replacement in _URL_REWRITES:
        if url.startswith(prefix):
            url = replacement + url[len(prefix):]
            break
    for old, new in _PARAM_REWRITES:
        url = url.replace(old, new)
    return url


# ---------------------------------------------------------------------------
# Server setup (only runs when executed as a script)
# ---------------------------------------------------------------------------

def _serialise(value):
    """Make a DB value JSON-serialisable."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _main() -> None:
    import os

    from mcp.server.fastmcp import FastMCP
    from sqlalchemy import create_engine, inspect, text

    DATABASE_URL = _normalize_url(os.environ["DATABASE_URL"])
    DB_MAX_ROWS = int(os.environ.get("DB_MAX_ROWS", "100"))
    DB_QUERY_TIMEOUT = int(os.environ.get("DB_QUERY_TIMEOUT", "30"))

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    dialect_name = engine.dialect.name
    mcp = FastMCP("database")

    # -------------------------------------------------------------------
    # Read-only transaction + dialect-specific timeout
    # -------------------------------------------------------------------

    def _readonly_connect():
        """Open a connection with read-only transaction and timeout."""
        conn = engine.connect()
        if dialect_name == "postgresql":
            conn.execute(text(f"SET statement_timeout = {DB_QUERY_TIMEOUT * 1000}"))
            conn.execute(text("SET TRANSACTION READ ONLY"))
        elif dialect_name == "mysql":
            conn.execute(text(f"SET max_execution_time = {DB_QUERY_TIMEOUT * 1000}"))
            conn.execute(text("SET TRANSACTION READ ONLY"))
        # SQLite: inherently read-only for SELECT (no SET TRANSACTION support)
        return conn

    # -------------------------------------------------------------------
    # Tools
    # -------------------------------------------------------------------

    @mcp.tool()
    def list_tables() -> str:
        """List all tables in the database with approximate row counts."""
        insp = inspect(engine)
        tables = []
        with engine.connect() as conn:
            for name in insp.get_table_names():
                try:
                    quoted = engine.dialect.identifier_preparer.quote(name)
                    row = conn.execute(text(f"SELECT COUNT(*) FROM {quoted}")).scalar()
                    tables.append({"name": name, "row_count": row})
                except Exception:
                    tables.append({"name": name, "row_count": None})
        return json.dumps(tables, indent=2)

    @mcp.tool()
    def describe_table(table_name: str) -> str:
        """Describe a table's columns, types, primary keys, foreign keys, and indexes."""
        insp = inspect(engine)
        available = insp.get_table_names()
        if table_name not in available:
            return json.dumps({"error": f"Table '{table_name}' not found. Available: {available}"})

        columns = []
        for col in insp.get_columns(table_name):
            columns.append({
                "name": col["name"],
                "type": str(col["type"]),
                "nullable": col.get("nullable", True),
                "default": str(col["default"]) if col.get("default") is not None else None,
            })

        pk = insp.get_pk_constraint(table_name)
        fks = insp.get_foreign_keys(table_name)
        indexes = insp.get_indexes(table_name)

        return json.dumps({
            "table": table_name,
            "columns": columns,
            "primary_key": pk.get("constrained_columns", []) if pk else [],
            "foreign_keys": [
                {
                    "columns": fk["constrained_columns"],
                    "referred_table": fk["referred_table"],
                    "referred_columns": fk["referred_columns"],
                }
                for fk in fks
            ],
            "indexes": [
                {"name": idx["name"], "columns": idx["column_names"], "unique": idx["unique"]}
                for idx in indexes
            ],
        }, indent=2)

    @mcp.tool()
    def run_query(sql: str) -> str:
        """Execute a read-only SQL query and return the results as JSON.

        Only SELECT, WITH, and EXPLAIN queries are allowed. Results are capped
        at the configured row limit.
        """
        error = _validate_readonly(sql)
        if error:
            return json.dumps({"error": error})

        with _readonly_connect() as conn:
            result = conn.execute(text(sql))
            columns = list(result.keys())
            rows = []
            truncated = False
            for i, row in enumerate(result):
                if i >= DB_MAX_ROWS:
                    truncated = True
                    break
                rows.append([_serialise(v) for v in row])

        return json.dumps({
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
        }, indent=2)

    # -------------------------------------------------------------------
    # Validate connectivity then start
    # -------------------------------------------------------------------

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:
        print(f"Database connection failed: {e}", file=sys.stderr)
        sys.exit(1)

    mcp.run()


if __name__ == "__main__":
    _main()
