"""
DaVinci Resolve MCP Server

A FastMCP server that exposes DaVinci Resolve Studio's scripting API
as MCP tools, allowing Claude to control Resolve via natural language.
"""

from mcp.server.fastmcp import FastMCP, Context, Image
import json
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Any, List

from .connection import get_resolve_connection
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

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("ResolveMCP")


# ── Lifespan ──

@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Manage server startup and shutdown lifecycle."""
    try:
        logger.info("ResolveMCP server starting up")
        try:
            conn = get_resolve_connection()
            project = conn.get_project()
            logger.info("Connected to Resolve — project: %s", project.GetName())
        except Exception as e:
            logger.warning("Could not connect to Resolve on startup: %s", e)
            logger.warning("Make sure DaVinci Resolve is running before using tools")
        yield {}
    finally:
        logger.info("ResolveMCP server shut down")


# ── FastMCP instance ──

mcp = FastMCP("ResolveMCP", lifespan=server_lifespan)


# ═══════════════════════════════════════════════════════════════════
#  HELPER: get a timeline item by track/index
# ═══════════════════════════════════════════════════════════════════

def _get_timeline_item(track_type: str, track_index: int, item_index: int):
    """Get a specific TimelineItem from the current timeline."""
    conn = get_resolve_connection()
    timeline = conn.get_current_timeline()
    if timeline is None:
        raise RuntimeError("No active timeline")

    items = timeline.GetItemListInTrack(track_type, track_index)
    if not items:
        raise RuntimeError(
            f"No items found on {track_type} track {track_index}"
        )
    if item_index < 0 or item_index >= len(items):
        raise RuntimeError(
            f"Item index {item_index} out of range (0-{len(items) - 1})"
        )
    return items[item_index]


# ═══════════════════════════════════════════════════════════════════
#  PROJECT & NAVIGATION
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def get_project_info(ctx: Context) -> str:
    """
    Get information about the current DaVinci Resolve project.

    Returns project name, settings (frame rate, resolution),
    timeline count, current page, and version info.
    """
    try:
        conn = get_resolve_connection()
        resolve = conn.get_resolve()
        project = conn.get_project()

        info = {
            "project_name": project.GetName(),
            "resolve_version": resolve.GetVersionString(),
            "current_page": resolve.GetCurrentPage(),
            "timeline_count": project.GetTimelineCount(),
        }

        # Key project settings
        for key in (
            "timelineFrameRate",
            "timelineResolutionWidth",
            "timelineResolutionHeight",
            "timelinePlaybackFrameRate",
            "videoCaptureCodec",
            "audioCaptureCodec",
        ):
            val = project.GetSetting(key)
            if val:
                info[key] = val

        return json.dumps(info, indent=2)
    except Exception as e:
        return f"Error getting project info: {e}"


@mcp.tool()
def open_page(ctx: Context, page: str) -> str:
    """
    Switch to a specific page in DaVinci Resolve.

    Parameters:
    - page: One of "media", "cut", "edit", "fusion", "color", "fairlight", "deliver"
    """
    valid_pages = ("media", "cut", "edit", "fusion", "color", "fairlight", "deliver")
    if page not in valid_pages:
        return f"Invalid page '{page}'. Must be one of: {', '.join(valid_pages)}"
    try:
        conn = get_resolve_connection()
        resolve = conn.get_resolve()
        success = resolve.OpenPage(page)
        if success:
            return f"Switched to {page} page"
        return f"Failed to switch to {page} page"
    except Exception as e:
        return f"Error switching page: {e}"


@mcp.tool()
def get_current_page(ctx: Context) -> str:
    """Get the currently active page in DaVinci Resolve."""
    try:
        conn = get_resolve_connection()
        resolve = conn.get_resolve()
        page = resolve.GetCurrentPage()
        return page or "unknown"
    except Exception as e:
        return f"Error getting current page: {e}"


# ═══════════════════════════════════════════════════════════════════
#  MEDIA POOL
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def get_media_pool_structure(ctx: Context, max_depth: int = 3, max_clips: int = 50) -> str:
    """
    Get the folder/clip structure of the media pool.

    Parameters:
    - max_depth: Maximum folder recursion depth (default: 3)
    - max_clips: Maximum clips to list per folder (default: 50)
    """
    try:
        conn = get_resolve_connection()
        mp = conn.get_media_pool()
        root = mp.GetRootFolder()
        structure = folder_to_dict(root, max_depth, max_clips)
        return json.dumps(structure, indent=2, default=str)
    except Exception as e:
        return f"Error getting media pool structure: {e}"


@mcp.tool()
def import_media(ctx: Context, file_paths: list[str]) -> str:
    """
    Import media files into the current media pool folder.

    Parameters:
    - file_paths: List of absolute file paths to import
    """
    try:
        conn = get_resolve_connection()
        mp = conn.get_media_pool()
        items = mp.ImportMedia(file_paths)
        if items:
            names = [item.GetName() for item in items]
            return json.dumps({
                "imported": len(names),
                "clips": names,
            }, indent=2)
        return "No media was imported. Check file paths are valid."
    except Exception as e:
        return f"Error importing media: {e}"


@mcp.tool()
def create_timeline(ctx: Context, name: str) -> str:
    """
    Create a new empty timeline in the current project.

    Parameters:
    - name: Name for the new timeline
    """
    try:
        conn = get_resolve_connection()
        mp = conn.get_media_pool()
        timeline = mp.CreateEmptyTimeline(name)
        if timeline:
            return json.dumps(timeline_to_dict(timeline), indent=2)
        return f"Failed to create timeline '{name}'. Name may already exist."
    except Exception as e:
        return f"Error creating timeline: {e}"


# ═══════════════════════════════════════════════════════════════════
#  TIMELINE
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def get_current_timeline_info(ctx: Context) -> str:
    """Get detailed information about the current timeline."""
    try:
        conn = get_resolve_connection()
        timeline = conn.get_current_timeline()
        if timeline is None:
            return "No active timeline"
        return json.dumps(timeline_to_dict(timeline), indent=2, default=str)
    except Exception as e:
        return f"Error getting timeline info: {e}"


@mcp.tool()
def get_timeline_items(ctx: Context, track_type: str, track_index: int) -> str:
    """
    List all clips/items on a specific track of the current timeline.

    Parameters:
    - track_type: "video", "audio", or "subtitle"
    - track_index: 1-based track index
    """
    try:
        conn = get_resolve_connection()
        timeline = conn.get_current_timeline()
        if timeline is None:
            return "No active timeline"

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
        return f"Error getting timeline items: {e}"


@mcp.tool()
def append_to_timeline(ctx: Context, clip_names: list[str]) -> str:
    """
    Append media pool clips to the current timeline by name.

    Parameters:
    - clip_names: List of clip names to append (must exist in the current media pool folder)
    """
    try:
        conn = get_resolve_connection()
        mp = conn.get_media_pool()
        folder = mp.GetCurrentFolder()
        all_clips = folder.GetClipList() or []

        # Find matching clips
        name_to_clip = {}
        for clip in all_clips:
            name_to_clip[clip.GetName()] = clip

        clips_to_add = []
        not_found = []
        for name in clip_names:
            if name in name_to_clip:
                clips_to_add.append(name_to_clip[name])
            else:
                not_found.append(name)

        if not clips_to_add:
            return f"No matching clips found. Not found: {not_found}"

        result = mp.AppendToTimeline(clips_to_add)
        output = {"appended": len(clips_to_add)}
        if not_found:
            output["not_found"] = not_found
        if result:
            output["timeline_items"] = [item.GetName() for item in result]
        return json.dumps(output, indent=2)
    except Exception as e:
        return f"Error appending to timeline: {e}"


@mcp.tool()
def add_marker(
    ctx: Context,
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
    - color: Marker color ("Red", "Orange", "Yellow", "Green", "Cyan", "Blue", "Purple", "Pink", "Fuchsia", "Rose", "Lavender", "Sky", "Mint", "Lemon", "Sand", "Cocoa", "Cream")
    - name: Marker name
    - note: Optional note text
    - duration: Marker duration in frames (default: 1)
    - custom_data: Optional custom data string
    """
    try:
        conn = get_resolve_connection()
        timeline = conn.get_current_timeline()
        if timeline is None:
            return "No active timeline"

        success = timeline.AddMarker(frame_id, color, name, note, duration, custom_data)
        if success:
            return f"Marker '{name}' added at frame {frame_id}"
        return "Failed to add marker"
    except Exception as e:
        return f"Error adding marker: {e}"


