# AuraFrame

Set-and-forget ambient music display for Raspberry Pi 4. Listens to room audio, identifies the track, and shows album artwork full-screen.

**Target hardware:** Raspberry Pi 4 Model B (4 GB) + 14" 1920×1200 HDMI display

## Status

Phase 3: display + artwork cache + audio monitor / music gate.

See [AuraFrame_PRD.md](AuraFrame_PRD.md) for full requirements.

## Setup

```bash
# System deps (Debian/Ubuntu/Pi OS)
sudo apt-get install -y python3-venv portaudio19-dev libportaudio2

cd auraframe
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env   # add API tokens later
```

After `pip install -e .`, the `auraframe` command is available while the venv is active.

## Run

```bash
auraframe --windowed
auraframe --list-devices
auraframe --monitor-only
auraframe --audio-demo
auraframe --windowed --demo-art assets/demo_art.png \
  --title "Neon Nights" --artist "Aura Demo"
```

Keys: `Esc` or `q` to quit.

Caches: artwork `~/.cache/auraframe/art`, samples `~/.cache/auraframe/samples`.

Tune the gate in `config.yaml` (`gate_rms_threshold`, `gate_band_ratio_min`, `cooldown_s`). Set `audio.device` to a device index or name substring if the default mic is wrong.

## Layout

```text
auraframe/
├── src/
│   ├── main.py            # entry + orchestrator
│   ├── display.py         # Pygame fullscreen UI
│   ├── audio_monitor.py   # capture + music gate
│   ├── recognizer.py      # stub (phase 4)
│   ├── artwork.py         # download + disk cache
│   └── config.py
├── assets/
├── config.yaml
├── pyproject.toml
└── AuraFrame_PRD.md
```

## Config

Edit `config.yaml` for resolution, fade, overlay, and timeouts. Secrets go in `.env`.

## License

TBD
