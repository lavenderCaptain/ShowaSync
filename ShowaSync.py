"""
ShowaSync v3 - Japanese Media Translation Pipeline (Simple Browser)
====================================================================

A complete pipeline that takes Japanese video files (.mkv/.mp4/.ts) and produces
English .srt subtitle files, orchestrated from a responsive Textual TUI. The app
manages an entire workflow: audio extraction -> transcription -> LLM translation ->
subtitle rebalancing, with smart queuing utilities to batch-process only what
needs work.

Architecture overview:
- Frontend (TUI): `ShowaSync` class provides the interactive interface where users
  select files, define context profiles, and initiate batch jobs. It manages overall
  state (queuing, progress tracking).
- File browser: Two stacked ListViews -- a folder tree showing parent directories up
  to root, and current_contents showing files/dirs in the active directory. Both
  update together whenever `_current_path` changes.
- Pipeline execution: Sequential subprocess management for external tools (FFmpeg,
  WhisperX, Ollama). Each stage depends on the previous one succeeding.
- Config persistence: A single runtime dict `self._config_values` serves as the
  source of truth, saved to disk via event handlers and on_unmount().

Author: LavenderCaptain
License: MIT
"""

import os
import re
import time
import json
import threading
import shutil
from pathlib import Path
from datetime import timedelta

import requests
import subprocess
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Button, Input, Select, RichLog, ProgressBar, Label, ListView, ListItem, Static
from textual.containers import Horizontal, Vertical
from textual import work
from textual.events import Click, Key

try:
    from ollama import Client
except ImportError:
    Client = None


