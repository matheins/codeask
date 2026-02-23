<div align="center">

<h1>🔍 CodeAsk</h1>

<p><em>Tired of explaining functionality to your non-techie colleagues?<br>
You produce more code than ever but your docs are always outdated?</em></p>

<p><strong>Let anyone ask how your product works — and get answers straight from the source code.</strong></p>

<p>
CodeAsk connects Claude to your GitHub repo via <a href="https://github.com/oraios/serena">Serena</a> code intelligence,<br>
so it actually reads and understands your code before answering.
</p>

<p>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.12+-blue.svg" alt="Python 3.12+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License: MIT"></a>
  <a href="https://github.com/matheins/codeask/stargazers"><img src="https://img.shields.io/github/stars/matheins/codeask?style=social" alt="GitHub Stars"></a>
  <a href="#contributing"><img src="https://img.shields.io/badge/PRs-welcome-brightgreen.svg" alt="PRs Welcome"></a>
</p>

<p>
  <a href="https://railway.com/new/template?template=https://github.com/matheins/codeask"><img src="https://railway.com/button.svg" alt="Deploy on Railway" height="32"></a>
  &nbsp;
  <a href="https://render.com/deploy?repo=https://github.com/matheins/codeask"><img src="https://render.com/images/deploy-to-render-button.svg" alt="Deploy to Render" height="32"></a>
</p>

</div>

---

## Why CodeAsk?

