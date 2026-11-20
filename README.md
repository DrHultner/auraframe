# AuraFrame

Set-and-forget ambient music display for Raspberry Pi 4. Captures room audio, checks for music with **YAMNet**, identifies the track with **AudD**, and shows album artwork until the track is predicted to end.

**Target hardware:** Raspberry Pi 4 Model B (4 GB) + 14" 1920×1200 HDMI display

## Status

Phase 5 logic: fixed window capture → YAMNet → AudD → hold to track end.

## Loop

1. Capture **10 s** of mic audio  
2. **YAMNet** scores music content  
3. If music → **AudD** identify  
4. On match → show artwork; wait `duration - timecode + margin`  
5. When hold ends → capture again  

## Setup

```bash
# System deps (Debian/Ubuntu/Pi OS)
sudo apt-get install -y python3-venv portaudio19-dev libportaudio2

cd auraframe
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Pi: prefer lightweight TFLite if available for your Python/arch, else tensorflow
# pip install tflite-runtime
# or: pip install tensorflow

cp .env.example .env
# AUDD_API_TOKEN=...
```

YAMNet model downloads automatically to `assets/models/yamnet.tflite` on first run.

## Run

```bash
auraframe --check
auraframe --list-devices
auraframe --live                 # full UI loop
auraframe --live --windowed
auraframe --monitor-only         # headless YAMNet+AudD loop

auraframe --list-samples
auraframe --play-sample
auraframe --recognize-file ~/.cache/auraframe/samples/sample-XXXX.wav
```

## Config

See `config.yaml`:

- `audio.sample_seconds` — capture window (10)  
- `yamnet.music_threshold` — music confidence (default 0.35)  
- `recognition.end_margin_s` — extra wait after predicted end  
- `audio.device` / `sample_rate` — mic selection  

## License

TBD
