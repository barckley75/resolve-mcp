"""
Serialization helpers that convert DaVinci Resolve API objects into
JSON-serializable Python dicts. Keeps server.py clean.
"""

import base64
import logging
import struct
import zlib
from typing import Any

logger = logging.getLogger("ResolveMCP")


def _safe(fn, *args, default=None):
    """Call fn(*args) and return the result, or *default* on any failure."""
    try:
        val = fn(*args)
        return val if val is not None else default
    except Exception as e:
        logger.debug("Resolve API call failed (%s): %s", getattr(fn, '__name__', '?'), e)
        return default


def folder_to_dict(folder, max_depth: int = 3, max_clips: int = 50, _depth: int = 0) -> dict:
    result = {
        "name": _safe(folder.GetName, default="(unnamed)"),
        "clips": [],
        "subfolders": [],
    }

    clips = _safe(folder.GetClipList, default=[]) or []
    for i, clip in enumerate(clips):
        if i >= max_clips:
            result["clips"].append(f"... and {len(clips) - max_clips} more clips")
            break
        result["clips"].append(clip_to_dict_brief(clip))

    result["clip_count"] = len(clips)

    if _depth < max_depth:
        subfolders = _safe(folder.GetSubFolderList, default=[]) or []
        for sub in subfolders:
            result["subfolders"].append(
                folder_to_dict(sub, max_depth, max_clips, _depth + 1)
            )

    return result


def clip_to_dict_brief(clip) -> dict:
    result = {"name": _safe(clip.GetName, default="(unnamed)")}
    try:
        props = clip.GetClipProperty()
        if isinstance(props, dict):
            for key in ("Duration", "FPS", "Resolution", "File Path", "Clip Color", "Type"):
                val = props.get(key)
                if val:
                    result[key.lower().replace(" ", "_")] = val
    except Exception as e:
        logger.debug("clip_to_dict_brief: GetClipProperty failed: %s", e)
    return result


def clip_to_dict(clip) -> dict:
    result = {
        "name": _safe(clip.GetName, default="(unnamed)"),
    }

    mid = _safe(clip.GetMediaId)
    if mid:
        result["media_id"] = mid

    try:
        props = clip.GetClipProperty()
        if isinstance(props, dict) and props:
            result["properties"] = props
    except Exception as e:
        logger.debug("clip_to_dict: GetClipProperty failed: %s", e)

    markers = _safe(clip.GetMarkers)
    if isinstance(markers, dict) and markers:
        result["markers"] = {str(k): v for k, v in markers.items()}

    flags = _safe(clip.GetFlagList)
    if flags:
        result["flags"] = flags

    color = _safe(clip.GetClipColor)
    if color:
        result["clip_color"] = color

    return result


def timeline_to_dict(timeline) -> dict:
    result = {
        "name": _safe(timeline.GetName, default="(unnamed)"),
    }

    start_frame = _safe(timeline.GetStartFrame)
    if start_frame is not None:
        result["start_frame"] = start_frame

    end_frame = _safe(timeline.GetEndFrame)
    if end_frame is not None:
        result["end_frame"] = end_frame

    start_tc = _safe(timeline.GetStartTimecode)
    if start_tc:
        result["start_timecode"] = start_tc

    for track_type in ("video", "audio", "subtitle"):
        count = _safe(timeline.GetTrackCount, track_type, default=0)
        result[f"{track_type}_track_count"] = count

    for setting in ("timelineFrameRate", "timelineResolutionWidth", "timelineResolutionHeight"):
        val = _safe(timeline.GetSetting, setting)
        if val:
            result[setting] = val

    tc = _safe(timeline.GetCurrentTimecode)
    if tc:
        result["current_timecode"] = tc

    markers = _safe(timeline.GetMarkers)
    if isinstance(markers, dict) and markers:
        result["markers"] = {str(k): v for k, v in markers.items()}

    return result


def timeline_item_to_dict(item) -> dict:
    result = {
        "name": _safe(item.GetName, default="(unnamed)"),
    }

    for attr, key in [
        (item.GetStart, "start"),
        (item.GetEnd, "end"),
        (item.GetDuration, "duration"),
    ]:
        val = _safe(attr)
        if val is not None:
            result[key] = val

    # Left/right offsets (trimmed frames from source)
    left = _safe(item.GetLeftOffset)
    if left is not None:
        result["left_offset"] = left
    right = _safe(item.GetRightOffset)
    if right is not None:
        result["right_offset"] = right

    color = _safe(item.GetClipColor)
    if color:
        result["clip_color"] = color

    enabled = _safe(item.GetClipEnabled)
    if enabled is not None:
        result["enabled"] = enabled

    return result


