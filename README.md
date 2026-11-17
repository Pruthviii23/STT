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

This creates `.venv`, installs the requirements, adds a Startup shortcut (starts silently to the tray at
login) and a Start Menu shortcut, and starts Murmur. If an older Murmur is running it is restarted. The
first start downloads the Whisper model; the tray tooltip reads "loading model..." and then "ready".
To run with a visible console for debugging, use `scriptsun_console.ps1`.

**Pin it to the taskbar:** open Start, find Murmur, right-click, Pin to taskbar. (Windows does not allow
apps to pin themselves.) Clicking the pinned icon opens the notes window; the running window and the
pinned icon share one taskbar button.

## The window

Launching Murmur (Start Menu, pinned icon, or `python -m murmur`) shows the notes window. If Murmur is
already running, the launch just asks the running copy to show its window and exits. Closing the window
leaves Murmur running in the tray. `python -m murmur --background` starts silently with no window (this is
what the Startup shortcut uses). Left-clicking the tray icon also opens the window.

## Usage

- Hold `/` for about a third of a second. A popup on the right edge shows a live level meter.
- Release to transcribe. The cleaned text appears briefly and is saved.
- A quick tap of `/` still types `/`; `Shift+/`, `Ctrl+/` and other combinations are untouched.
- Spoken commands: "new line" starts a new line; "bullet point" (or "next bullet") starts a bullet.

### Dictate into any app

- Hold `` ` `` (backtick, the key left of `1`), speak, release. The text is typed wherever your cursor
  is: a text box, an email, a chat message, a document.
- If no text field has focus (e.g. you're on the desktop), nothing is typed: the text is shown in the
  popup and copied to the clipboard so you can paste it yourself.
- Dictated text is not saved to your notes. Line breaks are typed as Shift+Enter, so chat apps add a new
  line instead of sending.
- A quick tap of `` ` `` still types `` ` ``.

### Formal dictation (emails, Teams)

- Hold `M`, speak casually, release. A local Ollama model rewrites what you said in a polite,
  professional tone ("can you send me the slides" -> "Could you please send me the slides?") and types it
  at the cursor, with the same no-text-field fallback as above.
- It keeps your names, numbers and points, and adds no greeting or sign-off you didn't say.
- Needs Ollama. If it isn't running, the plain transcript is used and the popup says so. A bigger model
  writes better: set `formal_model` (e.g. `gemma4:latest`). The model starts loading when you press `M`,
  but the first use after a while can still take several seconds.
- Typing `m` normally still works (a quick press types `m`; `Shift+M` is untouched), but the letter appears
  when you release the key rather than when you press it. Set `formal_hotkey` to `""` to turn this off, or
  to another key.

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
| `hotkey` | `/` | Key to hold for a note |
| `dictate_hotkey` | `` ` `` | Key to hold to dictate into the focused field (`""` disables) |
| `formal_hotkey` | `m` | Key to hold to dictate formally (`""` disables) |
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
| `formal_model` | `""` | Ollama model for formal mode (`""` = `ollama_model`) |
| `formal_timeout_s` | `45.0` | Give up on the formal rewrite after this long (plain text is used) |
| `popup_seconds` | `4.0` | How long the result popup stays |
| `min_record_s` | `0.4` | Recordings shorter than this are ignored |
| `max_record_s` | `300` | Recording auto-stops after this many seconds |
| `sound_cues` | `true` | Start/stop/error tones |

## Tray menu

Open Murmur (default, left click), Enabled (toggle), Open notes folder, Settings, Quit.

## Troubleshooting

- **Hotkey does nothing in some windows:** Windows does not show a normal process the keys typed into
  elevated (administrator) windows. Run Murmur elevated if you need it there.
- **"Can't record":** open Settings > Privacy & security > Microphone, allow microphone access and
  "Let desktop apps access your microphone", and check the default input device.
- **Slow transcription:** without a usable CUDA setup Murmur runs on CPU. For an NVIDIA GPU, install the
  CUDA libraries into the venv: `.venv\Scripts\python -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12`
  (their DLL folders may also need to be on `PATH`). Murmur falls back to CPU automatically if CUDA fails.
- **Nothing happens:** another copy may already be running (Murmur allows one instance; launching again opens its window); check the log.
- **No grammar polish:** Ollama is not running or the model is missing; only the rule-based cleanup is used.
- **Log:** `%APPDATA%\Murmur\murmur.log`

## Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1
```

Removes the Startup and Start Menu shortcuts and stops running instances. Notes and `%APPDATA%\Murmur` are kept.

## Tests

```powershell
.venv\Scripts\python tests\run_all.py
```

Runs the unit tests (the live Ollama test is skipped when Ollama is not reachable).
`tests\smoke_transcribe.py` and `tests\e2e_smoke.py` are slower end-to-end checks, and
`tools\demo_popup.py` is a visual demo. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how it works.

## License

MIT
