"""
ShowaSync v2 - Japanese Media Translation Pipeline (Simple Browser)
====================================================================

A complete pipeline that takes Japanese video files (.mkv/.mp4/.ts) and produces
English .srt subtitle files, orchestrated from a responsive Textual TUI. The app
manages an entire workflow: audio extraction → transcription → LLM translation →
subtitle rebalancing, with smart queuing utilities to batch-process only what
needs work.

Architecture overview:
- Frontend (TUI): `ShowaSync` class provides the interactive interface where users
  select files, define context profiles, and initiate batch jobs. It manages overall
  state (queuing, progress tracking).
- File browser: Two stacked ListViews — a folder tree showing parent directories up
  to root, and current_contents showing files/dirs in the active directory. Both
  update together whenever `_current_path` changes.
- Pipeline execution: Sequential subprocess management for external tools (FFmpeg,
  WhisperX, Ollama). Each stage depends on the previous one succeeding.
- Config persistence: Two dicts serve different purposes — `self.config` holds raw
  loaded values from disk (used in compose() and on_mount()), while `_config_values`
  is the runtime source of truth that gets saved back to disk via event handlers
  and on_unmount().

Author: LavenderCaptain
License: MIT
"""

import os
import re
import time
import json
import requests
import subprocess
import shutil
import threading
from datetime import timedelta
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Button, Input, Select, RichLog, ProgressBar, Label, ListView, ListItem, Static
from textual.containers import Horizontal, Vertical
from textual import work
from textual.events import Click, Key
import sys

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

    #folder_tree, #current_contents {
        padding: 0 1;
    }

    #breadcrumb {
        height: 3;
        padding: 1;
        text-style: bold;
        color: $primary;
        border-bottom: solid $secondary;
        align: left middle;
    }

    /* Top pane: folder tree */
    #folder_tree {
        width: 100%;
        height: 45%;
        overflow-y: scroll;
        border-bottom: double $accent;
    }

    /* Bottom pane: current folder contents */
    #current_contents {
        width: 100%;
        height: 55%;
        overflow-y: scroll;
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
        self.target_files = []

        # Runtime config dict — the source of truth for all settings. Populated
        # by event handlers (on_input_changed, on_select_changed, on_list_view_item_selected)
        # and _handle_folder_click whenever a relevant value changes. Saved to disk
        # via _save_config() in those same handlers AND in on_unmount().
        self._config_values = {}

        # Path to the persistent config file (JSON). Stored as instance attr so
        # both _load_config and _save_config can reference it consistently.
        self.config_file = Path("showasync_config.json")

        # Raw loaded config from disk — used in compose() and on_mount() for initial
        # UI state. NOT the runtime source of truth (that's _config_values).
        self.config = self._load_config()

        # After loading, sync raw config into _config_values so that any settings
        # previously saved to disk are available at runtime. Without this step,
        # loaded values would live only in self.config and never reach the event
        # handlers that actually use them (e.g., on_input_changed writes to
        # _config_values["ollama_ip"], not self.config["ollama_ip"]).
        if self.config:
            self._config_values.update(self.config)

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

    def _load_config(self) -> dict:
        """Load saved config from disk.

        Returns a dict of key-value pairs previously written by _save_config().
        If the file doesn't exist (first launch) or is corrupt, returns an empty
        dict so the app falls back to defaults without crashing.

        Why we return {} instead of raising: callers (compose(), on_mount()) need
        safe fallback behavior — they read keys with .get() and default values,
        so an empty dict means "use built-in defaults" rather than a crash.
        """
        if self.config_file.exists():
            try:
                with open(self.config_file, "r") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                # File exists but is malformed or unreadable — treat as no saved config.
                return {}
        return {}

    def _save_config(self) -> None:
        """Persist _config_values to disk as JSON.

        Called from event handlers (on_input_changed, on_select_changed, etc.)
        whenever a relevant setting changes, AND from on_unmount() as a safety net
        for any values not yet saved. This ensures settings survive app restarts.

        IOError is silently swallowed because config loss should never crash the app —
        users can always re-enter settings on next launch.
        """
        try:
            with open(self.config_file, "w") as f:
                json.dump(self._config_values, f, indent=2)
        except IOError:
            pass

    def compose(self) -> ComposeResult:
        """Build the TUI layout.

        The interface is split into three vertical zones:
        1. Header — always visible, shows app title and clock
        2. Main content area (Horizontal container):
           - Left column (35% width): File browser with two stacked ListViews
             * folder_tree: Shows parent directory hierarchy up to root
             * current_contents: Shows files/dirs in the active directory
           - Right panel (65% width): Queue, config inputs, console output
        3. Footer — always visible, shows keyboard shortcut hints

        Config values are read from self.config (loaded at __init__) for initial UI
        state. The Ollama URL and profile select get pre-filled with saved values;
        the model_select starts as "Waiting..." until poll_models_worker populates it.
        """
        yield Header(show_clock=True)

        # Read saved config values for UI initialization. These come from self.config
        # (loaded at __init__), NOT _config_values — because compose() runs before any
        # event handlers have fired to populate _config_values. After init, all reads
        # go through _config_values which is kept in sync via the sync step in __init__.
        saved_ollama = self.config.get("ollama_ip", "http://127.0.0.1:11434")
        saved_profile = self.config.get("profile", "Generic / Catch-all")

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
        self._save_config()

    def _check_prerequisites(self) -> list[str]:
        """Verify that required external tools are installed and accessible.

        Returns a list of missing tool names. Empty list means all prerequisites met.
        Called from on_mount() to warn users before the app starts processing files.
        Missing tools won't crash the app — they'll fail later when the pipeline tries
        to invoke them — but early detection gives better UX.

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

    def _get_base_filename(self, filepath: str) -> str:
        filename = os.path.basename(filepath)
        root, _ = os.path.splitext(filename)
        root = root.replace('_16k_mono', '').replace('-translated', '').replace('-balanced', '')
        return root

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
        clicked widget to check which ListView owns it — folder_tree items represent
        path hierarchy levels (parent dirs), while current_contents items represent
        files/dirs in the active directory. The same click handler must behave
        differently for each: clicking a parent dir in folder_tree navigates UP,
        while clicking a subfolder in current_contents navigates INTO it.

        Important: we only process ListItem children of folder_tree or current_contents.
        Clicks on other widgets inside browser_column (e.g., Select dropdowns) are
        ignored to avoid corrupting Textual's internal event routing — see issue where
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

        # Determine which ListView was clicked by walking up to browser_column.
        # folder_tree items have id="folder_tree" in their ancestor chain;
        # current_contents items do not — they're siblings of folder_tree.
        current = getattr(widget, 'parent', None)
        is_in_folder_tree = False
        while current:
            if hasattr(current, 'id') and current.id == "folder_tree":
                is_in_folder_tree = True
                break
            current = getattr(current, 'parent', None)

        # --- Navigation logic ---
        if data == "PARENT":
            # Clicked the ".. (parent)" entry in folder_tree — go up one level.
            self._current_path = self._current_path.parent.resolve()
        elif Path(data).is_dir():
            # It's a directory. Update _current_path to point at it so subsequent
            # operations (like Smart queue) scan from the correct location.
            self._current_path = Path(data).resolve()

            if is_in_folder_tree:
                # Clicked a parent dir in folder_tree — stay on current level,
                # just refresh both views to show the selected path's contents.
                pass
            else:
                # Clicked a subfolder in current_contents — navigate INTO it by
                # updating _current_path and refreshing views to show its children.
                pass

        # Persist last directory so we can restore it on next launch via config.
        self._config_values["last_directory"] = str(self._current_path)
        self._save_config()

        if Path(data).is_file():
            filepath = str(Path(data))
            if filepath not in self.target_files:
                self.target_files.append(filepath)
                self._refresh_queue_ui_main_thread()
                self.write_main_log(f"Queued: {os.path.basename(filepath)}")
            # NOTE: We do NOT return early here. Both views must refresh after any
            # click on an item in current_contents so they stay in sync with
            # _current_path. If we returned early, navigating away via folder_tree
            # afterward would leave the displayed contents stale (showing files
            # from wherever _current_path last pointed instead of where you are now).

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
        self.write_main_log("ShowaSync v2 loaded. Checking prerequisites...")
        missing_tools = self._check_prerequisites()
        if missing_tools:
            self.write_main_log(f"[yellow]Warning: Missing tools: {', '.join(missing_tools)}[/yellow]")

        # Initialize folder browser with current directory
        saved_dir = self.config.get("last_directory", None)
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
            if self.target_files:
                filename = os.path.basename(self.target_files[0])
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
            config = {
                "ip": self.query_one("#ollama_ip", Input).value.rstrip('/'),
                "model": self.query_one("#model_select", Select).value,
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
            self._config_values["ollama_ip"] = ip
            self._config_values["model"] = model
            self.execute_patcher_batch(ip, model)

    def on_list_view_item_selected(self, event) -> None:
        """Handle item selection from browser list (keyboard Enter/Space)."""
        item = event.item
        data = getattr(item, "data", None)
        if not data or data == "PARENT":
            return

        # Determine which ListView triggered the selection
        widget = event.widget
        is_in_folder_tree = False
        current = widget
        while current:
            if hasattr(current, 'id') and current.id == "folder_tree":
                is_in_folder_tree = True
                break
            current = getattr(current, 'parent', None)

        # Handle folder navigation (only from current_contents via keyboard)
        if Path(data).is_dir():
            self._current_path = Path(data).resolve()
            self._config_values["last_directory"] = str(self._current_path)
            self._save_config()
            self._refresh_folder_tree()
            self._refresh_current_contents()
            return

        # Handle file selection
        if not Path(data).is_file():
            return
        filepath = str(Path(data))
        if filepath not in self.target_files:
            self.target_files.append(filepath)
            self._refresh_queue_ui_main_thread()
            self.write_main_log(f"Queued: {os.path.basename(filepath)}")

    def on_input_changed(self, event: Input.Changed) -> None:
        """Persist Ollama IP whenever it changes."""
        if event.input.id == "ollama_ip":
            self._config_values["ollama_ip"] = event.value.rstrip('/')

    def on_select_changed(self, event: Select.Changed) -> None:
        """Persist profile and model whenever they change."""
        if event.select.id == "profile_select":
            self._config_values["profile"] = event.value
        elif event.select.id == "model_select":
            self._config_values["model"] = event.value

    def _refresh_queue_ui_main_thread(self) -> None:
        queue = self.query_one("#queue_view", ListView)
        queue.clear()
        for f in self.target_files:
            queue.append(ListItem(Label(os.path.basename(f))))

    def on_key(self, event: Key) -> None:
        if event.key == "delete":
            if self.target_files:
                filename = os.path.basename(self.target_files[0])
                self.target_files.pop(0)
                self._refresh_queue_ui_main_thread()
                self.write_main_log(f"[dim]Removed: {filename}[/dim]")
                event.stop()

    def action_clear_queue(self) -> None:
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
                    if filepath not in self.target_files:
                        self.target_files.append(filepath)
                        added_count += 1
                except (OSError, IOError) as e:
                    skipped_count += 1
                    self.write_main_log(f"[yellow]Skipped {os.path.basename(str(path))}: {e}[/yellow]")
        except (OSError, IOError) as e:
            self.write_main_log(f"[red]Error scanning directories: {e}[/red]")

        if added_count > 0:
            self._refresh_queue_ui_main_thread()
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

        try:
            for path in scan_dir.rglob("*"):
                try:
                    if not path.is_file():
                        continue

                    filepath = str(path.resolve())
                    dir_path = os.path.dirname(filepath)
                    base_name = self._get_base_filename(filepath)

                    if mode == "vids_no_srt" and path.suffix.lower() in vid_extensions:
                        has_srt = os.path.exists(os.path.join(dir_path, f"{base_name}.srt")) or \
                                  os.path.exists(os.path.join(dir_path, f"{base_name}_16k_mono.srt"))
                        if not has_srt and filepath not in self.target_files:
                            self.target_files.append(filepath)
                            added_count += 1

                    elif mode == "untra_srt" and path.suffix.lower() == ".srt":
                        if not path.name.endswith('-translated.srt') and not path.name.endswith('-balanced.srt'):
                            has_trans = os.path.exists(os.path.join(dir_path, f"{base_name}-translated.srt"))
                            if not has_trans and filepath not in self.target_files:
                                self.target_files.append(filepath)
                                added_count += 1

                    elif mode == "needs_rebalance" and path.name.endswith('-translated.srt'):
                        bal_path = os.path.join(dir_path, f"{base_name}-balanced.srt")
                        needs_balance = False

                        if not os.path.exists(bal_path):
                            needs_balance = True
                        else:
                            if os.path.getmtime(filepath) > os.path.getmtime(bal_path):
                                needs_balance = True

                        if needs_balance and filepath not in self.target_files:
                            self.target_files.append(filepath)
                            added_count += 1
                except (OSError, IOError) as e:
                    skipped_count += 1
                    self.write_main_log(f"[yellow]Skipped {os.path.basename(filepath)}: {e}[/yellow]")
        except (OSError, IOError) as e:
            self.write_main_log(f"[red]Error scanning directories: {e}[/red]")

        summary = f"Smart Queue: Added {added_count} files"
        if skipped_count > 0:
            summary += f", Skipped {skipped_count} due to errors"

        self._refresh_queue_ui_main_thread()
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
        /api/tags returns a JSON list of loaded models — we extract just the names
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

            # Update UI on main thread — can't touch Textual widgets from background threads.
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

        Args:
            models: List of model names returned by Ollama /api/tags endpoint.
                Empty list means no models loaded — show placeholder option.
        """
        select = self.query_one("#model_select", Select)
        if models:
            # Build display options from model names (label and value are identical)
            select.set_options([(m, m) for m in models])

            # Prefer saved model if it's still available; otherwise use first model.
            # Check self.config first (loaded at __init__), then _config_values
            # (runtime source of truth updated by event handlers).
            saved_model = self.config.get("model") or self._config_values.get("model")
            if saved_model and saved_model in models:
                select.value = saved_model
                self._config_values["model"] = saved_model
            else:
                select.value = models[0]
                self._config_values["model"] = models[0]
        else:
            select.set_options([("No models found", "None")])

    # --- Logging & Telemetry ---

    def write_main_log(self, message: str) -> None:
        """Write a message to the console RichLog widget.

        This is the main-thread logging method. For use from background threads,
        call write_thread_log() instead — it will marshal the call to the main thread.
        """
        self.query_one("#console", RichLog).write(message)

    def write_thread_log(self, message: str) -> None:
        """Thread-safe logging that marshals to main thread if needed.

        Textual widgets can only be modified from the main (UI) thread. If this method
        is called from a background worker thread (like poll_models_worker or pipeline
        execution), it uses call_from_thread() to safely invoke write_main_log on the
        main thread. If already on the main thread, calls directly.

        Args:
            message: Log message string (supports Textual markup for styling).
        """
        if threading.current_thread() == threading.main_thread():
            self.write_main_log(message)
        else:
            self.call_from_thread(self.write_main_log, message)

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
        pct = (current / total) * 100 if total > 0 else 0
        status_text = f"Step: {step_name} | Batch: {current}/{total} | Speed: {speed:.1f}/min | ETA: {timedelta(seconds=eta)}"
        if threading.current_thread() == threading.main_thread():
            self._set_progress(pct, status_text)
        else:
            self.call_from_thread(self._set_progress, pct, status_text)

    def _set_progress(self, pct: float, text: str) -> None:
        self.query_one("#progress_bar", ProgressBar).progress = pct
        self.query_one("#status_label", Static).update(text)

    # --- Data Parsing & Anchoring ---

    def get_base_prompt(self) -> str:
        profile_key = self.query_one("#profile_select", Select).value
        context = self.profiles.get(profile_key, "")
        return f"""{context}
CRITICAL LAWS OF RE-FORMATTING:
1. Preserve the original SRT line numbers and timestamps exactly.
2. Split continuous walls of text into new lines when the speaker changes. Start new speakers with a hyphen (-).
3. Return ONLY the finished SRT block output. Do not include markdown blocks or notes."""

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

        out_file = input_srt.replace('.srt', '-balanced.srt')
        with open(out_file, 'w', encoding='utf-8') as f:
            f.write("\n\n".join(new_blocks))
        self.write_thread_log(f"Rebalanced file saved: {os.path.basename(out_file)}")

    # --- Primary Execution Engines ---

    @work(thread=True)
    def execute_pipeline_batch(self, config: dict) -> None:
        """Execute the full translation pipeline on all queued files.

        Runs sequentially through each file in self.target_files, applying these steps:
          1. FFmpeg - Extract audio from video (.mkv/.mp4/.ts etc.) to mono WAV
          2. WhisperX - Transcribe Japanese audio → SRT with timestamps
          3. Ollama Translation - Translate Japanese SRT → English using LLM
          4. Rebalance - Split long subtitle lines (>85 chars) for readability
          5. Cleanup - Remove temporary files (WAV, JSON, VTT, TXT, TSV)

        Each step is optional based on config flags (run_ffmpeg, run_whisper, etc.).
        If any step fails for a file, the pipeline continues to the next file.

        Thread: Runs in background thread (@work). All UI updates use call_from_thread.
        Args:
            config: Dict with boolean flags for each pipeline stage:
                {"run_ffmpeg": bool, "run_whisper": bool, "run_translate": bool,
                 "run_rebalance": bool, "run_clean": bool}
        """
        if not self.target_files:
            self.write_thread_log("[red]ERROR: No files selected in queue.[/red]")
            return

        queue_snapshot = list(self.target_files)
        total_files = len(queue_snapshot)

        self.write_thread_log(f"\n[bold magenta]=== INITIATING BATCH PIPELINE ({total_files} Files) ===[/bold magenta]")

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
                    self.write_thread_log(f"[red]FFmpeg Error: {e}[/red]")
                    pipeline_failed = True

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
                    result = subprocess.run(cmd, capture_output=True, text=True,
                                           encoding="utf-8", errors="ignore", timeout=600)
                    if result.returncode == 0:
                        expected_srt = os.path.join(target_dir, f"{base_name}_16k_mono.srt")
                        if os.path.exists(expected_srt):
                            target = expected_srt
                            self.write_thread_log("[green]WhisperX transcription complete.[/green]")
                            self.call_from_thread(self._set_progress, 50,
                                                 f"Step: WhisperX | File: {idx+1}/{total_files} | Complete")
                        else:
                            raise FileNotFoundError("WhisperX did not produce an SRT file.")
                    else:
                        stderr_msg = result.stderr.strip() if result.stderr else "Unknown error"
                        raise RuntimeError(f"WhisperX failed: {stderr_msg}")
                except subprocess.TimeoutExpired:
                    self.write_thread_log("[red]WhisperX Error: Process timed out after 600 seconds[/red]")
                    pipeline_failed = True
                except Exception as e:
                    self.write_thread_log(f"[red]WhisperX Error: {e}[/red]")
                    pipeline_failed = True

            # Step 3: Ollama Translation - Translate Japanese SRT to English
            # Sends Japanese SRT blocks to local Ollama instance for LLM translation.
            # Uses the user-selected profile's system prompt as context for translation style.
            if config["run_translate"] and target.endswith('.srt') and not pipeline_failed:
                if not Client:
                    self.write_thread_log("[red]ERROR: 'ollama' package missing.[/red]")
                    pipeline_failed = True
                else:
                    ollama_client = Client(host=config["ip"])
                    output_srt = os.path.join(target_dir, f"{base_name}-translated.srt")
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
                                            {"role": "system", "content": self.get_base_prompt()},
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
                                    self.write_thread_log(f"[red]LLM Error on Batch {batch_num}: {e}[/red]")

                                self.update_telemetry(batch_num, total_batches, start_time)

                        self.call_from_thread(self._set_progress, 100,
                                             "Status: Translation Complete | Speed: 0 | ETA: 00:00:00")
                        target = output_srt

            # Step 4: Rebalance - Split long subtitle lines
            # Splits translated SRT blocks >85 chars into two smaller blocks to improve
            # readability. Preserves timestamps by splitting at natural breaks (periods, etc.).
            if config["run_rebalance"] and target.endswith('.srt') and not pipeline_failed:
                self.rebalance_srt_file(target)

            # Step 5: Cleanup - Remove temporary files
            # Removes intermediate artifacts (WAV audio, WhisperX JSON/VTT/TXT/TSV) to save disk space.
            if config["run_clean"]:
                self.write_thread_log("Cleaning up temp files...")
                if os.path.exists(wav_out):
                    try:
                        os.remove(wav_out)
                    except Exception as e:
                        self.write_thread_log(f"[yellow]Warning: Could not remove temp file {wav_out}: {e}[/yellow]")
                for ext_to_del in ['.json', '.vtt', '.txt', '.tsv']:
                    junk = os.path.join(target_dir, f"{base_name}_16k_mono{ext_to_del}")
                    if os.path.exists(junk):
                        try:
                            os.remove(junk)
                        except Exception as e:
                            self.write_thread_log(f"[yellow]Warning: Could not remove {junk}: {e}[/yellow]")

            # Remove from queue regardless of success/failure
            if original_target in self.target_files:
                self.target_files.remove(original_target)

            if pipeline_failed:
                self.write_thread_log(f"[red]Pipeline FAILED for {os.path.basename(target)} - moving to next file[/red]")
            else:
                self.write_thread_log(f"[green]Completed processing: {os.path.basename(target)}[/green]")

            # Remove processed file from queue so users don't re-process it.
            # This also refreshes the UI to show the updated queue state.
            if original_target in self.target_files:
                self.target_files.remove(original_target)
            self.call_from_thread(self._refresh_queue_ui_main_thread)

        self.write_thread_log("\n[bold magenta]=== ALL PIPELINE TASKS COMPLETE ===[/bold magenta]")

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

        # Ensure all paths are absolute for consistent comparison later.
        self.target_files = [os.path.abspath(f) if not os.path.isabs(f) else f for f in self.target_files]
        queue_snapshot = list(self.target_files)
        self.write_thread_log(f"\n[bold magenta]=== STARTING VERIFY & PATCH ON {len(queue_snapshot)} QUEUED FILES ===[/bold magenta]")

        for original_target in queue_snapshot:
            target = original_target
            target_dir = os.path.dirname(os.path.abspath(target)) or "."
            base_name = self._get_base_filename(target)

            # Patcher only works on .srt files — skip others with a log message.
            if not target.endswith('.srt'):
                self.write_thread_log(f"[dim]Skipping {os.path.basename(target)}: Patcher requires an .srt file.[/dim]")
                if original_target in self.target_files:
                    self.target_files.remove(original_target)
                    self.call_from_thread(self._refresh_queue_ui_main_thread)
                continue

            trans_file = os.path.join(target_dir, f"{base_name}-translated.srt")
            if not os.path.exists(trans_file):
                self.write_thread_log(f"[dim]Skipping {os.path.basename(target)}: Could not find matching '-translated.srt' file.[/dim]")
                if original_target in self.target_files:
                    self.target_files.remove(original_target)
                    self.call_from_thread(self._refresh_queue_ui_main_thread)
                continue

            source_jp = os.path.join(target_dir, f"{base_name}_16k_mono.srt")
            if not os.path.exists(source_jp):
                source_jp = os.path.join(target_dir, f"{base_name}.srt")
                if not os.path.exists(source_jp):
                    self.write_thread_log(f"[dim]Skipping {os.path.basename(target)}: Could not find original Japanese source (.srt or _16k_mono.srt).[/dim]")
                    if original_target in self.target_files:
                        self.target_files.remove(original_target)
                        self.call_from_thread(self._refresh_queue_ui_main_thread)
                    continue

            self.write_thread_log(f"\n[bold yellow]=== INITIATING SURGICAL PATCHER: {os.path.basename(trans_file)} ===[/bold yellow]")

            # Parse both Japanese and translated SRT files into block dictionaries.
            # Keys are block indices, values are the full SRT block text (number, timestamps, text).
            jp_blocks = self.parse_srt_blocks(source_jp)
            trans_blocks = self.parse_srt_blocks(trans_file)

            if not jp_blocks:
                self.write_thread_log("[red]ERROR: No Japanese source blocks found.[/red]")
                continue

            # Find indices present in Japanese but missing from translated file.
            # These are the segments that need translation.
            missing_indices = [idx for idx in sorted(jp_blocks.keys()) if idx not in trans_blocks]

            if not missing_indices:
                self.write_thread_log("[green]No gaps found - all segments already translated.[/green]")
                continue

            self.write_thread_log(f"Found {len(missing_indices)} missing segments. Translating...")

            if not Client:
                self.write_thread_log("[red]ERROR: 'ollama' package missing.[/red]")
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
                            {"role": "system", "content": self.get_base_prompt()},
                            {"role": "user", "content": f"<subtitles>\n{jp_text}\n</subtitles>"}
                        ],
                        options={"temperature": 0.2, "num_ctx": 8192, "num_predict": -1}
                    )

                    clean_out = response['message']['content'].replace('```srt', '').replace('```', '').strip()
                    new_blocks = self.parse_srt_blocks_from_string(clean_out)

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
            if original_target in self.target_files:
                self.target_files.remove(original_target)
                self.call_from_thread(self._refresh_queue_ui_main_thread)

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


if __name__ == "__main__":
    app = ShowaSync()
    app.run()