class ShowaSync(App):
    TITLE = "ShowaSync"

    CSS = """
    Screen { background: $surface; }

    #main_content {
        height: 1fr;
        layout: horizontal;
    }

    /* Left side: file browser (two panes stacked) */
    #browser_column {
        width: 35%;
        height: 100%;
        layout: vertical;
        border-left: solid $accent;
        border-right: solid $accent;
        padding: 0 1;
    }

    /* Top pane: folder tree */
    #folder_tree {
        width: 100%;
        height: 45%;
        overflow-y: scroll;
        border-bottom: double $accent;
        padding: 0 1;
    }

    /* Bottom pane: current folder contents */
    #current_contents {
        width: 100%;
        height: 55%;
        overflow-y: scroll;
        padding: 0 1;
    }

    /* Right panel */
    #right_panel {
        width: 65%;
        height: 100%;
        layout: vertical;
        border-left: solid $primary;
    }

    #queue_section {
        height: 8;
        overflow-y: scroll;
        border-bottom: solid $secondary;
    }

    .panel_title {
        text-style: bold;
        color: $primary;
        margin-bottom: 0;
    }

    .telemetry_bar {
        height: 3;
        layout: horizontal;
        margin-top: 1;
    }

    #progress_bar { width: 70%; height: 1; }
    .status_text {
        width: 30%;
        text-align: right;
        color: $success;
        text-style: bold;
    }

    #console {
        height: 1fr;
        overflow-y: scroll;
        border-top: double $accent;
        background: $panel;
    }

    .config_section {
        height: auto;
        layout: horizontal;
        padding: 0 1;
        margin-bottom: 1;
    }

    .config_label { width: 25%; align: right middle; margin-right: 1; }
    .config_input, .config_model, .config_select { width: 25%; min-width: 30; }

    .action_bar {
        height: auto;
        layout: horizontal;
        padding: 1 2;
        border-top: solid $surface-lighten-1;
    }

    .spacer { width: 1fr; }
    Button { margin: 0 1; }
    #queue_view { height: 100%; overflow-y: scroll; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("p", "poll_models", "Poll Ollama"),
        ("c", "clear_queue", "Clear Queue"),
        ("v", "queue_videos", "Queue Videos"),
        ("s", "queue_srts", "Queue SRTs"),
        ("n", "smart_vids", "Smart: No SRTs"),
        ("t", "smart_translate", "Smart: Translate"),
        ("b", "smart_rebalance", "Smart: Rebalance"),
        ("x", "start_pipeline", "Start Pipeline"),
        ("z", "verify_patch", "Verify & Patch"),
    ]

    def __init__(self):
        super().__init__()
        # target_files is the user's queue of media/SRT files to process.
        # It holds absolute paths (resolved via _current_path.rglob) so that
        # execute_pipeline_batch and execute_patcher_batch can use them directly.
        # Protected by self._queue_lock for thread safety -- background workers
        # and the main UI thread both read/write this list concurrently.
        self.target_files = []
        self._queue_lock = threading.Lock()

        # Mapping from original long filenames to safe short filenames (when truncation was needed).
        # Keys are the original base names, values are dicts with 'translated' and 'balanced' paths.
        # This allows downstream code to find files even when they were renamed for path length safety.
        self._filename_mapping = {}

        # Runtime config dict -- the single source of truth for all settings. Populated
        # by event handlers (on_input_changed, on_select_changed, on_list_view_item_selected)
        # and _handle_folder_click whenever a relevant value changes. Saved to disk
        # via _save_config() in those same handlers AND in on_unmount().
        self._config_values = {}

        # Path to the persistent config file (JSON). Stored in the user's home
        # directory (~/.showasync/config.json) so it's independent of CWD and
        # avoids permission errors when the app is launched from a read-only
        # directory (e.g., a mounted Windows drive via WSL).
        self.config_dir = Path.home() / ".showasync"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_file = self.config_dir / "config.json"

        # Load previously saved config so settings persist across restarts.
        loaded = self._load_config()
        if loaded:
            self._config_values.update(loaded)

        # Context profiles for LLM prompting. Each key is a display name shown in
        # the profile_select UI; each value is the system prompt prefix sent to
        # Ollama before translating subtitles. Users pick one to influence how the
        # LLM handles genre-specific translation quirks (slang, formality levels, etc.).
        self.profiles = {
            "Generic / Catch-all": "You are an expert Japanese-to-English subtitle translator. Translate the text naturally and professionally.",
            "Variety Show (Chaos)": "You are translating a Japanese variety show. Expect heavy slang, constant overlapping dialogue, and distinct visual on-screen text. Prioritize clarity and character separation.",
            "Documentary": "You are translating a formal documentary. Maintain precise, professional terminology. Clearly distinguish between a formal narrator and casual interview subjects.",
            "Sports Broadcast": "You are translating a live sports broadcast. Use high-energy play-by-play terminology, color commentary formatting, and standard athletic jargon.",
            "1959 Hardboiled Noir": "Context: A 1950s Showa-era film. The dialogue must reflect hardboiled, mid-century tough-guy aesthetics and corporate suspense.",
            "1980s Yamadamura Comedy": "Context: A 1980s Japanese comedy. Characters speak in an exaggerated rural dialect ending in 'pya'. Translate this into a comedic, exaggerated 'country' English dialect."
        }

    # --- Config Persistence ---

    def _load_config(self) -> dict:
        """Load saved config from disk.

        Returns a dict of key-value pairs previously written by _save_config().
        If the file doesn't exist (first launch) or is corrupt, returns an empty
        dict so the app falls back to defaults without crashing.

        Why we return {} instead of raising: callers (compose(), on_mount()) need
        safe fallback behavior -- they read keys with .get() and default values,
        so an empty dict means "use built-in defaults" rather than a crash.
        """
        if self.config_file.exists():
            try:
                with open(self.config_file, "r") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                # File exists but is malformed or unreadable -- treat as no saved config.
                return {}
        return {}

    def _save_config(self) -> None:
        """Persist _config_values to disk as JSON.

        Called from event handlers (on_input_changed, on_select_changed, etc.)
        whenever a relevant setting changes, AND from on_unmount() as a safety net
        for any values not yet saved. This ensures settings survive app restarts.

        Filters out non-serializable values (like Textual's NoSelection sentinel)
        before writing -- these can leak in from Select widget defaults and would
        otherwise cause TypeError on exit. IOError is logged rather than swallowed
        so users know if their settings can't be persisted (disk full, etc.).
        """
        try:
            # Filter out values that can't be JSON serialized (Textual NoSelection sentinels, etc.)
            serializable = {}
            for k, v in self._config_values.items():
                try:
                    json.dumps(v)  # Test if value is serializable
                    serializable[k] = v
                except (TypeError, ValueError):
                    pass  # Skip non-serializable values silently

            with open(self.config_file, "w") as f:
                json.dump(serializable, f, indent=2)
        except IOError as e:
            self.write_thread_log(f"[yellow]Warning: Could not save config: {e}[/yellow]")

    def _set_and_save_config(self, key: str, value) -> None:
        """Update a config value and persist it to disk immediately.

        Convenience helper that replaces the repeated pattern of setting a key in
        _config_values followed by calling _save_config(). Ensures consistency and
        prevents accidentally saving stale or missing values.

        Skips the save if the value is the "Waiting..." placeholder, because that
        is a transient UI state and persisting it would overwrite any previously
        saved model preference on the next app launch.

        Args:
            key: Config dict key (e.g., "ollama_ip", "profile").
            value: New string value to store.
        """
        # Guard: never persist the "Waiting..." placeholder to disk.
        # If the model_select widget fires on_select_changed before Ollama
        # responds, we'd otherwise save the placeholder and overwrite any
        # previously saved model preference.
        if key == "model" and value == "Waiting...":
            self._config_values[key] = value
            return
        self._config_values[key] = value
        self._save_config()
        # Debug log -- confirms the save actually reached disk. If this doesn't
        # show up, _save_config is silently failing (IOError path) and we need
        # to investigate that separately.
        if key == "model":
            saved = json.loads(self.config_file.read_text())
            self.write_main_log(f"[dim]config save: model={saved.get('model')!r}[/dim]")

    def compose(self) -> ComposeResult:
        """Build the TUI layout.

        The interface is split into three vertical zones:
        1. Header -- always visible, shows app title and clock
        2. Main content area (Horizontal container):
           - Left column (35% width): File browser with two stacked ListViews
             * folder_tree: Shows parent directory hierarchy up to root
             * current_contents: Shows files/dirs in the active directory
           - Right panel (65% width): Queue, config inputs, console output
        3. Footer -- always visible, shows keyboard shortcut hints

        Config values are read from _config_values for initial UI state. This dict
        is kept in sync with disk via event handlers and on_unmount().
        """
        yield Header(show_clock=True)

        # Read saved config values for UI initialization. These come from
        # _config_values which was populated by the sync step in __init__ if a
        # previous config file existed. After init, all reads go through this
        # dict which is kept in sync via event handlers.
        saved_ollama = self._config_values.get("ollama_ip", "http://127.0.0.1:11434")
        saved_profile = self._config_values.get("profile", "Generic / Catch-all")
        saved_model = self._config_values.get("model", "")

        with Horizontal(id="main_content"):
            # Left side: file browser (two panes stacked vertically)
            with Vertical(id="browser_column"):
                yield Static("Current Folder", id="breadcrumb")
                yield ListView(id="folder_tree")
                yield ListView(id="current_contents")

            # Right side: queue list, progress telemetry, console log
            with Vertical(id="right_panel"):
                with Vertical(id="queue_section"):
                    yield Label("Queued Files", classes="panel_title")
                    yield ListView(id="queue_view")

                with Horizontal(classes="telemetry_bar"):
                    yield ProgressBar(total=100, show_eta=True, id="progress_bar")
                    yield Static("Status: Idle | Step: --", id="status_label", classes="status_text")

                yield RichLog(id="console", highlight=True, markup=True)

        # Config row: Ollama URL input, model selector (populated async), profile selector
        with Horizontal(classes="config_section"):
            yield Static("Ollama URL:", classes="config_label")
            yield Input(value=saved_ollama.rstrip("/"), id="ollama_ip", placeholder="http://127.0.0.1:11434", classes="config_input")
            # Note: model_select is initialized with "Waiting..." placeholder.
            # _update_model_select() (called after Ollama responds) will set the actual selection
            # from saved config or default to first available model.
            yield Select([("Waiting...", "Waiting...")], value="Waiting...", id="model_select", classes="config_model")
            yield Select([(k, k) for k in self.profiles.keys()], value=saved_profile, id="profile_select", classes="config_select")

        # Action bar: queuing controls | smart queue shortcuts | pipeline execution
        with Horizontal(classes="action_bar"):
            yield Button("Queue Videos", id="btn_queue_videos", variant="primary")
            yield Button("Queue SRTs", id="btn_queue_srts", variant="primary")
            yield Button("Clear Queue", id="btn_clear_queue", variant="error")
            yield Static("", classes="spacer")
            yield Button("Remove Selected", id="btn_remove_selected", variant="warning")
            yield Static("", classes="spacer")
            yield Button("Smart: No SRTs", id="btn_smart_vids", variant="success")
            yield Button("Smart: Translate", id="btn_smart_srts", variant="warning")
            yield Button("Smart: Rebalance", id="btn_smart_rebalance", variant="default")
            yield Static("", classes="spacer")
            yield Button("Verify & Patch", id="btn_patch", variant="warning")
            yield Button("Start Pipeline", id="btn_start", variant="success")

        yield Footer()

    def on_unmount(self) -> None:
        # Save settings on exit. NOTE: we deliberately do NOT save the model here.
        # The model is already persisted by on_select_changed (user picks a model)
        # and by _update_model_select (restoring from saved config). Saving it here
        # would overwrite the saved value with whatever the widget currently shows,
        # which is models[0] (e.g. "deepseek") if the background poll hasn't
        # finished yet or if the saved model isn't in the available list.
        try:
            ollama_ip = self.query_one("#ollama_ip", Input).value.rstrip('/')
            profile = self.query_one("#profile_select", Select).value
            self._set_and_save_config("ollama_ip", ollama_ip)
            self._set_and_save_config("profile", profile)
        except Exception as e:
            # If widgets aren't ready (shouldn't happen, but defensive), skip silently.
            pass

        # Final save and log what's on disk for debugging.
        self._save_config()
        try:
            raw = json.loads(self.config_file.read_text())
            self.write_main_log(f"[dim]on_unmount config keys: {list(raw.keys())}[/dim]")
        except Exception:
            pass

    # --- Helpers (extracted from duplicated patterns) ---

    def _is_widget_in_folder_tree(self, widget) -> bool:
        """Check if a widget is a descendant of the folder_tree ListView.

        Walks up the parent chain looking for an ancestor with id="folder_tree".
        Used to distinguish clicks/selections on the folder tree (parent dir hierarchy)
        from those on current_contents (files in active directory).

        Args:
            widget: The widget to check -- typically a ListItem or its child.

        Returns:
            True if widget is inside folder_tree, False otherwise.
        """
        current = getattr(widget, 'parent', None)
        while current:
            if hasattr(current, 'id') and current.id == "folder_tree":
                return True
            current = getattr(current, 'parent', None)
        return False

    def _remove_from_queue(self, filepath: str) -> None:
        """Remove a file from the queue and refresh the UI.

        Thread-safe helper that replaces the repeated pattern of checking membership,
        removing the item, and refreshing the queue ListView. Used in multiple places
        (button handlers, keyboard handler, pipeline batch methods).

        The UI refresh is marshaled to the main thread via call_from_thread because
        this method can be called from background worker threads where Textual widgets
        are not accessible (would raise LookupError on active_app ContextVar).

        Args:
            filepath: Absolute path to remove from self.target_files.
        """
        with self._queue_lock:
            if filepath in self.target_files:
                self.target_files.remove(filepath)
        # Marshal UI refresh to main thread -- this method is called from background
        # workers where direct widget access would crash (LookupError on active_app).
        self.call_from_thread(self._refresh_queue_ui_main_thread)

    def _validate_ollama_url(self, url: str) -> bool:
        """Validate that an Ollama URL has a proper scheme (http:// or https://).

        Prevents confusing connection errors downstream by catching malformed URLs
        early and logging a clear message. Returns True if valid, False otherwise.

        Args:
            url: User-provided Ollama base URL.

        Returns:
            True if the URL has http(s) scheme, False otherwise.
        """
        return url.startswith("http://") or url.startswith("https://")

    # --- Folder Browser Logic ---

    def _init_folder_browser(self, start_dir: Path) -> None:
        """Initialize folder tree and current contents for a starting directory."""
        self._current_path = start_dir.resolve()
        self._refresh_folder_tree()
        self._refresh_current_contents()

    def _refresh_folder_tree(self) -> None:
        """Refresh the folder tree ListView showing parent directories up to root."""
        folder_tree = self.query_one("#folder_tree", ListView)
        folder_tree.clear()

        if not hasattr(self, '_current_path'):
            return

        # Show ".." at top for navigation up (with icon)
        if self._current_path != self._current_path.parent:
            item = ListItem(Label("[dim]📂[/dim] .. (parent)"))
            item.data = "PARENT"  # type: ignore[attr-defined]
            folder_tree.append(item)

        # Show current path hierarchy with tree-style icons and connectors
        try:
            parts = list(self._current_path.parts)
            for i in range(1, len(parts)):
                partial = Path(*parts[:i+1])
                if partial.is_dir():
                    name = parts[i]
                    # Build prefix: folder icon + tree connectors based on depth
                    is_last = (i == len(parts) - 1)
                    connector = "└──" if is_last else "├──"

                    # Add vertical bars for intermediate levels
                    prefix_parts = []
                    for j in range(1, i):
                        prefix_parts.append("│   ")
                    prefix_parts.append(f"{connector} 📁")

                    prefix = "".join(prefix_parts)
                    item = ListItem(Label(f"{prefix}[bold]{name}[/bold]"))
                    item.data = str(partial)  # type: ignore[attr-defined]
                    folder_tree.append(item)
        except Exception:
            pass

    def _refresh_current_contents(self) -> None:
        """Refresh the current contents ListView showing files/dirs in current_path."""
        if not hasattr(self, '_current_path'):
            return

        contents = self.query_one("#current_contents", ListView)
        contents.clear()

        valid_ext = {".mp4", ".mkv", ".ts", ".avi", ".mov", ".srt", ".m4v"}
        excluded = ("-translated.srt", "-balanced.srt")

        try:
            for item in sorted(self._current_path.iterdir()):
                entry = None  # Reset for each iteration

                if item.is_dir():
                    label = f"[bold]📁 {item.name}[/bold]"
                    entry = ListItem(Label(label))
                    entry.data = str(item)  # type: ignore[attr-defined]
                elif item.is_file() and item.suffix.lower() in valid_ext:
                    if not any(item.name.endswith(s) for s in excluded):
                        label = f"├── {item.name}"
                        entry = ListItem(Label(label))
                        entry.data = str(item.resolve())  # type: ignore[attr-defined]

                if entry is not None:
                    contents.append(entry)
        except Exception as e:
            self.write_main_log(f"[red]Error loading folder: {e}[/red]")

    def _handle_folder_click(self, event) -> None:
        """Handle clicks on folder tree or contents list items.

        This is the central dispatch for all left-panel navigation. It determines
        which ListView was clicked (folder_tree vs current_contents), interprets
        the stored data accordingly, and updates state + views.

        Why we distinguish the two ListViews: both are children of browser_column,
        so Textual's on_click fires for clicks in either one. We walk up from the
        clicked widget to check which ListView owns it -- folder_tree items represent
        path hierarchy levels (parent dirs), while current_contents items represent
        files/dirs in the active directory. The same click handler must behave
        differently for each: clicking a parent dir in folder_tree navigates UP,
        while clicking a subfolder in current_contents navigates INTO it.

        Important: we only process ListItem children of folder_tree or current_contents.
        Clicks on other widgets inside browser_column (e.g., Select dropdowns) are
        ignored to avoid corrupting Textual's internal event routing -- see issue where
        clicking the profile/model selects caused a NoneType crash in _forward_event.
        """
        widget = event.widget

        # Walk up from the clicked widget to find its owning ListItem.
        # Textual events fire on the innermost widget (e.g., a Static label),
        # not the container, so we need to climb the parent chain first.
        while widget and not isinstance(widget, ListItem):
            widget = widget.parent
        if not widget:
            return

        data = getattr(widget, "data", None)
        if not data:
            return

        # Determine which ListView was clicked using the shared helper.
        is_in_folder_tree = self._is_widget_in_folder_tree(widget)

        # --- Navigation logic ---
        if data == "PARENT":
            # Clicked the ".. (parent)" entry in folder_tree -- go up one level.
            self._current_path = self._current_path.parent.resolve()
        elif Path(data).is_dir():
            # It's a directory. Update _current_path to point at it so subsequent
            # operations (like Smart queue) scan from the correct location.
            self._current_path = Path(data).resolve()

            if is_in_folder_tree:
                # Clicked a parent dir in folder_tree -- refresh both views to show
                # the selected path's contents without navigating into it.
                self._refresh_folder_tree()
                self._refresh_current_contents()
            else:
                # Clicked a subfolder in current_contents -- navigate INTO it by
                # updating _current_path and refreshing views to show its children.
                # This is critical for Smart queue operations: without this, _current_path
                # stays at the parent directory, so rglob scans from the wrong base path
                # and misses files in subdirectories you just clicked into.
                self._refresh_folder_tree()
                self._refresh_current_contents()

        # Persist last directory so we can restore it on next launch via config.
        self._set_and_save_config("last_directory", str(self._current_path))

        if Path(data).is_file():
            filepath = str(Path(data))
            with self._queue_lock:
                if filepath not in self.target_files:
                    self.target_files.append(filepath)
            self._refresh_queue_ui_main_thread()
            self.write_main_log(f"Queued: {os.path.basename(filepath)}")

        # Refresh both views after any folder/file click to keep them in sync.
        if is_in_folder_tree or Path(data).is_dir():
            self._refresh_folder_tree()
            self._refresh_current_contents()

    def on_click(self, event) -> None:
        """Route clicks to folder browser handler if they hit the left panel."""
        widget = event.widget
        # Check if click is in the browser column
        current = getattr(widget, 'parent', None)
        while current:
            if hasattr(current, 'id') and current.id == "browser_column":
                self._handle_folder_click(event)
                return
            current = getattr(current, 'parent', None)

    def on_mount(self) -> None:
        self.write_main_log("ShowaSync v3 loaded. Checking prerequisites...")
        missing_tools = self._check_prerequisites()
        if missing_tools:
            self.write_main_log(f"[yellow]Warning: Missing tools: {', '.join(missing_tools)}[/yellow]")

        # Initialize folder browser with current directory
        saved_dir = self._config_values.get("last_directory", None)
        start_dir = Path(saved_dir).resolve() if saved_dir and Path(saved_dir).is_dir() else Path.cwd().resolve()
        self._init_folder_browser(start_dir)

        self.write_main_log("Polling Ollama in the background...")
        ip = self.query_one("#ollama_ip", Input).value.rstrip('/')
        self.poll_models_worker(ip)

    # --- File Browser Logic ---

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "btn_queue_videos":
            self.action_queue_type("video")
        elif btn_id == "btn_queue_srts":
            self.action_queue_type("srt")
        elif btn_id == "btn_clear_queue":
            self.action_clear_queue()
        elif btn_id == "btn_remove_selected":
            with self._queue_lock:
                if not self.target_files:
                    return
                filepath = self.target_files[0]
                filename = os.path.basename(filepath)
                self.target_files.pop(0)
            self._refresh_queue_ui_main_thread()
            self.write_main_log(f"[dim]Removed: {filename}[/dim]")
        elif btn_id == "btn_smart_vids":
            self.action_smart_queue("vids_no_srt")
        elif btn_id == "btn_smart_srts":
            self.action_smart_queue("untra_srt")
        elif btn_id == "btn_smart_rebalance":
            self.action_smart_queue("needs_rebalance")
        elif btn_id == "btn_start":
            ip = self.query_one("#ollama_ip", Input).value.rstrip('/')
            model = self.query_one("#model_select", Select).value

            # Validate Ollama URL before starting the pipeline.
            if not self._validate_ollama_url(ip):
                self.write_main_log(f"[red]ERROR: Invalid Ollama URL: {ip}[/red]")
                return

            config = {
                "ip": ip,
                "model": model,
                "run_ffmpeg": True,
                "run_whisper": True,
                "run_translate": True,
                "run_rebalance": True,
                "run_clean": True,
            }
            self.execute_pipeline_batch(config)
        elif btn_id == "btn_patch":
            ip = self.query_one("#ollama_ip", Input).value.rstrip('/')
            model = self.query_one("#model_select", Select).value

            # Validate Ollama URL before starting the patcher.
            if not self._validate_ollama_url(ip):
                self.write_main_log(f"[red]ERROR: Invalid Ollama URL: {ip}[/red]")
                return

            self._set_and_save_config("ollama_ip", ip)
            self._set_and_save_config("model", model)
            self.execute_patcher_batch(ip, model)

    def on_list_view_item_selected(self, event) -> None:
        """Handle item selection from browser list (keyboard Enter/Space)."""
        item = event.item
        data = getattr(item, "data", None)
        if not data or data == "PARENT":
            return

        # Determine which ListView triggered the selection using shared helper.
        is_in_folder_tree = self._is_widget_in_folder_tree(event.widget)

        # Handle folder navigation (only from current_contents via keyboard)
        if Path(data).is_dir():
            self._current_path = Path(data).resolve()
            self._set_and_save_config("last_directory", str(self._current_path))
            self._refresh_folder_tree()
            self._refresh_current_contents()
            return

        # Handle file selection
        if not Path(data).is_file():
            return
        filepath = str(Path(data))
        with self._queue_lock:
            if filepath not in self.target_files:
                self.target_files.append(filepath)
        self._refresh_queue_ui_main_thread()
        self.write_main_log(f"Queued: {os.path.basename(filepath)}")

    def on_input_changed(self, event: Input.Changed) -> None:
        """Persist Ollama IP whenever it changes."""
        if event.input.id == "ollama_ip":
            self._set_and_save_config("ollama_ip", event.value.rstrip('/'))

    def on_select_changed(self, event: Select.Changed) -> None:
        """Persist profile and model whenever they change.

        Skips saving the model when the value is the "Waiting..." placeholder,
        because that's a transient UI state, not a real user selection. Saving it
        would overwrite any previously persisted model preference on the next
        app launch.
        """
        if event.select.id == "profile_select":
            self._set_and_save_config("profile", event.value)
        elif event.select.id == "model_select":
            if event.value == "Waiting...":
                return  # Don't persist the placeholder
            self._set_and_save_config("model", event.value)

    def _refresh_queue_ui_main_thread(self) -> None:
        queue = self.query_one("#queue_view", ListView)
        queue.clear()
        for f in self.target_files:
            queue.append(ListItem(Label(os.path.basename(f))))

    def on_key(self, event: Key) -> None:
        if event.key == "delete":
            with self._queue_lock:
                if not self.target_files:
                    return
                filepath = self.target_files[0]
                filename = os.path.basename(filepath)
                self.target_files.pop(0)
            self._refresh_queue_ui_main_thread()
            self.write_main_log(f"[dim]Removed: {filename}[/dim]")
            event.stop()

    def action_clear_queue(self) -> None:
        with self._queue_lock:
            self.target_files.clear()
        self._refresh_queue_ui_main_thread()
        self.write_main_log("[yellow]Queue cleared.[/yellow]")

    def action_queue_type(self, file_type: str) -> None:
        if file_type == "video":
            valid_extensions = {".mp4", ".mkv", ".ts", ".avi", ".mov", ".m4v"}
        else:
            valid_extensions = {".srt"}

        added_count = 0
        skipped_count = 0
        scan_dir = getattr(self, '_current_path', Path("."))

        try:
            for path in scan_dir.rglob("*"):
                try:
                    if not path.is_file():
                        continue
                    if path.suffix.lower() not in valid_extensions:
                        continue
                    if file_type == "srt" and (path.name.endswith('-translated.srt') or path.name.endswith('-balanced.srt')):
                        continue
                    filepath = str(path.resolve())
                    with self._queue_lock:
                        if filepath not in self.target_files:
                            self.target_files.append(filepath)
                            added_count += 1
                except (OSError, IOError) as e:
                    skipped_count += 1
                    self.write_main_log(f"[yellow]Skipped {os.path.basename(str(path))}: {e}[/yellow]")
        except (OSError, IOError) as e:
            self.write_main_log(f"[red]Error scanning directories: {e}[/red]")

        if added_count > 0:
            with self._queue_lock:
                snapshot = list(self.target_files)
            # Refresh UI directly (we're already on the main thread).
            def _refresh():
                queue = self.query_one("#queue_view", ListView)
                queue.clear()
                for f in self.target_files:
                    queue.append(ListItem(Label(os.path.basename(f))))
            _refresh()

            type_name = "videos" if file_type == "video" else "SRTs"
            msg = f"[green]Successfully queued {added_count} {type_name}.[/green]"
            if skipped_count > 0:
                msg += f" Skipped {skipped_count} due to errors."
            self.write_main_log(msg)
        else:
            self.write_main_log(f"[yellow]No new valid {file_type} files found in the current directory.[/yellow]")

    def action_smart_queue(self, mode: str) -> None:
        added_count = 0
        skipped_count = 0
        vid_extensions = {".mp4", ".mkv", ".ts", ".avi", ".mov", ".m4v"}

        scan_dir = getattr(self, '_current_path', Path("."))
        self.write_main_log(f"[dim]Smart queue '{mode}': scanning from {scan_dir.resolve()}[/dim]")

        # Wrap the entire rglob in a try/except because broken symlinks or unresponsive
        # mounts ([Errno 5] I/O error) raise OSError during iteration, which the inner
        # per-file try/except can't catch since it fires before entering the loop body.
        try:
            paths = list(scan_dir.rglob("*"))
        except (OSError, IOError) as e:
            self.write_main_log(f"[red]Error scanning directory tree: {e}[/red]")
            return

        for path in paths:
            try:
                if not path.is_file():
                    continue

                filepath = str(path.resolve())
                dir_path = os.path.dirname(filepath)
                base_name = self._get_base_filename_v3(filepath, is_srt=(path.suffix.lower() == '.srt'))

                if mode == "vids_no_srt" and path.suffix.lower() in vid_extensions:
                    srt_a = self._find_srt_file_flexible(dir_path, base_name, "")
                    srt_b = self._find_srt_file_flexible(dir_path, base_name, "_16k_mono")
                    has_srt_a = srt_a and os.path.exists(srt_a)
                    has_srt_b = srt_b and os.path.exists(srt_b)
                    if not (has_srt_a or has_srt_b):
                        if filepath not in self.target_files:
                            with self._queue_lock:
                                self.target_files.append(filepath)
                            added_count += 1
                            self.write_main_log(f"[green]+ queued[/green] {os.path.basename(filepath)}")
                    else:
                        found = "srt" if has_srt_a else "_16k_mono.srt"
                        self.write_main_log(f"[dim]- skipped (has SRT)[/dim] {os.path.basename(filepath)} ({found})")

                elif mode == "untra_srt" and path.suffix.lower() == ".srt":
                    if not path.name.endswith('-translated.srt') and not path.name.endswith('-balanced.srt'):
                        has_trans = bool(self._find_srt_file_flexible(dir_path, base_name, "-translated"))
                        if not has_trans and filepath not in self.target_files:
                            with self._queue_lock:
                                self.target_files.append(filepath)
                            added_count += 1

                elif mode == "needs_rebalance" and path.name.endswith('-translated.srt'):
                    bal_path = self._find_srt_file_flexible(dir_path, base_name, "-balanced")
                    needs_balance = False

                    if not bal_path or not os.path.exists(bal_path):
                        needs_balance = True
                    else:
                        if os.path.getmtime(filepath) > os.path.getmtime(bal_path):
                            needs_balance = True

                    if needs_balance and filepath not in self.target_files:
                        with self._queue_lock:
                            self.target_files.append(filepath)
                        added_count += 1
            except (OSError, IOError) as e:
                # Per-file error handling -- skip this file and continue scanning.
                skipped_count += 1
                self.write_main_log(f"[yellow]Skipped {os.path.basename(str(path))}: {e}[/yellow]")

        summary = f"Smart Queue: Added {added_count} files"
        if skipped_count > 0:
            summary += f", Skipped {skipped_count} due to errors"

        with self._queue_lock:
            snapshot = list(self.target_files)
        # Refresh UI directly (we're already on the main thread).
        def _refresh():
            queue = self.query_one("#queue_view", ListView)
            queue.clear()
            for f in self.target_files:
                queue.append(ListItem(Label(os.path.basename(f))))
        _refresh()

        self.write_main_log(f"[green]{summary}.[/green]")

    def action_poll_models(self) -> None:
        ip = self.query_one("#ollama_ip", Input).value.rstrip('/')
        self.write_main_log(f"Initiating manual poll for {ip}...")
        self.poll_models_worker(ip)

    # --- Keyboard shortcut actions ---

    def action_queue_videos(self) -> None:
        self.action_queue_type("video")

    def action_queue_srts(self) -> None:
        self.action_queue_type("srt")

    def action_smart_vids(self) -> None:
        self.action_smart_queue("vids_no_srt")

    def action_smart_translate(self) -> None:
        self.action_smart_queue("untra_srt")

    def action_smart_rebalance(self) -> None:
        self.action_smart_queue("needs_rebalance")

    def action_start_pipeline(self) -> None:
        btn = self.query_one("#btn_start", Button)
        if btn.visible:
            btn.press()

    def action_verify_patch(self) -> None:
        btn = self.query_one("#btn_patch", Button)
        if btn.visible:
            btn.press()

    # --- Background Workers ---

    @work(thread=True)
    def poll_models_worker(self, ip: str) -> None:
        """Poll the local Ollama instance to discover available models.

        Runs in a background thread (via Textual's @work decorator) so it doesn't
        block the TUI while waiting for network response. The Ollama API endpoint
        /api/tags returns a JSON list of loaded models -- we extract just the names
        and update the model_select UI widget on the main thread.

        Why background thread: HTTP requests can hang or timeout; doing this on the
        main TUI thread would freeze the interface until Ollama responds (or fails).

        Args:
            ip: Base URL of the Ollama instance (e.g., "http://127.0.0.1:11434").
                Passed from on_mount() or action_poll_models(). Must include scheme.

        Side effects:
            - Updates #model_select Select widget with discovered model names
            - Writes success/error message to console log
            - If Ollama unreachable, leaves model_select at "Waiting..." state
        """
        try:
            response = requests.get(f"{ip}/api/tags", timeout=5)
            response.raise_for_status()
            models = [m['name'] for m in response.json().get('models', [])]

            # Update UI on main thread -- can't touch Textual widgets from background threads.
            self.call_from_thread(self._update_model_select, models)
            self.write_thread_log(f"[green]Success. Found {len(models)} models.[/green]")
        except Exception as e:
            self.write_thread_log(f"[red]Error reaching Ollama: {e}[/red]")

    def _update_model_select(self, models: list) -> None:
        """Populate the model_select UI widget with discovered Ollama models.

        Called from poll_models_worker on the main thread (via call_from_thread).
        If a previously saved model name is still available, restore it as selected;
        otherwise default to the first model in the list. Saves the selection to
        _config_values so it persists across app restarts.

        Why we always save explicitly: Textual's on_select_changed event does not
        reliably fire for programmatic value changes (it depends on whether the new
        value differs from the current one and on widget state). Since _set_and_save_config
        is called by that handler too, relying on it alone would miss saves triggered
        here. So we save directly after every select.value assignment.

        Args:
            models: List of model names returned by Ollama /api/tags endpoint.
                Empty list means no models loaded -- show placeholder option.
        """
        select = self.query_one("#model_select", Select)
        if models:
            # Build display options from model names (label and value are identical)
            select.set_options([(m, m) for m in models])

            # Prefer saved model if it exists; otherwise use first available model.
            saved_model = self._config_values.get("model")
            self.write_main_log(f"[dim]Loaded config model: {saved_model!r}[/dim]")
            self.write_main_log(f"[dim]Ollama models ({len(models)}): {[m for m in models]}[/dim]")
            # Only use placeholder if there's no saved model at all.
            # Always preserve the saved model even if Ollama doesn't currently have it loaded.
            if saved_model and saved_model != "Waiting...":
                select.value = saved_model
                # Add saved model to options if not already there (might be unloaded from Ollama)
                if saved_model not in models:
                    current_options = select.options
                    select.set_options([(m, m) for m in models] + [(saved_model, saved_model)])
                self._set_and_save_config("model", saved_model)
                self.write_main_log(f"[green]Restored model: {saved_model}[/green]")
            else:
                select.value = models[0]
                self._set_and_save_config("model", models[0])
                self.write_main_log(f"[dim]No saved model available; using first model: {models[0]}[/dim]")
        elif self._config_values.get("model"):
            # Ollama returned no models but we have a saved selection -- offer it
            # as a selectable option so the user can still pick it. Without this,
            # the dropdown only shows "No models found" and the saved model is
            # invisible, so it never gets re-saved on the next launch.
            saved = self._config_values["model"]
            select.set_options([(saved, saved), ("No models found", "None")])
            select.value = saved
            self._set_and_save_config("model", saved)
            self.write_main_log(f"[dim]Ollama unavailable; offering saved model: {saved!r}[/dim]")
        else:
            # No models AND no saved selection -- show placeholder.
            # (The disk config fallback via self.config was removed -- self.config is
            # a Textual ConfigManager, not a dict, so .get() doesn't work as intended.)
            select.set_options([("No models found", "None")])

    # --- Logging & Telemetry ---

    def write_main_log(self, message: str) -> None:
        """Write a message to the console RichLog widget.

        This is the main-thread logging method. For use from background threads,
        call write_thread_log() instead -- it will marshal the call to the main thread.
        During shutdown, the widget may already be torn down -- silently drop in that case.
        """
        try:
            self.query_one("#console", RichLog).write(message)
        except Exception:
            pass

    def write_thread_log(self, message: str) -> None:
        """Thread-safe logging that marshals to main thread if needed.

        Textual widgets can only be modified from the main (UI) thread. If this method
        is called from a background worker thread (like poll_models_worker or pipeline
        execution), it uses call_from_thread() to safely invoke write_main_log on the
        main thread. If already on the main thread, calls directly.

        During app shutdown, widgets may already be torn down. In that case, silently
        drop the message rather than crashing -- the app is exiting anyway.

        Args:
            message: Log message string (supports Textual markup for styling).
        """
        try:
            if threading.current_thread() == threading.main_thread():
                self.write_main_log(message)
            else:
                self.call_from_thread(self.write_main_log, message)
        except Exception:
            # Widgets destroyed during shutdown -- silently drop.
            pass

    def update_telemetry(self, current: int, total: int, start_time: float, step_name: str = "Processing") -> None:
        """Update progress bar and status text with pipeline telemetry.

        Calculates elapsed time, average speed, ETA, and percentage complete based
        on (current, total) work units and the wall-clock start time. Updates both
        the ProgressBar widget (#progress_bar) and the status Static widget.

        Thread-safe: marshals widget updates to main thread if called from background.

        Args:
            current: Number of completed work units (batches processed).
            total: Total number of work units in this batch.
            start_time: Wall-clock time when processing began (from time.time()).
            step_name: Label for the current pipeline stage (e.g., "FFmpeg", "WhisperX").
        """
        if current == 0: return
        elapsed = time.time() - start_time
        avg_time = elapsed / current
        eta = int((total - current) * avg_time)
        speed = 60.0 / avg_time if avg_time > 0 else 0
        pct = min(100.0, (current / total) * 100) if total > 0 else 0
        status_text = f"Step: {step_name} | Batch: {current}/{total} | Speed: {speed:.1f}/min | ETA: {timedelta(seconds=eta)}"
        if threading.current_thread() == threading.main_thread():
            self._set_progress(pct, status_text)
        else:
            self.call_from_thread(self._set_progress, pct, status_text)

    def _set_progress(self, pct: float, text: str) -> None:
        self.query_one("#progress_bar", ProgressBar).progress = pct
        self.query_one("#status_label", Static).update(text)

    # --- Data Parsing & Anchoring ---

    def get_base_prompt(self, profile_key: str) -> str:
        """Build the system prompt for LLM translation.

        Thread-safe version that takes the profile key as an argument instead of
        querying the widget tree (which would crash from a background thread).

        Args:
            profile_key: The display name of the selected context profile.

        Returns:
            System prompt string combining the profile context with formatting rules.
        """
        context = self.profiles.get(profile_key, "")
        return f"""{context}
CRITICAL LAWS OF RE-FORMATTING:
1. Preserve the original SRT line numbers and timestamps exactly.
2. Split continuous walls of text into new lines when the speaker changes. Start new speakers with a hyphen (-).
3. Return ONLY the finished SRT block output. Do not include markdown blocks or notes."""

    def _get_profile_key(self) -> str:
        """Get the currently selected profile key from the UI widget tree.

        Must be called on the main thread (or marshaled there via call_from_thread).
        Returns the value of the #profile_select Select widget.
        """
        return self.query_one("#profile_select", Select).value

    def parse_srt_blocks(self, file_path: str) -> dict:
        if not os.path.exists(file_path): return {}
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
        blocks = re.split(r'\n\s*\n', content)
        parsed = {}
        for b in blocks:
            match = re.match(r'^(\d+)\n', b)
            if match: parsed[int(match.group(1))] = b
        return parsed

    def anchor_timestamps(self, raw_llm_text: str, original_jp_blocks: dict) -> str:
        llm_blocks = re.split(r'\n\s*\n', raw_llm_text.strip())
        anchored_output = []

        for block in llm_blocks:
            lines = block.strip().split('\n')
            if len(lines) >= 3:
                try:
                    idx = int(lines[0].strip())
                    if idx in original_jp_blocks:
                        orig_lines = original_jp_blocks[idx].split('\n')
                        true_time = orig_lines[1]
                        text = "\n".join(lines[2:])
                        anchored_output.append(f"{idx}\n{true_time}\n{text}")
                    else:
                        anchored_output.append(block)
                except ValueError:
                    anchored_output.append(block)
            else:
                anchored_output.append(block)

        return "\n\n".join(anchored_output)

    def time_to_ms(self, t_str: str) -> int:
        h, m, s, ms = map(int, re.split('[:,]', t_str))
        return (h * 3600000) + (m * 60000) + (s * 1000) + ms

    def ms_to_time(self, ms: int) -> str:
        td = timedelta(milliseconds=ms)
        h, rem = divmod(int(td.total_seconds()), 3600)
        m, s = divmod(rem, 60)
        return f"{h:02}:{m:02}:{s:02},{ms % 1000:03}"

    def rebalance_srt_file(self, input_srt: str) -> None:
        self.write_thread_log(f"Rebalancing {os.path.basename(input_srt)}...")

        # Guard against processing already-balanced files -- writing output to the
        # same path would silently overwrite the previous result. Skip with a log
        # message so users know why no new file was produced.
        if input_srt.endswith('-balanced.srt'):
            self.write_thread_log(f"Skipping {os.path.basename(input_srt)}: already balanced.")
            return

        blocks = self.parse_srt_blocks(input_srt)
        new_blocks, new_index = [], 1

        for idx in sorted(blocks.keys()):
            block = blocks[idx]
            lines = block.split('\n')
            if len(lines) < 3: continue

            time_line = lines[1]
            clean_text = "\n".join(lines[2:]).replace('\n', ' ').strip()

            if len(clean_text) > 85:
                t_match = re.search(r'(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})', time_line)
                if t_match:
                    start_ms, end_ms = self.time_to_ms(t_match.group(1)), self.time_to_ms(t_match.group(2))
                    dur = end_ms - start_ms

                    mid = len(clean_text) // 2
                    split_idx = mid
                    for i in range(mid, len(clean_text)):
                        if clean_text[i] in '.?!':
                            split_idx = i + 1
                            break

                    part1, part2 = clean_text[:split_idx].strip(), clean_text[split_idx:].strip()
                    ratio = len(part1) / len(clean_text) if len(clean_text) > 0 else 0.5
                    mid_time = int(start_ms + (dur * ratio))

                    new_blocks.append(f"{new_index}\n{self.ms_to_time(start_ms)} --> {self.ms_to_time(mid_time)}\n{part1}")
                    new_blocks.append(f"{new_index+1}\n{self.ms_to_time(mid_time)} --> {self.ms_to_time(end_ms)}\n{part2}")
                    new_index += 2
                    continue

            new_blocks.append(f"{new_index}\n{time_line}\n{chr(10).join(lines[2:])}")
            new_index += 1

        # Extract just the filename, then reassemble path with safe_output_path
        base_name = os.path.splitext(os.path.basename(input_srt))[0]
        dir_path = os.path.dirname(input_srt) or "."
        out_file = self._safe_output_path(dir_path, base_name, "-balanced")
        with open(out_file, 'w', encoding='utf-8') as f:
            f.write("\n\n".join(new_blocks))
        self.write_thread_log(f"Rebalanced file saved: {os.path.basename(out_file)}")

    # --- Filename Helper (v3: handles _16k_mono SRT files correctly) ---

    def _get_base_filename_v3(self, filepath: str, is_srt: bool = False) -> str:
        """Get the base filename for pipeline operations.

        Strips common pipeline suffixes (_16k_mono, -translated, -balanced) to derive a
        canonical base name used consistently across all modes. Previously SRT inputs
        preserved _16k_mono while video inputs stripped it -- this inconsistency caused
        Smart: No SRT and Smart: Translate to look for filenames like 'foo_16k_mono-translated.srt'
        that don't exist (the pipeline writes 'foo-translated.srt').

        Args:
            filepath: Full path to the file.
            is_srt: Deprecated -- kept for API compatibility but no longer affects logic.

        Returns:
            Base filename without extension or pipeline-specific suffixes.
        """
        filename = os.path.basename(filepath)
        root, _ = os.path.splitext(filename)

        # Strip all pipeline stage suffixes uniformly so different modes match correctly.
        root = root.replace('_16k_mono', '').replace('-translated', '').replace('-balanced', '')
        return root

    def _safe_output_path(self, dir_path: str, long_filename: str, suffix: str = "") -> str:
        """Generate a safe output path that won't exceed OS path length limits.

        On Windows (and WSL accessing Windows drives), paths are limited to ~260 characters.
        Japanese filenames can be very long, and adding pipeline suffixes (-translated, -balanced)
        can push the total path over the limit, causing OSError: [Errno 36] File name too long.

        This helper detects when the output path would be too long and uses a shorter
        canonical name (truncated base + hash) to stay within limits while maintaining
        uniqueness.

        Args:
            dir_path: Directory to write the file in.
            long_filename: The desired filename (may be too long).
            suffix: Optional suffix to append before .srt (e.g., "-translated", "-balanced").

        Returns:
            A safe output path. If the long filename works, returns it unchanged.
            Otherwise, returns a path with a truncated filename that includes a hash
            for uniqueness.
        """
        # Try the original path first — append suffix + .srt to get the full desired filename
        safe_filename = f"{long_filename}{suffix}.srt"
        candidate = os.path.join(dir_path, safe_filename)
        if len(candidate) <= 260:  # Windows MAX_PATH limit
            return candidate

        # Path too long -- generate a shorter canonical name
        # Use a hash of the original filename to ensure uniqueness
        import hashlib
        filename_hash = hashlib.md5(long_filename.encode('utf-8')).hexdigest()[:8]

        # Calculate available space for filename
        # Path = dir_path + "/" + filename
        dir_path_len = len(dir_path) + 1  # +1 for the "/" separator
        max_filename_len = 260 - dir_path_len  # Leave some safety margin

        base_name = os.path.splitext(os.path.basename(long_filename))[0]

        # Calculate how much we need to truncate
        suffix_len = len(suffix) + 5  # +5 for ".srt" extension
        available = max_filename_len - suffix_len

        if len(base_name) > available:
            # Truncate base name but keep it readable
            # Reserve space for "_XXXXXXXX" hash suffix
            truncated = base_name[:max(10, available - 9)] + f"_{filename_hash}"
        else:
            truncated = base_name

        safe_filename = f"{truncated}{suffix}.srt"
        safe_path = os.path.join(dir_path, safe_filename)

        # Verify the safe path is actually within limits
        if len(safe_path) > 260:
            # Emergency truncation - use just the hash
            safe_filename = f"{filename_hash}{suffix}.srt"
            safe_path = os.path.join(dir_path, safe_filename)
            self.write_thread_log(f"[yellow]WARNING: Path still too long, using hash-only name: {safe_filename}[/yellow]")

        # Log the truncation so users know what happened
        self.write_thread_log(f"[dim]Path truncated for safety: {os.path.basename(long_filename)[:50]}... -> {os.path.basename(safe_path)}[/dim]")

        return safe_path

    def _find_srt_file_flexible(self, dir_path: str, base_name: str, suffix: str) -> str:
        """Find an SRT file with flexible name matching for path-truncated files.

        When filenames are truncated for path length safety, downstream code needs to
        find the resulting files. This helper tries multiple name variants:
        1. Original exact name (works when no truncation occurred)
        2. Pattern match for files with the exact suffix (for truncated files)

        Args:
            dir_path: Directory to search in.
            base_name: The original base filename (may be truncated in some contexts).
            suffix: File suffix to look for (e.g., "-translated", "_16k_mono", "-balanced").

        Returns:
            Full path to the found file, or empty string if not found.
        """
        # Try exact match first (fast path for non-truncated files)
        exact_path = os.path.join(dir_path, f"{base_name}{suffix}.srt")
        if os.path.exists(exact_path):
            return exact_path

        # For truncated files, look for files with the suffix that share a prefix with base_name.
        # This avoids matching unrelated files that happen to end with the same suffix.
        if suffix:
            import glob
            # Use base_name as a prefix filter: only match files starting with base_name[:20] + suffix.srt
            prefix = base_name[:20] if len(base_name) > 20 else base_name
            pattern = os.path.join(dir_path, f"{prefix}{suffix}.srt")
            matches = glob.glob(pattern)
            if matches:
                return max(matches, key=os.path.getmtime)

        return ""

    def _get_base_filename(self, filepath: str) -> str:
        """Legacy alias -- delegates to _get_base_filename_v3 for video files."""
        return self._get_base_filename_v3(filepath, is_srt=False)

    # --- Primary Execution Engines ---

    @work(thread=True)
    def execute_pipeline_batch(self, config: dict) -> None:
        """Execute the full translation pipeline on all queued files.

        Runs sequentially through each file in self.target_files, applying these steps:
          1. FFmpeg - Extract audio from video (.mkv/.mp4/.ts etc.) to mono WAV
          2. WhisperX - Transcribe Japanese audio -> SRT with timestamps
          3. Ollama Translation - Translate Japanese SRT -> English using LLM
          4. Rebalance - Split long subtitle lines (>85 chars) for readability
          5. Cleanup - Remove temporary files (WAV, JSON, VTT, TXT, TSV)

        Each step is optional based on config flags (run_ffmpeg, run_whisper, etc.).
        If any step fails for a file, the pipeline continues to the next file.

        Thread: Runs in background thread (@work). All UI updates use call_from_thread.
        Args:
            config: Dict with boolean flags for each pipeline stage and connection info:
                {"ip": str, "model": str, "run_ffmpeg": bool, "run_whisper": bool,
                 "run_translate": bool, "run_rebalance": bool, "run_clean": bool}
        """
        if not self.target_files:
            self.write_thread_log("[red]ERROR: No files selected in queue.[/red]")
            return

        # Snapshot the queue under lock to protect against concurrent UI mutations.
        with self._queue_lock:
            queue_snapshot = list(self.target_files)
        total_files = len(queue_snapshot)

        # Capture profile key on main thread before entering background work,
        # since get_base_prompt now takes it as an argument (thread safety).
        profile_key = self.call_from_thread_sync(self._get_profile_key) if hasattr(self, '_profile_cache') else "Generic / Catch-all"
        # Fallback: read from config values which are safe to access from any thread.
        profile_key = self._config_values.get("profile", "Generic / Catch-all")

        self.write_thread_log(f"\n[bold magenta]=== INITIATING BATCH PIPELINE ({total_files} Files) ===[/bold magenta]")

        success_count = 0
        fail_count = 0

        for idx, original_target in enumerate(queue_snapshot):
            target = original_target
            file_ext = os.path.splitext(target)[1]
            target_dir = os.path.dirname(os.path.abspath(target)) or "."
            base_name = self._get_base_filename(target)
            pipeline_failed = False

            self.write_thread_log(f"\n[bold cyan]--- PROCESSING [{idx+1}/{total_files}]: {os.path.basename(target)} ---[/bold cyan]")

            wav_out = os.path.join(target_dir, f"{base_name}_16k_mono.wav")

            # Step 1: FFmpeg - Extract audio from video
            if config["run_ffmpeg"] and file_ext.lower() in ['.mkv', '.mp4', '.ts', '.avi', '.mov', '.m4v']:
                self.write_thread_log("Extracting audio via FFmpeg...")
                try:
                    start_time = time.time()
                    result = subprocess.run(
                        ["ffmpeg", "-i", target, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav_out, "-y"],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="ignore",
                        timeout=300
                    )
                    if result.returncode == 0 and os.path.exists(wav_out):
                        target = wav_out
                        self.write_thread_log("[green]FFmpeg extraction complete.[/green]")
                        self.call_from_thread(self._set_progress, 25, f"Step: FFmpeg | File: {idx+1}/{total_files} | Complete")
                    else:
                        stderr_msg = result.stderr.strip() if result.stderr else "Unknown error"
                        raise FileNotFoundError(f"FFmpeg failed: {stderr_msg}")
                except subprocess.TimeoutExpired:
                    self.write_thread_log("[red]FFmpeg Error: Process timed out after 300 seconds[/red]")
                    pipeline_failed = True
                except Exception as e:
                    # Always set pipeline_failed for any exception -- prevents WhisperX from
                    # running on a non-existent WAV file.
                    pipeline_failed = True
                    self.write_thread_log(f"[red]FFmpeg Error: {e}[/red]")

            # Step 2: WhisperX - Transcribe audio to Japanese SRT
            # Uses large-v3 model for best accuracy on Japanese. Outputs mono SRT with
            # timestamps aligned to the extracted WAV file. Max line width/count limits
            # prevent overly wide/subtitle blocks that would need rebalancing later.
            if config["run_whisper"] and target.endswith('.wav') and not pipeline_failed:
                self.write_thread_log("Running WhisperX (Generating Source SRT)...")
                try:
                    start_time = time.time()
                    cmd = ["whisperx", target, "--model", "large-v3", "--language", "ja",
                           "--max_line_width", "40", "--max_line_count", "2",
                           "--compute_type", "float16", "--output_format", "srt",
                           "--output_dir", target_dir]

                    # Use Popen to allow proper cleanup on timeout (fixes resource leak).
                    process = subprocess.Popen(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        encoding="utf-8", errors="ignore"
                    )
                    try:
                        stdout, stderr = process.communicate(timeout=600)
                        if process.returncode == 0:
                            expected_srt = self._find_srt_file_flexible(target_dir, base_name, "_16k_mono")
                            if expected_srt and os.path.exists(expected_srt):
                                target = expected_srt
                                self.write_thread_log("[green]WhisperX transcription complete.[/green]")
                                self.call_from_thread(self._set_progress, 50,
                                                     f"Step: WhisperX | File: {idx+1}/{total_files} | Complete")
                            else:
                                raise FileNotFoundError("WhisperX did not produce an SRT file.")
                        else:
                            stderr_msg = stderr.strip() if stderr else "Unknown error"
                            raise RuntimeError(f"WhisperX failed: {stderr_msg}")
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                        self.write_thread_log("[red]WhisperX Error: Process timed out after 600 seconds[/red]")
                        pipeline_failed = True
                except Exception as e:
                    pipeline_failed = True
                    self.write_thread_log(f"[red]WhisperX Error: {e}[/red]")

            # Step 3: Ollama Translation - Translate Japanese SRT to English
            if config["run_translate"] and target.endswith('.srt') and not pipeline_failed:
                if not Client:
                    self.write_thread_log("[red]ERROR: 'ollama' package missing.[/red]")
                    pipeline_failed = True
                else:
                    ollama_client = Client(host=config["ip"])
                    output_srt = self._safe_output_path(target_dir, base_name, "-translated")
                    self.write_thread_log(f"Starting LLM Translation: {os.path.basename(output_srt)}")

                    jp_blocks_dict = self.parse_srt_blocks(target)
                    blocks = list(jp_blocks_dict.values())

                    if not blocks:
                        self.write_thread_log("[red]ERROR: No subtitles found in SRT.[/red]")
                        pipeline_failed = True
                    else:
                        chunk_size = 20
                        total_batches = (len(blocks) // chunk_size) + (1 if len(blocks) % chunk_size != 0 else 0)

                        self.call_from_thread(self._set_progress, 0,
                                             "Status: Warming up LLM... | ETA: Calculating...")
                        start_time = time.time()

                        with open(output_srt, 'w', encoding='utf-8') as f_out:
                            for i in range(0, len(blocks), chunk_size):
                                batch_num = (i // chunk_size) + 1
                                batch_text = "\n\n".join(blocks[i:i + chunk_size])
                                self.write_thread_log(f"Sending Batch {batch_num}/{total_batches} to LLM...")

                                try:
                                    response = ollama_client.chat(
                                        model=config["model"],
                                        messages=[
                                            {"role": "system", "content": self.get_base_prompt(profile_key)},
                                            {"role": "user", "content": f"<subtitles>\n{batch_text}\n</subtitles>"}
                                        ],
                                        options={"temperature": 0.2, "num_ctx": 16384, "num_predict": -1}
                                    )
                                    clean_out = response['message']['content'].replace('```srt','').replace('```','').strip()

                                    # Anchor timestamps from original Japanese blocks to preserve sync.
                                    # The LLM may reorder/modify text but we must keep the original timing.
                                    anchored_out = self.anchor_timestamps(clean_out, jp_blocks_dict)

                                    f_out.write(anchored_out + "\n\n")
                                except Exception as e:
                                    pipeline_failed = True
                                    self.write_thread_log(f"[red]LLM Error on Batch {batch_num}: {e}[/red]")

                                self.update_telemetry(batch_num, total_batches, start_time)

                        self.call_from_thread(self._set_progress, 100,
                                             "Status: Translation Complete | Speed: 0 | ETA: 00:00:00")
                        target = output_srt

            # Step 4: Rebalance - Split long subtitle lines
            if config["run_rebalance"] and target.endswith('.srt') and not pipeline_failed:
                self.rebalance_srt_file(target)

            # Step 5: Cleanup - Remove temporary files using pathlib for safety.
            # Only clean up on success -- if any prior step failed, the temp files may
            # not exist (WhisperX cleanup) or may be locked by an in-progress process.
            if config["run_clean"] and not pipeline_failed:
                self.write_thread_log("Cleaning up temp files...")
                wav_path = Path(wav_out)
                if wav_path.exists():
                    try:
                        wav_path.unlink()
                    except OSError as e:
                        self.write_main_log(f"[yellow]Warning: Could not remove {wav_path.name}: {e}[/yellow]")

                for ext_to_del in ['.json', '.vtt', '.txt', '.tsv']:
                    junk = Path(target_dir) / f"{base_name}_16k_mono{ext_to_del}"
                    if junk.exists():
                        try:
                            junk.unlink()
                        except OSError as e:
                            self.write_main_log(f"[yellow]Warning: Could not remove {junk.name}: {e}[/yellow]")

            # Remove from queue and track results. Using helper ensures consistent
            # behavior regardless of success or failure.
            self._remove_from_queue(original_target)

            if pipeline_failed:
                self.write_thread_log(f"[red]Pipeline FAILED for {os.path.basename(target)} - moving to next file[/red]")
                fail_count += 1
            else:
                self.write_thread_log(f"[green]Completed processing: {os.path.basename(target)}[/green]")
                success_count += 1

        self.write_thread_log(f"\n[bold magenta]=== ALL PIPELINE TASKS COMPLETE ===[/bold magenta]")
        self.write_thread_log(f"Results: {success_count} succeeded, {fail_count} failed out of {total_files} files.")

    @work(thread=True)
    def execute_patcher_batch(self, ip: str, model: str) -> None:
        """Verify & Patch mode: Translate only missing segments in partially-translated SRTs.

        Unlike the full pipeline, this mode assumes some translation already exists.
        It compares the Japanese source (.srt or _16k_mono.srt) against the translated
        file (-translated.srt), finds gaps (Japanese blocks without English translations),
        and uses Ollama to translate only those missing segments.

        This is useful for:
          - Resuming interrupted translations
          - Adding new episodes where only some segments were translated previously
          - Quick fixes without re-running the full pipeline

        Args:
            ip: Ollama instance URL (e.g., "http://127.0.0.1:11434")
            model: Model name to use for translation (e.g., "llama3", "mistral")
        """
        if not self.target_files:
            self.write_thread_log("[red]ERROR: No files selected.[/red]")
            return

        # Ensure all paths are absolute and snapshot under lock.
        with self._queue_lock:
            queue_snapshot = [os.path.abspath(f) for f in self.target_files]
        self.write_thread_log(f"\n[bold magenta]=== STARTING VERIFY & PATCH ON {len(queue_snapshot)} QUEUED FILES ===[/bold magenta]")

        # Capture profile key safely from background thread.
        profile_key = self._config_values.get("profile", "Generic / Catch-all")

        for original_target in queue_snapshot:
            target = original_target
            target_dir = os.path.dirname(os.path.abspath(target)) or "."
            base_name = self._get_base_filename_v3(target, is_srt=True)

            # Patcher only works on .srt files -- skip others with a log message.
            if not target.endswith('.srt'):
                self.write_thread_log(f"[dim]Skipping {os.path.basename(target)}: Patcher requires an .srt file.[/dim]")
                self._remove_from_queue(original_target)
                continue

            trans_file = self._find_srt_file_flexible(target_dir, base_name, "-translated")
            if not trans_file or not os.path.exists(trans_file):
                self.write_thread_log(f"[dim]Skipping {os.path.basename(target)}: Could not find matching '-translated.srt' file.[/dim]")
                self._remove_from_queue(original_target)
                continue

            source_jp = self._find_srt_file_flexible(target_dir, base_name, "_16k_mono")
            if not source_jp or not os.path.exists(source_jp):
                source_jp = self._find_srt_file_flexible(target_dir, base_name, "")
                if not source_jp or not os.path.exists(source_jp):
                    self.write_thread_log(f"[dim]Skipping {os.path.basename(target)}: Could not find original Japanese source (.srt or _16k_mono.srt).[/dim]")
                    self._remove_from_queue(original_target)
                    continue

            self.write_thread_log(f"\n[bold yellow]=== INITIATING SURGICAL PATCHER: {os.path.basename(trans_file)} ===[/bold yellow]")

            # Parse both Japanese and translated SRT files into block dictionaries.
            # Keys are block indices, values are the full SRT block text (number, timestamps, text).
            jp_blocks = self.parse_srt_blocks(source_jp)
            trans_blocks = self.parse_srt_blocks(trans_file)

            if not jp_blocks:
                self.write_thread_log("[red]ERROR: No Japanese source blocks found.[/red]")
                self._remove_from_queue(original_target)
                continue

            # Find indices present in Japanese but missing from translated file.
            # These are the segments that need translation.
            missing_indices = [idx for idx in sorted(jp_blocks.keys()) if idx not in trans_blocks]

            if not missing_indices:
                self.write_thread_log("[green]No gaps found - all segments already translated.[/green]")
                self._remove_from_queue(original_target)
                continue

            self.write_thread_log(f"Found {len(missing_indices)} missing segments. Translating...")

            if not Client:
                self.write_thread_log("[red]ERROR: 'ollama' package missing.[/red]")
                self._remove_from_queue(original_target)
                continue

            ollama_client = Client(host=ip)
            chunk_size = 10
            # Calculate total number of batches needed (round up).
            total_chunks = (len(missing_indices) // chunk_size) + (1 if len(missing_indices) % chunk_size != 0 else 0)

            self.call_from_thread(self._set_progress, 0, "Status: Patching gaps... | ETA: Calculating...")
            start_time = time.time()

            for i in range(0, len(missing_indices), chunk_size):
                batch_indices = missing_indices[i:i + chunk_size]
                batch_num = (i // chunk_size) + 1
                self.write_thread_log(f"Patching Batch {batch_num}/{total_chunks}...")

                try:
                    jp_text = "\n\n".join(jp_blocks[idx] for idx in batch_indices if idx in jp_blocks)

                    response = ollama_client.chat(
                        model=model,
                        messages=[
                            {"role": "system", "content": self.get_base_prompt(profile_key)},
                            {"role": "user", "content": f"<subtitles>\n{jp_text}\n</subtitles>"}
                        ],
                        options={"temperature": 0.2, "num_ctx": 8192, "num_predict": -1}
                    )

                    clean_out = response['message']['content'].replace('```srt', '').replace('```', '').strip()
                    new_blocks = self.parse_srt_blocks_from_string(clean_out)

                    # Safety check: if LLM returned fewer blocks than expected, log the
                    # truncation so users know some segments were silently dropped.
                    if len(new_blocks) < len(batch_indices):
                        self.write_thread_log(f"[yellow]WARNING: LLM returned {len(new_blocks)} blocks but {len(batch_indices)} were requested. First {len(new_blocks)} segments patched; remaining skipped.[/yellow]")

                    for idx, block in zip(batch_indices, new_blocks):
                        trans_blocks[idx] = block

                except Exception as e:
                    self.write_thread_log(f"[red]LLM Error on Patch Batch {batch_num}: {e}[/red]")

                self.update_telemetry(batch_num, total_chunks, start_time)

            output_srt = trans_file
            # Write the merged SRT file: original translated blocks + newly patched blocks.
            # Sort by index to maintain proper sequence order.
            with open(output_srt, 'w', encoding='utf-8') as f:
                for idx in sorted(trans_blocks.keys()):
                    f.write(trans_blocks[idx] + "\n\n")

            self.call_from_thread(self._set_progress, 100, "Status: Patching Complete | Speed: 0 | ETA: 00:00:00")
            self.write_thread_log(f"[green]Patching complete for {os.path.basename(trans_file)}.[/green]")

            # Remove processed file from queue so users don't re-patch it.
            self._remove_from_queue(original_target)

        self.write_thread_log("\n[bold magenta]=== ALL PATCHING TASKS COMPLETE ===[/bold magenta]")

    def parse_srt_blocks_from_string(self, srt_text: str) -> list[str]:
        """Split raw SRT text into individual block strings.

        Uses double newline as delimiter between blocks. Filters out empty blocks
        that may result from trailing whitespace or extra newlines in the input.

        Args:
            srt_text: Raw SRT content (may include markdown code fences).

        Returns:
            List of block strings, each containing: index, timestamps, and text.
        """
        if not srt_text.strip():
            return []
        blocks = re.split(r'\n\s*\n', srt_text.strip())
        return [b for b in blocks if b.strip()]

    # --- Prerequisite Check ---

    def _check_prerequisites(self) -> list[str]:
        """Verify that required external tools are installed and accessible.

        Returns a list of missing tool names. Empty list means all prerequisites met.
        Called from on_mount() to warn users before the app starts processing files.
        Missing tools won't crash the app -- they'll fail later when the pipeline tries
        to invoke them -- but early detection gives better UX.

        Tools checked:
        - ffmpeg: Audio extraction from video files (.mkv/.mp4/.ts)
        - whisperx: Japanese transcription and timestamp alignment
        - ollama Python package: Required for LLM translation via Ollama API
          (checked via import, not shutil.which since it's a Python package)
        """
        missing = []
        for cmd in ["ffmpeg", "whisperx"]:
            if shutil.which(cmd) is None:
                missing.append(cmd)
        if Client is None:
            missing.append("ollama package (pip install ollama)")
        return missing


if __name__ == "__main__":
    app = ShowaSync()
    app.run()
