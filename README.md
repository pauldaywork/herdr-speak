# herdr-speak

A [herdr](https://herdr.dev/) plugin that reads the focused pane's last Claude Code or Pi answer aloud through a local [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI) text-to-speech server. Press `prefix+shift+s` to hear it rewritten for listening, or `prefix+shift+v` to hear it as written. Press either key again to stop. Press `prefix+shift+p` to pick a markdown plan and hear it.

It finds the answer from the session herdr records for the pane. While the agent is still working, it reads the last finished answer instead of the one in progress.

## How it works

The plugin rewrites the answer for listening with `claude -p` on Haiku, using the rules in `prompt.md`. It streams the rewrite sentence by sentence to Kokoro and plays the raw audio through `ffplay`, so speech starts within about two seconds. Extended thinking is turned off for the rewrite, because it can delay the first words by a minute. If the rewrite fails, the plugin reads the answer with the markdown and code removed. The `speak.last-verbatim` action always reads it that way.

`speak.plan` opens an fzf picker over the focused pane. It lists the markdown files under the pane's working directory and in `~/.claude/plans`, newest first, with a preview. The chosen file is rewritten with `prompt-plan.md`, which keeps every step and decision in order rather than shortening the plan. The picker skips hidden and dependency folders (such as `node_modules`) and looks at most six levels deep. The spoken text and audio are saved as `spoken/<project>/<name>.md` and `.opus` in the plugin state directory. That is outside every project and outside `~/.claude`, so an agent never mistakes them for a new plan. The project is the repository name, the same for every worktree of that repository, or the folder name for a file outside git (so files in `~/.claude/plans` land in `spoken/plans/`). The name is the file's path within the repository, with `--` between folders, so `docs/README.md` is saved as `docs--README` and doesn't collide with `README.md`. Playing a plan again replays the saved audio, as long as the file hasn't changed since, with no rewrite or TTS. If you stop playback early, nothing is saved.

| Action | What it does |
| --- | --- |
| `speak.last` | Speak the last answer, rewritten for listening. Invoke again to stop. |
| `speak.last-verbatim` | Speak the last answer as written, with markdown and code removed. |
| `speak.plan` | Pick a markdown plan and speak it. Invoke again to stop. |
| `speak.stop` | Stop playback. |

## Requirements

- herdr 0.7 or newer, with the Claude integration installed (`herdr integration install claude`).
- A Kokoro-FastAPI server, by default at `http://127.0.0.1:8880/v1`.
- `python3`, `ffplay` and `ffmpeg` (with libopus) from FFmpeg, `fzf` for the plan picker, and the `claude` CLI signed in.

## Start Kokoro on an NVIDIA GPU

Kokoro runs in Docker. The host needs an NVIDIA driver, Docker Engine, and the NVIDIA Container Toolkit. Follow the official [Docker installation guide](https://docs.docker.com/engine/install/ubuntu/) and [NVIDIA Container Toolkit guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html). Confirm the host driver works with `nvidia-smi` and that Docker accepts `--gpus all`. You do not need a host CUDA Toolkit or Python for Kokoro: the GPU image contains its runtime and dependencies.

The plugin starts Kokoro for you. When the server at a local `baseUrl` doesn't answer `/health`, it runs `docker start kokoro-tts`. If that container doesn't exist yet, it runs the `docker run` command below, choosing the image for your GPU with `nvidia-smi`. Then it waits up to two minutes for Kokoro to report healthy. A notification tells you it is starting. The first `docker run` also downloads the image, which takes longer. You can still start Kokoro yourself with the command below.

For an RTX 50-series or other Blackwell GPU, use Kokoro's CUDA 12.8 image:

```bash
docker run -d --name kokoro-tts --restart unless-stopped \
  --gpus all -p 127.0.0.1:8880:8880 \
  ghcr.io/remsky/kokoro-fastapi-gpu:latest-cu128
```

For an RTX 30 or 40 series GPU, use `ghcr.io/remsky/kokoro-fastapi-gpu:latest` instead. Check the [Kokoro image instructions](https://github.com/remsky/Kokoro-FastAPI) for your GPU. The `127.0.0.1` port binding makes the speech API available only on this computer.

Confirm Kokoro is ready:

```bash
curl --fail --silent --show-error http://127.0.0.1:8880/health
```

The expected response includes `"status":"healthy"`.

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

[[keys.command]]
key = "prefix+shift+p"
type = "plugin_action"
command = "speak.plan"
description = "speak: pick a plan file and read it aloud (again to stop)"
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
  "rewriteTimeout": 60,
  "planRewriteTimeout": 300,
  "spokenDir": "~/.local/state/herdr/plugins/speak/spoken",
  "autoStart": true,
  "container": "kokoro-tts",
  "startTimeout": 120
}
```

`planRewriteTimeout` is how many seconds a plan's rewrite may take, which is longer than for answers because plans are long. `spokenDir` is where spoken plans are saved. Keep it outside your projects and `~/.claude`.

Set `autoStart` to `false` to stop the plugin from starting Docker. When the plugin creates the container, it picks the image from the GPU's compute capability. It uses `ghcr.io/remsky/kokoro-fastapi-gpu:latest-cu128` for an RTX 50-series or other Blackwell GPU and `ghcr.io/remsky/kokoro-fastapi-gpu:latest` for an RTX 30 or 40 series. To override the choice, set `"image"` in `config.json`.

`player` can also be set to another command that plays raw 24 kHz mono 16-bit PCM from standard input, such as `["pw-play", "--rate", "24000", "--channels", "1", "--format", "s16", "-"]`.

Errors appear as herdr notifications. The worker log is at `~/.local/state/herdr/plugins/speak/speak.log`.
