# AuraFrame

Set-and-forget ambient music display for Raspberry Pi 4. Listens to room audio, identifies the track, and shows album artwork full-screen.

**Target hardware:** Raspberry Pi 4 Model B (4 GB) + 14" 1920×1200 HDMI display

## Status

Early development. See [AuraFrame_PRD.md](AuraFrame_PRD.md) for product requirements.

## Planned stack

- Python 3 + Pygame (fullscreen UI)
- Local audio capture + music detection gate
- Cloud track recognition (AudD / ACRCloud)
- systemd service for 24/7 appliance operation

## License

TBD
