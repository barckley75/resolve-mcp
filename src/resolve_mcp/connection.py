"""
ResolveConnection — manages the connection to a running DaVinci Resolve instance.

DaVinci Resolve's scripting API is accessible from external Python processes
via the native fusionscript module. This class handles:
  - Auto-configuring sys.path and environment variables for the Resolve module
  - Connecting to the running Resolve instance
  - Providing fresh accessors for project/timeline/media pool (avoids stale refs)
  - Executing arbitrary Python code with the Resolve API available
  - Thread safety via an RLock around all API calls
"""

import sys
import os
import io
import logging
import threading
import traceback
from contextlib import redirect_stdout
from typing import Any, Optional

logger = logging.getLogger("ResolveMCP")

# Default paths per platform
_PLATFORM_DEFAULTS = {
    "darwin": {
        "script_api": "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting",
        "script_lib": "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so",
    },
    "win32": {
        "script_api": os.path.join(
            os.getenv("PROGRAMDATA", "C:\\ProgramData"),
            "Blackmagic Design", "DaVinci Resolve", "Support", "Developer", "Scripting",
        ),
        "script_lib": "C:\\Program Files\\Blackmagic Design\\DaVinci Resolve\\fusionscript.dll",
    },
    "linux": {
        "script_api": "/opt/resolve/Developer/Scripting",
        "script_lib": "/opt/resolve/libs/Fusion/fusionscript.so",
    },
}


def _get_platform_key() -> str:
    if sys.platform.startswith("darwin"):
        return "darwin"
    elif sys.platform.startswith("win") or sys.platform.startswith("cygwin"):
        return "win32"
    else:
        return "linux"