- **Instant answers** — ask plain-English questions, get responses grounded in your actual source code
- **Slack-native** — mention the bot in any channel and get answers where your team already works
- **Always up-to-date** — auto-syncs the repo on a configurable interval so answers reflect the latest code
- **Database-aware** — optionally connect a read-only database so the agent can combine code analysis with real data queries
- **Extensible** — plug in additional [MCP](https://modelcontextprotocol.io/) tool servers alongside the built-in Serena code intelligence

## How It Works

```
         ┌──────────┐       ┌──────────┐
         │  Slack @  │       │ HTTP API │
         └────┬─────┘       └────┬─────┘
              │                   │
              └─────────┬─────────┘
                        ▼
               ┌─────────────────┐
               │   Claude Agent  │
               │  (agentic loop) │
               └────────┬────────┘
                        │  tool calls
                ┌───────┴───────┐
                ▼               ▼
       ┌─────────────────┐  ┌──────────────┐
       │  Serena (MCP)   │  │ Database MCP │
       │ code intelligence│  │ (optional)   │
       └────────┬────────┘  └──────┬───────┘
                │  reads code      │  queries
                ▼                  ▼
       ┌─────────────────┐  ┌──────────────┐
       │   Your Repo     │  │  Your DB     │
       │  (auto-synced)  │  │ (read-only)  │
       └─────────────────┘  └──────────────┘
```

## Quick Start

```bash
git clone https://github.com/matheins/codeask.git
cd codeask
pip install .
cp .env.example .env   # ← fill in your keys
uvicorn src.main:app --port 8000
```

<details>
<summary><strong>Or use Docker</strong></summary>

```bash
docker build -t codeask .
docker run --env-file .env -p 8000:8000 codeask
```

</details>

## Configuration

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key |
| `GITHUB_REPO_URL` | Yes | Git URL of the repo to explore |
| `API_KEY` | Yes | Secret key for HTTP endpoint auth (sent via `X-API-Key` header) |
| `GITHUB_TOKEN` | No | GitHub PAT for private repos |
| `REPO_BRANCH` | No | Branch to track (default: `main`) |
| `CLONE_DIR` | No | Local path to clone into (default: `./repo`) |
| `SLACK_BOT_TOKEN` | No | Slack bot token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | No | Slack app token (`xapp-...`) for Socket Mode |
| `SYNC_INTERVAL` | No | Seconds between repo syncs (default: `300`) |
| `MCP_SERVERS_CONFIG` | No | Path to JSON config for additional MCP tool servers (Serena is built-in) |
| `MAX_ITERATIONS` | No | Max agent tool-call rounds (default: `20`) |
| `ENABLE_THINKING` | No | Enable extended thinking for deeper reasoning (default: `true`) |
| `THINKING_BUDGET` | No | Token budget for thinking when enabled (default: `10000`) |
| `DATABASE_URL` | No | SQLAlchemy connection URL for read-only DB access (see [Database Integration](#database-integration)) |
| `DB_MAX_ROWS` | No | Max rows returned per query (default: `100`) |
| `DB_QUERY_TIMEOUT` | No | Query timeout in seconds (default: `30`) |

## API Reference

### Health check

```bash
curl http://localhost:8000/health
```

### Ask a question

```bash
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{"question": "How does authentication work?"}'
```

### Trigger a repo sync

```bash
curl -X POST http://localhost:8000/sync \
  -H "X-API-Key: YOUR_API_KEY"
```

## Slack Integration

### 1. Create a Slack App

- Go to [api.slack.com/apps](https://api.slack.com/apps) and click **Create New App** → **From scratch**
- Give it a name (e.g. "CodeAsk") and select your workspace

### 2. Enable Socket Mode

- In the left sidebar, go to **Socket Mode** and toggle it **on**
- You'll be prompted to create an **App-Level Token** — give it a name (e.g. "codeask-socket") and add the `connections:write` scope
- Copy the token (starts with `xapp-...`) → this is your **`SLACK_APP_TOKEN`**

### 3. Add Bot Token Scopes

- Go to **OAuth & Permissions** in the left sidebar
- Under **Bot Token Scopes**, add:
  - `app_mentions:read` — lets the bot see when it's mentioned
  - `chat:write` — lets the bot send messages
  - `channels:history` — lets the bot read thread context in public channels
  - `groups:history` — lets the bot read thread context in private channels

### 4. Install the App to Your Workspace

- Go to **OAuth & Permissions** and click **Install to Workspace**
- Authorize the app
- Copy the **Bot User OAuth Token** (starts with `xoxb-...`) → this is your **`SLACK_BOT_TOKEN`**

### 5. Subscribe to Events

- Go to **Event Subscriptions** in the left sidebar and toggle it **on**
- Under **Subscribe to bot events**, add `app_mention`

### 6. Configure Your `.env`

```bash
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-token
```

The bot will start automatically when both tokens are present. Invite it to a channel and mention it to ask a question.

## Database Integration

CodeAsk can optionally connect to a read-only database, allowing the agent to combine code analysis with real data queries. When enabled, the agent can answer questions like "how many users signed up this week?" by understanding the schema from code **and** querying the actual database.

### Setup

Set `DATABASE_URL` in your `.env` file. Standard connection URLs work — no special format needed:

```bash
# PostgreSQL
DATABASE_URL=postgres://user:password@localhost:5432/mydb

# MySQL
DATABASE_URL=mysql://user:password@localhost:3306/mydb

# SQLite
DATABASE_URL=sqlite:///path/to/database.db
```

Drivers for PostgreSQL and MySQL are bundled — no extra install steps.

### Agent Tools

When `DATABASE_URL` is set, three tools are exposed to the agent:

| Tool | Description |
|---|---|
| `list_tables` | Lists all tables with row counts |
| `describe_table` | Shows columns, types, PKs, FKs, and indexes for a table |
| `run_query` | Executes a read-only SQL query (SELECT/WITH/EXPLAIN only) |

### Security Model

Database access is **read-only** with defense in depth:

- **Read-only transactions** — connections use `SET TRANSACTION READ ONLY` (PostgreSQL/MySQL) to enforce read-only at the database level
- **SQL validation** — queries are parsed and rejected if they contain write operations (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `INTO`, etc.)
- **Multi-statement rejection** — only single SQL statements are allowed
- **Row limits** — results are capped at `DB_MAX_ROWS` (default 100)
- **Query timeouts** — dialect-specific statement timeouts prevent long-running queries
- **PII protection** — the agent is instructed to aggregate/anonymize data and never expose raw PII

> **Recommendation:** For production deployments, create a dedicated read-only database user with `SELECT`-only privileges. This provides the strongest guarantee regardless of application-level checks.

### Behavior

- The database server is **optional** — when `DATABASE_URL` is not set, no database tools are exposed
- Connection failure is **non-fatal** — the agent continues with code-only tools if the database is unavailable
- The agent automatically receives instructions for using database tools alongside code analysis when the database is connected

## Extending with MCP Servers

[Serena](https://github.com/oraios/serena) code intelligence is built-in — no configuration needed.

To give the agent access to additional tools, create a JSON config file and point `MCP_SERVERS_CONFIG` at it:

```json
{
  "mcpServers": {
    "example": {
      "command": "npx",
      "args": ["-y", "@example/mcp-server"]
    }
  }
}
```

Any server that speaks the [Model Context Protocol](https://modelcontextprotocol.io/) will work. Extra server failures are non-fatal — the agent continues with Serena and any other servers that connected successfully.

## Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

[MIT](LICENSE)
