"""
ShowaSync - Japanese Media Translation Pipeline
================================================

A Textual TUI application that automates the translation of Japanese video
subtitles using local AI models via Ollama. The pipeline processes media files
through multiple stages: audio extraction, speech recognition, translation, and
subtitle formatting.

Architecture Overview
---------------------
This is a monolithic Python application that orchestrates several external tools:
1. FFmpeg - Audio extraction and format conversion
2. WhisperX - Speech-to-text transcription (Japanese)
3. Ollama - Local LLM inference for translation
4. Textual - Responsive TUI framework

The application manages state through a queue system and processes files
sequentially through the pipeline stages, with progress tracking and error
handling at each step.

Key Design Decisions
--------------------
- Uses subprocess calls to external tools rather than Python libraries for
  reliability and version compatibility
- Thread-safe UI updates via call_from_thread() to prevent crashes when
  background workers update the interface
- Config persistence via JSON file in current directory
- Smart queue modes that automatically detect files needing processing

Author: LavenderCaptain
License: MIT
"""

import os
import re
import glob
import time
import json
import requests
import subprocess
import shutil
import threading
from datetime import timedelta, datetime
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Button, Checkbox, Input, Select, RichLog, ProgressBar, Label, DirectoryTree, ListView, ListItem, Static
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual import work
from textual.events import Click, Key
import sys

try:
    from ollama import Client
except ImportError:
    Client = None


class FilteredDirectoryTree(DirectoryTree):
    """
    A Textual DirectoryTree widget with file extension filtering.

    This custom directory tree only displays files with specific extensions
    (.mp4, .mkv, .ts, etc.) and excludes processed subtitle files. It also
    adds navigation to parent directories via ".." entries.

    Attributes:
        show_root (bool): Whether to display the root directory entry
    """

    def __init__(self, path: Path, *args, **kwargs):
        """
        Initialize FilteredDirectoryTree with custom filtering.

        Args:
            path (Path): Starting directory path
            *args, **kwargs: Additional arguments passed to parent DirectoryTree
        """
        super().__init__(path, *args, **kwargs)
        self.show_root = False  # Don't show root entry in tree view

    def filter_paths(self, paths: list[Path]) -> list[Path]:
        """
        Filter directory entries to only show valid media and subtitle files.

        This method is called by Textual when displaying directory contents.
        It filters out files with unsupported extensions and processed
        subtitle files (those ending in '-translated.srt' or '-balanced.srt').

        Args:
            paths (list[Path]): List of Path objects to filter

        Returns:
            list[Path]: Filtered list containing only valid entries
        """
        # Valid file extensions for media and subtitles
        valid_extensions = {".mp4", ".mkv", ".ts", ".avi", ".mov", ".srt", ".m4v"}
        filtered = []

        # Add parent directory (..) if not at root to enable navigation up
        current_dir = getattr(self, 'directory', None)
        if current_dir:
            try:
                abs_curr = Path(current_dir).resolve()
                abs_parent = abs_curr.parent
                # Only add ".." if we're not already at filesystem root
                if str(abs_curr) != str(abs_parent):
                    filtered.append(Path(str(abs_parent)) / "..")
            except Exception:
                pass

        for path in paths:
            if path.is_dir():
                filtered.append(path)  # Always show directories
            elif path.suffix.lower() in valid_extensions:
                # Exclude processed subtitle files to avoid confusion
                if not path.name.endswith('-translated.srt') and \
                   not path.name.endswith('-balanced.srt'):
                    filtered.append(path)
        return filtered

    def action_directory_selected(self, directory: Path) -> None:
        """
        Handle directory selection events with parent navigation support.

        This method overrides the default behavior to handle ".." entries
        that allow users to navigate up one directory level. When a ".."
        entry is selected, it loads the parent directory instead of trying
        to open it as a real directory.

        Args:
            directory (Path): The selected directory path (may end with "/..")
        """
        # Check if this is a parent directory navigation request
        if str(directory).endswith("/..") or directory.name == "..":
            current_dir = getattr(self, 'directory', None)
            if current_dir:
                try:
                    parent = Path(current_dir).parent
                    self.load_directory(parent)  # Load parent directory
                    return  # Don't call super() - we handled it
                except Exception:
                    pass
        super().action_directory_selected(directory)

    def get_directory(self) -> Path | None:
        """
        Get the current directory being displayed.

        Returns:
            Path | None: Current directory path, or None if not set
        """
        return getattr(self, 'directory', None)


