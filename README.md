# brd-enhancer-mcp

An MCP server that enhances developer tasks with context pulled from your project documentation.

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

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `API_KEY` | ✅ Yes | — | Your personal API key |
| `PROJECT_ID` | ✅ Yes | — | Your project ID/GUID |
| `API_URL` | ❌ No | `http://localhost:8000` | Backend URL |

---

## Quick Reference

| Scenario | Command in config |
|----------|-------------------|
| Global install (Official Python) | `"command": "brd-enhancer-mcp"` |
| Global install (Microsoft Store Python) | Full path from `where brd-enhancer-mcp` |
| Virtual environment (any OS) | Full path from `python -c "import shutil; print(shutil.which('brd-enhancer-mcp'))"` |
