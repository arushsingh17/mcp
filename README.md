# brd-enhancer-mcp

A package that ships three MCP servers for an AI-assisted SDLC workflow:

| Server | Executable | Purpose |
|---|---|---|
| enhance-prompt | `prompt-enhancer-mcp` | Enhance dev tasks with project documentation context |
| test-workflow | `test-workflow-mcp` | Generate & submit Gherkin test cases from Confluence |
| pipeline-analyzer | `pipeline-analyzer-mcp` | Analyze Harness pipeline failures with RAG context from past incidents |

---

## Prerequisites

### ⚠️ Use Official Python (NOT Microsoft Store Python)

Download Python from **https://www.python.org/downloads/**

During installation, make sure to check:
> ✅ **"Add Python to PATH"**

**Verify you have the correct Python:**
```bash
where python
```
| Output | Status |
|--------|--------|
| `C:\Python313\python.exe` | ✅ Official Python — Good |
| `C:\Users\...\WindowsApps\python.exe` | ❌ Microsoft Store Python — Reinstall from python.org |

---

## Installation

### Option A — Global Install (Recommended for most developers)

```bash
pip install git+https://github.com/arushsingh17/mcp.git
```

**Verify installation:**
```bash
pip show brd-enhancer-mcp
where brd-enhancer-mcp        # Windows
which brd-enhancer-mcp        # Mac/Linux
```

---

### Option B — Virtual Environment Install

```bash
# Step 1: Create and activate a virtual environment
python -m venv venv
venv\Scripts\activate          # Windows
source venv/bin/activate       # Mac/Linux

# Step 2: Install the package
pip install git+https://github.com/arushsingh17/mcp.git

# Step 3: Get the exact executable path (needed for config)
python -c "import shutil; print(shutil.which('brd-enhancer-mcp'))"
```

Example output:
```
C:\Users\YourName\Desktop\myproject\venv\Scripts\brd-enhancer-mcp.exe
```
> Copy this path — you will need it in the config below.

---

## Configuration

### 🌍 Global Install Config

Since `brd-enhancer-mcp` is registered in system PATH, no file path is needed.

**Claude Desktop** → `%APPDATA%\Claude\claude_desktop_config.json` (Windows)
**Claude Desktop** → `~/Library/Application Support/Claude/claude_desktop_config.json` (Mac)
**Claude Code** → `~/.claude.json`

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

---

### 📦 Virtual Environment Config

Use the full path you got from the command above.

> ⚠️ On Windows replace every `\` with `\\` in the path

```json
{
    "mcpServers": {
        "brd-enhancer": {
            "command": "C:\\Users\\YourName\\Desktop\\myproject\\venv\\Scripts\\brd-enhancer-mcp.exe",
            "env": {
                "API_KEY": "your_api_key",
                "PROJECT_ID": "your_project_id",
                "API_URL": "https://your-backend.com"
            }
        }
    }
}
```

**Mac/Linux venv config:**
```json
{
    "mcpServers": {
        "brd-enhancer": {
            "command": "/Users/yourname/myproject/venv/bin/brd-enhancer-mcp",
            "env": {
                "API_KEY": "your_api_key",
                "PROJECT_ID": "your_project_id",
                "API_URL": "https://your-backend.com"
            }
        }
    }
}
```

---

## Environment Variables

### Shared (all servers)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `API_KEY` | ✅ Yes | — | Your personal backend API key |
| `PROJECT_ID` | ✅ Yes | — | Your project ID/GUID |
| `API_URL` | ❌ No | `http://localhost:8000` | Backend URL |

### pipeline-analyzer-mcp only

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `HARNESS_API_KEY` | ✅ Yes | — | Harness personal access token (`pat.ACCOUNT.TOKEN_ID.SECRET`) |
| `HARNESS_ACCOUNT_ID` | ✅ Yes | — | Harness account identifier |
| `HARNESS_PROJECT_ID` | ✅ Yes | — | Harness project identifier |
| `HARNESS_ORG_ID` | ❌ No | `default` | Harness organization identifier |
| `HARNESS_BASE_URL` | ❌ No | `https://app.harness.io` | Harness base URL (change for self-hosted) |

### pipeline-analyzer example config

```json
{
    "mcpServers": {
        "pipeline-analyzer": {
            "command": "pipeline-analyzer-mcp",
            "env": {
                "API_URL": "https://your-backend.com",
                "API_KEY": "your_backend_api_key",
                "PROJECT_ID": "your_project_id",
                "HARNESS_API_KEY": "pat.ACCOUNT.TOKEN_ID.SECRET",
                "HARNESS_ACCOUNT_ID": "your_harness_account_id",
                "HARNESS_ORG_ID": "default",
                "HARNESS_PROJECT_ID": "your_harness_project_id",
                "HARNESS_BASE_URL": "https://app.harness.io"
            }
        }
    }
}
```

---

## Quick Reference

| Scenario | Command in config |
|----------|-------------------|
| Global install (Official Python) | `"command": "brd-enhancer-mcp"` |
| Global install (Microsoft Store Python) | Full path from `where brd-enhancer-mcp` |
| Virtual environment (any OS) | Full path from `python -c "import shutil; print(shutil.which('brd-enhancer-mcp'))"` |
