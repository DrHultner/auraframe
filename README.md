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

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # add API tokens later
```

## Run

```bash
# Windowed idle screen (dev)
python -m src.main --windowed

# Demo: local file
python -m src.main --windowed --demo-art assets/demo_art.png \
  --title "Neon Nights" --artist "Aura Demo" --demo-delay 1.5

# Demo: download + cache from URL
python -m src.main --windowed \
  --demo-art "https://picsum.photos/1000/1000.jpg" \
  --title "Cached Cover" --artist "Aura Demo"

# Fullscreen on target display (Pi)
python -m src.main

# List microphones
python -m src.main --list-devices

# Audio gate only (no UI) — play music and watch logs / WAV samples
python -m src.main --monitor-only

# Full UI + live gate demo (shows placeholder art when music detected)
python -m src.main --windowed --audio-demo
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
├── requirements.txt
└── AuraFrame_PRD.md
```

## Config

Edit `config.yaml` for resolution, fade, overlay, and timeouts. Secrets go in `.env`.

## License

TBD