class ShowaSync(App):
    """
    Main application class for ShowaSync TUI.

    This is the core orchestrator that manages:
    - File queue state and UI updates
    - Configuration persistence (Ollama URL, selected model, profile)
    - Pipeline execution (FFmpeg -> WhisperX -> Ollama translation)
    - Smart queue modes for automatic file discovery

    The application runs on Textual framework and uses threading for
    background processing to keep the UI responsive.

    State Management:
        target_files (list[str]): Queue of files to process
        _config_values (dict): Runtime config values synced to disk
        config (dict): Loaded configuration from JSON file
    """

    TITLE = "ShowaSync"

    # CSS styles for the TUI layout
    CSS = """
    Screen { background: $surface; }

    /* Main content - split between file browser and right panel */
    #main_content {
        height: 1fr;
        layout: horizontal;
    }

    /* Left: File browser (fixed width) */
    #tree_view {
        width: 35%;
        height: 100%;
        border: solid $accent;
        overflow-y: scroll;
    }

    /* Right panel - stack queue, telemetry, and console vertically */
    #right_panel {
        width: 65%;
        height: 100%;
        layout: vertical;
        border-left: solid $primary;
    }

    /* Queue section */
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

    /* Telemetry bar - compact status display */
    .telemetry_bar {
        height: 3;
        layout: horizontal;
        margin-top: 1;
    }

    #progress_bar {
        width: 70%;
        height: 1;
    }

    .status_text {
        width: 30%;
        text-align: right;
        color: $success;
        text-style: bold;
    }

    /* Console - takes remaining space */
    #console {
        height: 1fr;
        overflow-y: scroll;
        border-top: double $accent;
        background: $panel;
    }

    /* Config section - minimal, compact */
    .config_section {
        height: auto;
        layout: horizontal;
        padding: 0;
        margin-bottom: 1;
    }

    .config_label {
        width: 25%;
        content-align: right middle;
        margin-right: 1;
    }

    .config_input, .config_model, .config_select {
        width: 25%;
        min-width: 30;
    }

    /* Action bar - bottom row */
    .action_bar {
        height: auto;
        layout: horizontal;
        padding: 1 2;
        border-top: solid $surface-lighten-1;
    }

    .spacer {
        width: 1fr;
    }

    Button { margin: 0 1; }

    /* Hidden buttons for palette/keyboard shortcuts */
    .hidden_button {
        display: none;
    }

    #queue_view {
        height: 100%;
        overflow-y: scroll;
    }
    """

    # Keyboard bindings for quick access to common actions
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
        """
        Initialize ShowaSync application.

        Sets up initial state, loads configuration from disk, and
        defines available translation profiles with their system prompts.
        """
        super().__init__()
        self.target_files = []  # Queue of files to process
        self._config_values = {}  # Runtime config values synced to disk

        # Config file path - saved in current directory
        self.config_file = Path("showasync_config.json")

        # Load config if exists (returns empty dict on failure)
        self.config = self._load_config()

        # Translation profiles with contextual prompts for different content types
        self.profiles = {
            "Generic / Catch-all": "You are an expert Japanese-to-English subtitle translator. Translate the text naturally and professionally.",
            "Variety Show (Chaos)": "You are translating a Japanese variety show. Expect heavy slang, constant overlapping dialogue, and distinct visual on-screen text. Prioritize clarity and character separation.",
            "Documentary": "You are translating a formal documentary. Maintain precise, professional terminology. Clearly distinguish between a formal narrator and casual interview subjects.",
            "Sports Broadcast": "You are translating a live sports broadcast. Use high-energy play-by-play terminology, color commentary formatting, and standard athletic jargon.",
            "1959 Hardboiled Noir": "Context: A 1950s Showa-era film. The dialogue must reflect hardboiled, mid-century tough-guy aesthetics and corporate suspense.",
            "1980s Yamadamura Comedy": "Context: A 1980s Japanese comedy. Characters speak in an exaggerated rural dialect ending in 'pya'. Translate this into a comedic, exaggerated 'country' English dialect."
        }

    def _load_config(self) -> dict:
        """
        Load saved configuration from JSON file.

        Attempts to read the config file and parse it as JSON. Returns
        empty dictionary on any error (file not found, corrupt JSON, etc.)
        to ensure graceful degradation.

        Returns:
            dict: Loaded configuration values, or empty dict if loading fails
        """
        if self.config_file.exists():
            try:
                with open(self.config_file, "r") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return {}  # Return empty config on any error
        return {}

    def _save_config(self) -> None:
        """
        Save current configuration to JSON file.

        Writes the runtime config values (_config_values dict) to disk
        as formatted JSON. Errors are silently ignored to prevent crashes
        during normal operation.
        """
        try:
            # Write the stored config values to disk
            with open(self.config_file, "w") as f:
                json.dump(self._config_values, f, indent=2)
        except IOError:
            pass  # Silently ignore write errors

    def compose(self) -> ComposeResult:
        """
        Build the TUI layout by yielding widget hierarchy.

        This method is called by Textual to construct the interface. It
        creates a two-panel layout with file browser on left and queue/
        config/console on right, plus action buttons at bottom.

        Returns:
            ComposeResult: Iterator of widgets defining the UI structure
        """
        yield Header(show_clock=True)

        # Get saved config values or use defaults
        saved_ollama = self.config.get("ollama_ip", "http://127.0.0.1:11434")
        saved_profile = self.config.get("profile", "Generic / Catch-all")

        with Horizontal(id="main_content"):
            # Left side: File browser
            yield FilteredDirectoryTree(Path.cwd().resolve(), id="tree_view")

            # Right side: Queue, Config, Console (stacked)
            with Vertical(id="right_panel"):
                # Queue display
                with Vertical(id="queue_section"):
                    yield Label("Queued Files", classes="panel_title")
                    yield ListView(id="queue_view")

                # Telemetry bar - shows current progress during jobs
                with Horizontal(classes="telemetry_bar"):
                    yield ProgressBar(total=100, show_eta=True, id="progress_bar")
                    yield Static("Status: Idle | Step: --", id="status_label", classes="status_text")

                # Console/Log at the bottom (larger)
                yield RichLog(id="console", highlight=True, markup=True)

        # Config section - separate row (minimal)
        with Horizontal(classes="config_section"):
            yield Static("Ollama URL:", classes="config_label")
            yield Input(value=saved_ollama.rstrip("/"), id="ollama_ip", placeholder="http://127.0.0.1:11434", classes="config_input")
            # Always start with "Waiting..." - will be populated when Ollama is polled
            yield Select([("Waiting...", "Waiting...")], value="Waiting...", id="model_select", classes="config_model")
            yield Select([(k, k) for k in self.profiles.keys()], value=saved_profile, id="profile_select", classes="config_select")

        # Bottom action bar
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
        """Skip config saving during unmount - widgets are being torn down."""
        pass

    def on_mount(self) -> None:
        """
        Initialize app after UI is mounted.

        Checks for required external tools (FFmpeg, WhisperX, Ollama SDK)
        and starts background polling of Ollama to populate model list.
        """
        self.write_main_log("ShowaSync loaded. Checking prerequisites...")
        missing_tools = self._check_prerequisites()
        if missing_tools:
            self.write_main_log(f"[yellow]Warning: Missing tools: {', '.join(missing_tools)}[/yellow]")
        self.write_main_log("Polling Ollama in the background...")
        ip = self.query_one("#ollama_ip", Input).value.rstrip('/')
        self.poll_models_worker(ip)

    def _check_prerequisites(self) -> list[str]:
        """
        Check if required external tools are installed.

        Verifies that FFmpeg, WhisperX CLI, and Ollama Python SDK are
        available. Returns list of missing tool names for error reporting.

        Returns:
            list[str]: Names of missing required tools
        """
        missing = []
        for cmd in ["ffmpeg", "whisperx"]:
            if shutil.which(cmd) is None:
                missing.append(cmd)
        if Client is None:
            missing.append("ollama package (pip install ollama)")
        return missing

    def _get_base_filename(self, filepath: str) -> str:
        """
        Extract base filename without processing suffixes.

        Removes common suffixes added by pipeline stages (_16k_mono,
        -translated, -balanced) to get the original media filename.

        Args:
            filepath (str): Full path to file

        Returns:
            str: Base filename with processing suffixes removed
        """
        filename = os.path.basename(filepath)
        root, _ = os.path.splitext(filename)
        # Remove common pipeline suffixes
        root = root.replace('_16k_mono', '').replace('-translated', '').replace('-balanced', '')
        return root

    # --- UI Interactions ---

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        """
        Handle file selection from directory tree.

        When a user clicks on a media file in the browser, add it to
        the processing queue if not already present.

        Args:
            event (DirectoryTree.FileSelected): Event containing selected path
        """
        filepath = str(event.path)
        if filepath not in self.target_files:
            self.target_files.append(filepath)
            self._refresh_queue_ui_main_thread()
            self.write_main_log(f"Queued: {os.path.basename(filepath)}")

    def on_directory_tree_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        """
        Handle directory selection from file browser.

        Saves the selected directory path to config for persistence
        across app restarts.

        Args:
            event (DirectoryTree.DirectorySelected): Event containing selected path
        """
        if hasattr(self, 'config'):
            self.config["last_directory"] = str(event.path)
            self._save_config()

    def _refresh_queue_ui_main_thread(self) -> None:
        """
        Refresh the queue view with current target files.

        Clears the ListView and repopulates it with items from
        target_files list. Called after any queue modification.
        """
        queue = self.query_one("#queue_view", ListView)
        queue.clear()
        for f in self.target_files:
            queue.append(ListItem(Label(os.path.basename(f))))

    def on_key(self, event: Key) -> None:
        """
        Handle keyboard shortcuts.

        Currently supports Delete key to remove first item from queue
        when focused on the queue view.

        Args:
            event (Key): Keyboard event with key information
        """
        if event.key == "delete":
            # Remove first item from queue when Delete is pressed
            if self.target_files:
                filename = os.path.basename(self.target_files[0])
                self.target_files.pop(0)
                self._refresh_queue_ui_main_thread()
                self.write_main_log(f"[dim]Removed: {filename}[/dim]")
                event.stop()

    def action_clear_queue(self) -> None:
        """Clear all items from the processing queue."""
        self.target_files.clear()
        self._refresh_queue_ui_main_thread()
        self.write_main_log("[yellow]Queue cleared.[/yellow]")

    def action_queue_type(self, file_type: str) -> None:
        """
        Scan current directory for files of specified type and add to queue.

        Recursively searches from current working directory for media
        files (.mp4, .mkv, etc.) or SRT files depending on file_type
        parameter. Skips already-processed files and handles IO errors
        gracefully.

        Args:
            file_type (str): "video" for media files, "srt" for subtitles
        """
        if file_type == "video":
            valid_extensions = {".mp4", ".mkv", ".ts", ".avi", ".mov", ".m4v"}
        else:
            valid_extensions = {".srt"}

        added_count = 0
        skipped_count = 0
        root_dir = Path(".")

        # Search recursively through all subdirectories
        try:
            for path in root_dir.rglob("*"):
                try:
                    if not path.is_file():
                        continue
                    if path.suffix.lower() not in valid_extensions:
                        continue
                    if file_type == "srt" and (path.name.endswith('-translated.srt') or path.name.endswith('-balanced.srt')):
                        continue  # Skip processed files
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
        """
        Smart queue mode that auto-detects files needing processing.

        Three modes available:
        - "vids_no_srt": Videos without corresponding SRT files
        - "untra_srt": Transcription-only SRTs (no translation yet)
        - "needs_rebalance": Translated SRTs older than their source

        Args:
            mode (str): One of "vids_no_srt", "untra_srt", or "needs_rebalance"
        """
        added_count = 0
        skipped_count = 0
        vid_extensions = {".mp4", ".mkv", ".ts", ".avi", ".mov", ".m4v"}

        # Use rglob for recursive search through all subdirectories
        try:
            for path in Path(".").rglob("*"):
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
                    # Log individual file errors so user knows what was skipped
                    self.write_main_log(f"[yellow]Skipped {os.path.basename(filepath)}: {e}[/yellow]")
        except (OSError, IOError) as e:
            self.write_main_log(f"[red]Error scanning directories: {e}[/red]")

        # Summary message showing what happened
        summary = f"Smart Queue: Added {added_count} files"
        if skipped_count > 0:
            summary += f", Skipped {skipped_count} due to errors"

        self._refresh_queue_ui_main_thread()
        self.write_main_log(f"[green]{summary}.[/green]")

    def action_poll_models(self) -> None:
        """Manually trigger Ollama model polling."""
        ip = self.query_one("#ollama_ip", Input).value.rstrip('/')
        self.write_main_log(f"Initiating manual poll for {ip}...")
        self.poll_models_worker(ip)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """
        Handle button press events.

        Routes button clicks to appropriate action methods based on
        button ID. Handles queue management, smart queue modes, and
        pipeline execution.

        Args:
            event (Button.Pressed): Event containing pressed button info
        """
        btn_id = event.button.id
        if btn_id == "btn_queue_videos":
            self.action_queue_type("video")
        elif btn_id == "btn_queue_srts":
            self.action_queue_type("srt")
        elif btn_id == "btn_clear_queue":
            self.action_clear_queue()
        elif btn_id == "btn_remove_selected":
            # Remove first item from queue (same as Delete key)
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
                "run_clean": False,
            }
            self.execute_pipeline_batch(config)

        elif btn_id == "btn_patch":
            ip = self.query_one("#ollama_ip", Input).value.rstrip('/')
            model = self.query_one("#model_select", Select).value
            # Store current values for config persistence
            self._config_values["ollama_ip"] = ip
            self._config_values["model"] = model
            self.execute_patcher_batch(ip, model)

    def on_select_changed(self, event: Select.Changed) -> None:
        """
        Handle selection changes in dropdown menus.

        Saves profile and model selections to config for persistence
        across app restarts.

        Args:
            event (Select.Changed): Event containing changed select widget
        """
        # Handle profile changes
        if event.select.id == "profile_select":
            self._config_values["profile"] = event.select.value
            self._save_config()
            return

        # Handle model selection changes
        if event.select.id == "model_select":
            self._config_values["model"] = event.select.value
            self._save_config()
            return

        # Handle queue list refresh (from DirectoryTree selection)
        if event.select.id == "queue_view":
            return  # This shouldn't happen but just in case

    def on_input_changed(self, event: Input.Changed) -> None:
        """
        Save config when Ollama IP input changes.

        Args:
            event (Input.Changed): Event containing changed input widget
        """
        if event.input.id == "ollama_ip":
            self._config_values["ollama_ip"] = event.value.rstrip("/")

    # --- Keyboard shortcut actions ---

    def action_queue_videos(self) -> None:
        """Queue videos via keyboard shortcut (v key)."""
        self.action_queue_type("video")

    def action_queue_srts(self) -> None:
        """Queue SRTs via keyboard shortcut (s key)."""
        self.action_queue_type("srt")

    def action_smart_vids(self) -> None:
        """Smart queue videos without SRTs via keyboard shortcut (n key)."""
        self.action_smart_queue("vids_no_srt")

    def action_smart_translate(self) -> None:
        """Smart queue untranslated SRTs via keyboard shortcut (t key)."""
        self.action_smart_queue("untra_srt")

    def action_smart_rebalance(self) -> None:
        """Smart queue needs-rebalancing files via keyboard shortcut (b key)."""
        self.action_smart_queue("needs_rebalance")

    def action_start_pipeline(self) -> None:
        """Trigger start pipeline button via keyboard shortcut (x key)."""
        btn = self.query_one("#btn_start", Button)
        if btn.visible:
            btn.press()

    def action_verify_patch(self) -> None:
        """Trigger verify & patch button via keyboard shortcut (z key)."""
        btn = self.query_one("#btn_patch", Button)
        if btn.visible:
            btn.press()

    # --- Background Workers ---

    @work(thread=True)
    def poll_models_worker(self, ip: str) -> None:
        """
        Fetch available models from Ollama API in background thread.

        Queries the Ollama REST API at /api/tags to get list of
        loaded models. Updates model dropdown on main thread using
        call_from_thread to prevent UI crashes.

        Args:
            ip (str): Ollama server IP/URL (e.g., "http://127.0.0.1:11434")
        """
        try:
            response = requests.get(f"{ip}/api/tags", timeout=5)
            response.raise_for_status()
            models = [m['name'] for m in response.json().get('models', [])]
            self.call_from_thread(self._update_model_select, models)
            self.write_thread_log(f"[green]Success. Found {len(models)} models.[/green]")
        except Exception as e:
            self.write_thread_log(f"[red]Error reaching Ollama: {e}[/red]")

    def _update_model_select(self, models: list) -> None:
        """
        Update model dropdown with fetched models.

        Sets options to available models and selects saved model if
        still available, otherwise first model in list. This method
        runs on main thread (called via call_from_thread).

        Args:
            models (list): List of model names from Ollama API
        """
        select = self.query_one("#model_select", Select)
        if models:
            # Set options first (required before setting value)
            select.set_options([(m, m) for m in models])
            # Check if saved model is available, otherwise use first
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
        """
        Write message to console widget (main thread only).

        Args:
            message (str): Message to display in console
        """
        self.query_one("#console", RichLog).write(message)

    def write_thread_log(self, message: str) -> None:
        """
        Thread-safe log writer.

        Checks if running on main thread and uses appropriate method
        to avoid crashes from cross-thread UI updates.

        Args:
            message (str): Message to display in console
        """
        if threading.current_thread() == threading.main_thread():
            self.write_main_log(message)
        else:
            self.call_from_thread(self.write_main_log, message)

    def update_telemetry(self, current: int, total: int, start_time: float, step_name: str = "Processing") -> None:
        """
        Update progress bar and status text with telemetry data.

        Calculates percentage complete, speed, and ETA based on
        processing metrics. Thread-safe via internal check.

        Args:
            current (int): Current item number (1-indexed)
            total (int): Total items in batch
            start_time (float): Unix timestamp when batch started
            step_name (str): Name of current pipeline stage
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
        """
        Set progress bar value and status text (main thread only).

        Args:
            pct (float): Progress percentage (0-100)
            text (str): Status text to display
        """
        self.query_one("#progress_bar", ProgressBar).progress = pct
        self.query_one("#status_label", Static).update(text)

    # --- Data Parsing & Anchoring ---

    def get_base_prompt(self) -> str:
        """
        Get translation prompt based on selected profile.

        Combines the system context from current profile with
        formatting instructions for SRT output.

        Returns:
            str: Complete prompt string for LLM
        """
        profile_key = self.query_one("#profile_select", Select).value
        context = self.profiles.get(profile_key, "")
        return f"""{context}