@mcp.tool()
def get_markers(ctx: Context) -> str:
    """Get all markers on the current timeline."""
    try:
        conn = get_resolve_connection()
        timeline = conn.get_current_timeline()
        if timeline is None:
            return "No active timeline"

        markers = timeline.GetMarkers()
        if not markers:
            return "No markers on timeline"
        return json.dumps({str(k): v for k, v in markers.items()}, indent=2, default=str)
    except Exception as e:
        return f"Error getting markers: {e}"


@mcp.tool()
def set_current_timecode(ctx: Context, timecode: str) -> str:
    """
    Move the playhead to a specific timecode.

    Parameters:
    - timecode: Timecode string in "HH:MM:SS:FF" format
    """
    try:
        conn = get_resolve_connection()
        timeline = conn.get_current_timeline()
        if timeline is None:
            return "No active timeline"

        success = timeline.SetCurrentTimecode(timecode)
        if success:
            return f"Playhead moved to {timecode}"
        return f"Failed to set timecode to {timecode}"
    except Exception as e:
        return f"Error setting timecode: {e}"


@mcp.tool()
def get_current_timecode(ctx: Context) -> str:
    """Get the current playhead timecode."""
    try:
        conn = get_resolve_connection()
        timeline = conn.get_current_timeline()
        if timeline is None:
            return "No active timeline"
        return timeline.GetCurrentTimecode()
    except Exception as e:
        return f"Error getting timecode: {e}"


