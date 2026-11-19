# AuraFrame — Product Requirements Document

**Version:** 1.0  
**Date:** 2026-11-18  
**Status:** Draft for Development  
**Target Platform:** Raspberry Pi 4 Model B (4 GB) + Showscren 14" 1920×1200 HDMI Monitor

---

## 1. Executive Summary

AuraFrame is a set-and-forget ambient music display appliance. It continuously listens to room audio through a microphone, detects when music is playing, identifies the track, and displays the corresponding album artwork full-screen on a dedicated display.

The system is designed to run as a dedicated appliance on a Raspberry Pi 4 B (4 GB) with a 14-inch HDMI monitor. The user interface is a lightweight Python/Pygame fullscreen application optimized for low resource usage. No touch input is used; the device operates autonomously.

## 2. Goals & Non-Goals

### Goals
- Continuously monitor ambient audio and detect music
- Identify the currently playing track with acceptable accuracy and latency
- Display high-quality album artwork full-screen
- Run reliably 24/7 as a dedicated appliance on Raspberry Pi 4 B (4 GB)
- Keep resource usage low (CPU, memory, power)
- Operate without user interaction (no touchscreen, no remote required for core function)
- Support local artwork caching for performance and offline resilience

### Non-Goals (v1)
- Touchscreen interaction or on-device controls
- User accounts, multi-user support, or social features
- High-fidelity audio playback
- Complex settings UI or remote configuration (beyond basic service management)
- Support for video content or non-music audio identification
- Battery operation or portable use

## 3. Hardware Requirements

| Component | Specification | Notes |
|-----------|---------------|-------|
| Compute | Raspberry Pi 4 Model B, 4 GB RAM | Primary target |
| Display | Showscren 14" 1920×1200 HDMI monitor | HDMI only; touch cable not used |
| Audio Input | USB microphone (or I2S) | Continuous ambient capture |
| Storage | microSD (32 GB+ recommended) | OS + artwork cache |
| Power | Official or high-quality 5 V / 3 A+ supply | Stable 24/7 operation |
| Cooling | Passive heatsink or case with airflow | Recommended for continuous use |

**Display Configuration Notes**
- Resolution target: 1920×1200
- HDMI connection only
- `config.txt` adjustments may be required for correct resolution
- Mouse cursor hidden; screensaver/blanking disabled

## 4. Functional Requirements

### 4.1 Audio Monitoring & Music Detection
- Continuously capture audio from the microphone
- Implement a local music detection gate to avoid unnecessary recognition calls (e.g., energy + spectral features or lightweight model such as YAMNet TFLite)
- Extract short audio samples (typically 8–15 seconds) when music is likely present
- Adapt sampling behavior to reduce API usage during continuous music or silence

### 4.2 Track Recognition
- Send audio samples to a cloud recognition service (primary: AudD or ACRCloud; fallback options acceptable)
- Retrieve track metadata: title, artist, album, and artwork URL when available
- Handle recognition failures, rate limits, and network issues gracefully
- Cache successful recognition results to avoid redundant API calls for the same track

### 4.3 Album Artwork Display
- Fetch and cache album artwork locally
- Display artwork full-screen using a lightweight Python/Pygame application
- Scale artwork to fit the 1920×1200 display while preserving aspect ratio
- Show optional subtle overlay with track title and artist
- Transition smoothly between tracks (fade or simple cut acceptable for v1)
- Display a neutral/idle state when no music is detected

### 4.4 System Behavior
- Run as a systemd service that starts on boot
- Restart automatically on crash
- Log operational events and errors to journalctl
- Minimize CPU and memory usage during idle and active states
- Survive temporary network loss and recover when connectivity returns

## 5. Non-Functional Requirements

| Category | Requirement |
|----------|-------------|
| Performance | Recognition latency target < 10–15 seconds end-to-end under normal conditions |
| Resource Usage | Idle CPU < 15%; memory footprint suitable for 4 GB Pi with OS overhead |
| Reliability | 24/7 operation; automatic recovery from crashes and transient network issues |
| Privacy | Audio samples are short-lived and used only for recognition; no continuous cloud upload of raw audio |
| Maintainability | Python 3 codebase with clear module separation; deployable via git + systemd |
| Power | Suitable for continuous operation on a Pi 4 with appropriate cooling |

## 6. Technical Architecture

### 6.1 High-Level Components
1. **Audio Monitor** — Continuous capture + local music detection gate
2. **Recognizer** — Cloud API client + result caching
3. **Artwork Manager** — Fetch, cache, and serve album images
4. **Display Engine** — Pygame fullscreen rendering (primary UI)
5. **Orchestrator** — Main loop coordinating the above components
6. **System Service** — systemd unit for boot-time start and supervision

### 6.2 Technology Choices
- **Language / Runtime:** Python 3 (venv)
- **Display:** Pygame (fullscreen, efficient on Pi 4)
- **Audio Capture:** `sounddevice` or `pyaudio`
- **Image Handling:** Pillow + Pygame
- **Recognition:** AudD / ACRCloud (or equivalent) via HTTP
- **Deployment:** git clone + systemd service
- **Caching:** Local filesystem for artwork and recent recognition results

### 6.3 Proposed Project Structure
```text
auraframe/
├── src/
│   ├── main.py
│   ├── display.py
│   ├── audio_monitor.py
│   ├── recognizer.py
│   └── artwork.py
├── assets/
├── requirements.txt
└── README.md
```

## 7. User Experience

- Device powers on and automatically begins monitoring
- When music is detected and identified, album art appears full-screen
- Track/artist text may appear as a subtle overlay (configurable or always-on for v1)
- When music stops or cannot be identified, the display returns to an idle state (solid color, logo, or last-known art with dimming)
- No on-device interaction required for normal operation

## 8. Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Recognition API cost / rate limits | Local music detection gate; caching; adaptive sampling |
| Ambient noise / false positives | Tunable detection thresholds; short sample windows |
| Network dependency | Graceful degradation; local cache of recent art |
| Pi 4 thermal / stability under load | Heatsink, adequate power supply, efficient codebase |
| Display resolution issues | Documented `config.txt` settings for 1920×1200 |

## 9. Success Criteria (v1)

- System boots and starts monitoring automatically
- Music played in the room is detected and identified with reasonable accuracy
- Corresponding album artwork is displayed full-screen within the latency target
- Device runs for multiple days without intervention
- Resource usage remains acceptable on Pi 4 B 4 GB

## 10. Future Considerations (Out of Scope for v1)

- Physical button or GPIO control for manual override
- Local web interface for status and basic settings
- Multiple recognition provider fallback
- Enhanced idle states / ambient visuals
- Support for additional displays or resolutions

---

*Document generated for development kickoff. Hardware and technical choices reflect confirmed platform: Raspberry Pi 4 B (4 GB) + Showscren 14" HDMI panel with Python/Pygame fullscreen UI.*
