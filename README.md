# brd-enhancer-mcp

An MCP server that enhances developer tasks with context pulled from your project documentation.

## Installation

Install directly from GitHub using pip:

```bash
pip install git+https://github.com/arushsingh17/mcp.git
```

## Required Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `API_KEY` | ✅ Yes | — | Your API key for the backend |
| `PROJECT_ID` | ✅ Yes | — | The project ID/GUID to search within |
| `API_URL` | ❌ No | `http://localhost:8000` | Your backend URL |

## Usage

### Run directly

```bash
export API_KEY=your_api_key
export PROJECT_ID=your_project_id
export API_URL=https://your-backend.com

brd-enhancer-mcp
```

### Claude Desktop Config

Add this to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "brd-enhancer": {
      "command": "brd-enhancer-mcp",
      "env": {
        "API_KEY": "your_api_key",
        "PROJECT_ID": "your_project_id",
        "API_URL": "https://your-backend.com"
      }
    }
  }
}
```

### Claude Code Config (`~/.claude.json`)

```json
{
  "mcpServers": {
    "brd-enhancer": {
      "command": "brd-enhancer-mcp",
      "env": {
        "API_KEY": "your_api_key",
        "PROJECT_ID": "your_project_id",
        "API_URL": "https://your-backend.com"
      }
    }
  }
}
```

## What it does

Exposes two MCP capabilities:

- **`enhance_task` (tool)** — Takes a task description, queries your project docs backend, and returns an enhanced prompt with relevant context.
- **`enhance` (prompt)** — Same functionality exposed as an MCP prompt.
