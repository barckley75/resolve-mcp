# Quick Start — Run ResolveMCP from this local repo

Get the server talking to Claude Desktop in 3 steps.

## Prerequisites

- DaVinci Resolve Studio installed and **running**
- `uv` installed: `brew install uv`
- Claude Desktop installed

## 1. Install dependencies

```bash
cd "/Volumes/Mac mini/Projects/resolve-mcp"
uv sync --all-extras
```

This creates `.venv/` and installs the MCP server + `mlx-whisper` for local transcription.

## 2. Configure Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` and add:

```json
{
  "mcpServers": {
    "resolve": {
      "command": "/opt/homebrew/bin/uv",
      "args": [
        "--directory",
        "/Volumes/Mac mini/Projects/resolve-mcp",
        "run",
        "resolve-mcp"
      ],
      "env": {
        "RESOLVE_SCRIPT_LIB": "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so",
        "PYTHONPATH": "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules/"
      }
    }
  }
}
```

Key differences from the `uvx` install in the main README:
- `command` is the full path to `uv` (not `uvx`)
- `--directory` points to this repo so any code changes are picked up immediately

If `uv` lives somewhere else, check with:

```bash
which uv
```

## 3. Enable Resolve scripting and restart Claude Desktop

1. In DaVinci Resolve: **Preferences → General → External scripting using → Local**
2. Quit Claude Desktop completely (⌘Q)
3. Re-open Claude Desktop

You should see a 🔨 hammer icon — click it and "resolve" should be listed with its tools.

## 4. Grant Screen Recording permission (for `screenshot` tool)

macOS will prompt the first time. Or do it ahead of time:

**System Settings → Privacy & Security → Screen Recording → add Claude Desktop**

## Verify it works

Ask Claude:

> "What project do I have open in Resolve?"

Or:

> "Take a screenshot of Resolve."

If something breaks, check the Claude Desktop MCP logs:

```bash
tail -f ~/Library/Logs/Claude/mcp-server-resolve.log
```

## Updating the code

Since Claude Desktop runs `uv run` in this directory, any Python changes you make are picked up on the next tool call. If you change tool signatures or add new tools, restart Claude Desktop (⌘Q and reopen) for the new schema to load.