# ═══════════════════════════════════════════════════════════════════
#  TIMELINE ITEM PROPERTIES
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def get_timeline_item_properties(
    ctx: Context, track_type: str, track_index: int, item_index: int
) -> str:
    """
    Get all properties of a specific timeline item.

    Parameters:
    - track_type: "video", "audio", or "subtitle"
    - track_index: 1-based track index
    - item_index: 0-based index of the item in the track
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        return json.dumps(timeline_item_full_dict(item), indent=2, default=str)
    except Exception as e:
        return f"Error getting item properties: {e}"


@mcp.tool()
def set_timeline_item_property(
    ctx: Context,
    track_type: str,
    track_index: int,
    item_index: int,
    property_key: str,
    property_value: str,
) -> str:
    """
    Set a property on a specific timeline item.

    Parameters:
    - track_type: "video", "audio", or "subtitle"
    - track_index: 1-based track index
    - item_index: 0-based index of the item in the track
    - property_key: Property name (e.g. "Pan", "Tilt", "ZoomX", "ZoomY", "Opacity",
                    "CropLeft", "CropRight", "CropTop", "CropBottom", "RotationAngle",
                    "FlipX", "FlipY", "CompositeMode", "RetimeProcess", "Scaling", etc.)
    - property_value: Value to set (will be auto-converted to appropriate type)
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
            if property_value.lower() in ("true", "false"):
                value = property_value.lower() == "true"

        success = item.SetProperty(property_key, value)
        if success:
            return f"Set {property_key} = {value} on item {item_index}"
        return f"Failed to set {property_key}. Check key name and value range."
    except Exception as e:
        return f"Error setting property: {e}"


