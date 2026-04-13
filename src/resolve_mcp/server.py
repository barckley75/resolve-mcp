"""
DaVinci Resolve MCP Server

A FastMCP server that exposes DaVinci Resolve Studio's scripting API
as MCP tools, allowing Claude to control Resolve via natural language.
"""

from mcp.server.fastmcp import FastMCP, Image
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Any, List, Optional

from .connection import get_resolve_connection, ResolveConnection
from .transcription import (
    transcribe as _transcribe,
    segments_to_srt,
    WHISPER_MODELS,
    DEFAULT_MODEL,
)
from .resolve_utils import (
    folder_to_dict,
    clip_to_dict,
    clip_to_dict_brief,
    timeline_to_dict,
    timeline_item_to_dict,
    timeline_item_full_dict,
    node_graph_to_dict,
    thumbnail_to_png_bytes,
    safe_serialize,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("ResolveMCP")


# ── Helpers ──

def _conn() -> ResolveConnection:
    """Shorthand that gives a clear error when Resolve isn't reachable."""
    return get_resolve_connection()


def _require_timeline(conn: ResolveConnection):
    """Return the current timeline or raise with a helpful message."""
    tl = conn.get_current_timeline()
    if tl is None:
        raise RuntimeError(
            "No active timeline. Create or open a timeline first."
        )
    return tl


def _get_timeline_item(track_type: str, track_index: int, item_index: int):
    """Get a specific TimelineItem from the current timeline."""
    conn = _conn()
    timeline = _require_timeline(conn)

    items = timeline.GetItemListInTrack(track_type, track_index)
    if not items:
        raise RuntimeError(
            f"No items found on {track_type} track {track_index}. "
            f"Check track_type ('video'/'audio'/'subtitle') and track_index (1-based)."
        )
    if item_index < 0 or item_index >= len(items):
        raise RuntimeError(
            f"item_index {item_index} out of range — track has {len(items)} item(s) (0-{len(items) - 1})"
        )
    return items[item_index]


def _ok(result: Any, success_msg: str, fail_msg: str) -> str:
    """Return success_msg if result is truthy, else fail_msg."""
    return success_msg if result else fail_msg


# ── Lifespan ──

@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    try:
        logger.info("ResolveMCP server starting up")
        try:
            conn = get_resolve_connection()
            project = conn.get_project()
            logger.info("Connected to Resolve — project: %s", project.GetName())
        except Exception as e:
            logger.warning("Could not connect to Resolve on startup: %s", e)
            logger.warning("Tools will attempt to connect when called.")
        yield {}
    finally:
        logger.info("ResolveMCP server shut down")


mcp = FastMCP("ResolveMCP", lifespan=server_lifespan)


# ═══════════════════════════════════════════════════════════════════
#  PROJECT & NAVIGATION
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def get_project_info() -> str:
    """
    Get information about the current DaVinci Resolve project.

    Returns project name, settings (frame rate, resolution),
    timeline count, current page, and version info.
    """
    try:
        conn = _conn()
        resolve = conn.get_resolve()
        project = conn.get_project()

        info = {
            "project_name": project.GetName(),
            "resolve_version": resolve.GetVersionString(),
            "current_page": resolve.GetCurrentPage(),
            "timeline_count": project.GetTimelineCount(),
        }

        for key in (
            "timelineFrameRate",
            "timelineResolutionWidth",
            "timelineResolutionHeight",
            "timelinePlaybackFrameRate",
        ):
            val = project.GetSetting(key)
            if val:
                info[key] = val

        return json.dumps(info, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def open_page(page: str) -> str:
    """
    Switch to a specific page in DaVinci Resolve.

    Parameters:
    - page: One of "media", "cut", "edit", "fusion", "color", "fairlight", "deliver"
    """
    valid_pages = ("media", "cut", "edit", "fusion", "color", "fairlight", "deliver")
    if page not in valid_pages:
        return f"Invalid page '{page}'. Must be one of: {', '.join(valid_pages)}"
    try:
        conn = _conn()
        resolve = conn.get_resolve()
        success = resolve.OpenPage(page)
        return _ok(success, f"Switched to {page} page", f"Failed to switch to {page} page")
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_current_page() -> str:
    """Get the currently active page in DaVinci Resolve."""
    try:
        conn = _conn()
        resolve = conn.get_resolve()
        page = resolve.GetCurrentPage()
        return page or "unknown"
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════════════
#  MEDIA POOL
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def get_media_pool_structure(max_depth: int = 3, max_clips: int = 50) -> str:
    """
    Get the folder/clip structure of the media pool.

    Parameters:
    - max_depth: Maximum folder recursion depth (default: 3)
    - max_clips: Maximum clips to list per folder (default: 50)
    """
    try:
        conn = _conn()
        mp = conn.get_media_pool()
        root = mp.GetRootFolder()
        if root is None:
            return "Error: Could not get root folder from media pool"
        structure = folder_to_dict(root, max_depth, max_clips)
        return json.dumps(structure, indent=2, default=str)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def import_media(file_paths: List[str]) -> str:
    """
    Import media files into the current media pool folder.

    Parameters:
    - file_paths: List of absolute file paths to import
    """
    try:
        conn = _conn()
        mp = conn.get_media_pool()
        items = mp.ImportMedia(file_paths)
        if items:
            names = [item.GetName() for item in items if item]
            return json.dumps({"imported": len(names), "clips": names}, indent=2)
        return "No media was imported. Check that file paths exist and are supported formats."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def create_timeline(name: str) -> str:
    """
    Create a new empty timeline in the current project.

    Parameters:
    - name: Name for the new timeline
    """
    try:
        conn = _conn()
        mp = conn.get_media_pool()
        timeline = mp.CreateEmptyTimeline(name)
        if timeline:
            return json.dumps(timeline_to_dict(timeline), indent=2)
        return f"Failed to create timeline '{name}'. Name may already exist."
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════════════
#  TIMELINE
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def get_current_timeline_info() -> str:
    """Get detailed information about the current timeline."""
    try:
        conn = _conn()
        timeline = conn.get_current_timeline()
        if timeline is None:
            return "No active timeline. Create or open a timeline first."
        return json.dumps(timeline_to_dict(timeline), indent=2, default=str)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_timeline_items(track_type: str = "video", track_index: int = 1) -> str:
    """
    List all clips/items on a specific track of the current timeline.

    Parameters:
    - track_type: "video", "audio", or "subtitle" (default: "video")
    - track_index: 1-based track index (default: 1)
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)

        items = timeline.GetItemListInTrack(track_type, track_index)
        if not items:
            return f"No items on {track_type} track {track_index}"

        result = []
        for i, item in enumerate(items):
            d = timeline_item_to_dict(item)
            d["index"] = i
            result.append(d)

        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def append_to_timeline(clip_names: List[str]) -> str:
    """
    Append media pool clips to the current timeline by name.

    Parameters:
    - clip_names: List of clip names to append (must exist in the current media pool folder)
    """
    try:
        conn = _conn()
        mp = conn.get_media_pool()
        folder = mp.GetCurrentFolder()
        if folder is None:
            return "Error: Could not get current media pool folder"

        all_clips = folder.GetClipList() or []
        name_to_clip = {}
        for clip in all_clips:
            n = clip.GetName()
            if n:
                name_to_clip[n] = clip

        clips_to_add = []
        not_found = []
        for name in clip_names:
            if name in name_to_clip:
                clips_to_add.append(name_to_clip[name])
            else:
                not_found.append(name)

        if not clips_to_add:
            available = list(name_to_clip.keys())[:20]
            return (
                f"No matching clips found. Not found: {not_found}\n"
                f"Available clips in current folder: {available}"
            )

        result = mp.AppendToTimeline(clips_to_add)
        output: dict = {"appended": len(clips_to_add)}
        if not_found:
            output["not_found"] = not_found
        if result:
            output["timeline_items"] = [item.GetName() for item in result if item]
        return json.dumps(output, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def add_marker(
    frame_id: int,
    color: str,
    name: str,
    note: str = "",
    duration: int = 1,
    custom_data: str = "",
) -> str:
    """
    Add a marker to the current timeline.

    Parameters:
    - frame_id: Frame position for the marker
    - color: Marker color ("Red", "Orange", "Yellow", "Green", "Cyan", "Blue",
             "Purple", "Pink", "Fuchsia", "Rose", "Lavender", "Sky", "Mint",
             "Lemon", "Sand", "Cocoa", "Cream")
    - name: Marker name
    - note: Optional note text
    - duration: Marker duration in frames (default: 1)
    - custom_data: Optional custom data string
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        success = timeline.AddMarker(frame_id, color, name, note, duration, custom_data)
        return _ok(success, f"Marker '{name}' added at frame {frame_id}", "Failed to add marker")
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_markers() -> str:
    """Get all markers on the current timeline."""
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        markers = timeline.GetMarkers()
        if not markers:
            return "No markers on timeline"
        return json.dumps({str(k): v for k, v in markers.items()}, indent=2, default=str)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def set_current_timecode(timecode: str) -> str:
    """
    Move the playhead to a specific timecode.

    Parameters:
    - timecode: Timecode string in "HH:MM:SS:FF" format
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        success = timeline.SetCurrentTimecode(timecode)
        return _ok(success, f"Playhead moved to {timecode}", f"Failed to set timecode to {timecode}")
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_current_timecode() -> str:
    """Get the current playhead timecode."""
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        tc = timeline.GetCurrentTimecode()
        return tc or "Could not read timecode"
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════════════
#  TIMELINE ITEM PROPERTIES
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def get_timeline_item_properties(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """
    Get all properties of a specific timeline item.

    Parameters:
    - track_type: "video", "audio", or "subtitle" (default: "video")
    - track_index: 1-based track index (default: 1)
    - item_index: 0-based index of the item in the track (default: 0)
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        return json.dumps(timeline_item_full_dict(item), indent=2, default=str)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def set_timeline_item_property(
    property_key: str,
    property_value: str,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """
    Set a property on a specific timeline item.

    Parameters:
    - property_key: Property name (e.g. "Pan", "Tilt", "ZoomX", "ZoomY", "Opacity",
                    "CropLeft", "CropRight", "CropTop", "CropBottom", "RotationAngle",
                    "FlipX", "FlipY", "CompositeMode", "RetimeProcess", "Scaling", etc.)
    - property_value: Value to set (will be auto-converted to appropriate type)
    - track_type: "video", "audio", or "subtitle" (default: "video")
    - track_index: 1-based track index (default: 1)
    - item_index: 0-based index of the item in the track (default: 0)
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)

        # Auto-convert value types
        value: Any = property_value
        try:
            value = float(property_value)
            if value == int(value):
                value = int(value)
        except (ValueError, TypeError):
            if isinstance(property_value, str) and property_value.lower() in ("true", "false"):
                value = property_value.lower() == "true"

        success = item.SetProperty(property_key, value)
        if success:
            return f"Set {property_key} = {value} on item {item_index}"
        return (
            f"Failed to set {property_key}={value}. "
            f"Check the property name is valid and the value is in the accepted range."
        )
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════════════
#  COLOR GRADING
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def get_node_graph(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """
    Get the color grading node graph info for a timeline item.
    Must be on the Color page with a clip selected.

    Parameters:
    - track_type: "video", "audio", or "subtitle" (default: "video")
    - track_index: 1-based track index (default: 1)
    - item_index: 0-based item index (default: 0)
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        graph = item.GetNodeGraph()
        if graph is None:
            return (
                "No node graph available. "
                "Make sure you are on the Color page and have a video clip selected."
            )
        return json.dumps(node_graph_to_dict(graph), indent=2, default=str)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def set_lut(
    node_index: int,
    lut_path: str,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """
    Apply a LUT to a node in a clip's color node graph.

    Parameters:
    - node_index: 1-based node index
    - lut_path: Absolute path to the LUT file (.cube, .3dl, etc.)
    - track_type: "video" (default)
    - track_index: 1-based track index (default: 1)
    - item_index: 0-based item index (default: 0)
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        graph = item.GetNodeGraph()
        if graph is None:
            return "No node graph available. Switch to the Color page first."
        success = graph.SetLUT(node_index, lut_path)
        return _ok(
            success,
            f"LUT applied to node {node_index}: {lut_path}",
            "Failed to apply LUT. Check that node_index is valid and LUT file exists.",
        )
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def set_cdl(
    node_index: int,
    slope: str = "1.0 1.0 1.0",
    offset: str = "0.0 0.0 0.0",
    power: str = "1.0 1.0 1.0",
    saturation: float = 1.0,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """
    Apply CDL (Color Decision List) values to a node.

    Parameters:
    - node_index: 1-based node index
    - slope: RGB slope as space-separated string (default: "1.0 1.0 1.0")
    - offset: RGB offset as space-separated string (default: "0.0 0.0 0.0")
    - power: RGB power as space-separated string (default: "1.0 1.0 1.0")
    - saturation: Saturation value (default: 1.0)
    - track_type/track_index/item_index: Clip locator
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        cdl_map = {
            "NodeIndex": node_index,
            "Slope": slope,
            "Offset": offset,
            "Power": power,
            "Saturation": str(saturation),
        }
        success = item.SetCDL(cdl_map)
        if success:
            return f"CDL applied to node {node_index}"
        return (
            "Failed to apply CDL. Make sure you are on the Color page "
            "and the node_index is valid."
        )
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════════════
#  RENDERING
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def get_render_formats(render_format: Optional[str] = None) -> str:
    """
    Get available render formats and codecs.

    Parameters:
    - render_format: If provided, returns codecs for that format. Otherwise returns all formats.
    """
    try:
        conn = _conn()
        project = conn.get_project()

        if render_format:
            codecs = project.GetRenderCodecs(render_format)
            return json.dumps({"format": render_format, "codecs": codecs}, indent=2, default=str)

        formats = project.GetRenderFormats()
        return json.dumps({"formats": formats}, indent=2, default=str)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_render_settings() -> str:
    """Get current render format, codec, render job list, and render presets."""
    try:
        conn = _conn()
        project = conn.get_project()

        result: dict = {}

        try:
            result["current_format_codec"] = project.GetCurrentRenderFormatAndCodec()
        except Exception:
            pass
        try:
            result["render_mode"] = project.GetCurrentRenderMode()
        except Exception:
            pass
        try:
            jobs = project.GetRenderJobList()
            result["render_jobs"] = safe_serialize(jobs) if jobs else []
        except Exception:
            pass
        try:
            presets = project.GetRenderPresetList()
            result["render_presets"] = safe_serialize(presets) if presets else []
        except Exception:
            pass
        try:
            result["is_rendering"] = project.IsRenderingInProgress()
        except Exception:
            pass

        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def set_render_settings(
    settings: Optional[Dict[str, Any]] = None,
    render_format: Optional[str] = None,
    codec: Optional[str] = None,
) -> str:
    """
    Configure render settings for the current project.

    Parameters:
    - settings: Dict of render settings. Common keys:
        "TargetDir" (str), "CustomName" (str), "SelectAllFrames" (bool),
        "MarkIn" (int), "MarkOut" (int), "ExportVideo" (bool), "ExportAudio" (bool),
        "FormatWidth" (int), "FormatHeight" (int), "FrameRate" (float)
    - render_format: Format string (e.g. "mp4", "mov"). Set together with codec.
    - codec: Codec string (e.g. "H.264", "H.265", "ProRes 422 HQ")
    """
    try:
        conn = _conn()
        project = conn.get_project()
        results: dict = {}

        if render_format and codec:
            success = project.SetCurrentRenderFormatAndCodec(render_format, codec)
            results["format_codec"] = "set" if success else "failed"

        if settings:
            success = project.SetRenderSettings(settings)
            results["settings"] = "set" if success else "failed"

        if not results:
            return "No settings provided. Pass 'settings' dict and/or 'render_format'+'codec'."

        return json.dumps(results, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def add_render_job() -> str:
    """Add a render job to the queue based on current render settings."""
    try:
        conn = _conn()
        project = conn.get_project()
        job_id = project.AddRenderJob()
        if job_id:
            return json.dumps({"job_id": job_id})
        return "Failed to add render job. Configure render settings first (set_render_settings)."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def start_rendering(job_ids: Optional[List[str]] = None) -> str:
    """
    Start rendering queued jobs.

    Parameters:
    - job_ids: Optional list of job IDs to render. If None, renders all queued jobs.
    """
    try:
        conn = _conn()
        project = conn.get_project()
        if job_ids:
            success = project.StartRendering(job_ids)
        else:
            success = project.StartRendering()
        return _ok(success, "Rendering started", "Failed to start rendering. Check render job queue.")
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_render_status(job_id: str) -> str:
    """
    Get the status of a render job.

    Parameters:
    - job_id: The render job ID (returned by add_render_job)
    """
    try:
        conn = _conn()
        project = conn.get_project()
        status = project.GetRenderJobStatus(job_id)
        if status is None:
            return f"No render job found with ID '{job_id}'"
        return json.dumps(safe_serialize(status), indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def stop_rendering() -> str:
    """Stop any currently running render processes."""
    try:
        conn = _conn()
        project = conn.get_project()
        project.StopRendering()
        return "Rendering stopped"
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════════════
#  AI / NEURAL ENGINE FEATURES (Resolve 19+ / Studio only)
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def create_magic_mask(
    mode: str = "F",
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """
    Create an AI-powered Magic Mask on a timeline item for subject isolation.
    Requires DaVinci Resolve Studio with Neural Engine.

    Parameters:
    - mode: "F" (forward), "B" (backward), or "BI" (bidirectional)
    - track_type/track_index/item_index: Clip locator
    """
    valid_modes = ("F", "B", "BI")
    if mode not in valid_modes:
        return f"Invalid mode '{mode}'. Must be one of: {', '.join(valid_modes)}"
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        if not hasattr(item, 'CreateMagicMask'):
            return "CreateMagicMask is not available. Requires Resolve Studio 19+."
        success = item.CreateMagicMask(mode)
        return _ok(success, f"Magic Mask created (mode: {mode})", "Failed to create Magic Mask")
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def regenerate_magic_mask(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """
    Regenerate an existing Magic Mask on a timeline item.

    Parameters:
    - track_type/track_index/item_index: Clip locator
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        if not hasattr(item, 'RegenerateMagicMask'):
            return "RegenerateMagicMask is not available. Requires Resolve Studio 19+."
        success = item.RegenerateMagicMask()
        return _ok(success, "Magic Mask regenerated", "Failed to regenerate Magic Mask")
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def smart_reframe(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """
    Apply Smart Reframe to a timeline item (AI-based reframing).
    Requires DaVinci Resolve Studio with Neural Engine.

    Parameters:
    - track_type/track_index/item_index: Clip locator
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        if not hasattr(item, 'SmartReframe'):
            return "SmartReframe is not available. Requires Resolve Studio 19+."
        success = item.SmartReframe()
        return _ok(success, "Smart Reframe applied", "Failed to apply Smart Reframe")
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def stabilize(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """
    Apply stabilization to a timeline item using DaVinci Neural Engine.
    Requires DaVinci Resolve Studio.

    Parameters:
    - track_type/track_index/item_index: Clip locator
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        if not hasattr(item, 'Stabilize'):
            return "Stabilize is not available. Requires Resolve Studio 19+."
        success = item.Stabilize()
        return _ok(success, "Stabilization applied", "Failed to stabilize")
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def detect_scene_cuts() -> str:
    """
    Detect scene cuts in the current timeline using AI.
    Requires DaVinci Resolve Studio.
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        if not hasattr(timeline, 'DetectSceneCuts'):
            return "DetectSceneCuts is not available. Requires Resolve Studio 19+."
        success = timeline.DetectSceneCuts()
        return _ok(success, "Scene cuts detected and applied", "Failed to detect scene cuts")
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def create_subtitles_from_audio(
    language: str = "auto",
    preset: str = "default",
    chars_per_line: int = 42,
    line_break: str = "single",
    gap: int = 0,
) -> str:
    """
    Generate subtitles from audio using AI speech recognition.
    Requires DaVinci Resolve Studio 19+.

    Parameters:
    - language: "auto", "english", "french", "german", "italian", "japanese",
                "korean", "mandarin_simplified", "mandarin_traditional",
                "portuguese", "russian", "spanish", "danish", "dutch",
                "norwegian", "swedish"
    - preset: "default", "teletext", or "netflix"
    - chars_per_line: Characters per line (1-60, default: 42)
    - line_break: "single" or "double"
    - gap: Gap between subtitles in frames (0-10, default: 0)
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        resolve = conn.get_resolve()

        if not hasattr(timeline, 'CreateSubtitlesFromAudio'):
            return "CreateSubtitlesFromAudio is not available. Requires Resolve Studio 19+."

        # Build settings using Resolve constants from the existing connection
        language_map = {
            "auto": "AUTO_CAPTION_AUTO",
            "english": "AUTO_CAPTION_ENGLISH",
            "french": "AUTO_CAPTION_FRENCH",
            "german": "AUTO_CAPTION_GERMAN",
            "italian": "AUTO_CAPTION_ITALIAN",
            "japanese": "AUTO_CAPTION_JAPANESE",
            "korean": "AUTO_CAPTION_KOREAN",
            "mandarin_simplified": "AUTO_CAPTION_MANDARIN_SIMPLIFIED",
            "mandarin_traditional": "AUTO_CAPTION_MANDARIN_TRADITIONAL",
            "portuguese": "AUTO_CAPTION_PORTUGUESE",
            "russian": "AUTO_CAPTION_RUSSIAN",
            "spanish": "AUTO_CAPTION_SPANISH",
            "danish": "AUTO_CAPTION_DANISH",
            "dutch": "AUTO_CAPTION_DUTCH",
            "norwegian": "AUTO_CAPTION_NORWEGIAN",
            "swedish": "AUTO_CAPTION_SWEDISH",
        }
        preset_map = {
            "default": "AUTO_CAPTION_SUBTITLE_DEFAULT",
            "teletext": "AUTO_CAPTION_TELETEXT",
            "netflix": "AUTO_CAPTION_NETFLIX",
        }
        line_break_map = {
            "single": "AUTO_CAPTION_LINE_SINGLE",
            "double": "AUTO_CAPTION_LINE_DOUBLE",
        }

        def _resolve_const(name):
            return getattr(resolve, name, None)

        lang_const = _resolve_const(language_map.get(language, "AUTO_CAPTION_AUTO"))
        preset_const = _resolve_const(preset_map.get(preset, "AUTO_CAPTION_SUBTITLE_DEFAULT"))
        lb_const = _resolve_const(line_break_map.get(line_break, "AUTO_CAPTION_LINE_SINGLE"))

        subtitle_lang_key = _resolve_const("SUBTITLE_LANGUAGE")
        subtitle_preset_key = _resolve_const("SUBTITLE_CAPTION_PRESET")
        subtitle_cpl_key = _resolve_const("SUBTITLE_CHARS_PER_LINE")
        subtitle_lb_key = _resolve_const("SUBTITLE_LINE_BREAK")
        subtitle_gap_key = _resolve_const("SUBTITLE_GAP")

        if subtitle_lang_key is None:
            return (
                "Subtitle constants not available on this Resolve version. "
                "Requires Resolve Studio 19+."
            )

        settings = {
            subtitle_lang_key: lang_const,
            subtitle_preset_key: preset_const,
            subtitle_cpl_key: chars_per_line,
            subtitle_lb_key: lb_const,
            subtitle_gap_key: gap,
        }

        result = timeline.CreateSubtitlesFromAudio(settings)
        return _ok(result, "Subtitles generated from audio successfully", "Failed to generate subtitles")
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════════════
#  FUSION (Compositing / VFX)
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def get_fusion_comp_list(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """
    Get all Fusion compositions associated with a timeline item.

    Parameters:
    - track_type/track_index/item_index: Clip locator
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        count = item.GetFusionCompCount() or 0
        names = item.GetFusionCompNameList() or []
        return json.dumps({
            "item_name": item.GetName(),
            "fusion_comp_count": count,
            "fusion_comp_names": list(names),
        }, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def add_fusion_comp(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """
    Add a new Fusion composition to a timeline item.

    Parameters:
    - track_type/track_index/item_index: Clip locator
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        comp = item.AddFusionComp()
        return _ok(comp, f"Fusion composition added to '{item.GetName()}'", "Failed to add Fusion composition")
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def import_fusion_comp(
    comp_path: str,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """
    Import a Fusion composition from file into a timeline item.

    Parameters:
    - comp_path: Absolute path to the .comp or .setting file
    - track_type/track_index/item_index: Clip locator
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        comp = item.ImportFusionComp(comp_path)
        return _ok(comp, f"Fusion comp imported from '{comp_path}'", "Failed to import Fusion composition. Check file path.")
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def export_fusion_comp(
    export_path: str,
    comp_index: int = 1,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """
    Export a Fusion composition from a timeline item to a file.

    Parameters:
    - export_path: Destination file path
    - comp_index: 1-based Fusion composition index (default: 1)
    - track_type/track_index/item_index: Clip locator
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        success = item.ExportFusionComp(export_path, comp_index)
        return _ok(success, f"Fusion comp {comp_index} exported to '{export_path}'", "Failed to export Fusion composition")
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def load_fusion_comp(
    comp_name: str,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """
    Load a named Fusion composition as the active composition.

    Parameters:
    - comp_name: Name of the Fusion composition to load
    - track_type/track_index/item_index: Clip locator
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        comp = item.LoadFusionCompByName(comp_name)
        return _ok(comp, f"Loaded Fusion composition '{comp_name}'", f"Failed to load '{comp_name}'")
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def delete_fusion_comp(
    comp_name: str,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """
    Delete a named Fusion composition from a timeline item.

    Parameters:
    - comp_name: Name of the Fusion composition to delete
    - track_type/track_index/item_index: Clip locator
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        success = item.DeleteFusionCompByName(comp_name)
        return _ok(success, f"Deleted Fusion composition '{comp_name}'", f"Failed to delete '{comp_name}'")
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def rename_fusion_comp(
    old_name: str,
    new_name: str,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """
    Rename a Fusion composition on a timeline item.

    Parameters:
    - old_name: Current name of the Fusion composition
    - new_name: New name for the composition
    - track_type/track_index/item_index: Clip locator
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        success = item.RenameFusionCompByName(old_name, new_name)
        return _ok(success, f"Renamed '{old_name}' to '{new_name}'", "Failed to rename Fusion composition")
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def create_fusion_clip(
    track_type: str = "video",
    track_index: int = 1,
    item_indices: Optional[List[int]] = None,
) -> str:
    """
    Create a Fusion clip from one or more timeline items.

    Parameters:
    - track_type: "video" (default)
    - track_index: 1-based track index (default: 1)
    - item_indices: List of 0-based item indices to merge. If None, uses all items.
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)

        all_items = timeline.GetItemListInTrack(track_type, track_index)
        if not all_items:
            return f"No items on {track_type} track {track_index}"

        if item_indices is not None:
            items = [all_items[i] for i in item_indices if 0 <= i < len(all_items)]
        else:
            items = list(all_items)

        if not items:
            return "No valid items selected"

        result = timeline.CreateFusionClip(items)
        return _ok(result, f"Fusion clip created from {len(items)} item(s)", "Failed to create Fusion clip")
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def insert_fusion_generator(generator_name: str) -> str:
    """
    Insert a Fusion generator into the current timeline at the playhead.

    Parameters:
    - generator_name: Name of the Fusion generator to insert
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        item = timeline.InsertFusionGeneratorIntoTimeline(generator_name)
        return _ok(item, f"Fusion generator '{generator_name}' inserted", f"Failed to insert generator '{generator_name}'")
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def insert_fusion_composition() -> str:
    """Insert a blank Fusion composition into the current timeline at the playhead."""
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        if not hasattr(timeline, 'InsertFusionCompositionIntoTimeline'):
            return "InsertFusionCompositionIntoTimeline is not available in this Resolve version."
        item = timeline.InsertFusionCompositionIntoTimeline()
        return _ok(item, "Fusion composition inserted into timeline", "Failed to insert Fusion composition")
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def insert_fusion_title(title_name: str) -> str:
    """
    Insert a Fusion title into the current timeline at the playhead.

    Parameters:
    - title_name: Name of the Fusion title template to insert
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        item = timeline.InsertFusionTitleIntoTimeline(title_name)
        return _ok(item, f"Fusion title '{title_name}' inserted", f"Failed to insert title '{title_name}'")
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════════════
#  TIMELINE EXPORT
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def export_timeline(
    file_path: str,
    export_type: str = "fcpxml_1_10",
    export_subtype: str = "none",
) -> str:
    """
    Export the current timeline to a file.

    Parameters:
    - file_path: Destination file path
    - export_type: One of "aaf", "drt", "edl", "fcp_7_xml", "fcpxml_1_8",
                   "fcpxml_1_9", "fcpxml_1_10", "hdr_10_profile_a",
                   "hdr_10_profile_b", "csv", "tab", "otio", "ale", "ale_cdl"
    - export_subtype: For AAF: "aaf_new" or "aaf_existing".
                      For EDL: "cdl", "sdl", "missing_clips", or "none".
    """
    try:
        conn = _conn()
        resolve = conn.get_resolve()
        timeline = _require_timeline(conn)

        type_map = {
            "aaf": "EXPORT_AAF",
            "drt": "EXPORT_DRT",
            "edl": "EXPORT_EDL",
            "fcp_7_xml": "EXPORT_FCP_7_XML",
            "fcpxml_1_8": "EXPORT_FCPXML_1_8",
            "fcpxml_1_9": "EXPORT_FCPXML_1_9",
            "fcpxml_1_10": "EXPORT_FCPXML_1_10",
            "hdr_10_profile_a": "EXPORT_HDR_10_PROFILE_A",
            "hdr_10_profile_b": "EXPORT_HDR_10_PROFILE_B",
            "csv": "EXPORT_TEXT_CSV",
            "tab": "EXPORT_TEXT_TAB",
            "otio": "EXPORT_OTIO",
            "ale": "EXPORT_ALE",
            "ale_cdl": "EXPORT_ALE_CDL",
        }
        subtype_map = {
            "none": "EXPORT_NONE",
            "aaf_new": "EXPORT_AAF_NEW",
            "aaf_existing": "EXPORT_AAF_EXISTING",
            "cdl": "EXPORT_CDL",
            "sdl": "EXPORT_SDL",
            "missing_clips": "EXPORT_MISSING_CLIPS",
        }

        type_const_name = type_map.get(export_type)
        sub_const_name = subtype_map.get(export_subtype, "EXPORT_NONE")

        if type_const_name is None:
            return f"Unknown export_type '{export_type}'. Valid: {list(type_map.keys())}"

        exp_type = getattr(resolve, type_const_name, None)
        exp_sub = getattr(resolve, sub_const_name, None)

        if exp_type is None:
            return f"Export constant '{type_const_name}' not available in this Resolve version."

        result = timeline.Export(file_path, exp_type, exp_sub)
        return _ok(result, f"Timeline exported to {file_path}", f"Failed to export timeline to {file_path}")
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════════════
#  THUMBNAIL / SCREENSHOT
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def get_current_thumbnail() -> Image:
    """
    Get a thumbnail of the current frame from the Color page.
    Must be on the Color page with a clip selected.
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)

        thumbnail_data = timeline.GetCurrentClipThumbnailImage()
        if not thumbnail_data or not isinstance(thumbnail_data, dict):
            raise RuntimeError(
                "No thumbnail available. Make sure you are on the Color page with a clip selected."
            )

        png_bytes = thumbnail_to_png_bytes(thumbnail_data)
        return Image(data=png_bytes, format="png")
    except Exception as e:
        raise RuntimeError(f"Error getting thumbnail: {e}")


@mcp.tool()
def export_current_frame(file_path: str) -> str:
    """
    Export the current frame as a still image.

    Parameters:
    - file_path: Destination file path (.png, .jpg, .tif, .dpx, .exr)
    """
    try:
        conn = _conn()
        project = conn.get_project()
        if not hasattr(project, 'ExportCurrentFrameAsStill'):
            return "ExportCurrentFrameAsStill is not available in this Resolve version."
        success = project.ExportCurrentFrameAsStill(file_path)
        return _ok(success, f"Current frame exported to {file_path}", "Failed to export frame. Check file path and extension.")
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════════════
#  AUDIO
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def get_voice_isolation_state(track_index: int) -> str:
    """
    Get the Voice Isolation state for an audio track.
    Requires DaVinci Resolve Studio.

    Parameters:
    - track_index: 1-based audio track index
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        if not hasattr(timeline, 'GetVoiceIsolationState'):
            return "Voice Isolation is not available in this Resolve version."
        state = timeline.GetVoiceIsolationState(track_index)
        return json.dumps(safe_serialize(state), indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def set_voice_isolation_state(
    track_index: int,
    enabled: bool,
    amount: int = 100,
) -> str:
    """
    Set Voice Isolation on an audio track to isolate speech from background noise.
    Requires DaVinci Resolve Studio.

    Parameters:
    - track_index: 1-based audio track index
    - enabled: True to enable, False to disable
    - amount: Isolation amount (0-100, default: 100)
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        if not hasattr(timeline, 'SetVoiceIsolationState'):
            return "Voice Isolation is not available in this Resolve version."
        success = timeline.SetVoiceIsolationState(
            track_index, {"isEnabled": enabled, "amount": amount}
        )
        state = "enabled" if enabled else "disabled"
        return _ok(success, f"Voice Isolation {state} (amount: {amount}) on audio track {track_index}", "Failed to set voice isolation state")
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════════════
#  CODE EXECUTION (POWER TOOL)
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def execute_resolve_code(code: str) -> str:
    """
    Execute arbitrary Python code in the DaVinci Resolve scripting environment.
    Use this for operations not covered by specific tools.

    Pre-loaded namespace variables:
    - resolve: The DaVinci Resolve object
    - project: The current project
    - mediaPool: The current media pool
    - timeline: The current timeline (may be None)
    - mediaStorage: The media storage object

    Use print() to output results, or set a variable named 'result'.

    Parameters:
    - code: Python code to execute
    """
    try:
        conn = _conn()
        return conn.execute_code(code)
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════════════
#  PROMPT: Editing Strategy
# ═══════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════
#  LOCAL TRANSCRIPTION (mlx-whisper on Apple Silicon)
#
#  Long files are auto-chunked with ffmpeg (5-min pieces) so each
#  whisper call finishes well within any MCP timeout.
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def transcribe_audio(
    file_path: str,
    model: str = DEFAULT_MODEL,
    language: Optional[str] = None,
    word_timestamps: bool = False,
    initial_prompt: Optional[str] = None,
) -> str:
    """
    Transcribe an audio/video file locally using mlx-whisper (Apple Silicon).
    Long files are automatically split into 5-minute chunks so it never times out.

    Returns ALL segments with timestamps inline (compact format) plus saves
    an SRT file next to the source for Resolve import.

    Parameters:
    - file_path: Absolute path to audio/video file (mp3, wav, m4a, mp4, mov, etc.)
    - model: "tiny" (fastest), "base", "small", "medium", "large" (most accurate),
             "turbo" (best speed/quality, default). Or a full HuggingFace repo path.
    - language: Language code (e.g. "en", "fr", "de", "ja"). None = auto-detect.
    - word_timestamps: Include word-level timestamps in output.
    - initial_prompt: Optional text to guide the model's vocabulary/style.
    """
    try:
        result = _transcribe(
            audio_path=file_path,
            model=model,
            language=language,
            word_timestamps=word_timestamps,
            initial_prompt=initial_prompt,
        )

        segments = result.get("segments", [])

        # Write SRT file next to the source for Resolve import
        base = os.path.splitext(file_path)[0]
        srt_path = base + ".srt"
        srt_content = segments_to_srt(segments)
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(srt_content)

        # Build compact timestamped transcript inline —
        # one line per segment: [MM:SS-MM:SS] text
        lines = []
        for s in segments:
            t0 = s["start"]
            t1 = s["end"]
            m0, s0 = int(t0 // 60), int(t0 % 60)
            m1, s1 = int(t1 // 60), int(t1 % 60)
            lines.append(f"[{m0:02d}:{s0:02d}-{m1:02d}:{s1:02d}] {s['text'].strip()}")

        transcript_block = "\n".join(lines)

        return (
            f"Language: {result.get('language', 'unknown')}\n"
            f"Segments: {len(segments)}\n"
            f"SRT saved: {srt_path}\n"
            f"\n{transcript_block}"
        )
    except ImportError:
        return (
            "mlx-whisper is not installed. Install with:\n"
            "  uv pip install 'mlx-whisper>=0.4.3'\n"
            "Or: pip install 'resolve-mcp[transcription]'"
        )
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def transcribe_and_add_subtitles(
    file_path: str,
    model: str = DEFAULT_MODEL,
    language: Optional[str] = None,
    initial_prompt: Optional[str] = None,
) -> str:
    """
    Transcribe audio locally with mlx-whisper and add subtitle markers to the timeline.
    Long files are auto-chunked so this works on any length.

    Parameters:
    - file_path: Absolute path to the audio/video file to transcribe
    - model: Whisper model size ("tiny", "base", "small", "medium", "large", "turbo")
    - language: Language code (e.g. "en", "fr"). None = auto-detect.
    - initial_prompt: Optional text to guide recognition vocabulary
    """
    try:
        result = _transcribe(
            audio_path=file_path,
            model=model,
            language=language,
            initial_prompt=initial_prompt,
        )

        segments = result.get("segments", [])
        if not segments:
            return "Transcription produced no segments. Check that the file contains speech."

        conn = _conn()
        timeline = _require_timeline(conn)

        fps_str = timeline.GetSetting("timelineFrameRate")
        fps = float(fps_str) if fps_str else 24.0
        timeline_start = timeline.GetStartFrame() or 0

        added = 0
        for seg in segments:
            frame_pos = timeline_start + int(seg["start"] * fps)
            duration_frames = max(1, int((seg["end"] - seg["start"]) * fps))
            text = seg["text"].strip()

            if timeline.AddMarker(frame_pos, "Cream", text, text, duration_frames, ""):
                added += 1

        srt_content = segments_to_srt(segments)

        return json.dumps({
            "language": result.get("language", "unknown"),
            "total_segments": len(segments),
            "markers_added": added,
            "srt_preview": srt_content[:2000],
            "note": (
                f"Added {added} timeline markers. "
                "Use export_srt() to save an SRT file for import as a subtitle track."
            ),
        }, indent=2, ensure_ascii=False)
    except ImportError:
        return "mlx-whisper is not installed. Install with: uv pip install 'mlx-whisper>=0.4.3'"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def export_srt(
    file_path: str,
    output_path: str,
    model: str = DEFAULT_MODEL,
    language: Optional[str] = None,
    initial_prompt: Optional[str] = None,
) -> str:
    """
    Transcribe audio and save as an SRT subtitle file.
    The SRT can then be imported into Resolve's subtitle track.

    Parameters:
    - file_path: Absolute path to the audio/video file to transcribe
    - output_path: Where to save the .srt file
    - model: Whisper model size ("tiny", "base", "small", "medium", "large", "turbo")
    - language: Language code or None for auto-detect
    - initial_prompt: Optional vocabulary/style hint
    """
    try:
        result = _transcribe(
            audio_path=file_path,
            model=model,
            language=language,
            initial_prompt=initial_prompt,
        )

        segments = result.get("segments", [])
        if not segments:
            return "Transcription produced no segments."

        srt = segments_to_srt(segments)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(srt)

        return json.dumps({
            "language": result.get("language", "unknown"),
            "segments": len(segments),
            "output_path": output_path,
            "note": "SRT saved. Import into Resolve: File > Import > Subtitle.",
        }, indent=2)
    except ImportError:
        return "mlx-whisper is not installed. Install with: uv pip install 'mlx-whisper>=0.4.3'"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def list_whisper_models() -> str:
    """List available mlx-whisper models with their HuggingFace repo paths."""
    return json.dumps({
        "models": {k: v for k, v in WHISPER_MODELS.items()},
        "default": DEFAULT_MODEL,
        "note": "First use of each model triggers a one-time download.",
    }, indent=2)


# ═══════════════════════════════════════════════════════════════════
#  PROMPT: Editing Strategy
# ═══════════════════════════════════════════════════════════════════

@mcp.prompt()
def editing_strategy() -> str:
    """Defines the recommended workflow for editing in DaVinci Resolve"""
    return """When working with DaVinci Resolve through MCP, follow this workflow:

    1. ALWAYS start by checking the current state:
       - Use get_project_info() to understand the project
       - Use get_current_timeline_info() to see the active timeline
       - Use get_current_page() to know which page you're on

    2. For media management:
       - Use get_media_pool_structure() to see available clips
       - Use import_media() to bring in new footage
       - Use create_timeline() to start a new edit
       - Use append_to_timeline() to add clips

    3. For editing operations:
       - Use get_timeline_items() to see what's on each track
       - Use set_timeline_item_property() for transforms (Pan, Tilt, Zoom, Opacity, Crop)
       - Use add_marker() to mark important points
       - Use set_current_timecode() to navigate

    4. For color grading (switch to Color page first):
       - Use get_node_graph() to see the current grade
       - Use set_lut() to apply LUTs
       - Use set_cdl() for CDL adjustments

    5. For transcription and subtitles (local, no Studio needed):
       - Use transcribe_audio() to transcribe any audio/video file locally via mlx-whisper
       - Use transcribe_and_add_subtitles() to transcribe and add markers to the timeline
       - Use export_srt() to save transcription as an SRT file for import
       - Use list_whisper_models() to see available model sizes

    6. For AI-powered features (Resolve Studio 19+ only):
       - Use detect_scene_cuts() to auto-detect cuts
       - Use create_magic_mask() for AI subject isolation
       - Use smart_reframe() for automatic reframing
       - Use stabilize() for clip stabilization
       - Use create_subtitles_from_audio() for Resolve's built-in AI subtitles
       - Use set_voice_isolation_state() to isolate speech

    7. For rendering:
       - Use get_render_formats() to see available options
       - Use set_render_settings() to configure output
       - Use add_render_job() then start_rendering()
       - Use get_render_status() to monitor progress

    8. For Fusion (compositing/VFX):
       - Use get_fusion_comp_list() to see existing compositions
       - Use add_fusion_comp() to create a new composition
       - Use import_fusion_comp() / export_fusion_comp() for .comp files
       - Use create_fusion_clip() to merge clips into a Fusion composition
       - Use insert_fusion_generator() / insert_fusion_title() for generators and titles
       - For advanced Fusion node manipulation, use execute_resolve_code()

    9. For anything not covered by specific tools:
       - Use execute_resolve_code() to run arbitrary Python
       - The Resolve Python API is comprehensive — most operations are possible

    IMPORTANT: DaVinci Resolve must be running for all tools to work.
    Some features require the Color page. AI features require Resolve Studio 19+.
    """


# ── Entry point ──

def main():
    """Run the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
