"""
ResolveConnection — manages the connection to a running DaVinci Resolve instance.

Unlike BlenderMCP which needs a TCP socket to communicate with Blender,
DaVinci Resolve's scripting API is accessible from external Python processes
via the native fusionscript module. This class handles:
  - Auto-configuring sys.path and environment variables for the Resolve module
  - Connecting to the running Resolve instance
  - Providing fresh accessors for project/timeline/media pool (avoids stale refs)
  - Executing arbitrary Python code with the Resolve API available
  - Thread safety via a lock around all API calls
"""

import sys
import os
import io
import logging
import threading
import traceback
from contextlib import redirect_stdout
from typing import Any

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
            "Blackmagic Design", "DaVinci Resolve", "Support", "Developer", "Scripting"
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
        self._lock = threading.Lock()

    def connect(self) -> bool:
        """
        Import DaVinciResolveScript and connect to the running Resolve instance.
        Returns True on success, False on failure.
        """
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
                logger.info("Connected to DaVinci Resolve: %s", self.resolve.GetVersionString())
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
        """Configure sys.path and environment variables for the Resolve scripting module."""
        platform_key = _get_platform_key()
        defaults = _PLATFORM_DEFAULTS.get(platform_key, _PLATFORM_DEFAULTS["linux"])

        # Set RESOLVE_SCRIPT_LIB if not already set
        if not os.getenv("RESOLVE_SCRIPT_LIB"):
            lib_path = defaults["script_lib"]
            if os.path.exists(lib_path):
                os.environ["RESOLVE_SCRIPT_LIB"] = lib_path
                logger.info("Auto-set RESOLVE_SCRIPT_LIB=%s", lib_path)

        # Add the Modules directory to sys.path
        script_api = os.getenv("RESOLVE_SCRIPT_API", defaults["script_api"])
        modules_path = os.path.join(script_api, "Modules")
        if modules_path not in sys.path:
            sys.path.insert(0, modules_path)
            logger.info("Added to sys.path: %s", modules_path)

    def disconnect(self):
        """Release the Resolve connection."""
        with self._lock:
            self.resolve = None
            logger.info("Disconnected from DaVinci Resolve")

    def _ensure_connected(self):
        """Raise ConnectionError if not connected."""
        if self.resolve is None:
            raise ConnectionError(
                "Not connected to DaVinci Resolve. Make sure Resolve is running."
            )

    # ── Accessors (fresh on each call to avoid stale references) ──

    def get_resolve(self):
        """Return the Resolve object."""
        with self._lock:
            self._ensure_connected()
            return self.resolve

    def get_project_manager(self):
        """Return the current ProjectManager."""
        resolve = self.get_resolve()
        pm = resolve.GetProjectManager()
        if pm is None:
            raise RuntimeError("Could not get ProjectManager from Resolve")
        return pm

    def get_project(self):
        """Return the currently loaded Project."""
        pm = self.get_project_manager()
        project = pm.GetCurrentProject()
        if project is None:
            raise RuntimeError("No project is currently open in DaVinci Resolve")
        return project

    def get_media_pool(self):
        """Return the MediaPool for the current project."""
        project = self.get_project()
        mp = project.GetMediaPool()
        if mp is None:
            raise RuntimeError("Could not get MediaPool from current project")
        return mp

    def get_current_timeline(self):
        """Return the current Timeline, or None if no timeline is active."""
        project = self.get_project()
        return project.GetCurrentTimeline()

    def get_media_storage(self):
        """Return the MediaStorage object."""
        resolve = self.get_resolve()
        ms = resolve.GetMediaStorage()
        if ms is None:
            raise RuntimeError("Could not get MediaStorage from Resolve")
        return ms

    def get_gallery(self):
        """Return the Gallery object for the current project."""
        project = self.get_project()
        gallery = project.GetGallery()
        if gallery is None:
            raise RuntimeError("Could not get Gallery from current project")
        return gallery

    # ── Code execution ──

    def execute_code(self, code: str) -> str:
        """
        Execute arbitrary Python code with Resolve API objects available in the namespace.

        Available variables:
          - resolve: the Resolve object
          - project: current project
          - mediaPool: current media pool
          - timeline: current timeline (may be None)
          - mediaStorage: media storage object

        Captured stdout is returned as a string.
        """
        with self._lock:
            self._ensure_connected()

            # Build namespace
            project = None
            media_pool = None
            timeline = None
            media_storage = None

            try:
                project = self.resolve.GetProjectManager().GetCurrentProject()
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
                # Check if there's a 'result' variable set by the user code
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

_resolve_connection: ResolveConnection | None = None


def get_resolve_connection() -> ResolveConnection:
    """Get or create a persistent ResolveConnection singleton."""
    global _resolve_connection

    if _resolve_connection is None:
        _resolve_connection = ResolveConnection()
        if not _resolve_connection.connect():
            _resolve_connection = None
            raise ConnectionError(
                "Could not connect to DaVinci Resolve. "
                "Make sure Resolve is running and scripting is enabled in Preferences."
            )
        logger.info("Created new ResolveConnection")

    # Validate the connection is still alive
    try:
        _resolve_connection.get_project()
    except Exception:
        logger.warning("Existing connection is stale, reconnecting...")
        _resolve_connection = None
        return get_resolve_connection()

    return _resolve_connection
