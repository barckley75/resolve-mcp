"""
Serialization helpers that convert DaVinci Resolve API objects into
JSON-serializable Python dicts. Keeps server.py clean.
"""

import base64
import io
import json
import logging
import struct
import zlib
from typing import Any

logger = logging.getLogger("ResolveMCP")


def folder_to_dict(folder, max_depth: int = 3, max_clips: int = 50, _depth: int = 0) -> dict:
    """
    Recursively convert a MediaPool Folder into a serializable dict.

    Args:
        folder: A Resolve Folder object.
        max_depth: Maximum recursion depth for subfolders.
        max_clips: Maximum number of clips to include per folder.
    """
    result = {
        "name": folder.GetName(),
        "clips": [],
        "subfolders": [],
    }

    # Add clips
    clips = folder.GetClipList() or []
    for i, clip in enumerate(clips):
        if i >= max_clips:
            result["clips"].append(f"... and {len(clips) - max_clips} more clips")
            break
        result["clips"].append(clip_to_dict_brief(clip))

    result["clip_count"] = len(clips)

    # Recurse into subfolders
    if _depth < max_depth:
        subfolders = folder.GetSubFolderList() or []
        for sub in subfolders:
            result["subfolders"].append(
                folder_to_dict(sub, max_depth, max_clips, _depth + 1)
            )

    return result


def clip_to_dict_brief(clip) -> dict:
    """Return a brief summary of a MediaPoolItem (name + key properties)."""
    result = {"name": clip.GetName()}
    try:
        props = clip.GetClipProperty()
        if props:
            for key in ("Duration", "FPS", "Resolution", "File Path", "Clip Color", "Type"):
                if key in props and props[key]:
                    result[key.lower().replace(" ", "_")] = props[key]
    except Exception:
        pass
    return result


def clip_to_dict(clip) -> dict:
    """Return full details of a MediaPoolItem."""
    result = {
        "name": clip.GetName(),
        "media_id": clip.GetMediaId(),
    }

    # All properties
    try:
        props = clip.GetClipProperty()
        if props:
            result["properties"] = props
    except Exception:
        pass

    # Markers
    try:
        markers = clip.GetMarkers()
        if markers:
            result["markers"] = {str(k): v for k, v in markers.items()}
    except Exception:
        pass

    # Flags
    try:
        flags = clip.GetFlagList()
        if flags:
            result["flags"] = flags
    except Exception:
        pass

    # Clip color
    try:
        color = clip.GetClipColor()
        if color:
            result["clip_color"] = color
    except Exception:
        pass

    return result


def timeline_to_dict(timeline) -> dict:
    """Convert a Timeline object into a serializable dict."""
    result = {
        "name": timeline.GetName(),
        "start_frame": timeline.GetStartFrame(),
        "end_frame": timeline.GetEndFrame(),
        "start_timecode": timeline.GetStartTimecode(),
    }

    # Track counts
    for track_type in ("video", "audio", "subtitle"):
        try:
            result[f"{track_type}_track_count"] = timeline.GetTrackCount(track_type)
        except Exception:
            result[f"{track_type}_track_count"] = 0

    # Settings
    try:
        for setting in ("timelineFrameRate", "timelineResolutionWidth", "timelineResolutionHeight"):
            val = timeline.GetSetting(setting)
            if val:
                result[setting] = val
    except Exception:
        pass

    # Current timecode
    try:
        result["current_timecode"] = timeline.GetCurrentTimecode()
    except Exception:
        pass

    # Markers
    try:
        markers = timeline.GetMarkers()
        if markers:
            result["markers"] = {str(k): v for k, v in markers.items()}
    except Exception:
        pass

    return result


def timeline_item_to_dict(item) -> dict:
    """Convert a TimelineItem into a serializable dict."""
    result = {
        "name": item.GetName(),
        "start": item.GetStart(),
        "end": item.GetEnd(),
        "duration": item.GetDuration(),
    }

    # Source frames
    try:
        result["source_start_frame"] = item.GetSourceStartFrame()
        result["source_end_frame"] = item.GetSourceEndFrame()
    except Exception:
        pass

    # Clip color
    try:
        color = item.GetClipColor()
        if color:
            result["clip_color"] = color
    except Exception:
        pass

    # Clip enabled
    try:
        result["enabled"] = item.GetClipEnabled()
    except Exception:
        pass

    return result


