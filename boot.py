"""The start-up sequence: a real systems check, shown on the HUD and scored.

Everything here is honest. Each line of the check is read from the system
it names at start-up -- the listening engine, the voice, the language models,
memory, connected services, the phone link -- and one that did not come up
says OFFLINE in red rather than a reassuring ONLINE.

The sound is synthesised here, in numpy, when JARVIS starts: a sub-bass
swell, a rising power-up whine, a data chirp as each system reports, and an
ignition chord with a short reverb tail. No recording is used, so there is
nothing to license and no asset to ship.

The HUD and the sound share the timings below, so each chirp lands as its
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


def sound(items=None):
    """The boot sound as a (samples, 2) float32 array at SAMPLE_RATE."""
    items = items if items is not None else []
    count = int(DURATION * SAMPLE_RATE)
    t = np.arange(count) / SAMPLE_RATE
    rng = np.random.default_rng(7)

    # A sub-bass swell: felt as much as heard, climbing towards ignition.
    swell_freq = 38 + 16 * _smooth(t / IGNITION)
    swell = _tone(swell_freq, t) * 0.55 * _smooth(t / (IGNITION - 0.3)) * (t < IGNITION + 0.05)

    # The power-up whine: an exponential sweep with a touch of vibrato and
    # two harmonics, so it reads as a machine rather than a test tone.
    start, end = 0.3, IGNITION
    progress = np.clip((t - start) / (end - start), 0.0, 1.0)
    whine_freq = 220 * (1760 / 220) ** progress * (1 + 0.004 * np.sin(2 * np.pi * 6 * t))
    phase = 2 * np.pi * np.cumsum(whine_freq) / SAMPLE_RATE
    whine = np.sin(phase) + 0.3 * np.sin(2 * phase) + 0.12 * np.sin(3 * phase)
    whine *= 0.11 * _smooth(progress * 1.4) * ((t >= start) & (t < end)).astype(float)
    whine *= np.clip((end - t) / 0.06, 0.0, 1.0)

    # A data chirp as each line of the check reports: rising and bright when
    # a system is up, a low double blip when one is not.
    chirps = np.zeros(count)

    for index, (_label, status) in enumerate(items):
        at = line_time(index) + 0.18

        if status == OFFLINE:
            for offset in (0.0, 0.09):
                blip = _envelope(t, at + offset, 0.004, 0.035)
                chirps += 0.16 * blip * np.sign(np.sin(2 * np.pi * 330 * t))
        else:
            glide = 2400 + 900 * np.clip((t - at) / 0.06, 0.0, 1.0)
            chirps += 0.12 * _envelope(t, at, 0.003, 0.03) * _tone(glide, t)

    # Ignition: a detuned, bright A-major chord over a low boom and a burst
    # of air, all decaying into the tail.
    # Only the stretch after ignition is computed: sixty partials over the
    # whole sound cost most of a second at start-up.
    chord = np.zeros(count)
    lit = slice(int(IGNITION * SAMPLE_RATE), count)
    after = t[lit]

    for root in (110.0, 164.81, 220.0, 277.18, 329.63):
        for detune in (-0.004, 0.0, 0.004):
            for harmonic in range(1, 5):
                chord[lit] += np.sin(2 * np.pi * root * (1 + detune) * harmonic * after) / (harmonic * 1.6)

    chord *= 0.028 * _envelope(t, IGNITION, 0.015, 0.55)
    boom = 0.5 * _envelope(t, IGNITION, 0.008, 0.35) * np.sin(2 * np.pi * 55 * t)
    air = rng.standard_normal(count)
    air = np.diff(air, prepend=0.0) * 0.025 * _envelope(t, IGNITION, 0.002, 0.08)

    # The air stays out of the reverb: smeared across a second it is hiss.
    tonal = swell + whine + chirps + chord + boom
    dry = tonal + air

    # A short synthetic room: noise decaying over a second, convolved by FFT.
    impulse_length = int(1.1 * SAMPLE_RATE)
    impulse_t = np.arange(impulse_length) / SAMPLE_RATE
    impulse = rng.standard_normal(impulse_length)

    # Darkened, as a real room is: white noise as a room turns every sharp
    # sound into a second of hiss.
    impulse = np.convolve(impulse, np.hanning(24), mode="same")
    impulse *= np.exp(-impulse_t / 0.28)
    impulse[0] = 0.0
    impulse /= np.sqrt(np.sum(impulse ** 2))

    size = 1 << int(np.ceil(np.log2(count + impulse_length)))
    wet = np.fft.irfft(np.fft.rfft(tonal, size) * np.fft.rfft(impulse, size), size)[:count]

    left = dry + 0.32 * wet

    # Width: the right channel hears the whine and the tail a moment later.
    delay = int(0.011 * SAMPLE_RATE)
    late_whine = np.concatenate([np.zeros(delay), whine[:-delay]])
    late_wet = np.concatenate([np.zeros(delay), wet[:-delay]])
    right = dry - whine + late_whine + 0.32 * late_wet

    stereo = np.stack([left, right], axis=1)

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
