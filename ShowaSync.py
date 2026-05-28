import os
import re
import glob
import time
import requests
import subprocess
from datetime import timedelta
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Button, Checkbox, Input, Select, RichLog, ProgressBar, Label, DirectoryTree, ListView, ListItem
from textual.containers import Horizontal, Vertical
from textual import work

try:
    from ollama import Client
except ImportError:
    Client = None

class FilteredDirectoryTree(DirectoryTree):
    """Filters the directory tree to only show relevant media and source SRT files."""
    def filter_paths(self, paths: list[Path]) -> list[Path]:
        valid_extensions = {".mp4", ".mkv", ".ts", ".avi", ".mov", ".srt"}
        return [
            path for path in paths
            if path.is_dir() or (
                path.suffix.lower() in valid_extensions 
                and not path.name.endswith('-translated.srt') 
                and not path.name.endswith('-balanced.srt')
            )
        ]

class SubtitleTUI(App):
    """A Textual TUI for the Subtitle E2E Processing Suite."""
    
    CSS = """
    Screen { background: $surface; }
    #main_container { padding: 1 2; height: 100%; }
    
    /* Layouts */
    .config_row { height: auto; margin-bottom: 1; layout: horizontal; }
    .file_browser_container { height: 15; layout: horizontal; margin-bottom: 1;}
    .checkbox_col { width: 1fr; height: auto; border: round $primary; padding: 1; margin-right: 1;}
    .action_row { height: auto; margin-top: 1; margin-bottom: 1; layout: horizontal; align: center middle;}
    
    /* Strict Widths to prevent UI collapse */
    #ollama_ip { width: 35; }
    #model_select { width: 35; }
    #profile_select { width: 1fr; }
    
    /* Components */
    #tree_view { width: 60%; height: 100%; border: solid $accent; }
    #queue_view { width: 40%; height: 100%; border: solid $secondary; margin-left: 1;}
    Button { margin: 0 1; }
    #console { height: 1fr; border: solid $accent; background: $panel; }
    #telemetry_label { margin-top: 1; text-align: right; width: 100%; text-style: bold; color: $success; }
    """

    BINDINGS = [
        ("q", "quit", "Quit Application"),
        ("p", "poll_models", "Poll Ollama"),
        ("c", "clear_queue", "Clear File Queue")
    ]

    def __init__(self):
        super().__init__()
        self.target_files = []
        
        self.profiles = {
            "Generic / Catch-all": "You are an expert Japanese-to-English subtitle translator. Translate the text naturally and professionally.",
            "Variety Show (Chaos)": "You are translating a Japanese variety show. Expect heavy slang, constant overlapping dialogue, and distinct visual on-screen text. Prioritize clarity and character separation.",
            "Documentary": "You are translating a formal documentary. Maintain precise, professional terminology. Clearly distinguish between a formal narrator and casual interview subjects.",
            "Sports Broadcast": "You are translating a live sports broadcast. Use high-energy play-by-play terminology, color commentary formatting, and standard athletic jargon.",
            "1959 Hardboiled Noir": "Context: A 1950s Showa-era film. The dialogue must reflect hardboiled, mid-century tough-guy aesthetics and corporate suspense.",
            "1980s Yamadamura Comedy": "Context: A 1980s Japanese comedy. Characters speak in an exaggerated rural dialect ending in 'pya'. Translate this into a comedic, exaggerated 'country' English dialect."
        }

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        
        with Vertical(id="main_container"):
            # 1. Config Row
            with Horizontal(classes="config_row"):
                yield Input(value="http://127.0.0.1:11434", id="ollama_ip", placeholder="Ollama URL")
                yield Select([("Waiting for poll...", "Waiting...")], id="model_select")
                yield Select([(k, k) for k in self.profiles.keys()], value="Generic / Catch-all", id="profile_select")
            
            # 2. File Browser & Queue
            yield Label("Select files using arrow keys + Enter (Press 'c' to clear queue):", id="file_instruction")
            with Horizontal(classes="file_browser_container"):
                yield FilteredDirectoryTree(Path("."), id="tree_view")
                yield ListView(id="queue_view")
            
            # 3. Pipeline Toggles
            with Horizontal(classes="config_row"):
                with Vertical(classes="checkbox_col"):
                    yield Checkbox("1. FFmpeg (16kHz Mono)", value=True, id="chk_ffmpeg")
                    yield Checkbox("2. WhisperX (Japanese SRT)", value=True, id="chk_whisper")
                    yield Checkbox("3. Ollama (English Translation)", value=True, id="chk_translate")
                with Vertical(classes="checkbox_col"):
                    yield Checkbox("4. Auto-Rebalance Lines", value=True, id="chk_rebalance")
                    yield Checkbox("5. Cleanup Temp Files", value=True, id="chk_cleanup")

            # 4. Actions & Telemetry
            with Horizontal(classes="action_row"):
                yield Button("VERIFY & PATCH GAPS", id="btn_patch", variant="warning")
                yield Button("START BATCH PIPELINE", id="btn_start", variant="success")
            
            yield ProgressBar(total=100, show_eta=False, id="progress_bar")
            yield Label("Status: Idle | Speed: -- | ETA: --:--:--", id="telemetry_label")

            # 5. Console
            yield RichLog(id="console", highlight=True, markup=True)
            
        yield Footer()

    def on_mount(self) -> None:
        """Runs immediately when the TUI boots."""
        self.write_main_log("Application loaded. Polling Ollama in the background...")
        ip = self.query_one("#ollama_ip", Input).value.rstrip('/')
        self.poll_models_worker(ip)

    # --- UI Interactions & Bindings ---
    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        """Adds a selected file from the tree to the processing queue."""
        filepath = str(event.path)
        if filepath not in self.target_files:
            self.target_files.append(filepath)
            queue = self.query_one("#queue_view", ListView)
            queue.append(ListItem(Label(os.path.basename(filepath))))
            self.write_main_log(f"Queued: {os.path.basename(filepath)}")

    def action_clear_queue(self) -> None:
        """Clears the file queue when 'c' is pressed."""
        self.target_files.clear()
        self.query_one("#queue_view", ListView).clear()
        self.write_main_log("[yellow]Queue cleared.[/yellow]")

    def action_poll_models(self) -> None:
        """Triggered by pressing 'p'."""
        ip = self.query_one("#ollama_ip", Input).value.rstrip('/')
        self.write_main_log(f"Initiating manual poll for {ip}...")
        self.poll_models_worker(ip)

    # --- Background Workers ---
    @work(thread=True)
    def poll_models_worker(self, ip: str) -> None:
        """The background worker that safely talks to Ollama without freezing the UI."""
        try:
            response = requests.get(f"{ip}/api/tags", timeout=5)
            response.raise_for_status()
            models = [m['name'] for m in response.json().get('models', [])]
            self.call_from_thread(self._update_model_select, models)
            self.write_thread_log(f"[green]Success. Found {len(models)} models.[/green]")
        except Exception as e:
            self.write_thread_log(f"[red]Error reaching Ollama: {e}[/red]")

    def _update_model_select(self, models: list) -> None:
        select = self.query_one("#model_select", Select)
        if models:
            select.set_options([(m, m) for m in models])
            select.value = models[0]
        else:
            select.set_options([("No models found", "None")])

    # --- Thread-Safe Logging & Telemetry ---
    def write_main_log(self, message: str) -> None:
        self.query_one("#console", RichLog).write(message)

    def write_thread_log(self, message: str) -> None:
        self.call_from_thread(self.query_one("#console", RichLog).write, message)

    def update_telemetry(self, current: int, total: int, start_time: float) -> None:
        if current == 0: return
        elapsed = time.time() - start_time
        avg_time = elapsed / current
        eta = int((total - current) * avg_time)
        speed = 60.0 / avg_time if avg_time > 0 else 0
        pct = (current / total) * 100
        
        status_text = f"Status: Processing... | Speed: {speed:.1f} batches/min | ETA: {timedelta(seconds=eta)}"
        self.call_from_thread(self._set_progress, pct, status_text)

    def _set_progress(self, pct: float, text: str) -> None:
        self.query_one("#progress_bar", ProgressBar).progress = pct
        self.query_one("#telemetry_label", Label).update(text)

    # --- Translation Pipeline Logic ---
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

    # --- Button Routing ---
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn_start":
            self.execute_pipeline_batch()
        elif event.button.id == "btn_patch":
            self.execute_patcher_batch()

    # --- Threaded Execution Engine ---
    @work(thread=True)
    def execute_pipeline_batch(self) -> None:
        if not self.target_files:
            self.write_thread_log("[red]ERROR: No files selected in queue.[/red]")
            return

        ip = self.query_one("#ollama_ip", Input).value.rstrip('/')
        model = self.query_one("#model_select", Select).value
        run_ffmpeg = self.query_one("#chk_ffmpeg", Checkbox).value
        run_whisper = self.query_one("#chk_whisper", Checkbox).value
        run_translate = self.query_one("#chk_translate", Checkbox).value
        run_rebalance = self.query_one("#chk_rebalance", Checkbox).value
        run_clean = self.query_one("#chk_cleanup", Checkbox).value

        self.write_thread_log(f"\n[bold magenta]=== INITIATING BATCH PIPELINE ({len(self.target_files)} Files) ===[/bold magenta]")
        
        for idx, target in enumerate(self.target_files):
            file_root, ext = os.path.splitext(target)
            self.write_thread_log(f"\n[bold cyan]--- PROCESSING [{idx+1}/{len(self.target_files)}]: {os.path.basename(target)} ---[/bold cyan]")
            
            wav_out = f"{file_root}_16k_mono.wav"
            
            if run_ffmpeg and ext.lower() in ['.mkv', '.mp4', '.ts', '.avi', '.mov']:
                self.write_thread_log("Extracting audio via FFmpeg...")
                subprocess.run(["ffmpeg", "-i", target, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav_out, "-y"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                target = wav_out
                self.write_thread_log("[green]FFmpeg extraction complete.[/green]")

            if run_whisper and target.endswith('.wav'):
                self.write_thread_log("Running WhisperX (Generating Source SRT)...")
                subprocess.run(["whisperx", target, "--model", "large-v3", "--language", "ja", "--max_line_width", "40", "--max_line_count", "2", "--compute_type", "float16", "--output_format", "srt"])
                target = f"{file_root}_16k_mono.srt"
                self.write_thread_log("[green]WhisperX transcription complete.[/green]")

            if run_translate and target.endswith('.srt'):
                if not Client:
                    self.write_thread_log("[red]ERROR: 'ollama' package missing.[/red]")
                    continue
                
                ollama_client = Client(host=ip)
                output_srt = f"{file_root}-translated.srt"
                self.write_thread_log(f"Starting LLM Translation: {os.path.basename(output_srt)}")
                
                blocks = list(self.parse_srt_blocks(target).values())
                if not blocks: continue

                chunk_size = 20
                total_batches = (len(blocks) // chunk_size) + (1 if len(blocks) % chunk_size != 0 else 0)
                
                self.call_from_thread(self._set_progress, 0, "Status: Warming up LLM... | ETA: Calculating...")
                start_time = time.time()
                
                with open(output_srt, 'w', encoding='utf-8') as f_out:
                    for i in range(0, len(blocks), chunk_size):
                        batch_num = (i // chunk_size) + 1
                        batch_text = "\n\n".join(blocks[i:i + chunk_size])
                        self.write_thread_log(f"Sending Batch {batch_num}/{total_batches} to LLM...")
                        
                        try:
                            response = ollama_client.chat(
                                model=model,
                                messages=[
                                    {"role": "system", "content": self.get_base_prompt()},
                                    {"role": "user", "content": f"<subtitles>\n{batch_text}\n</subtitles>"}
                                ],
                                options={"temperature": 0.2, "num_predict": -1}
                            )
                            clean_out = response['message']['content'].replace('```srt','').replace('```','').strip()
                            f_out.write(clean_out + "\n\n")
                        except Exception as e:
                            self.write_thread_log(f"[red]LLM Error on Batch {batch_num}: {e}[/red]")
                        
                        self.update_telemetry(batch_num, total_batches, start_time)

                self.call_from_thread(self._set_progress, 100, "Status: Translation Complete | Speed: 0 | ETA: 00:00:00")
                target = output_srt

            if run_rebalance and target.endswith('.srt'):
                self.rebalance_srt_file(target)

            if run_clean:
                self.write_thread_log("Cleaning up temp files...")
                if os.path.exists(wav_out): os.remove(wav_out)
                for ext_to_del in ['.json', '.vtt', '.txt', '.tsv']:
                    junk = f"{file_root}_16k_mono{ext_to_del}"
                    if os.path.exists(junk): os.remove(junk)

        self.write_thread_log("\n[bold magenta]=== ALL PIPELINE TASKS COMPLETE ===[/bold magenta]")

    @work(thread=True)
    def execute_patcher_batch(self) -> None:
        if not self.target_files:
            self.write_thread_log("[red]ERROR: No files selected.[/red]")
            return

        ip = self.query_one("#ollama_ip", Input).value.rstrip('/')
        model = self.query_one("#model_select", Select).value

        for target in self.target_files:
            if not target.endswith('.srt'): continue
            
            if not target.endswith('-translated.srt'):
                trans_file = target.replace('.srt', '-translated.srt')
                if not os.path.exists(trans_file): continue
                target = trans_file

            file_root = target.replace('-translated.srt', '')
            source_jp = f"{file_root}_16k_mono.srt"
            if not os.path.exists(source_jp):
                source_jp = f"{file_root}.srt"
                if not os.path.exists(source_jp): continue

            self.write_thread_log(f"\n[bold yellow]=== INITIATING SURGICAL PATCHER: {os.path.basename(target)} ===[/bold yellow]")
            jp_blocks = self.parse_srt_blocks(source_jp)
            eng_blocks = self.parse_srt_blocks(target)
            
            missing_indices = sorted(list(set(jp_blocks.keys()) - set(eng_blocks.keys())))
            
            if not missing_indices:
                self.write_thread_log("[green]Verification passed. No missing indices found.[/green]")
                continue
                
            total = len(missing_indices)
            self.write_thread_log(f"[yellow]Found {total} missing blocks. Commencing targeted patching...[/yellow]")
            ollama_client = Client(host=ip)
            
            self.call_from_thread(self._set_progress, 0, "Status: Patching gaps... | ETA: Calculating...")
            start_time = time.time()
            
            for i, missing_idx in enumerate(missing_indices):
                current_num = i + 1
                self.write_thread_log(f"Patching Gap Index #{missing_idx} ({current_num}/{total})...")
                
                jp_text = jp_blocks[missing_idx]
                prev_ctx = eng_blocks.get(missing_idx - 1, "None")
                next_ctx = eng_blocks.get(missing_idx + 1, "None")
                
                patch_prompt = f"""{self.get_base_prompt()}
You are fixing a missing line in an established sequence.
=== PREVIOUS CONTEXT ===\n{prev_ctx}
=== NEXT CONTEXT ===\n{next_ctx}
Translate the missing Japanese block so it flows perfectly. Output ONLY the completed SRT block for index {missing_idx}."""

                try:
                    res = ollama_client.chat(
                        model=model,
                        messages=[{"role": "user", "content": patch_prompt + f"\n\n{jp_text}"}],
                        options={"temperature": 0.2}
                    )
                    eng_blocks[missing_idx] = res['message']['content'].replace('```srt','').replace('```','').strip()
                except Exception as e:
                    self.write_thread_log(f"[red]API Error on index {missing_idx}: {e}[/red]")
                
                self.update_telemetry(current_num, total, start_time)
                
            with open(target, 'w', encoding='utf-8') as f:
                for idx in sorted(eng_blocks.keys()):
                    f.write(eng_blocks[idx] + "\n\n")
                    
            self.call_from_thread(self._set_progress, 100, "Status: Patching Complete | Speed: 0 | ETA: 00:00:00")
            self.write_thread_log(f"[green]Patching complete for {os.path.basename(target)}.[/green]")

if __name__ == "__main__":
    app = SubtitleTUI()
    app.run()