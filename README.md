# ShowaSync - LLM Subtitle Processing Suite

An end-to-end, locally hosted Terminal User Interface (TUI) for translating Japanese media into professional English subtitles. 

Built for local hardware ecosystems, this suite seamlessly chains FFmpeg audio extraction, WhisperX transcription, and local LLMs (via Ollama) into a single automated pipeline. 

### Why this exists
Most automated translation scripts are brittle. When an LLM drops a token, hallucinates, or skips a line, standard scripts silently fail and leave your `.srt` file with massive desyncs. This tool was built to solve the two biggest headaches in AI subbing:
1. **The Dynamic Patcher:** If the LLM drops a subtitle index, this tool automatically isolates the gap, builds a "Context Sandwich" using the surrounding established English lines, and surgically patches the missing translation without requiring a full re-roll.
2. **The Auto-Rebalancer:** Audio VAD models often lump 15 seconds of overlapping Showa-era dialogue into massive, unreadable blocks. The integrated rebalancer mathematically splits these text walls into standard two-line cinematic chunks using proportional time-slicing.

---

## 🚀 Features

* **Beautiful TUI:** A responsive, mouse-compatible terminal interface built on `Textual`.
* **Batch Processing:** Select an entire season of `.mkv` or `.ts` files, queue them up, and walk away.
* **Context Profiles:** Hot-swap system prompts for different genres (e.g., 1950s Hardboiled Noir, 1980s Comedy, Sports Broadcasts) to force the LLM to maintain consistent dialects.
* **Live Telemetry:** Real-time ETA, processing speed (batches/min), and batch tracking.
* **Self-Cleaning:** Automatically purges intermediate 16kHz `.wav` files and orphaned Whisper logs after completion.

---

## 🛠️ Prerequisites

This script acts as a conductor for several heavy-duty AI tools. **You must have the following installed on your system PATH before running:**

1. **[FFmpeg](https://ffmpeg.org/):** Required for extracting and downmixing video audio to 16kHz mono.
2. **[WhisperX](https://github.com/m-bain/whisperX):** Required for the initial Japanese transcription and timestamp alignment.
3. **[Ollama](https://ollama.com/):** Must be running locally or accessible via your network (e.g., hosted on a dedicated Mac Studio or local server). 

*Hardware Note: For highly contextual translations, models with 14B to 32B parameters (like `qwen2.5:14b` or `qwen3:32b`) are strongly recommended.*

---

## 📦 Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/lavenderCaptain/ShowaSync.git 
   cd ShowaSync
2. Install the required Python UI and API packages:
   ```bash
   pip install -r requirements.txt
(Note: This installs textual, requests, and ollama.)

---

## 🎮 Usage
Launch the tool directly in your terminal from the folder containing your media or .srt files:

    python ShowaSync.py

## The Workflow:

1. Configure: Enter your Ollama IP address (defaults to http://127.0.0.1:11434) and press p to poll your available models.
2. Select Context: Choose a translation profile from the dropdown that matches your media's era and tone.
3. Queue Files: Navigate the left-hand directory tree using your arrow keys and press Enter to queue media files.
4. Execute: Click Start Batch Pipeline to begin the fully automated extraction, transcription, translation, and rebalancing process.

## Surgical Patching

If you have an existing -translated.srt file that you suspect is missing lines:

1. Select the file in the directory tree.
2. Click VERIFY & PATCH GAPS.
3. The engine will compare the file against its Japanese source, dynamically prompt the LLM to fix any missing indices, and rewrite the repaired file.

## License

MIT License
