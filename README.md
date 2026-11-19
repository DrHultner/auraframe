# AuraFrame

Set-and-forget ambient music display for Raspberry Pi 4. Listens to room audio, identifies the track, and shows album artwork full-screen.

**Target hardware:** Raspberry Pi 4 Model B (4 GB) + 14" 1920×1200 HDMI display

## Status

Phase 1: project scaffold + Pygame display engine (idle, artwork, overlay, fade).

See [AuraFrame_PRD.md](AuraFrame_PRD.md) for full requirements.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # add API tokens later
```

## Run

```bash
# Windowed idle screen (dev)
python -m src.main --windowed

# Demo: idle → artwork → idle after timeout
python -m src.main --windowed --demo-art assets/demo_art.png \
  --title "Neon Nights" --artist "Aura Demo" --demo-delay 1.5

# Fullscreen on target display (Pi)
python -m src.main
```

Keys: `Esc` or `q` to quit.

## Layout

```text
auraframe/
├── src/
│   ├── main.py            # entry + orchestrator
│   ├── display.py         # Pygame fullscreen UI
│   ├── audio_monitor.py   # stub (phase 3)
│   ├── recognizer.py      # stub (phase 4)
│   ├── artwork.py         # local path + cache dir
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
