# Murmur

Hold-to-talk voice notes for Windows. Hold `/`, speak, release: your words are transcribed locally with
[faster-whisper](https://github.com/SYSTRAN/faster-whisper), tidied up, and appended to a Markdown file.
Everything runs on your machine from a tray icon.

## Requirements

- Windows 10 or 11
- Python 3.11 (the `py` launcher must find it)
- Optional: [Ollama](https://ollama.com) with `llama3.2` for grammar polish (`ollama pull llama3.2`)

## Install

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

This creates `.venv`, installs the requirements, generates the icon, adds a Startup shortcut so Murmur
starts with Windows, and launches it. The first start downloads the Whisper model. The tray tooltip reads
"loading model..." and then "ready". To run with a visible console for debugging, use `scripts\run_console.ps1`.

## Usage

- Hold `/` for about a third of a second. A popup on the right edge shows a live level meter.
- Release to transcribe. The cleaned text appears briefly and is saved.
- A quick tap of `/` still types `/`; `Shift+/`, `Ctrl+/` and other combinations are untouched.
- Spoken commands: "new line" starts a new line; "bullet point" (or "next bullet") starts a bullet.

## Where notes go

`Documents\Murmur Notes\YYYY-MM.md`: one file per month, a heading per day, one bullet per note.

```markdown
# Notes — November 2026

## Saturday, 14 Nov 2026

- **14:32** — Remind me to call the dentist tomorrow at three PM.
- **15:01** — Shopping list
  - Milk
  - Eggs.
```

Files are append-only.

## Configuration

`%APPDATA%\Murmur\config.json` (tray menu > Settings). Restart Murmur after editing.

| Key | Default | Meaning |
|---|---|---|
| `hotkey` | `/` | Key to hold |
| `hold_ms` | `350` | Hold time before recording starts |
| `notes_dir` | `Documents\Murmur Notes` | Where notes are written |
| `whisper_model` | `small.en` | faster-whisper model (e.g. `base.en`, `medium.en`) |
| `whisper_device` | `auto` | `auto`, `cuda` or `cpu` |
| `whisper_compute` | `auto` | `auto`, `float16`, `int8`, ... |
| `language` | `en` | Transcription language |
| `llm_cleanup` | `true` | Polish text with a local Ollama model when available |
| `ollama_url` | `http://127.0.0.1:11434` | Ollama server |
| `ollama_model` | `llama3.2` | Ollama model |
| `llm_timeout_s` | `8.0` | Give up on the LLM after this long (rule-based text is used) |
| `popup_seconds` | `4.0` | How long the result popup stays |
| `min_record_s` | `0.4` | Recordings shorter than this are ignored |
| `max_record_s` | `300` | Recording auto-stops after this many seconds |
| `sound_cues` | `true` | Start/stop/error tones |

## Tray menu

Enabled (toggle), Open this month's notes, Open notes folder, Settings, Quit.

## Troubleshooting

- **Hotkey does nothing in some windows:** Windows does not show a normal process the keys typed into
  elevated (administrator) windows. Run Murmur elevated if you need it there.
- **"Can't record":** open Settings > Privacy & security > Microphone, allow microphone access and
  "Let desktop apps access your microphone", and check the default input device.
- **Slow transcription:** without a usable CUDA setup Murmur runs on CPU. For an NVIDIA GPU, install the
  CUDA libraries into the venv: `.venv\Scripts\python -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12`
  (their DLL folders may also need to be on `PATH`). Murmur falls back to CPU automatically if CUDA fails.
- **Nothing happens:** another copy may already be running (Murmur allows one instance); check the log.
- **No grammar polish:** Ollama is not running or the model is missing; only the rule-based cleanup is used.
- **Log:** `%APPDATA%\Murmur\murmur.log`

## Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1
```

Removes the Startup shortcut and stops running instances. Notes and `%APPDATA%\Murmur` are kept.

## Tests

```powershell
.venv\Scripts\python tests\run_all.py
```

Runs the unit tests (the live Ollama test is skipped when Ollama is not reachable).
`tests\smoke_transcribe.py` and `tests\e2e_smoke.py` are slower end-to-end checks, and
`tools\demo_popup.py` is a visual demo. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how it works.

## License

MIT
