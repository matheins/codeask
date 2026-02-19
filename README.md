# codeask

API service that answers product questions by exploring a GitHub repo's codebase with Claude. Ships with an HTTP API, a Slack bot, and optional MCP server support for extended tool use.

## Features

- **HTTP API** — ask questions about your codebase via `/ask`
- **Slack bot** — mention the bot in Slack to ask questions (Socket Mode)
- **MCP servers** — optionally connect external tool servers for richer answers
- **Auto-sync** — periodically pulls the latest code from the target repo

## Prerequisites

- Python 3.12+
- An [Anthropic API key](https://console.anthropic.com/)
- Git
- (Optional) Docker

## Setup

```bash
git clone https://github.com/matheins/codeask.git
cd codeask
python -m venv .venv
source .venv/bin/activate
pip install .
```

Copy the example env file and fill in your values:

```bash
cp .env.example .env
```

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
| `MCP_SERVERS_CONFIG` | No | Path to `mcp_servers.json` for external tool servers |
| `MAX_ITERATIONS` | No | Max agent tool-call rounds (default: `15`) |

## Running

### Locally

```bash
uvicorn src.main:app --host 0.0.0.0 --port 8000
```

### Docker

```bash
docker build -t codeask .
docker run --env-file .env -p 8000:8000 codeask
```

## API usage

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

## Slack bot

To enable the Slack bot:

1. Create a Slack app with Socket Mode enabled
2. Add bot token scopes: `app_mentions:read`, `chat:write`, `channels:history`, `groups:history`
3. Subscribe to the `app_mention` event
4. Set `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` in your `.env`

## MCP servers

You can extend the agent's capabilities by connecting MCP tool servers. Create a `mcp_servers.json` file:

```json
{
  "servers": [
    {
      "name": "example",
      "command": "npx",
      "args": ["-y", "@example/mcp-server"]
    }
  ]
}
```

Set `MCP_SERVERS_CONFIG=mcp_servers.json` in your `.env`.

## License

MIT
