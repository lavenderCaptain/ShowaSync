# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 🏗️ High-Level Architecture
The ShowaSync suite is a monolithic Python application (`ShowaSync.py`) that functions as a conductor for several external, heavyweight AI tools and system utilities. It manages an entire pipeline from media source to final `.srt` output, relying on local subprocess execution rather than internal library calls for the core heavy lifting.

**Core Components & Flow:**
1.  **Input/Frontend (TUI):** `ShowaSync.py` provides a responsive Textual TUI layer where users select files, define context profiles, and initiate batch jobs. This module handles the overall state management (queuing, progress tracking).
2.  **Audio Extraction:** Uses **FFmpeg** to extract audio from video inputs (`.mkv`, `.ts`) and downmix/downsample them into a uniform 16kHz mono format.
3.  **Transcription & Timestamps:** Leverages **WhisperX** for the initial, high-accuracy Japanese transcription and timestamp alignment.
4.  **Translation & Patching (The Core Logic):** This is where the most complex business logic resides in `ShowaSync.py`. It manages:
    *   **Context Sandwiched Prompting:** Automatically prompting the LLM with surrounding translated English lines when an index gap occurs, enabling "Dynamic Patching."
    *   **Auto-Rebalancing:** Implementing proportional time-slicing algorithms to split large blocks of raw dialogue text into cinematic two-line chunks.
5.  **Language Model Interaction:** Communicates with the local **Ollama** instance via API calls, requiring users to manage model availability (recommended: Qwen 14B–32B).

**Key Architectural Note:** The system's complexity lies in its sequential and stateful subprocess management, not necessarily in intricate Python object relationships. Understanding the dependencies between these external tools is key to maintenance.

## 🚀 Core Commands
| Task | Command / Steps | Details |
| :--- | :--- | :--- |
| **Install Dependencies** | `pip install -r requirements.txt` | Installs core Python libraries (Textual, requests, etc.). |
| **Run Full Pipeline** | `python ShowaSync.py` | Launches the TUI. Follow prompts to select context and queue media files for end-to-end processing. |
| **Surgical Patching** | In TUI: Select file -> Click "VERIFY & PATCH GAPS" | A quick utility run that triggers gap detection by comparing a partial `.srt` against its Japanese source, patching only missing segments via the LLM. |

## 💡 Development Notes
*   **Development Focus:** Most future development will occur within `ShowaSync.py`, modifying the state machine and orchestrating external process calls.
*   **Testing Single Features:** Testing requires mock inputs for FFmpeg/WhisperX or running against dummy media files, as end-to-end testing is resource-intensive. For unit tests, mock the subprocess execution path within `ShowaSync.py`.