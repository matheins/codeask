<div align="center">

<h1>🔍 CodeAsk</h1>

<p><strong>Ask questions about any codebase — get instant, accurate answers powered by AI.</strong></p>

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

</div>

---

## Why CodeAsk?

- **Instant answers** — ask plain-English questions, get responses grounded in your actual source code
- **Slack-native** — mention the bot in any channel and get answers where your team already works
- **Always up-to-date** — auto-syncs the repo on a configurable interval so answers reflect the latest code
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
                        ▼
               ┌─────────────────┐
               │  Serena (MCP)   │
               │ code intelligence│
               └────────┬────────┘
                        │  reads code
                        ▼
               ┌─────────────────┐
               │   Your Repo     │
               │  (auto-synced)  │
               └─────────────────┘
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
| `MAX_ITERATIONS` | No | Max agent tool-call rounds (default: `15`) |

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

1. Create a Slack app with **Socket Mode** enabled
2. Add bot token scopes: `app_mentions:read`, `chat:write`, `channels:history`, `groups:history`
3. Subscribe to the `app_mention` event
4. Set `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` in your `.env`

The bot will start automatically when both tokens are present.

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