def timeline_item_full_dict(item) -> dict:
    """Convert a TimelineItem with all properties into a serializable dict."""
    result = timeline_item_to_dict(item)

    # All properties (Pan, Tilt, Zoom, Opacity, Crop, etc.)
    try:
        props = item.GetProperty()
        if props:
            result["properties"] = props
    except Exception:
        pass

    # Markers
    try:
        markers = item.GetMarkers()
        if markers:
            result["markers"] = {str(k): v for k, v in markers.items()}
    except Exception:
        pass

    # Flags
    try:
        flags = item.GetFlagList()
        if flags:
            result["flags"] = flags
    except Exception:
        pass

    # Fusion comp info
    try:
        comp_count = item.GetFusionCompCount()
        if comp_count and comp_count > 0:
            result["fusion_comp_count"] = comp_count
            result["fusion_comp_names"] = item.GetFusionCompNameList()
    except Exception:
        pass

    # Version info
    try:
        version = item.GetCurrentVersion()
        if version:
            result["current_version"] = version
    except Exception:
        pass

    # Track info
    try:
        track_info = item.GetTrackTypeAndIndex()
        if track_info:
            result["track_type"] = track_info[0]
            result["track_index"] = track_info[1]
    except Exception:
        pass

    return result


def node_graph_to_dict(graph) -> dict:
    """Convert a Graph (color node graph) into a serializable dict."""
    result = {
        "num_nodes": graph.GetNumNodes(),
        "nodes": [],
    }

    for i in range(1, graph.GetNumNodes() + 1):
        node_info = {"index": i}
        try:
            node_info["label"] = graph.GetNodeLabel(i)
        except Exception:
            node_info["label"] = ""
        try:
            lut = graph.GetLUT(i)
            if lut:
                node_info["lut"] = lut
        except Exception:
            pass
        try:
            node_info["tools"] = graph.GetToolsInNode(i)
        except Exception:
            pass
        result["nodes"].append(node_info)

    return result


def thumbnail_to_png_bytes(thumbnail_data: dict) -> bytes:
    """
    Convert the raw RGB base64 thumbnail data from
    Timeline.GetCurrentClipThumbnailImage() into PNG bytes.

    Uses pure Python (struct + zlib) — no PIL/numpy dependency.
    """
    width = thumbnail_data["width"]
    height = thumbnail_data["height"]
    raw_rgb = base64.b64decode(thumbnail_data["data"])

    # Build PNG from raw RGB data
    def make_png(width: int, height: int, rgb_data: bytes) -> bytes:
        def chunk(chunk_type: bytes, data: bytes) -> bytes:
            c = chunk_type + data
            crc = struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
            return struct.pack(">I", len(data)) + c + crc

        # PNG signature
        sig = b"\x89PNG\r\n\x1a\n"

        # IHDR
        ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
        ihdr = chunk(b"IHDR", ihdr_data)

        # IDAT — raw pixel rows with filter byte 0 (None) prepended
        raw_rows = b""
        row_size = width * 3
        for y in range(height):
            raw_rows += b"\x00"  # filter byte
            raw_rows += rgb_data[y * row_size : (y + 1) * row_size]

        compressed = zlib.compress(raw_rows)
        idat = chunk(b"IDAT", compressed)

        # IEND
        iend = chunk(b"IEND", b"")

        return sig + ihdr + idat + iend

    return make_png(width, height, raw_rgb)


def safe_serialize(obj: Any) -> Any:
    """Make an object JSON-serializable, handling Resolve API objects gracefully."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): safe_serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [safe_serialize(item) for item in obj]
    # Fallback: try str()
    try:
        return str(obj)
    except Exception:
        return "<non-serializable>"