def timeline_item_full_dict(item) -> dict:
    result = timeline_item_to_dict(item)

    # All transform/composite properties
    try:
        props = item.GetProperty()
        if isinstance(props, dict) and props:
            result["properties"] = props
    except Exception as e:
        logger.debug("GetProperty() failed: %s", e)
        # Fallback: try fetching known properties individually
        known_props = {}
        for key in ("Pan", "Tilt", "ZoomX", "ZoomY", "ZoomGang",
                     "RotationAngle", "Opacity", "AnchorPointX", "AnchorPointY",
                     "CropLeft", "CropRight", "CropTop", "CropBottom",
                     "CropSoftness", "CropRetain", "FlipX", "FlipY",
                     "CompositeMode", "Scaling", "RetimeProcess"):
            val = _safe(item.GetProperty, key)
            if val is not None:
                known_props[key] = val
        if known_props:
            result["properties"] = known_props

    markers = _safe(item.GetMarkers)
    if isinstance(markers, dict) and markers:
        result["markers"] = {str(k): v for k, v in markers.items()}

    flags = _safe(item.GetFlagList)
    if flags:
        result["flags"] = flags

    comp_count = _safe(item.GetFusionCompCount)
    if comp_count and comp_count > 0:
        result["fusion_comp_count"] = comp_count
        names = _safe(item.GetFusionCompNameList)
        if names:
            result["fusion_comp_names"] = names

    # Media pool item reference
    mpi = _safe(item.GetMediaPoolItem)
    if mpi:
        mpi_name = _safe(mpi.GetName)
        if mpi_name:
            result["media_pool_item"] = mpi_name

    return result


def node_graph_to_dict(graph) -> dict:
    num_nodes = _safe(graph.GetNumNodes, default=0)
    result = {
        "num_nodes": num_nodes,
        "nodes": [],
    }

    for i in range(1, num_nodes + 1):
        node_info = {"index": i}

        label = _safe(graph.GetNodeLabel, i, default="")
        node_info["label"] = label

        lut = _safe(graph.GetLUT, i)
        if lut:
            node_info["lut"] = lut

        result["nodes"].append(node_info)

    return result


def thumbnail_to_png_bytes(thumbnail_data: dict) -> bytes:
    """
    Convert the raw RGB base64 thumbnail data from
    Timeline.GetCurrentClipThumbnailImage() into PNG bytes.
    """
    width = thumbnail_data.get("width", 0)
    height = thumbnail_data.get("height", 0)
    data = thumbnail_data.get("data", "")

    if not width or not height or not data:
        raise ValueError(
            f"Invalid thumbnail data: width={width}, height={height}, data_len={len(data)}"
        )

    raw_rgb = base64.b64decode(data)
    expected_size = width * height * 3
    if len(raw_rgb) < expected_size:
        raise ValueError(
            f"Thumbnail data too short: got {len(raw_rgb)} bytes, "
            f"expected {expected_size} for {width}x{height} RGB"
        )

    def _make_png(w: int, h: int, rgb: bytes) -> bytes:
        def chunk(chunk_type: bytes, chunk_data: bytes) -> bytes:
            c = chunk_type + chunk_data
            crc = struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
            return struct.pack(">I", len(chunk_data)) + c + crc

        sig = b"\x89PNG\r\n\x1a\n"
        ihdr_data = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
        ihdr = chunk(b"IHDR", ihdr_data)

        raw_rows = bytearray()
        row_size = w * 3
        for y in range(h):
            raw_rows += b"\x00"
            raw_rows += rgb[y * row_size : (y + 1) * row_size]

        idat = chunk(b"IDAT", zlib.compress(bytes(raw_rows)))
        iend = chunk(b"IEND", b"")

        return sig + ihdr + idat + iend

    return _make_png(width, height, raw_rgb)


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
    try:
        return str(obj)
    except Exception:
        return "<non-serializable>"
