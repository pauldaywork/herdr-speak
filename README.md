# herdr-speak

A [herdr](https://herdr.dev/) plugin that reads the focused pane's last Claude Code or Pi answer aloud through a local [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI) text-to-speech server. Press `prefix+shift+s` to hear it rewritten for listening, or `prefix+shift+v` to hear it as written. Press either key again to stop.

It finds the answer from the session herdr records for the pane. While the agent is still working, it reads the last finished answer instead of the one in progress.

## How it works

The plugin rewrites the answer for listening with `claude -p` on Haiku, using the rules in `prompt.md`. It streams the rewrite sentence by sentence to Kokoro and plays the raw audio through `ffplay`, so speech starts within about two seconds. Extended thinking is turned off for the rewrite, because it can delay the first words by a minute. If the rewrite fails, the plugin reads the answer with the markdown and code removed. The `speak.last-verbatim` action always reads it that way.

| Action | What it does |
| --- | --- |
| `speak.last` | Speak the last answer, rewritten for listening. Invoke again to stop. |
| `speak.last-verbatim` | Speak the last answer as written, with markdown and code removed. |
| `speak.stop` | Stop playback. |

## Requirements

- herdr 0.7 or newer, with the Claude integration installed (`herdr integration install claude`).
- A Kokoro-FastAPI server, by default at `http://127.0.0.1:8880/v1`.
- `python3`, `ffplay` from FFmpeg, and the `claude` CLI signed in.

## Install

```bash
herdr plugin link ~/Projects/herdr-speak
```

Then add this to `~/.config/herdr/config.toml` and run `herdr server reload-config`:

```toml
[[keys.command]]
key = "prefix+shift+s"
type = "plugin_action"
command = "speak.last"
description = "speak: read the last answer aloud (again to stop)"

[[keys.command]]
key = "prefix+shift+v"
type = "plugin_action"
command = "speak.last-verbatim"
description = "speak: read the last answer as written, no rewrite (again to stop)"
```

Either key stops speech that is already playing, whichever one started it.

For Pi panes, run `herdr integration install pi` so herdr records Pi's session. Without it, the plugin falls back to the newest Pi session in the pane's working directory.

## Configure

The voice, speed, and server come from `~/.pi/speak.json` when it exists, so Pi's [privateer-speak](https://pi.dev/packages/privateer-speak) and this plugin sound the same. To override them, put a `config.json` in the directory printed by `herdr plugin config-dir speak`:

```json
{
  "baseUrl": "http://127.0.0.1:8880/v1",
  "voice": "af_bella",
  "speed": 1.0,
  "rewrite": true,
  "rewriteModel": "haiku",
  "rewriteTimeout": 60
}
```

`player` can also be set to another command that plays raw 24 kHz mono 16-bit PCM from standard input, such as `["pw-play", "--rate", "24000", "--channels", "1", "--format", "s16", "-"]`.

Errors appear as herdr notifications. The worker log is at `~/.local/state/herdr/plugins/speak/speak.log`.
