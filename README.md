# DaVinci Resolve MCP Integration

Connect **DaVinci Resolve Studio** to **Claude AI** through the [Model Context Protocol (MCP)](https://modelcontextprotocol.io). Control timelines, grade footage, composite in Fusion, mix audio, and render — all through natural language.

> **Third-party integration.** Not affiliated with or endorsed by Blackmagic Design.

> **Platform support:** Core Resolve control (52 tools) works on macOS, Windows, and Linux. Local transcription and `screenshot` are macOS / Apple Silicon only.

---

## Quick Start

1. **Open DaVinci Resolve Studio** with a project loaded
2. Enable scripting: **Preferences → General → External scripting using → Local**
3. **Restart Claude Desktop** (quit completely, reopen)
4. Look for the hammer icon (🔨) in Claude Desktop — that's your 52 Resolve tools

---

## What This MCP Server Can Do

### Page Navigation
Switch between any page in Resolve programmatically.

| Command | What it does |
|---|---|
| `open_page` | Jump to Media, Cut, Edit, Fusion, Color, Fairlight, or Deliver page |
| `get_current_page` | Query which page is active |
| `get_project_info` | Read project name, frame rate, resolution, timeline count, Resolve version |

---

### Media Pool
Import footage and organize the media pool.

| Command | What it does |
|---|---|
| `get_media_pool_structure` | Browse folder/clip hierarchy (configurable depth) |
| `import_media` | Import files by absolute path into the current media pool folder |
| `create_timeline` | Create a new empty timeline |

---

### Timeline Editing
Build and manipulate timelines. This is the core editing layer.

| Command | What it does |
|---|---|
| `get_current_timeline_info` | Read timeline name, frame rate, track counts, in/out points |
| `get_timeline_items` | List all clips on a specified video, audio, or subtitle track |
| `append_to_timeline` | Add clips from the media pool to the current timeline by name |
| `add_marker` | Place a color-coded marker at any frame with a name and note |
| `get_markers` | Read all markers on the current timeline |
| `set_current_timecode` | Move the playhead to an exact `HH:MM:SS:FF` position |
| `get_current_timecode` | Read the current playhead position |

> **Can it cut media files?** The server can append clips and set properties on existing timeline items. Direct timeline cuts (razor/split at playhead) are not a named tool — use `execute_resolve_code` with `timeline.Split()` for that operation.

---

### Timeline Item Properties
Fine-grained control over individual clips on the timeline.

| Command | What it does |
|---|---|
| `get_timeline_item_properties` | Read all properties of a clip (pan, tilt, zoom, opacity, crop, rotation…) |
| `set_timeline_item_property` | Set any property. Keys: `Pan`, `Tilt`, `ZoomX`, `ZoomY`, `Opacity`, `CropLeft`, `CropRight`, `CropTop`, `CropBottom`, `RotationAngle`, `FlipX`, `FlipY`, `CompositeMode`, `RetimeProcess`, `Scaling` |

---

### Color Grading (Color Page)
Full access to the node-based color pipeline.

| Command | What it does |
|---|---|
| `get_node_graph` | Inspect the node graph structure for any clip |
| `set_lut` | Apply a `.cube`, `.3dl`, or other LUT file to a specific node |
| `set_cdl` | Set CDL values — slope, offset, power, saturation — per node |
| `get_current_thumbnail` | Return a PNG of the current frame (Color page must be active) |
| `export_current_frame` | Save current frame as `.png`, `.jpg`, `.tif`, `.dpx`, or `.exr` |

---

### Audio — Fairlight
The server exposes Resolve's Voice Isolation feature on audio tracks. Direct Fairlight mixer controls (EQ, compression, routing, bus assignments) are not wrapped as named tools — use `execute_resolve_code` with the Fairlight scripting API for those operations.

| Command | What it does |
|---|---|
| `get_voice_isolation_state` | Check if Voice Isolation is enabled on an audio track and the current level |
| `set_voice_isolation_state` | Enable or disable Voice Isolation with configurable amount (0–100%) |

**Fairlight via `execute_resolve_code`:** EQ band values, compressor settings, bus routing, track muting/soloing, audio clip gain, and any other Fairlight API operation can be scripted through the power tool.

---

### AI / Neural Engine (Resolve Studio 19+ only)
These tools invoke DaVinci's built-in AI processing. Studio license required.

| Command | What it does |
|---|---|
| `detect_scene_cuts` | Auto-detect cut points in the current timeline using AI |
| `create_magic_mask` | AI subject isolation mask on a clip (forward, backward, or bidirectional) |
| `regenerate_magic_mask` | Regenerate an existing Magic Mask |
| `smart_reframe` | AI-driven reframing to a different aspect ratio |
| `stabilize` | Neural Engine-based clip stabilization |
| `create_subtitles_from_audio` | Generate subtitle tracks via Resolve's built-in AI speech recognition (16 languages: English, French, German, Italian, Japanese, Korean, Mandarin Simplified/Traditional, Portuguese, Russian, Spanish, Danish, Dutch, Norwegian, Swedish, auto) |

---

### Local Transcription — macOS / Apple Silicon only
Uses `mlx-whisper` running locally on your Mac's Neural Engine. No cloud calls, no API keys.

| Command | What it does |
|---|---|
| `transcribe_audio` | Transcribe any audio/video file. Returns timestamped transcript inline + saves `.srt` next to source |
| `transcribe_and_add_subtitles` | Transcribe and add timeline markers at the correct frame positions with the text |
| `export_srt` | Transcribe and save an `.srt` file ready for **File → Import → Subtitle** in Resolve |
| `list_whisper_models` | Show available models: `tiny` (fastest) → `base` → `small` → `medium` → `large` → `turbo` (default) |

Long files are automatically chunked into 5-minute pieces by `ffmpeg` — no timeouts on feature-length content.

---

### Fusion Compositing / VFX
Manage Fusion compositions attached to timeline items.

| Command | What it does |
|---|---|
| `get_fusion_comp_list` | List all Fusion compositions on a clip |
| `add_fusion_comp` | Add a new empty Fusion composition to a clip |
| `import_fusion_comp` | Load a `.comp` or `.setting` file into a clip |
| `export_fusion_comp` | Save a Fusion composition to disk |
| `load_fusion_comp` | Set a named composition as the active one |
| `delete_fusion_comp` | Remove a named Fusion composition |
| `rename_fusion_comp` | Rename a Fusion composition |
| `create_fusion_clip` | Merge one or more timeline items into a Fusion clip |
| `insert_fusion_generator` | Insert a named Fusion generator at the playhead |
| `insert_fusion_composition` | Insert a blank Fusion composition at the playhead |
| `insert_fusion_title` | Insert a Fusion title template at the playhead |

---

### Rendering & Delivery
Full render queue control.

| Command | What it does |
|---|---|
| `get_render_formats` | List all available formats (mp4, mov, mxf, etc.) and codecs |
| `get_render_settings` | Read current format/codec, render jobs, presets, and render-in-progress state |
| `set_render_settings` | Set format, codec, output directory, filename, frame range, resolution |
| `add_render_job` | Queue the current render configuration as a job |
| `start_rendering` | Start all queued jobs (or a specific job by ID) |
| `get_render_status` | Poll a job's completion status and progress |
| `stop_rendering` | Cancel active renders |

---

### Timeline Export
Export the current timeline to interchange formats via `export_timeline`.

Supported formats: `AAF`, `DRT`, `EDL`, `FCP 7 XML`, `FCPXML 1.8/1.9/1.10`, `HDR-10 Profile A/B`, `CSV`, `TAB`, `OTIO`, `ALE`, `ALE CDL`

---

### Screenshot (macOS only)
`screenshot` captures the Resolve window directly using macOS `screencapture` + Quartz. Lets Claude visually verify what's on screen without any manual steps. Works on all Resolve pages.

Requires **Screen Recording** permission for Claude Desktop in **System Settings → Privacy & Security → Screen Recording**.

> **Privacy:** Screenshots are sent to Anthropic for analysis. Do not use when client footage, NDA material, or sensitive content is on screen.

---

### Power Tool — Arbitrary Code Execution

`execute_resolve_code` runs any Python against the full Resolve scripting API. Pre-loaded: `resolve`, `project`, `mediaPool`, `timeline`, `mediaStorage`. Use `print()` or set `result` to return data.

This covers everything not wrapped as a named tool: timeline splits, Fairlight mixer control, project manager, gallery stills, remote grades, LUT browsing, and more.

---

## Tool Count Summary

| Category | Tools | Notes |
|---|---|---|
| Project & Navigation | 3 | All platforms |
| Media Pool | 3 | All platforms |
| Timeline | 7 | All platforms |
| Item Properties | 2 | All platforms |
| Color Grading | 3 | Color page required |
| Rendering | 7 | All platforms |
| AI / Neural Engine | 6 | Resolve Studio 19+ only |
| Audio | 2 | Voice Isolation only |
| Fusion | 11 | All platforms |
| Timeline Export | 1 | All platforms |
| Frame / Thumbnail | 2 | Color page required |
| Local Transcription | 4 | macOS / Apple Silicon only |
| Screenshot | 1 | macOS only |
| Code Execution | 1 | All platforms |
| **Total** | **52** | |

---

## Architecture

```
Claude AI (MCP Client — Claude Desktop)
         │
         ▼
ResolveMCP Server (FastMCP — uvx resolve-mcp)
         │
         ▼
fusionscript.so / DaVinciResolveScript.py
         │
         ▼
DaVinci Resolve Studio (must be running)
```

No addon required inside Resolve. No socket server. One process, direct scripting API connection.

---

## Prerequisites

| Requirement | Version |
|---|---|
| DaVinci Resolve Studio | 18.0+ (free version has limited scripting) |
| Python | 3.10+ |
| uv | any recent — `brew install uv` |
| macOS (transcription/screenshot) | Apple Silicon Mac |
| ffmpeg (transcription of long files) | any version in `PATH` |

---

## Enabling Scripting in Resolve

1. Open DaVinci Resolve Studio
2. **Preferences → General**
3. Set **External scripting using** to **Local**
4. Click **Save**

Without this, all tools return `"Could not connect to DaVinci Resolve"`.

---

## Troubleshooting

| Error | Fix |
|---|---|
| `Could not connect to DaVinci Resolve` | Resolve must be running. Scripting must be set to Local in Preferences → General |
| `Failed to import DaVinciResolveScript` | Verify `PYTHONPATH` points to the correct Modules directory |
| `No active timeline` | Open a project and load a timeline before using timeline tools |
| Tools not in Claude Desktop | Run `uv --version` to confirm uv is installed. Fully quit and reopen Claude Desktop |
| `screencapture failed` | Grant Screen Recording permission to Claude Desktop in System Settings |
| `mlx-whisper is not installed` | Run `uv pip install 'mlx-whisper>=0.4.3'` in the project venv |

---

## Disclaimer

**USE AT YOUR OWN RISK.** Unofficial third-party project. Not affiliated with Blackmagic Design or Anthropic. AI agents can make mistakes — they may modify, overwrite, or delete projects, timelines, clips, or files. `execute_resolve_code` runs arbitrary Python with full filesystem access. Screenshots are transmitted to Anthropic. Always work from a project backup.

---

## Source

Upstream: [github.com/barckley75/resolve-mcp](https://github.com/barckley75/resolve-mcp)  
Protocol: [modelcontextprotocol.io](https://modelcontextprotocol.io)  
Built with [FastMCP](https://github.com/jlowin/fastmcp) | Inspired by [BlenderMCP](https://github.com/ahujasid/blender-mcp)