CRITICAL LAWS OF RE-FORMATTING:
1. Preserve the original SRT line numbers and timestamps exactly.
2. Split continuous walls of text into new lines when the speaker changes. Start new speakers with a hyphen (-).
3. Return ONLY the finished SRT block output. Do not include markdown blocks or notes."""

    def parse_srt_blocks(self, file_path: str) -> dict:
        """
        Parse SRT file into dictionary of indexed blocks.

        Reads SRT file and splits by double newlines to extract
        individual subtitle blocks keyed by their index number.

        Args:
            file_path (str): Path to SRT file

        Returns:
            dict: Dictionary mapping index numbers to block strings
        """
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
        """
        Force LLM output to use original Japanese timestamps.

        Intercepts translated text from LLM and replaces any
        hallucinated timestamps with mathematically correct ones
        from the source Japanese SRT file. This ensures timing
        accuracy regardless of what the LLM outputs.

        Args:
            raw_llm_text (str): Raw output from LLM (may have wrong timestamps)
            original_jp_blocks (dict): Dictionary of original Japanese blocks with correct times

        Returns:
            str: Corrected SRT text with proper timestamps
        """
        llm_blocks = re.split(r'\n\s*\n', raw_llm_text.strip())
        anchored_output = []

        for block in llm_blocks:
            lines = block.strip().split('\n')
            if len(lines) >= 3:
                try:
                    idx = int(lines[0].strip())
                    if idx in original_jp_blocks:
                        # Grab the original Japanese block and extract line 2 (the true timestamp)
                        orig_lines = original_jp_blocks[idx].split('\n')
                        true_time = orig_lines[1]

                        # Rebuild the block: [Index] -> [True Time] -> [Translated Text]
                        text = "\n".join(lines[2:])
                        anchored_output.append(f"{idx}\n{true_time}\n{text}")
                    else:
                        # If LLM hallucinates an index that doesn't exist, we append it as-is
                        # so we don't lose text, but it's highly unlikely to break chronological order.
                        anchored_output.append(block)
                except ValueError:
                    anchored_output.append(block)
            else:
                anchored_output.append(block)

        return "\n\n".join(anchored_output)

    def time_to_ms(self, t_str: str) -> int:
        """
        Convert SRT timestamp string to milliseconds.

        Parses "HH:MM:SS,mmm" format and returns total milliseconds.

        Args:
            t_str (str): Timestamp string in SRT format

        Returns:
            int: Total milliseconds
        """
        h, m, s, ms = map(int, re.split('[:,]', t_str))
        return (h * 3600000) + (m * 60000) + (s * 1000) + ms

    def ms_to_time(self, ms: int) -> str:
        """
        Convert milliseconds to SRT timestamp string.

        Args:
            ms (int): Milliseconds to convert

        Returns:
            str: Timestamp in "HH:MM:SS,mmm" format
        """
        td = timedelta(milliseconds=ms)
        h, rem = divmod(int(td.total_seconds()), 3600)
        m, s = divmod(rem, 60)
        return f"{h:02}:{m:02}:{s:02},{ms % 1000:03}"

    def rebalance_srt_file(self, input_srt: str) -> None:
        """
        Rebalance long subtitle lines into shorter chunks.

        Splits subtitles longer than 85 characters at punctuation
        boundaries to improve readability. Creates new '-balanced.srt'
        file with proportional time distribution.

        Args:
            input_srt (str): Path to input SRT file
        """
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

                    # Find split point at punctuation near middle of text
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
        """
        Execute full translation pipeline on queued files.

        Processes each file through stages: FFmpeg (audio extraction)
        -> WhisperX (transcription) -> Ollama (translation) -> Rebalance.
        Runs in background thread to keep UI responsive.

        Args:
            config (dict): Pipeline configuration with keys for each stage
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
                        timeout=300  # 5 minute timeout
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
            if config["run_translate"] and target.endswith('.srt') and not pipeline_failed:
                if not Client:
                    self.write_thread_log("[red]ERROR: 'ollama' package missing.[/red]")
                    pipeline_failed = True
                else:
                    ollama_client = Client(host=config["ip"])
                    output_srt = os.path.join(target_dir, f"{base_name}-translated.srt")
                    self.write_thread_log(f"Starting LLM Translation: {os.path.basename(output_srt)}")

                    # Parse original Japanese blocks for timestamp anchoring
                    jp_blocks_dict = self.parse_srt_blocks(target)
                    blocks = list(jp_blocks_dict.values())

                    if not blocks:
                        self.write_thread_log("[red]ERROR: No subtitles found in SRT.[/red]")
                        pipeline_failed = True
                    else:
                        chunk_size = 20  # Process in batches of 20 blocks
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

                                    # Force the LLM output to conform to the original timestamps
                                    anchored_out = self.anchor_timestamps(clean_out, jp_blocks_dict)

                                    f_out.write(anchored_out + "\n\n")
                                except Exception as e:
                                    self.write_thread_log(f"[red]LLM Error on Batch {batch_num}: {e}[/red]")

                                self.update_telemetry(batch_num, total_batches, start_time)

                        self.call_from_thread(self._set_progress, 100,
                                             "Status: Translation Complete | Speed: 0 | ETA: 00:00:00")
                        target = output_srt

            # Step 4: Rebalance - Split long subtitle lines
            if config["run_rebalance"] and target.endswith('.srt') and not pipeline_failed:
                self.rebalance_srt_file(target)

            # Step 5: Cleanup - Remove temporary files
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

            self.call_from_thread(self._refresh_queue_ui_main_thread)

        self.write_thread_log("\n[bold magenta]=== ALL PIPELINE TASKS COMPLETE ===[/bold magenta]")

    @work(thread=True)
    def execute_patcher_batch(self, ip: str, model: str) -> None:
        """
        Execute surgical patching batch on queued SRT files.

        This method handles "Verify & Patch" functionality - it compares
        partial translated SRTs against their Japanese source to identify
        missing segments and fills them in via LLM translation. Unlike the
        full pipeline, this only processes already-translated files that have
        gaps compared to their source.

        Args:
            ip (str): Ollama server IP/URL
            model (str): Model name to use for translation
        """
        if not self.target_files:
            self.write_thread_log("[red]ERROR: No files selected.[/red]")
            return

        queue_snapshot = list(self.target_files)
        self.write_thread_log(f"\n[bold magenta]=== STARTING VERIFY & PATCH ON {len(queue_snapshot)} QUEUED FILES ===[/bold magenta]")

        for original_target in queue_snapshot:
            target = original_target
            target_dir = os.path.dirname(os.path.abspath(target)) or "."
            base_name = self._get_base_filename(target)

            # Verify input is an SRT file (patcher only works on subtitles)
            if not target.endswith('.srt'):
                self.write_thread_log(f"[dim]Skipping {os.path.basename(target)}: Patcher requires an .srt file.[/dim]")
                if original_target in self.target_files:
                    self.target_files.remove(original_target)
                    self.call_from_thread(self._refresh_queue_ui_main_thread)
                continue

            # Check for translated SRT (input to patcher is the partially-translated file)
            trans_file = os.path.join(target_dir, f"{base_name}-translated.srt")
            if not os.path.exists(trans_file):
                self.write_thread_log(f"[dim]Skipping {os.path.basename(target)}: Could not find matching '-translated.srt' file.[/dim]")
                if original_target in self.target_files:
                    self.target_files.remove(original_target)
                    self.call_from_thread(self._refresh_queue_ui_main_thread)
                continue

            # Find Japanese source SRT (either from WhisperX or original)
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

            # Parse both files to identify gaps (Japanese blocks without translation)
            jp_blocks = self.parse_srt_blocks(source_jp)
            trans_blocks = self.parse_srt_blocks(trans_file)

            if not jp_blocks:
                self.write_thread_log("[red]ERROR: No Japanese source blocks found.[/red]")
                continue

            # Find missing translations (Japanese indices not in translated file)
            missing_indices = [idx for idx in sorted(jp_blocks.keys()) if idx not in trans_blocks]

            if not missing_indices:
                self.write_thread_log("[green]No gaps found - all segments already translated.[/green]")
                continue

            self.write_thread_log(f"Found {len(missing_indices)} missing segments. Translating...")

            # Process missing blocks in chunks via LLM
            if not Client:
                self.write_thread_log("[red]ERROR: 'ollama' package missing.[/red]")
                continue

            ollama_client = Client(host=ip)
            chunk_size = 10  # Smaller chunks for patching to avoid context overflow
            total_chunks = (len(missing_indices) // chunk_size) + (1 if len(missing_indices) % chunk_size != 0 else 0)

            self.call_from_thread(self._set_progress, 0, "Status: Patching gaps... | ETA: Calculating...")
            start_time = time.time()

            for i in range(0, len(missing_indices), chunk_size):
                batch_indices = missing_indices[i:i + chunk_size]
                batch_num = (i // chunk_size) + 1
                self.write_thread_log(f"Patching Batch {batch_num}/{total_chunks}...")

                try:
                    # Extract Japanese text for these indices
                    jp_text = "\n\n".join(jp_blocks[idx] for idx in batch_indices if idx in jp_blocks)

                    response = ollama_client.chat(
                        model=model,
                        messages=[
                            {"role": "system", "content": self.get_base_prompt()},
                            {"role": "user", "content": f"<subtitles>\n{jp_text}\n</subtitles>"}
                        ],
                        options={"temperature": 0.2, "num_ctx": 8192, "num_predict": -1}
                    )

                    # Parse and merge translated blocks back into file
                    clean_out = response['message']['content'].replace('```srt', '').replace('```', '').strip()
                    new_blocks = self.parse_srt_blocks_from_string(clean_out)

                    for idx, block in zip(batch_indices, new_blocks):
                        trans_blocks[idx] = block  # Merge into existing blocks dict

                except Exception as e:
                    self.write_thread_log(f"[red]LLM Error on Patch Batch {batch_num}: {e}[/red]")

                self.update_telemetry(batch_num, total_chunks, start_time)

            # Write merged translated file
            output_srt = trans_file  # Overwrite the partially-translated file
            with open(output_srt, 'w', encoding='utf-8') as f:
                for idx in sorted(trans_blocks.keys()):
                    f.write(trans_blocks[idx] + "\n\n")

            self.call_from_thread(self._set_progress, 100, "Status: Patching Complete | Speed: 0 | ETA: 00:00:00")
            self.write_thread_log(f"[green]Patching complete for {os.path.basename(trans_file)}.[/green]")

            if original_target in self.target_files:
                self.target_files.remove(original_target)
                self.call_from_thread(self._refresh_queue_ui_main_thread)

        self.write_thread_log("\n[bold magenta]=== ALL PATCHING TASKS COMPLETE ===[/bold magenta]")

    def parse_srt_blocks_from_string(self, srt_text: str) -> list[str]:
        """
        Parse SRT text string into list of block strings (no index keying).

        Helper method for patcher to extract blocks from LLM output.

        Args:
            srt_text (str): Raw SRT format text

        Returns:
            list[str]: List of individual subtitle blocks
        """
        if not srt_text.strip():
            return []
        blocks = re.split(r'\n\s*\n', srt_text.strip())
        return [b for b in blocks if b.strip()]  # Filter empty blocks

if __name__ == "__main__":
    app = ShowaSync()
    app.run()