class ResolveConnection:
    """Manages a connection to a running DaVinci Resolve instance."""

    def __init__(self):
        self.resolve = None
        self._lock = threading.RLock()  # RLock so nested calls don't deadlock

    def connect(self) -> bool:
        with self._lock:
            if self.resolve is not None:
                return True

            try:
                self._setup_environment()
                import DaVinciResolveScript as dvr_script
                self.resolve = dvr_script.scriptapp("Resolve")
                if self.resolve is None:
                    logger.error("scriptapp('Resolve') returned None — is DaVinci Resolve running?")
                    return False
                version = self._safe_call(self.resolve.GetVersionString)
                logger.info("Connected to DaVinci Resolve: %s", version or "unknown version")
                return True
            except ImportError as e:
                logger.error("Failed to import DaVinciResolveScript: %s", e)
                logger.error(
                    "Make sure RESOLVE_SCRIPT_API and RESOLVE_SCRIPT_LIB environment variables are set, "
                    "or DaVinci Resolve is installed at the default location."
                )
                return False
            except Exception as e:
                logger.error("Failed to connect to DaVinci Resolve: %s", e)
                return False

    def _setup_environment(self):
        platform_key = _get_platform_key()
        defaults = _PLATFORM_DEFAULTS.get(platform_key, _PLATFORM_DEFAULTS["linux"])

        if not os.getenv("RESOLVE_SCRIPT_LIB"):
            lib_path = defaults["script_lib"]
            if os.path.exists(lib_path):
                os.environ["RESOLVE_SCRIPT_LIB"] = lib_path
                logger.info("Auto-set RESOLVE_SCRIPT_LIB=%s", lib_path)

        script_api = os.getenv("RESOLVE_SCRIPT_API", defaults["script_api"])
        modules_path = os.path.join(script_api, "Modules")
        if modules_path not in sys.path:
            sys.path.insert(0, modules_path)
            logger.info("Added to sys.path: %s", modules_path)

    def disconnect(self):
        with self._lock:
            self.resolve = None
            logger.info("Disconnected from DaVinci Resolve")

    @staticmethod
    def _safe_call(fn, *args, **kwargs):
        """Call a Resolve API function, returning None on failure instead of crashing."""
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            logger.debug("Resolve API call %s failed: %s", getattr(fn, '__name__', fn), e)
            return None

    def _ensure_connected(self):
        if self.resolve is None:
            raise ConnectionError(
                "Not connected to DaVinci Resolve. Make sure Resolve is running."
            )

    def is_alive(self) -> bool:
        """Check whether the connection is still functional."""
        with self._lock:
            if self.resolve is None:
                return False
            try:
                pm = self.resolve.GetProjectManager()
                if pm is None:
                    return False
                proj = pm.GetCurrentProject()
                return proj is not None
            except Exception:
                return False

    # ── Accessors (always go through the lock) ──

    def get_resolve(self):
        with self._lock:
            self._ensure_connected()
            return self.resolve

    def get_project_manager(self):
        with self._lock:
            self._ensure_connected()
            pm = self.resolve.GetProjectManager()
            if pm is None:
                raise RuntimeError("Could not get ProjectManager — is Resolve still running?")
            return pm

    def get_project(self):
        with self._lock:
            self._ensure_connected()
            pm = self.resolve.GetProjectManager()
            if pm is None:
                raise RuntimeError("Could not get ProjectManager — is Resolve still running?")
            project = pm.GetCurrentProject()
            if project is None:
                raise RuntimeError("No project is currently open in DaVinci Resolve")
            return project

    def get_media_pool(self):
        project = self.get_project()
        with self._lock:
            mp = project.GetMediaPool()
            if mp is None:
                raise RuntimeError("Could not get MediaPool from current project")
            return mp

    def get_current_timeline(self):
        project = self.get_project()
        with self._lock:
            return project.GetCurrentTimeline()

    def get_media_storage(self):
        with self._lock:
            self._ensure_connected()
            ms = self.resolve.GetMediaStorage()
            if ms is None:
                raise RuntimeError("Could not get MediaStorage from Resolve")
            return ms

    def get_gallery(self):
        project = self.get_project()
        with self._lock:
            gallery = project.GetGallery()
            if gallery is None:
                raise RuntimeError("Could not get Gallery from current project")
            return gallery

    # ── Code execution ──

    def execute_code(self, code: str) -> str:
        """
        Execute arbitrary Python code with Resolve API objects in the namespace.

        Available variables: resolve, project, mediaPool, timeline, mediaStorage
        Captured stdout is returned as a string.
        """
        with self._lock:
            self._ensure_connected()

            project = None
            media_pool = None
            timeline = None
            media_storage = None

            try:
                pm = self.resolve.GetProjectManager()
                if pm:
                    project = pm.GetCurrentProject()
            except Exception:
                pass
            if project:
                try:
                    media_pool = project.GetMediaPool()
                except Exception:
                    pass
                try:
                    timeline = project.GetCurrentTimeline()
                except Exception:
                    pass
            try:
                media_storage = self.resolve.GetMediaStorage()
            except Exception:
                pass

            namespace = {
                "resolve": self.resolve,
                "project": project,
                "mediaPool": media_pool,
                "timeline": timeline,
                "mediaStorage": media_storage,
            }

            stdout_capture = io.StringIO()
            try:
                with redirect_stdout(stdout_capture):
                    exec(code, namespace)
                output = stdout_capture.getvalue()
                if "result" in namespace and namespace["result"] is not None:
                    result_val = namespace["result"]
                    if output:
                        output += f"\nresult = {result_val}"
                    else:
                        output = str(result_val)
                return output if output else "Code executed successfully (no output)"
            except Exception as e:
                return f"Error executing code: {e}\n{traceback.format_exc()}"


# ── Module-level singleton ──

_resolve_connection: Optional[ResolveConnection] = None


def get_resolve_connection() -> ResolveConnection:
    """Get or create a persistent ResolveConnection singleton."""
    global _resolve_connection

    if _resolve_connection is not None and _resolve_connection.is_alive():
        return _resolve_connection

    # Either no connection or it went stale — (re)create
    if _resolve_connection is not None:
        logger.warning("Existing Resolve connection is stale, reconnecting...")

    conn = ResolveConnection()
    if not conn.connect():
        _resolve_connection = None
        raise ConnectionError(
            "Could not connect to DaVinci Resolve. "
            "Make sure Resolve is running and scripting is enabled in Preferences."
        )

    _resolve_connection = conn
    logger.info("ResolveConnection ready")
    return _resolve_connection
