"""The start-up sequence: a real systems check, shown on the HUD and scored.

Everything here is honest. Each line of the check is read from the system
it names at start-up -- the listening engine, the voice, the language models,
memory, connected services, the phone link -- and one that did not come up
says OFFLINE in red rather than a reassuring ONLINE.

The sound is synthesised here, in numpy, when JARVIS starts, in the manner
of a film HUD: tiny data blips while it wakes, a glassy two-note ping as each
system reports, a rush of air building, and a quick rising run into a bright
chord over a sub hit at ignition. No recording is used, so there is nothing
to license and no asset to ship.

The HUD and the sound share the timings below, so each ping lands as its
line appears. None of it holds anything up: the greeting is spoken while the
sequence runs, with the sound kept low enough to sit under his voice.

    BOOT_SEQUENCE=off   in .env skips the whole sequence
    BOOT_SOUND=off      keeps the sequence, silently
    BOOT_VOLUME=0.45    0 to 1
"""

import os

import numpy as np


SAMPLE_RATE = 44100

# Seconds from the start of the sequence.
DURATION = 4.6
FIRST_LINE = 0.9
LINE_GAP = 0.32
IGNITION = 3.15

ONLINE = "online"
READY = "ready"
OFFLINE = "offline"

# Nothing set up, which is not a fault: shown, but never counted as down.
NONE = "none"


def enabled():
    return (os.getenv("BOOT_SEQUENCE") or "on").strip().casefold() not in ("off", "0", "false", "no")


def sound_enabled():
    return (os.getenv("BOOT_SOUND") or "on").strip().casefold() not in ("off", "0", "false", "no")


def line_time(index):
    """When line [index] of the check appears, in seconds from the start."""
    return FIRST_LINE + index * LINE_GAP


# ---- the checks --------------------------------------------------------------

def _check(label, probe):
    """(label, status) from probe(), which returns a status or (label, status)."""
    try:
        result = probe()
    except Exception:
        return (label, OFFLINE)

    if isinstance(result, tuple):
        return result

    return (label, result)


def _voice():
    import voice

    name = getattr(voice.engine, "name", "")
    label = "VOICE (CLOUD)" if name.startswith("groq") or name.startswith("openai") else "VOICE (LOCAL)"

    return (label, ONLINE)


def _speech():
    try:
        import edge_tts  # noqa: F401
    except Exception:
        # The Windows voice still speaks; the neural one is missing.
        return ("SPEECH (BACKUP)", READY)

    return ("SPEECH", ONLINE)


def _models():
    import providers

    count = len(providers._pool)

    return (f"MODELS ({count})", ONLINE if count else OFFLINE)


def _memory():
    from actions import memory

    return (f"MEMORY ({len(memory.facts())})", ONLINE)


def _services():
    from actions import mcp_services

    count = len(mcp_services.configured())

    # Configured, not yet connected: they connect on first use, so READY is
    # the true word for them at start-up.
    return (f"SERVICES ({count})", READY if count else NONE)


def services_line(connected, configured):
    """The SERVICES line once connecting has finished: the truth, counted."""
    if not configured:
        return (f"SERVICES ({configured})", NONE)

    if connected == configured:
        return (f"SERVICES ({configured})", ONLINE)

    if connected:
        return (f"SERVICES ({connected}/{configured})", READY)

    return (f"SERVICES (0/{configured})", OFFLINE)


def _phone():
    from phone import phone_server

    thread = getattr(phone_server, "_thread", None)

    return ("PHONE LINK", ONLINE if thread is not None and thread.is_alive() else OFFLINE)


def checks():
    """The systems check, in the order it is shown: [(label, status)]."""
    return [
        _check("VOICE", _voice),
        _check("SPEECH", _speech),
        _check("MODELS", _models),
        _check("MEMORY", _memory),
        _check("SERVICES", _services),
        _check("PHONE LINK", _phone),
    ]


def summary(items):
    """The closing line: all online, or how many are not."""
    down = sum(1 for _label, status in items if status == OFFLINE)

    if not down:
        return "ALL SYSTEMS ONLINE"

    return f"{down} SYSTEM{'S' if down > 1 else ''} OFFLINE"


# ---- the sound ---------------------------------------------------------------

def _envelope(t, start, attack, decay):
    """0 before [start], rising over [attack], then decaying with time constant [decay]."""
    local = t - start
    rise = np.clip(local / attack, 0.0, 1.0)
    fall = np.exp(-np.clip(local - attack, 0.0, None) / decay)

    return np.where(local < 0, 0.0, rise * fall)


def _smooth(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)


def _tone(frequency, t):
    """A sine whose frequency may change over time, by integrating its phase."""
    frequency = np.broadcast_to(frequency, t.shape)
    phase = 2 * np.pi * np.cumsum(frequency) / SAMPLE_RATE

    return np.sin(phase)


# Notes for the digital voice of it, as frequencies: a pentatonic run in A,
# so any handful of blips played at random still sounds deliberate.
_SCALE = (880.0, 987.77, 1108.73, 1318.51, 1479.98, 1760.0, 1975.53, 2217.46, 2637.02, 2959.96)


