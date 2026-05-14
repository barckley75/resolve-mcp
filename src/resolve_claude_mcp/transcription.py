"""
Local audio transcription using mlx-whisper (optimized for Apple M-series chips).

Long files are split into chunks with ffmpeg so each transcription call
completes well within any MCP timeout.
"""

import logging
import os
import shutil
import subprocess
import tempfile
from typing import Optional, List, Dict, Any

logger = logging.getLogger("ResolveMCP")

# ── Models ──────────────────────────────────────────────────────────

WHISPER_MODELS = {
    "tiny": "mlx-community/whisper-tiny",
    "base": "mlx-community/whisper-base",
    "small": "mlx-community/whisper-small",
    "medium": "mlx-community/whisper-medium",
    "large": "mlx-community/whisper-large-v3",
    "turbo": "mlx-community/whisper-large-v3-turbo",
}

DEFAULT_MODEL = "turbo"

# Each chunk is this many seconds — short enough to never time out
CHUNK_SECONDS = 300  # 5 minutes


def _get_model_repo(model: str) -> str:
    if "/" in model:
        return model
    repo = WHISPER_MODELS.get(model)
    if repo is None:
        raise ValueError(
            f"Unknown model '{model}'. Choose from: {', '.join(WHISPER_MODELS.keys())} "
            f"or pass a full HuggingFace repo path."
        )
    return repo


# ── ffmpeg helpers ──────────────────────────────────────────────────

def _get_duration(path: str) -> float:
    """Get duration in seconds via ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        path,
    ]
    out = subprocess.check_output(cmd, text=True).strip()
    return float(out)


def _extract_chunk(src: str, start: float, duration: float, dst: str):
    """Extract a chunk of audio with ffmpeg."""
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-t", str(duration),
        "-i", src,
        "-vn",                # drop video
        "-acodec", "pcm_s16le",
        "-ar", "16000",       # whisper expects 16 kHz
        "-ac", "1",           # mono
        dst,
    ]
    subprocess.run(cmd, capture_output=True, check=True)


def _split_audio(path: str, chunk_sec: int, tmp_dir: str) -> List[Dict[str, Any]]:
    """Split into ≤chunk_sec WAV files.  Returns list of {path, offset}."""
    total = _get_duration(path)
    chunks = []
    offset = 0.0
    idx = 0
    while offset < total:
        chunk_path = os.path.join(tmp_dir, f"chunk_{idx:04d}.wav")
        _extract_chunk(path, offset, chunk_sec, chunk_path)
        chunks.append({"path": chunk_path, "offset": offset})
        offset += chunk_sec
        idx += 1
    return chunks


# ── Core transcription ─────────────────────────────────────────────

def transcribe(
    audio_path: str,
    model: str = DEFAULT_MODEL,
    language: Optional[str] = None,
    word_timestamps: bool = False,
    initial_prompt: Optional[str] = None,
    chunk_seconds: int = CHUNK_SECONDS,
) -> Dict[str, Any]:
    """
    Transcribe an audio/video file using mlx-whisper.

    Files longer than *chunk_seconds* are automatically split with ffmpeg
    so each chunk completes quickly.
    """
    try:
        import mlx_whisper
    except ImportError:
        raise ImportError(
            "mlx-whisper is not installed. Install with: "
            "uv pip install 'mlx-whisper>=0.4.3'"
        )

    if not os.path.isfile(audio_path):
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    repo = _get_model_repo(model)
    duration = _get_duration(audio_path)

    decode_options: Dict[str, Any] = {}
    if language:
        decode_options["language"] = language

    # Short file → transcribe directly
    if duration <= chunk_seconds:
        logger.info("Transcribing '%s' (%.0fs) with %s", audio_path, duration, repo)
        return mlx_whisper.transcribe(
            audio_path,
            path_or_hf_repo=repo,
            word_timestamps=word_timestamps,
            initial_prompt=initial_prompt,
            verbose=False,
            **decode_options,
        )

    # Long file → chunk, transcribe each, stitch
    logger.info(
        "Splitting '%s' (%.0fs) into %d-second chunks",
        audio_path, duration, chunk_seconds,
    )
    tmp_dir = tempfile.mkdtemp(prefix="resolve_whisper_")
    try:
        chunks = _split_audio(audio_path, chunk_seconds, tmp_dir)
        logger.info("Created %d chunks", len(chunks))

        all_segments: List[Dict[str, Any]] = []
        all_text_parts: List[str] = []
        detected_language = None

        for i, chunk in enumerate(chunks):
            logger.info("Transcribing chunk %d/%d (offset %.0fs)...", i + 1, len(chunks), chunk["offset"])

            result = mlx_whisper.transcribe(
                chunk["path"],
                path_or_hf_repo=repo,
                word_timestamps=word_timestamps,
                initial_prompt=initial_prompt,
                verbose=False,
                **decode_options,
            )

            if detected_language is None:
                detected_language = result.get("language")

            offset = chunk["offset"]
            for seg in result.get("segments", []):
                all_segments.append({
                    "start": seg["start"] + offset,
                    "end": seg["end"] + offset,
                    "text": seg["text"],
                })

            text = result.get("text", "")
            if text:
                all_text_parts.append(text.strip())

            # Use the tail of the last chunk's text as prompt for the next
            # to maintain context continuity across chunk boundaries
            if text:
                initial_prompt = text.strip()[-200:]

        return {
            "language": detected_language or "unknown",
            "text": " ".join(all_text_parts),
            "segments": all_segments,
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ── SRT helpers ────────────────────────────────────────────────────

def segments_to_srt(segments: List[Dict[str, Any]]) -> str:
    lines = []
    for i, seg in enumerate(segments, 1):
        start = _seconds_to_srt_time(seg["start"])
        end = _seconds_to_srt_time(seg["end"])
        text = seg["text"].strip()
        lines.append(f"{i}\n{start} --> {end}\n{text}\n")
    return "\n".join(lines)


def _seconds_to_srt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
