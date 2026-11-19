# AuraFrame

Set-and-forget ambient music display for Raspberry Pi 4. Listens to room audio, identifies the track, and shows album artwork full-screen.

**Target hardware:** Raspberry Pi 4 Model B (4 GB) + 14" 1920×1200 HDMI display

## Status

Phase 4: display + artwork cache + audio gate + AudD recognition.

See [AuraFrame_PRD.md](AuraFrame_PRD.md) for full requirements.

## Setup

```bash
# System deps (Debian/Ubuntu/Pi OS)
sudo apt-get install -y python3-venv portaudio19-dev libportaudio2

cd auraframe
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
# Edit .env and set AUDD_API_TOKEN=... (https://dashboard.audd.io/)
```

## Run

```bash
auraframe --list-devices
auraframe --monitor-only              # gate + AudD identify (no UI)
auraframe --recognize-file ~/.cache/auraframe/samples/sample-XXXX.wav
auraframe --recognize-file ~/sample.wav --show-result --windowed

# Full UI appliance loop
auraframe --live
auraframe --live --windowed
```

Requires `AUDD_API_TOKEN` in the project `.env` (same folder as `config.yaml`).

```bash
# Diagnose token / paths
auraframe --check
```

Keys: `Esc` or `q` to quit.

## Config notes

- `audio.device` — mic index or name substring (`auraframe --list-devices`)
- `gate_rms_threshold` / `gate_band_ratio_min` — music gate sensitivity
- `recognition.*` — AudD endpoint, cache TTL, artwork size
- Secrets: `.env` → `AUDD_API_TOKEN`

Caches:
- artwork `~/.cache/auraframe/art`
- samples `~/.cache/auraframe/samples`
- recognition `~/.cache/auraframe/recognition`

## Layout

```text
auraframe/
├── src/
│   ├── main.py
│   ├── display.py
│   ├── audio_monitor.py
│   ├── recognizer.py
│   ├── artwork.py
│   └── config.py
├── assets/
├── config.yaml
├── pyproject.toml
└── AuraFrame_PRD.md
```

## License

TBD