# ═══════════════════════════════════════════════════════════════════
#  COLOR GRADING
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def get_node_graph(
    ctx: Context,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """
    Get the color grading node graph info for a timeline item.

    Parameters:
    - track_type: "video", "audio", or "subtitle" (default: "video")
    - track_index: 1-based track index (default: 1)
    - item_index: 0-based item index (default: 0, i.e. current/first clip)
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        graph = item.GetNodeGraph()
        if graph is None:
            return "No node graph available for this item"
        return json.dumps(node_graph_to_dict(graph), indent=2, default=str)
    except Exception as e:
        return f"Error getting node graph: {e}"


@mcp.tool()
def set_lut(
    ctx: Context,
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
    - lut_path: Absolute or relative path to the LUT file
    - track_type: "video" (default)
    - track_index: 1-based track index (default: 1)
    - item_index: 0-based item index (default: 0)
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        graph = item.GetNodeGraph()
        if graph is None:
            return "No node graph available"
        success = graph.SetLUT(node_index, lut_path)
        if success:
            return f"LUT applied to node {node_index}: {lut_path}"
        return "Failed to apply LUT. Check node index and LUT path."
    except Exception as e:
        return f"Error setting LUT: {e}"


@mcp.tool()
def set_cdl(
    ctx: Context,
    node_index: int,
    slope: str = "1.0 1.0 1.0",
    offset: str = "0.0 0.0 0.0",
    power: str = "1.0 1.0 1.0",
    saturation: str = "1.0",
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """
    Apply CDL (Color Decision List) values to a node.

    Parameters:
    - node_index: 1-based node index
    - slope: RGB slope values as space-separated string (default: "1.0 1.0 1.0")
    - offset: RGB offset values (default: "0.0 0.0 0.0")
    - power: RGB power values (default: "1.0 1.0 1.0")
    - saturation: Saturation value (default: "1.0")
    - track_type/track_index/item_index: Clip locator (defaults to first clip on video track 1)
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        success = item.SetCDL({
            "NodeIndex": str(node_index),
            "Slope": slope,
            "Offset": offset,
            "Power": power,
            "Saturation": saturation,
        })
        if success:
            return f"CDL applied to node {node_index}"
        return "Failed to apply CDL values"
    except Exception as e:
        return f"Error setting CDL: {e}"


# ═══════════════════════════════════════════════════════════════════
#  RENDERING
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def get_render_formats(ctx: Context, render_format: str = None) -> str:
    """
    Get available render formats and codecs.

    Parameters:
    - render_format: If provided, returns codecs for that format. Otherwise returns all formats.
    """
    try:
        conn = get_resolve_connection()
        project = conn.get_project()

        if render_format:
            codecs = project.GetRenderCodecs(render_format)
            return json.dumps({"format": render_format, "codecs": codecs}, indent=2, default=str)

        formats = project.GetRenderFormats()
        return json.dumps({"formats": formats}, indent=2, default=str)
    except Exception as e:
        return f"Error getting render formats: {e}"


@mcp.tool()
def get_render_settings(ctx: Context) -> str:
    """Get current render format, codec, render job list, and render presets."""
    try:
        conn = get_resolve_connection()
        project = conn.get_project()

        result = {}

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
        return f"Error getting render settings: {e}"


@mcp.tool()
def set_render_settings(
    ctx: Context,
    settings: dict = None,
    render_format: str = None,
    codec: str = None,
) -> str:
    """
    Configure render settings for the current project.

    Parameters:
    - settings: Dict of render settings. Supported keys include:
        "TargetDir", "CustomName", "SelectAllFrames" (bool), "MarkIn" (int),
        "MarkOut" (int), "ExportVideo" (bool), "ExportAudio" (bool),
        "FormatWidth" (int), "FormatHeight" (int), "FrameRate" (float),
        "VideoQuality", "AudioCodec", "AudioBitDepth", "AudioSampleRate",
        "ColorSpaceTag", "GammaTag", "ExportAlpha" (bool), etc.
    - render_format: Render format (e.g. "mp4", "mov"). Set with codec.
    - codec: Render codec (e.g. "H.264", "H.265", "ProRes 422 HQ")
    """
    try:
        conn = get_resolve_connection()
        project = conn.get_project()
        results = {}

        if render_format and codec:
            success = project.SetCurrentRenderFormatAndCodec(render_format, codec)
            results["format_codec"] = "set" if success else "failed"

        if settings:
            success = project.SetRenderSettings(settings)
            results["settings"] = "set" if success else "failed"

        return json.dumps(results, indent=2)
    except Exception as e:
        return f"Error setting render settings: {e}"


@mcp.tool()
def add_render_job(ctx: Context) -> str:
    """Add a render job to the queue based on current render settings."""
    try:
        conn = get_resolve_connection()
        project = conn.get_project()
        job_id = project.AddRenderJob()
        if job_id:
            return json.dumps({"job_id": job_id})
        return "Failed to add render job. Check render settings."
    except Exception as e:
        return f"Error adding render job: {e}"


@mcp.tool()
def start_rendering(ctx: Context, job_ids: list[str] = None) -> str:
    """
    Start rendering queued jobs.

    Parameters:
    - job_ids: Optional list of job IDs to render. If None, renders all queued jobs.
    """
    try:
        conn = get_resolve_connection()
        project = conn.get_project()
        if job_ids:
            success = project.StartRendering(job_ids)
        else:
            success = project.StartRendering()
        if success:
            return "Rendering started"
        return "Failed to start rendering"
    except Exception as e:
        return f"Error starting render: {e}"


@mcp.tool()
def get_render_status(ctx: Context, job_id: str) -> str:
    """
    Get the status of a render job.

    Parameters:
    - job_id: The render job ID (returned by add_render_job)
    """
    try:
        conn = get_resolve_connection()
        project = conn.get_project()
        status = project.GetRenderJobStatus(job_id)
        return json.dumps(safe_serialize(status), indent=2)
    except Exception as e:
        return f"Error getting render status: {e}"


@mcp.tool()
def stop_rendering(ctx: Context) -> str:
    """Stop any currently running render processes."""
    try:
        conn = get_resolve_connection()
        project = conn.get_project()
        project.StopRendering()
        return "Rendering stopped"
    except Exception as e:
        return f"Error stopping render: {e}"


# ═══════════════════════════════════════════════════════════════════
#  AI / NEURAL ENGINE FEATURES
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def create_magic_mask(
    ctx: Context,
    mode: str = "F",
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """
    Create an AI-powered Magic Mask on a timeline item for subject isolation.
    Uses DaVinci Neural Engine to detect and isolate subjects (people, objects).

    Parameters:
    - mode: "F" (forward), "B" (backward), or "BI" (bidirectional)
    - track_type/track_index/item_index: Clip locator
    """
    valid_modes = ("F", "B", "BI")
    if mode not in valid_modes:
        return f"Invalid mode '{mode}'. Must be one of: {', '.join(valid_modes)}"
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        success = item.CreateMagicMask(mode)
        if success:
            return f"Magic Mask created (mode: {mode}) on '{item.GetName()}'"
        return "Failed to create Magic Mask"
    except Exception as e:
        return f"Error creating Magic Mask: {e}"


@mcp.tool()
def regenerate_magic_mask(
    ctx: Context,
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
        success = item.RegenerateMagicMask()
        if success:
            return f"Magic Mask regenerated on '{item.GetName()}'"
        return "Failed to regenerate Magic Mask"
    except Exception as e:
        return f"Error regenerating Magic Mask: {e}"


@mcp.tool()
def smart_reframe(
    ctx: Context,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """
    Apply Smart Reframe to a timeline item.
    Uses AI to automatically reframe content for different aspect ratios.

    Parameters:
    - track_type/track_index/item_index: Clip locator
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        success = item.SmartReframe()
        if success:
            return f"Smart Reframe applied to '{item.GetName()}'"
        return "Failed to apply Smart Reframe"
    except Exception as e:
        return f"Error applying Smart Reframe: {e}"


@mcp.tool()
def stabilize(
    ctx: Context,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """
    Apply stabilization to a timeline item using DaVinci Neural Engine.

    Parameters:
    - track_type/track_index/item_index: Clip locator
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        success = item.Stabilize()
        if success:
            return f"Stabilization applied to '{item.GetName()}'"
        return "Failed to stabilize"
    except Exception as e:
        return f"Error stabilizing: {e}"


@mcp.tool()
def detect_scene_cuts(ctx: Context) -> str:
    """
    Detect scene cuts in the current timeline using AI.
    Automatically finds and creates cuts at scene boundaries.
    """
    try:
        conn = get_resolve_connection()
        timeline = conn.get_current_timeline()
        if timeline is None:
            return "No active timeline"
        success = timeline.DetectSceneCuts()
        if success:
            return "Scene cuts detected and applied to timeline"
        return "Failed to detect scene cuts"
    except Exception as e:
        return f"Error detecting scene cuts: {e}"


@mcp.tool()
def create_subtitles_from_audio(
    ctx: Context,
    language: str = "auto",
    preset: str = "default",
    chars_per_line: int = 42,
    line_break: str = "single",
    gap: int = 0,
) -> str:
    """
    Generate subtitles from audio using AI speech recognition.

    Parameters:
    - language: Language code — "auto", "english", "french", "german", "italian",
                "japanese", "korean", "mandarin_simplified", "mandarin_traditional",
                "portuguese", "russian", "spanish", "danish", "dutch", "norwegian", "swedish"
    - preset: "default", "teletext", or "netflix"
    - chars_per_line: Characters per line (1-60, default: 42)
    - line_break: "single" or "double"
    - gap: Gap between subtitles in frames (0-10, default: 0)
    """
    try:
        conn = get_resolve_connection()
        timeline = conn.get_current_timeline()
        if timeline is None:
            return "No active timeline"

        # Build settings dict using Resolve constants
        # We pass the string identifiers and let execute_code handle the constants
        settings_code = f"""
import DaVinciResolveScript as dvr
resolve = dvr.scriptapp("Resolve")

language_map = {{
    "auto": resolve.AUTO_CAPTION_AUTO,
    "english": resolve.AUTO_CAPTION_ENGLISH,
    "french": resolve.AUTO_CAPTION_FRENCH,
    "german": resolve.AUTO_CAPTION_GERMAN,
    "italian": resolve.AUTO_CAPTION_ITALIAN,
    "japanese": resolve.AUTO_CAPTION_JAPANESE,
    "korean": resolve.AUTO_CAPTION_KOREAN,
    "mandarin_simplified": resolve.AUTO_CAPTION_MANDARIN_SIMPLIFIED,
    "mandarin_traditional": resolve.AUTO_CAPTION_MANDARIN_TRADITIONAL,
    "portuguese": resolve.AUTO_CAPTION_PORTUGUESE,
    "russian": resolve.AUTO_CAPTION_RUSSIAN,
    "spanish": resolve.AUTO_CAPTION_SPANISH,
    "danish": resolve.AUTO_CAPTION_DANISH,
    "dutch": resolve.AUTO_CAPTION_DUTCH,
    "norwegian": resolve.AUTO_CAPTION_NORWEGIAN,
    "swedish": resolve.AUTO_CAPTION_SWEDISH,
}}

preset_map = {{
    "default": resolve.AUTO_CAPTION_SUBTITLE_DEFAULT,
    "teletext": resolve.AUTO_CAPTION_TELETEXT,
    "netflix": resolve.AUTO_CAPTION_NETFLIX,
}}

line_break_map = {{
    "single": resolve.AUTO_CAPTION_LINE_SINGLE,
    "double": resolve.AUTO_CAPTION_LINE_DOUBLE,
}}

settings = {{
    resolve.SUBTITLE_LANGUAGE: language_map.get("{language}", resolve.AUTO_CAPTION_AUTO),
    resolve.SUBTITLE_CAPTION_PRESET: preset_map.get("{preset}", resolve.AUTO_CAPTION_SUBTITLE_DEFAULT),
    resolve.SUBTITLE_CHARS_PER_LINE: {chars_per_line},
    resolve.SUBTITLE_LINE_BREAK: line_break_map.get("{line_break}", resolve.AUTO_CAPTION_LINE_SINGLE),
    resolve.SUBTITLE_GAP: {gap},
}}

timeline = resolve.GetProjectManager().GetCurrentProject().GetCurrentTimeline()
result = timeline.CreateSubtitlesFromAudio(settings)
print("SUCCESS" if result else "FAILED")
"""
        conn_obj = get_resolve_connection()
        output = conn_obj.execute_code(settings_code)
        if "SUCCESS" in output:
            return "Subtitles generated from audio successfully"
        return f"Failed to generate subtitles: {output}"
    except Exception as e:
        return f"Error generating subtitles: {e}"


# ═══════════════════════════════════════════════════════════════════
#  FUSION (Compositing / VFX)
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def get_fusion_comp_list(
    ctx: Context,
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
        count = item.GetFusionCompCount()
        names = item.GetFusionCompNameList() or []
        return json.dumps({
            "item_name": item.GetName(),
            "fusion_comp_count": count,
            "fusion_comp_names": names,
        }, indent=2)
    except Exception as e:
        return f"Error getting Fusion comps: {e}"


@mcp.tool()
def add_fusion_comp(
    ctx: Context,
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
        if comp:
            return f"Fusion composition added to '{item.GetName()}'"
        return "Failed to add Fusion composition"
    except Exception as e:
        return f"Error adding Fusion comp: {e}"


@mcp.tool()
def import_fusion_comp(
    ctx: Context,
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
        if comp:
            return f"Fusion composition imported from '{comp_path}' to '{item.GetName()}'"
        return "Failed to import Fusion composition. Check file path."
    except Exception as e:
        return f"Error importing Fusion comp: {e}"


@mcp.tool()
def export_fusion_comp(
    ctx: Context,
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
        if success:
            return f"Fusion composition {comp_index} exported to '{export_path}'"
        return "Failed to export Fusion composition"
    except Exception as e:
        return f"Error exporting Fusion comp: {e}"


@mcp.tool()
def load_fusion_comp(
    ctx: Context,
    comp_name: str,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """
    Load a named Fusion composition as the active composition for a timeline item.

    Parameters:
    - comp_name: Name of the Fusion composition to load
    - track_type/track_index/item_index: Clip locator
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        comp = item.LoadFusionCompByName(comp_name)
        if comp:
            return f"Loaded Fusion composition '{comp_name}'"
        return f"Failed to load Fusion composition '{comp_name}'"
    except Exception as e:
        return f"Error loading Fusion comp: {e}"


@mcp.tool()
def delete_fusion_comp(
    ctx: Context,
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
        if success:
            return f"Deleted Fusion composition '{comp_name}'"
        return f"Failed to delete Fusion composition '{comp_name}'"
    except Exception as e:
        return f"Error deleting Fusion comp: {e}"


@mcp.tool()
def rename_fusion_comp(
    ctx: Context,
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
        if success:
            return f"Renamed Fusion composition '{old_name}' to '{new_name}'"
        return f"Failed to rename Fusion composition"
    except Exception as e:
        return f"Error renaming Fusion comp: {e}"


@mcp.tool()
def create_fusion_clip(
    ctx: Context,
    track_type: str = "video",
    track_index: int = 1,
    item_indices: list[int] = None,
) -> str:
    """
    Create a Fusion clip from one or more timeline items.
    Merges the specified items into a single Fusion composition.

    Parameters:
    - track_type: "video" (default)
    - track_index: 1-based track index (default: 1)
    - item_indices: List of 0-based item indices to merge. If None, uses all items.
    """
    try:
        conn = get_resolve_connection()
        timeline = conn.get_current_timeline()
        if timeline is None:
            return "No active timeline"

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
        if result:
            return f"Fusion clip created from {len(items)} item(s)"
        return "Failed to create Fusion clip"
    except Exception as e:
        return f"Error creating Fusion clip: {e}"


@mcp.tool()
def insert_fusion_generator(ctx: Context, generator_name: str) -> str:
    """
    Insert a Fusion generator into the current timeline.

    Parameters:
    - generator_name: Name of the Fusion generator to insert
    """
    try:
        conn = get_resolve_connection()
        timeline = conn.get_current_timeline()
        if timeline is None:
            return "No active timeline"
        item = timeline.InsertFusionGeneratorIntoTimeline(generator_name)
        if item:
            return f"Fusion generator '{generator_name}' inserted into timeline"
        return f"Failed to insert Fusion generator '{generator_name}'"
    except Exception as e:
        return f"Error inserting Fusion generator: {e}"


@mcp.tool()
def insert_fusion_composition(ctx: Context) -> str:
    """Insert a blank Fusion composition into the current timeline at the playhead."""
    try:
        conn = get_resolve_connection()
        timeline = conn.get_current_timeline()
        if timeline is None:
            return "No active timeline"
        item = timeline.InsertFusionCompositionIntoTimeline()
        if item:
            return "Fusion composition inserted into timeline"
        return "Failed to insert Fusion composition"
    except Exception as e:
        return f"Error inserting Fusion composition: {e}"


@mcp.tool()
def insert_fusion_title(ctx: Context, title_name: str) -> str:
    """
    Insert a Fusion title into the current timeline.

    Parameters:
    - title_name: Name of the Fusion title template to insert
    """
    try:
        conn = get_resolve_connection()
        timeline = conn.get_current_timeline()
        if timeline is None:
            return "No active timeline"
        item = timeline.InsertFusionTitleIntoTimeline(title_name)
        if item:
            return f"Fusion title '{title_name}' inserted into timeline"
        return f"Failed to insert Fusion title '{title_name}'"
    except Exception as e:
        return f"Error inserting Fusion title: {e}"


# ═══════════════════════════════════════════════════════════════════
#  TIMELINE EXPORT
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def export_timeline(
    ctx: Context,
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
                      For others: "none".
    """
    try:
        # Map string types to resolve constants via execute_code
        code = f"""
import DaVinciResolveScript as dvr
resolve = dvr.scriptapp("Resolve")

type_map = {{
    "aaf": resolve.EXPORT_AAF,
    "drt": resolve.EXPORT_DRT,
    "edl": resolve.EXPORT_EDL,
    "fcp_7_xml": resolve.EXPORT_FCP_7_XML,
    "fcpxml_1_8": resolve.EXPORT_FCPXML_1_8,
    "fcpxml_1_9": resolve.EXPORT_FCPXML_1_9,
    "fcpxml_1_10": resolve.EXPORT_FCPXML_1_10,
    "hdr_10_profile_a": resolve.EXPORT_HDR_10_PROFILE_A,
    "hdr_10_profile_b": resolve.EXPORT_HDR_10_PROFILE_B,
    "csv": resolve.EXPORT_TEXT_CSV,
    "tab": resolve.EXPORT_TEXT_TAB,
    "otio": resolve.EXPORT_OTIO,
    "ale": resolve.EXPORT_ALE,
    "ale_cdl": resolve.EXPORT_ALE_CDL,
}}

subtype_map = {{
    "none": resolve.EXPORT_NONE,
    "aaf_new": resolve.EXPORT_AAF_NEW,
    "aaf_existing": resolve.EXPORT_AAF_EXISTING,
    "cdl": resolve.EXPORT_CDL,
    "sdl": resolve.EXPORT_SDL,
    "missing_clips": resolve.EXPORT_MISSING_CLIPS,
}}

timeline = resolve.GetProjectManager().GetCurrentProject().GetCurrentTimeline()
exp_type = type_map.get("{export_type}")
exp_sub = subtype_map.get("{export_subtype}", resolve.EXPORT_NONE)
result = timeline.Export("{file_path}", exp_type, exp_sub)
print("SUCCESS" if result else "FAILED")
"""
        conn = get_resolve_connection()
        output = conn.execute_code(code)
        if "SUCCESS" in output:
            return f"Timeline exported to {file_path}"
        return f"Failed to export timeline: {output}"
    except Exception as e:
        return f"Error exporting timeline: {e}"


# ═══════════════════════════════════════════════════════════════════
#  THUMBNAIL / SCREENSHOT
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def get_current_thumbnail(ctx: Context) -> Image:
    """
    Get a thumbnail of the current frame from the Color page.
    Must be on the Color page with a clip selected.

    Returns the thumbnail as an Image.
    """
    try:
        conn = get_resolve_connection()
        timeline = conn.get_current_timeline()
        if timeline is None:
            raise RuntimeError("No active timeline")

        thumbnail_data = timeline.GetCurrentClipThumbnailImage()
        if not thumbnail_data or len(thumbnail_data) == 0:
            raise RuntimeError(
                "No thumbnail available. Make sure you're on the Color page with a clip selected."
            )

        png_bytes = thumbnail_to_png_bytes(thumbnail_data)
        return Image(data=png_bytes, format="png")
    except Exception as e:
        raise RuntimeError(f"Error getting thumbnail: {e}")


@mcp.tool()
def export_current_frame(ctx: Context, file_path: str) -> str:
    """
    Export the current frame as a still image.

    Parameters:
    - file_path: Destination file path (must end with a valid image extension: .png, .jpg, .tif, .dpx, .exr)
    """
    try:
        conn = get_resolve_connection()
        project = conn.get_project()
        success = project.ExportCurrentFrameAsStill(file_path)
        if success:
            return f"Current frame exported to {file_path}"
        return "Failed to export frame. Check file path and extension."
    except Exception as e:
        return f"Error exporting frame: {e}"


# ═══════════════════════════════════════════════════════════════════
#  AUDIO
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def get_voice_isolation_state(ctx: Context, track_index: int) -> str:
    """
    Get the Voice Isolation state for an audio track.

    Parameters:
    - track_index: 1-based audio track index
    """
    try:
        conn = get_resolve_connection()
        timeline = conn.get_current_timeline()
        if timeline is None:
            return "No active timeline"
        state = timeline.GetVoiceIsolationState(track_index)
        return json.dumps(safe_serialize(state), indent=2)
    except Exception as e:
        return f"Error getting voice isolation state: {e}"


@mcp.tool()
def set_voice_isolation_state(
    ctx: Context, track_index: int, enabled: bool, amount: int = 100
) -> str:
    """
    Set Voice Isolation on an audio track to isolate speech from background noise.

    Parameters:
    - track_index: 1-based audio track index
    - enabled: True to enable, False to disable
    - amount: Isolation amount (0-100, default: 100)
    """
    try:
        conn = get_resolve_connection()
        timeline = conn.get_current_timeline()
        if timeline is None:
            return "No active timeline"
        success = timeline.SetVoiceIsolationState(
            track_index, {"isEnabled": enabled, "amount": amount}
        )
        if success:
            state = "enabled" if enabled else "disabled"
            return f"Voice Isolation {state} (amount: {amount}) on audio track {track_index}"
        return "Failed to set voice isolation state"
    except Exception as e:
        return f"Error setting voice isolation: {e}"


# ═══════════════════════════════════════════════════════════════════
#  CODE EXECUTION (POWER TOOL)
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def execute_resolve_code(ctx: Context, code: str) -> str:
    """
    Execute arbitrary Python code in the DaVinci Resolve scripting environment.
    Use this for operations not covered by specific tools.

    The following variables are pre-loaded in the namespace:
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
        conn = get_resolve_connection()
        return conn.execute_code(code)
    except Exception as e:
        return f"Error executing code: {e}"


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

    5. For AI-powered features:
       - Use detect_scene_cuts() to auto-detect cuts in long footage
       - Use create_magic_mask() for AI subject isolation
       - Use smart_reframe() for automatic reframing
       - Use stabilize() for clip stabilization
       - Use create_subtitles_from_audio() for AI-generated subtitles
       - Use set_voice_isolation_state() to isolate speech from noise

    6. For rendering:
       - Use get_render_formats() to see available options
       - Use set_render_settings() to configure output
       - Use add_render_job() then start_rendering()
       - Use get_render_status() to monitor progress

    7. For Fusion (compositing/VFX):
       - Use get_fusion_comp_list() to see existing compositions on a clip
       - Use add_fusion_comp() to create a new composition
       - Use import_fusion_comp() / export_fusion_comp() to manage .comp files
       - Use create_fusion_clip() to merge clips into a Fusion composition
       - Use insert_fusion_generator() / insert_fusion_title() for generators and titles
       - Use insert_fusion_composition() for a blank comp at the playhead
       - For advanced Fusion node manipulation, use execute_resolve_code() with
         resolve.Fusion() to access the full Fusion scripting API

    8. For anything not covered by specific tools:
       - Use execute_resolve_code() to run arbitrary Python
       - The Resolve Python API is comprehensive — most operations are possible

    IMPORTANT: DaVinci Resolve must be running for all tools to work.
    Some tools require being on a specific page (e.g. thumbnails require Color page).
    """


# ── Entry point ──

def main():
    """Run the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
