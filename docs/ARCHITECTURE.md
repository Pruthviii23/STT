# Architecture

Murmur is a single-process Windows tray app. Holding a hotkey records the microphone; releasing it
transcribes the audio locally, tidies the text, appends it to a Markdown file and shows a popup.

## Modules (`murmur/`)

| Module | Role |
|---|---|
| `__main__.py` | `python -m murmur` entry point; calls `app.main()`. |
| `app.py` | Wiring and lifecycle: logging, single-instance mutex, `App` (hold flow, worker, model load, shutdown), `process_audio()` (audio to cleaned text to note). |
| `config.py` | `Config` dataclass, JSON load/save at `%APPDATA%\Murmur\config.json`, default notes folder (`Documents\Murmur Notes`, resolved through `SHGetKnownFolderPath`). |
| `hotkey.py` | `HoldHotkey`: global hook with suppression, tap/hold state machine, key replay via `SendInput`. |
| `recorder.py` | `Recorder`: `sounddevice` input stream at the device's native rate, resampled to 16 kHz on stop; per-block level callback. |
| `transcriber.py` | `Transcriber`: faster-whisper wrapper; CUDA float16 then CPU int8 fallback; filters known Whisper hallucinations on silence. |
| `postprocess.py` | `rule_clean()` (fillers, repeats, spoken commands, punctuation, capitalisation) and optional `llm_clean()` via Ollama with output guards; `clean()` combines them. |
| `storage.py` | `append_note()`: append-only monthly Markdown file, `fsync`ed. |
| `popup.py` | `Popup`: borderless, non-activating, always-on-top Tk card (listening / processing / result / message). |
| `tray.py` | `Tray`: pystray icon and menu. |
| `sounds.py` | In-memory sine-tone cues played async through `winsound`. |
| `icon.py` | PIL drawing of the tray icon and the `.ico` used by the installer. |

## Threads

| Thread | Owner | Work |
|---|---|---|
| Tk main thread | `App.run` | Tk mainloop; `Popup` drawing (public popup methods only enqueue closures, a 15 ms `after` pump runs them); `_poll` checks the quit flag every 200 ms and calls `shutdown()`. |
| Hook thread | `keyboard` library | Runs `HoldHotkey._on_event` for every key event. Must be fast; does short work under a lock and returns pass (True) or suppress (False). |
| Callback thread (`murmur-callbacks`) | `HoldHotkey` | Runs `on_hold_start` / `on_hold_end` strictly serialised; an end is only delivered if its start ran. |
| Inject thread (`murmur-inject`) | `HoldHotkey` | Replays suppressed keys with `SendInput`. |
| Worker thread (`murmur-worker`) | `App` | Pulls `(audio, start_time)` jobs from a queue: transcribe, clean, save, show popup. Waits on `model_ready` first. |
| Tray thread | pystray (`run_detached`) | Menu callbacks; they only set flags or open files. |
| Short-lived | `App` / `HoldHotkey` | `murmur-model` (loads Whisper, then pre-warms Ollama), the hold timer, the `max_record_s` timer, and a per-hold `murmur-watchdog`. |

Hotkey callbacks never block: `_hold_start` opens the microphone and returns; `_hold_end` stops it and
queues a job for the worker.

## Data flow: hold to note

1. Hook thread sees `/` down (no modifiers), suppresses it, starts the hold timer (`hold_ms`).
2. Timer fires: state becomes HOLDING and `"start"` is queued to the callback thread.
3. `App._hold_start`: start cue, `popup.show_listening()`, `Recorder.start()`, arm the `max_record_s` auto-stop timer.
4. `/` up: state returns to IDLE and `"end"` is queued. `App._finish` stops the recorder (resamples to 16 kHz).
5. Audio shorter than `min_record_s` is dropped. Otherwise the popup shows "processing" and `(audio, start_time)` goes to the worker queue.
6. Worker: `Transcriber.transcribe` (waits for model load) -> `postprocess.clean` (rules, then optional Ollama polish when the text has at least 6 words) -> `storage.append_note` -> `popup.show_result`. Empty or hallucinated transcripts show "Didn't catch that"; exceptions show an error popup.

Each hold gets a session id; `_finish(sid)` ignores a session that has already ended, so the hotkey's
release and the `max_record_s` timer cannot both finish the same recording.

## Hotkey state machine

States: `IDLE`, `PENDING` (key down, waiting to learn tap vs hold), `HOLDING`.

- **Down, IDLE:** if Shift/Ctrl/Alt/Win is held, pass through untouched. Otherwise suppress, go to PENDING, start the timer.
- **Hold:** timer fires while PENDING -> HOLDING, recording starts.
- **Tap:** up while PENDING -> IDLE; a `tap` job makes the inject thread type `/` (down + up via `SendInput`).
- **Interrupt:** another key goes down while PENDING -> that key is suppressed too, and the inject thread replays the `/` tap and then that key's press, preserving order. The physical `/` release that follows is swallowed (`_swallow_up`) because the replay already included an up.
- **Auto-repeat:** `/` down while not IDLE is suppressed and ignored.
- **Release while HOLDING:** suppressed, state IDLE, `"end"` queued.
- **Busy backlog:** while the inject thread has outstanding items (`_busy` > 0), every further key event is suppressed and queued behind them, so later events (such as key-ups) cannot overtake the replay.
- **Injection guard:** the `keyboard` library does not expose the injected flag, so each injected event is recorded in an `_expected` deque of `(scan_code, event_type, deadline)` (TTL 1 s). A matching event at the head of the deque is passed straight through and consumed. Injection uses our own `SendInput` because `keyboard.send` temporarily disables the hook for all events.
- **Watchdog:** a thread per hold ends it after `MAX_HOLD_S` (600 s) as a safety cap.
- **Disabled:** when `enabled` is False (tray toggle) every event passes through.
- **Stop:** unhooks; a HOLDING state delivers `end`, a PENDING state types the pending `/`.

`/` is matched by name or by scan code 53 on the main block (keypad `/` is ignored). A non-elevated
process does not receive key events destined for elevated windows.

## Notes format (`storage.py`)

`<notes_dir>\YYYY-MM.md`, created on the first note with `# Notes — Month YYYY`. A
`## Weekday, D Mon YYYY` heading is added when the last heading differs from today's. Each note is
`- **HH:MM** — text`; extra lines are indented two spaces. The file is only ever appended to, and
flushed with `fsync`.

## Config keys (`config.py`)

`hotkey`, `hold_ms`, `notes_dir`, `whisper_model`, `whisper_device`, `whisper_compute`, `language`,
`llm_cleanup`, `ollama_url`, `ollama_model`, `llm_timeout_s`, `popup_seconds`, `min_record_s`,
`max_record_s`, `sound_cues`. Defaults and meanings are in the README table. Unknown keys are ignored,
missing keys take defaults, and the file is rewritten with every key on each start. `notes_dir`
supports environment variables (`%VAR%`).

## Process-level details

- Single instance via the named mutex `Local\MurmurVoiceNotes`; a second launch exits immediately.
- Logging: rotating file `%APPDATA%\Murmur\murmur.log` (1 MB x 4), plus stderr when attached to a console.
- Shutdown (tray Quit): stop hotkey, tray and recorder, signal the worker, quit the Tk loop.