def _blip(out, at, frequency, length, gain, pan=0.0, glass=0.0):
    """One clean electronic blip: a sine, optionally with a glassy FM edge.

    Soft in and quickly out, so it pips rather than clicks.
    """
    start = int(at * SAMPLE_RATE)
    size = int(length * SAMPLE_RATE)

    if start >= len(out) or size <= 0:
        return

    size = min(size, len(out) - start)
    t = np.arange(size) / SAMPLE_RATE

    # A touch of frequency modulation at an inharmonic ratio is what makes
    # a pure tone sound like glass rather than a test signal.
    modulator = glass * frequency * np.sin(2 * np.pi * frequency * 1.414 * t) * np.exp(-t / (length * 0.4))
    phase = 2 * np.pi * np.cumsum(frequency + modulator) / SAMPLE_RATE
    envelope = np.clip(t / 0.002, 0, 1) * np.exp(-t / (length * 0.35))

    tone = gain * np.sin(phase) * envelope

    out[start:start + size, 0] += tone * (1 - max(0.0, pan))
    out[start:start + size, 1] += tone * (1 + min(0.0, pan))


def sound(items=None):
    """The boot sound as a (samples, 2) float32 array at SAMPLE_RATE.

    A heads-up display waking: a stream of tiny data blips while it thinks,
    a glassy two-note ping as each system reports (a falling pair for one
    that is down), a rush of air building, then a quick rising run into a
    bright chord over a sub hit at ignition.
    """
    items = items if items is not None else []
    count = int(DURATION * SAMPLE_RATE)
    t = np.arange(count) / SAMPLE_RATE
    rng = np.random.default_rng(11)

    out = np.zeros((count, 2))

    # Data chatter: tiny high blips, thickening as it wakes, thinning as
    # the check takes over.
    at = 0.05

    while at < FIRST_LINE + 0.25:
        density = 0.035 if at < FIRST_LINE else 0.07
        note = _SCALE[rng.integers(4, len(_SCALE))] * (2 if rng.random() < 0.3 else 1)
        _blip(out, at, note, 0.022, 0.11, pan=float(rng.uniform(-0.7, 0.7)))
        at += density * float(rng.uniform(0.6, 1.4))

    # A ping as each system reports: up a fifth when it is up, down when not.
    for index, (_label, status) in enumerate(items):
        at = line_time(index) + 0.18
        pan = 0.3 if index % 2 else -0.3

        if status == OFFLINE:
            _blip(out, at, 659.25, 0.07, 0.30, pan=pan)
            _blip(out, at + 0.075, 440.0, 0.11, 0.30, pan=pan)
        else:
            base = _SCALE[5 + index % 3]
            _blip(out, at, base, 0.045, 0.26, pan=pan, glass=0.8)
            _blip(out, at + 0.045, base * 1.5, 0.07, 0.24, pan=pan, glass=0.8)

    # Air building towards ignition: noise, brightening, never a tone.
    rise_start = IGNITION - 0.9
    rise = np.clip((t - rise_start) / (IGNITION - rise_start), 0.0, 1.0) * (t < IGNITION)
    air = rng.standard_normal(count)
    bright = np.diff(air, prepend=0.0)
    air = (1 - rise) * np.convolve(air, np.hanning(24) / 12, mode="same") + rise * bright * 0.5
    air *= 0.05 * rise ** 2
    out[:, 0] += air
    out[:, 1] += np.roll(air, 97)

    # Ignition: a quick run up the scale into a bright, glassy chord.
    for step, note in enumerate(_SCALE[3:9]):
        _blip(out, IGNITION - 0.19 + step * 0.03, note * 2, 0.05, 0.12, pan=(step - 2.5) / 4, glass=0.5)

    for note in (1760.0, 2217.46, 2637.02, 3520.0):
        _blip(out, IGNITION, note, 0.9, 0.12, glass=0.35)

    # The sub hit underneath, falling in pitch: weight without noise.
    hit = int(IGNITION * SAMPLE_RATE)
    tail = np.arange(count - hit) / SAMPLE_RATE
    frequency = 42 + 40 * np.exp(-tail / 0.04)
    sub = 0.9 * np.sin(2 * np.pi * np.cumsum(frequency) / SAMPLE_RATE) * np.exp(-tail / 0.22) * np.clip(tail / 0.003, 0, 1)
    out[hit:, 0] += sub
    out[hit:, 1] += sub

    # A digital echo rather than a room: a few clean taps, softer each time,
    # crossing sides. No noise, so no hiss.
    wet = np.zeros_like(out)

    for delay, gain in ((0.083, 0.30), (0.151, 0.20), (0.233, 0.12), (0.331, 0.07)):
        shift = int(delay * SAMPLE_RATE)
        wet[shift:, 0] += gain * out[:-shift, 1]
        wet[shift:, 1] += gain * out[:-shift, 0]

    stereo = out + wet

    # Fade the very end so nothing clicks, then set the level.
    fade = int(0.25 * SAMPLE_RATE)
    stereo[-fade:] *= np.linspace(1.0, 0.0, fade)[:, None]

    try:
        volume = float(os.getenv("BOOT_VOLUME") or 0.45)
    except ValueError:
        volume = 0.45

    volume = min(1.0, max(0.0, volume))
    peak = float(np.max(np.abs(stereo))) or 1.0

    return (stereo / peak * 0.7 * volume).astype(np.float32)


def play(audio):
    """Start the sound and return at once; failure to play is not failure to start."""
    try:
        import sounddevice as sd

        sd.play(audio, SAMPLE_RATE)
    except Exception as error:
        print(f"[JARVIS] boot sound unavailable: {error}")
