import hashlib
import json
import os
import queue
import re
import tempfile
import threading
import time
from collections import OrderedDict

import edge_tts
import numpy as np
import sounddevice as sd
import soundfile as sf


import voices


VOICE = os.getenv("JARVIS_VOICE") or "en-GB-RyanNeural"

# The film's JARVIS is measured and slightly clipped. Slowing the delivery a
# little and dropping the pitch gets closer to that than the stock reading.
# Both accept forms like "-8%" and "-6Hz"; set them empty for the default.
VOICE_RATE = os.getenv("JARVIS_VOICE_RATE", "-7%")
VOICE_PITCH = os.getenv("JARVIS_VOICE_PITCH", "-4Hz")
# Where the voice is made -- Google, Azure, Kokoro or Edge -- is voices.py's to say.
# Azure and Edge speak VOICE; Kokoro has its own (JARVIS_KOKORO_VOICE).
ENGINE = voices.engine()

# edge_tts defaults to 10s connect and 60s receive. Offline that stalls a
# reply for over a minute inside speak()'s lock, so the fallback voice is
# never reached. These are generous for a short reply and fail fast when
# there is no connection.
_NEURAL_CONNECT_TIMEOUT = 5
_NEURAL_RECEIVE_TIMEOUT = 20

# Remember a failed synthesis briefly so a run of uncached replies does not
# each pay the connect timeout while offline.
_NEURAL_COOLDOWN = 30.0
_neural_failed_at = 0.0
# edge_tts has its own timeouts, but a blocking getaddrinfo on a
# disconnected machine is an OS call they cannot interrupt. The attempt
# needs a deadline that does not depend on the network stack cooperating.
_NEURAL_SYNTHESIS_DEADLINE = 8.0

# A voice made on this PC (Kokoro) cannot stall on a network; its limit is
# only there for a model that hangs, and allows a slow CPU a long sentence.
_LOCAL_SYNTHESIS_DEADLINE = 30.0
# edge_tts synthesises an entire request before a single sample plays, so
# the deadline above is really a limit on how much text one request may
# carry. A 900-character answer cannot make it, and the voice drops to the
# local fallback part-way through. Splitting the text keeps every request
# small: the first words start almost immediately and length stops
# mattering.
#
# 220 characters is roughly two sentences of ordinary speech -- well
# inside the deadline, and large enough that the joins land on sentence
# ends rather than chopping mid-thought.
_SPOKEN_CHUNK = 220

# A reply of two sentences or more starts with its first sentence alone, so
# the first words wait only for that sentence's synthesis, not the whole
# reply's; the rest is synthesised while it plays. Not for a first sentence
# this short ("Yes, sir."), which is better said in one breath with the next.
_LEAD_SENTENCE_MIN = 25

# A first sentence longer than this starts with its first clause alone, when
# that clause can stand alone (_LEAD_SENTENCE_MIN). Made on this PC, Kokoro
# takes about half as long as the speech to make it, so a long first
# sentence ("Bitcoin is up 0.1 percent since 15:01 at sixty-two thousand,
# six hundred pounds.") kept the HUD's words waiting two seconds for the
# voice; its first clause waits half that, and the rest is made while it
# plays. The break falls at a comma, where a pause sounds natural anyway.
_LEAD_CLAUSE_AFTER = 60

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_CLAUSE_END = re.compile(r"(?<=[,;:])\s+")


def _speakable_chunks(text, limit=_SPOKEN_CHUNK):
    """_split_for_speech's pieces, with a long enough first sentence on its own so it starts sooner."""
    chunks = _split_for_speech(text, limit)

    if not chunks:
        return chunks

    sentences = _SENTENCE_END.split(chunks[0], maxsplit=1)
    lead, rest = (sentences + [""])[:2]

    if not rest or len(lead) < _LEAD_SENTENCE_MIN:
        lead, rest = chunks[0], ""

    if len(lead) > _LEAD_CLAUSE_AFTER:
        clauses = _CLAUSE_END.split(lead, maxsplit=1)

        if len(clauses) == 2 and len(clauses[0]) >= _LEAD_SENTENCE_MIN:
            lead, rest = clauses[0], f"{clauses[1]} {rest}".strip()

    return [piece for piece in (lead, rest) if piece] + chunks[1:]


def _split_for_speech(text, limit=_SPOKEN_CHUNK):
    """Text as speakable pieces, each small enough to synthesise in time.

    Sentence boundaries first, because that is where a pause sounds
    natural. A sentence longer than the limit is cut at clause breaks, and
    one with no punctuation at all is cut on whitespace -- rare, but a wall
    of text should still be spoken properly rather than lost to the
    fallback voice.

    Short neighbours are then recombined, so "Yes, sir." never becomes a
    request of its own.
    """
    text = " ".join(str(text or "").split())

    if not text:
        return []

    if len(text) <= limit:
        return [text]

    pieces = []

    for sentence in _SENTENCE_END.split(text):
        if len(sentence) <= limit:
            pieces.append(sentence)
            continue

        for clause in _CLAUSE_END.split(sentence):
            if len(clause) <= limit:
                pieces.append(clause)
                continue

            current = ""

            for word in clause.split(" "):
                candidate = f"{current} {word}".strip()

                if current and len(candidate) > limit:
                    pieces.append(current)
                    current = word
                else:
                    current = candidate

            if current:
                pieces.append(current)

    chunks = []

    for piece in pieces:
        if chunks and len(chunks[-1]) + 1 + len(piece) <= limit:
            chunks[-1] = f"{chunks[-1]} {piece}"
        else:
            chunks.append(piece)

    return chunks


# An abandoned synthesis keeps running and keeps holding the cache lock.
# Starting a second one behind it just stacks up stuck threads, so a new
# attempt goes straight to the fallback until the first one lets go.
_neural_busy = threading.Event()

# How long a reply waits for Kokoro still reading its voice off the disk.
# A first download is not waited for: the Windows voice speaks meanwhile.
_LOCAL_LOAD_WAIT = 15.0

# Windows' own voice is given this long, plus this much per character, to
# finish (about three times its normal pace), and this long to stop once
# asked. Set once it overruns: it is not used again that run.
_WINDOWS_VOICE_GRACE = 10.0
_WINDOWS_VOICE_PER_CHAR = 0.2
_WINDOWS_VOICE_STOP_GRACE = 2.0
_windows_voice_hung = threading.Event()

# Playback chunk size; smaller means the HUD reacts more finely.
_BLOCK = 1024

# Scales raw RMS up to a usable 0..1 range for the HUD.
_GAIN = 6.0

# Synthesised audio is cached on disk and in memory. JARVIS repeats himself
# constantly ("Done, sir."), and every fresh synthesis is a network round trip.
_CACHE_DIR = os.path.join(tempfile.gettempdir(), "jarvis_tts_cache")
_MEMORY_LIMIT = 48

# The in-memory cache is bounded by _MEMORY_LIMIT, but nothing previously
# bounded the disk cache at all -- every distinct phrase JARVIS has ever
# said (filenames, page titles, prices) accumulates there forever. This
# caps it, oldest-touched evicted first. Generous: at a few KB per short
# phrase, this comfortably holds a normal day's worth of dynamic speech
# without needing to re-synthesise, while still being an actual ceiling.
_DISK_CACHE_LIMIT = 1000

# Set True to print how long synthesis and playback take.
TIMING = False

# Incremented after every completed utterance so the listener can tell that
# JARVIS has spoken, and discard whatever the microphone picked up.
_epoch = 0
_epoch_lock = threading.Lock()

_speaking = threading.Event()

# Set while a real reply is being prepared, so background cache warming
# stands aside rather than making the reply queue behind it.
_priority = threading.Event()

# Pause between warmed phrases, leaving the connection free for real replies.
_PREWARM_GAP = 0.4

# Set by the HUD stop button to cut off the utterance being spoken. Cleared
# when the next one starts, so it never silences anything said later.
_stop = threading.Event()

# Set while playback is held mid-utterance. The audio callback writes
# silence and does not advance, so the device stays open and resuming is
# instant rather than a re-buffer. Only recitation uses it: pausing a
# spoken reply has no meaning, and anything left paused holds the speech
# lock, so stopping always clears it.
_paused = threading.Event()

# Told True when an utterance starts playing and False when it ends, so the
# HUD shows its stop button only while there is something to stop.
_speaking_listener = None


# When the current turn's first sound played (time.monotonic()), or None:
# main.py marks each turn and reads it after, for how long replies take.
_first_sound = None


def mark_turn():
    """A new turn: forget when the last one's first sound played."""
    global _first_sound
    _first_sound = None


def first_sound():
    """When this turn's first sound played, or None if nothing has yet."""
    return _first_sound


def _sounding():
    global _first_sound

    if _first_sound is None:
        _first_sound = time.monotonic()


def speech_epoch():
    """A counter that changes each time JARVIS finishes speaking."""
    with _epoch_lock:
        return _epoch


def is_speaking():
    """True while audio is actually being played."""
    return _speaking.is_set()


def _bump_epoch():
    global _epoch

    with _epoch_lock:
        _epoch += 1


def stop_speaking():
    """Cut off whatever is being said now. True if something was."""
    if not _speaking.is_set():
        return False

    _stop.set()
    _paused.clear()

    return True


def pause_speaking():
    """Hold playback where it is. True if something was playing."""
    if not _speaking.is_set():
        return False

    _paused.set()

    return True


def resume_speaking():
    """Carry on from where pause_speaking stopped."""
    _paused.clear()


def is_paused():
    return _paused.is_set()


def stopped():
    """True when the last utterance was cut off rather than finished."""
    return _stop.is_set()


def set_speaking_listener(listener):
    """Register a callable taking True when speech starts, False when it ends."""
    global _speaking_listener
    _speaking_listener = listener


def _report_speaking(active):
    if _speaking_listener:
        try:
            _speaking_listener(bool(active))
        except Exception:
            pass


class SpeechEngine:
    """Neural text-to-speech with caching; Kokoro, then Windows' SAPI5 voice, stand in when it fails.

    While speaking, the per-block RMS of the audio is reported to an optional
    listener so a UI can pulse in time with the voice.
    """

    def __init__(self, voice=VOICE, engine=ENGINE):
        self.engine = engine

        # What the cache keys phrases by. Azure and Edge make the same Ryan,
        # so a phrase either one has said plays from the cache for both.
        self.voice = voices.voice_id(engine, voice)
        self.voice_name = voice
        self._amplitude_listener = None
        self._sentence_listener = None

        # Reminders fire on their own thread, so utterances must not overlap.
        self._lock = threading.Lock()

        # Separate from _lock on purpose. _lock is held for a whole
        # utterance, including playback; _audio_for is called from
        # inside that (via speak) and also directly from prewarm(),
        # which runs on its own background thread and does NOT hold
        # _lock. Without a lock of its own, prewarm and a real speak()
        # could both mutate _memory (an OrderedDict, not thread-safe
        # for concurrent writes) and the disk cache at the same time --
        # prewarm's own "wait while a real reply is in progress" check
        # is a look-then-act race, not a guarantee, so this is a real
        # gap, not a theoretical one.
        self._cache_lock = threading.Lock()

        # Decoded audio keyed by phrase, most recently used last.
        self._memory = OrderedDict()

        try:
            os.makedirs(_CACHE_DIR, exist_ok=True)
        except OSError:
            pass

    def set_engine(self, engine):
        """Speak with [engine] from the next sentence on ("use the Google voice"). Waits for one being said."""
        global _neural_failed_at

        with self._lock:
            self.engine = engine
            self.voice = voices.voice_id(engine, self.voice_name)
            _neural_failed_at = 0.0

        if engine in voices.LOCAL:
            voices.local().load_in_background()

    def set_amplitude_listener(self, listener):
        """Register a callable taking a float 0..1, or None to clear."""
        self._amplitude_listener = listener

    def set_sentence_listener(self, listener):
        """Register a callable receiving the zero-based spoken sentence index."""
        self._sentence_listener = listener

    def _report_sentence(self, index):
        if self._sentence_listener:
            try:
                self._sentence_listener(int(index))
            except Exception:
                pass

    def _report(self, value):
        if self._amplitude_listener:
            try:
                self._amplitude_listener(float(value))
            except Exception:
                pass

    def speak(self, text):
        # Whatever says it, the voice never reads out "asterisk asterisk".
        text = _plain(text)

        print(f"JARVIS: {text}", flush=True)

        if not text or not text.strip():
            return

        _priority.set()

        with self._lock:
            _stop.clear()
            _paused.clear()
            _speaking.set()
            _report_speaking(True)

            # Kokoro still reading itself off the disk at start-up: the
            # greeting waits a second or two for it rather than going to
            # the Windows voice.
            if self.engine in voices.LOCAL:
                voices.local().wait(_LOCAL_LOAD_WAIT)

            global _neural_failed_at

            try:
                if _neural_busy.is_set():
                    # A stuck synthesis is still holding the cache lock, so
                    # even a cached phrase would block behind it.
                    self._speak_fallback(text)

                elif (
                    time.monotonic() - _neural_failed_at < _NEURAL_COOLDOWN
                    and not self._is_cached(text)
                ):
                    # The cooldown exists to avoid paying the timeout again.
                    # A cached phrase never touches the network, so there is
                    # nothing to avoid and it keeps the real voice.
                    self._speak_fallback(text)

                else:
                    try:
                        self._speak_neural(text)
                    except Exception as error:
                        # Stopped on purpose is not a voice failure: no
                        # cooldown, and no fallback voice finishing the job.
                        if not _stop.is_set():
                            _neural_failed_at = time.monotonic()
                            print(
                                f"[JARVIS] neural voice unavailable "
                                f"({error}); using fallback."
                            )
                            self._speak_fallback(text)

            finally:
                self._report(0.0)
                _speaking.clear()
                _report_speaking(False)
                _priority.clear()
                _bump_epoch()

    def play_file(self, path):
        """Play an audio file through the same path as synthesised speech.

        Anything that arrives as a recording rather than as text -- a
        recitation, say -- goes through here rather than opening its own
        output stream. That is what keeps the stop button, the speaking
        state and the HUD waveform working: all three come from
        _play_reactive checking _stop and reporting amplitude, and from
        the bookkeeping around it. A second player would have none of
        them, and would talk over JARVIS besides, since the lock held
        here is what serialises everything he says.

        Returns True when it played to the end, and False when it was
        stopped or could not be read.
        """
        if not path or not os.path.exists(path):
            return False

        _priority.set()

        with self._lock:
            _stop.clear()
            _paused.clear()
            _speaking.set()
            _report_speaking(True)

            try:
                data, samplerate = sf.read(path, dtype="float32")

                # Downloaded audio is often stereo; the output stream is
                # opened with one channel.
                if getattr(data, "ndim", 1) > 1:
                    data = data.mean(axis=1)

                # No sentence boundaries: those describe synthesised
                # speech, and a recording has none to report.
                self._play_reactive(data, samplerate, [])

                return not _stop.is_set()

            except Exception as error:
                print(
                    f"[JARVIS] could not play "
                    f"{os.path.basename(path)}: {error}"
                )

                return False

            finally:
                self._report(0.0)
                _speaking.clear()
                _report_speaking(False)
                _priority.clear()
                _bump_epoch()

    def prewarm(self, phrases):
        """Synthesise phrases ahead of time so they play instantly later.

        This runs in the background at startup and must never delay a real
        reply, so it waits whenever JARVIS is actually speaking and pauses
        between phrases to leave the connection free.
        """
        self._prune_disk_cache()

        for phrase in phrases:
            # A real utterance takes priority; wait for it to finish.
            while _priority.is_set():
                time.sleep(0.05)

            try:
                self._audio_for(phrase)
            except Exception as error:
                # One failure says the voice is unreachable; the rest would
                # each say so again, and each pay its timeout.
                print(f"[JARVIS] could not prewarm {phrase!r}: {error}; "
                      f"the rest are made as they are first said.")
                return

            # Leave a gap so a command arriving now is not stuck behind a
            # run of back-to-back requests.
            time.sleep(_PREWARM_GAP)

    def _prune_disk_cache(self):
        """Evict the least-recently-used disk cache entries over the cap.

        Runs once per process start rather than per call -- a disk scan
        on every utterance would undercut the entire point of caching.
        Never touches the in-memory cache; an evicted phrase still in
        _memory simply re-synthesises to disk next time it's needed.
        """
        with self._cache_lock:
            try:
                entries = [
                    name for name in os.listdir(_CACHE_DIR)
                    if name.endswith(".mp3")
                ]
            except OSError:
                return

            if len(entries) <= _DISK_CACHE_LIMIT:
                return

            def mtime(name):
                try:
                    return os.path.getmtime(os.path.join(_CACHE_DIR, name))
                except OSError:
                    return 0

            entries.sort(key=mtime)
            overflow = len(entries) - _DISK_CACHE_LIMIT

            for name in entries[:overflow]:
                stem = name[:-4]

                for suffix in (".mp3", ".txt", ".json"):
                    try:
                        os.remove(os.path.join(_CACHE_DIR, f"{stem}{suffix}"))
                    except OSError:
                        pass

            print(f"[JARVIS] pruned {overflow} old cached phrases")

    def invalidate(self, text):
        """Remove one phrase from both the memory and disk cache.

        For fixing a specific bad cache entry -- a wrong pronunciation,
        say -- without needing to know its hash or clear the whole
        cache. Returns True if anything was actually found and removed.
        """
        key = self._cache_key(text)
        path = os.path.join(_CACHE_DIR, f"{key}.mp3")
        sidecar = os.path.join(_CACHE_DIR, f"{key}.txt")
        metadata = os.path.join(_CACHE_DIR, f"{key}.json")

        with self._cache_lock:
            removed = self._memory.pop(key, None) is not None

            for target in (path, sidecar, metadata):
                try:
                    os.remove(target)
                    removed = True
                except OSError:
                    pass

        return removed

    def _cache_key(self, text):
        digest = hashlib.sha1(
            f"{self.voice}|{VOICE_RATE}|{VOICE_PITCH}|{text}".encode("utf-8")
        ).hexdigest()

        return digest

    def _load_boundaries(self, path):
        """Load cached SentenceBoundary timings, or return None."""
        try:
            with open(path, "r", encoding="utf-8") as handle:
                entries = json.load(handle)
        except (OSError, ValueError, TypeError):
            return None

        if not isinstance(entries, list):
            return None

        boundaries = []

        for entry in entries:
            if not isinstance(entry, dict):
                return None

            try:
                offset = float(entry["offset"])
                duration = float(entry["duration"])
            except (KeyError, TypeError, ValueError):
                return None

            if offset < 0 or duration < 0:
                return None

            boundaries.append({
                "offset": offset,
                "duration": duration,
                "text": str(entry.get("text") or ""),
            })

        if not boundaries:
            return None

        return boundaries

    def _audio_for(self, text):
        """Return (samples, samplerate, sentence boundaries)."""
        key = self._cache_key(text)
        path = os.path.join(_CACHE_DIR, f"{key}.mp3")
        sidecar = os.path.join(_CACHE_DIR, f"{key}.txt")
        metadata = os.path.join(_CACHE_DIR, f"{key}.json")

        with self._cache_lock:
            cached = self._memory.get(key)

            if cached is not None:
                if len(cached) == 3 and cached[2]:
                    self._memory.move_to_end(key)
                    self._touch(path)

                    return cached

                # An older in-memory entry may have audio but no usable
                # sentence timing metadata. Discard it so the disk metadata
                # can be validated and, if necessary, regenerated.
                self._memory.pop(key, None)

            started = time.monotonic()
            synthesised = False

            boundaries = self._load_boundaries(metadata)

            # Old cache entries pre-date sentence timing metadata. Rebuild them
            # once so playback and the HUD always share authoritative timings.
            if not os.path.exists(path) or boundaries is None:
                self._synthesize_to_cache(text, path, sidecar)
                boundaries = self._load_boundaries(metadata) or []
                synthesised = True

            data, samplerate = self._read_cached(text, path, sidecar)

            if not synthesised:
                self._touch(path)

            result = (data, samplerate, boundaries)

            self._memory[key] = result
            self._memory.move_to_end(key)

            while len(self._memory) > _MEMORY_LIMIT:
                self._memory.popitem(last=False)

            if TIMING:
                source = "synthesised" if synthesised else "disk cache"

                print(
                    f"[timing] {source} in "
                    f"{time.monotonic() - started:.2f}s: "
                    f"{text[:40]!r}",
                    flush=True,
                )

            return result

    def audio_bytes(self, text):
        """The synthesised MP3 for some text, without playing it here.

        For the phone: someone in another room wants the reply on the
        device they asked from, not announced to an empty desk. Reuses
        the same disk cache speak() fills, so anything JARVIS has
        already said is served with no synthesis at all -- and anything
        new is cached for the next time it's said out loud.
        """
        text = _plain(text)

        if not text or not text.strip():
            return None

        key = self._cache_key(text)
        path = os.path.join(_CACHE_DIR, f"{key}.mp3")
        sidecar = os.path.join(_CACHE_DIR, f"{key}.txt")

        with self._cache_lock:
            if not os.path.exists(path):
                try:
                    self._synthesize_to_cache(text, path, sidecar)
                except Exception as error:
                    print(
                        f"[JARVIS] could not synthesise for the phone: {error}")
                    return None
            else:
                self._touch(path)

            try:
                with open(path, "rb") as handle:
                    return handle.read()
            except OSError as error:
                print(f"[JARVIS] could not read cached audio: {error}")
                return None

    def pcm16(self, text, rate=16000, peak=None):
        """Some text spoken as raw 16-bit mono PCM at [rate], without playing it here.

        For a board with a speaker of its own and no MP3 decoder (the toy cam): the same voice
        and the same disk cache as speak(), only decoded and resampled on this side. None when
        there is nothing to say or the voice could not be made.
        """
        text = _plain(text)

        if not text or not text.strip():
            return None

        try:
            samples, samplerate, _boundaries = self._audio_for(text)
        except Exception as error:
            print(f"[JARVIS] could not synthesise for the toy: {error}")
            return None

        return to_pcm16(samples, samplerate, rate, peak)

    @staticmethod
    def _touch(path):
        """Mark a cache file as just used, for mtime-based LRU eviction.

        Uses mtime rather than atime deliberately: NTFS access-time
        updates are commonly disabled system-wide on Windows for
        performance, which would make atime silently unreliable here.
        """
        try:
            os.utime(path, None)
        except OSError:
            pass

    def _synthesize_to_cache(self, text, path, sidecar):
        """Synthesize one continuous utterance and cache sentence timings."""
        partial = f"{path}.partial"
        metadata = f"{os.path.splitext(path)[0]}.json"
        metadata_partial = f"{metadata}.partial"
        sidecar_partial = f"{sidecar}.partial"

        boundaries = []

        try:
            with open(partial, "wb") as audio:
                if self.engine == "edge":
                    self._edge_to(audio, boundaries, text)
                else:
                    # Azure and Kokoro hand back the whole MP3 and its sentences.
                    data, timings = voices.mp3(
                        self.engine, text, self.voice_name, VOICE_RATE, VOICE_PITCH)
                    audio.write(data)
                    boundaries.extend(timings)

            os.replace(partial, path)

            with open(metadata_partial, "w", encoding="utf-8") as handle:
                json.dump(boundaries, handle)

            os.replace(metadata_partial, metadata)

            with open(sidecar_partial, "w", encoding="utf-8") as handle:
                handle.write(text)

            os.replace(sidecar_partial, sidecar)

        except Exception:
            for target in (
                partial,
                metadata_partial,
                sidecar_partial,
            ):
                try:
                    os.remove(target)
                except OSError:
                    pass

            raise

    def _edge_to(self, audio, boundaries, text):
        """Stream Edge's MP3 for [text] into [audio], its sentences into [boundaries]."""
        options = {
            "boundary": "SentenceBoundary",
        }

        if VOICE_RATE:
            options["rate"] = VOICE_RATE

        if VOICE_PITCH:
            options["pitch"] = VOICE_PITCH

        communicate = edge_tts.Communicate(
            text,
            self.voice,
            connect_timeout=_NEURAL_CONNECT_TIMEOUT,
            receive_timeout=_NEURAL_RECEIVE_TIMEOUT,
            **options,
        )

        for chunk in communicate.stream_sync():
            if chunk["type"] == "audio":
                audio.write(chunk["data"])

            elif chunk["type"] == "SentenceBoundary":
                boundaries.append({
                    "offset": float(chunk["offset"]) / 10_000_000.0,
                    "duration": float(chunk["duration"]) / 10_000_000.0,
                    "text": chunk.get("text", ""),
                })

    def _read_cached(self, text, path, sidecar):
        """Read a cached file, recovering once from a corrupted one.

        A file that was written successfully can still go bad later --
        disk error, antivirus rewriting it, manual tampering, ordinary
        bit rot over a cache that's never pruned by age. This should
        never be able to take the whole speech pipeline down: on a read
        failure, the bad file is removed and re-synthesised exactly
        once, not retried indefinitely.
        """
        try:
            data, samplerate = sf.read(path, dtype="float32")
        except Exception as error:
            print(
                f"[JARVIS] cached audio was unreadable, re-synthesising "
                f"({error}): {text[:40]!r}"
            )

            for bad in (path, sidecar):
                try:
                    os.remove(bad)
                except OSError:
                    pass

            self._synthesize_to_cache(text, path, sidecar)
            data, samplerate = sf.read(path, dtype="float32")

        if data.ndim > 1:
            data = data.mean(axis=1)

        return data, samplerate

    def _is_cached(self, text):
        """True when this phrase can be played without the network.

        Deliberately does not take _cache_lock. A stuck synthesis may be
        holding it, and this is only a hint used to decide whether an
        attempt is worth making -- blocking here would defeat the point.
        """
        key = self._cache_key(text)
        cached = self._memory.get(key)

        if cached is not None and len(cached) == 3 and cached[2]:
            return True

        return (
            os.path.exists(os.path.join(_CACHE_DIR, f"{key}.mp3"))
            and os.path.exists(os.path.join(_CACHE_DIR, f"{key}.json"))
        )

    def _audio_for_bounded(self, text, timeout):
        """Fetch audio, abandoning the attempt if the network stalls."""
        if _neural_busy.is_set():
            raise RuntimeError(
                "a previous synthesis is still holding the cache"
            )

        outcome = {}
        done = threading.Event()

        def work():
            try:
                outcome["audio"] = self._audio_for(text)
            except BaseException as error:
                outcome["error"] = error
            finally:
                _neural_busy.clear()
                done.set()

        _neural_busy.set()
        threading.Thread(target=work, daemon=True).start()

        # Polled rather than one long wait, so a stop pressed while the
        # audio is still being made returns at once.
        deadline = time.monotonic() + timeout

        while not done.wait(0.05):
            if _stop.is_set():
                raise RuntimeError("speech stopped")

            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"synthesis did not finish within {timeout}s"
                )

        if "error" in outcome:
            raise outcome["error"]

        return outcome["audio"]

    def _warm_quietly(self, text):
        """Synthesise a chunk into the cache while the previous one plays.

        Failures are swallowed on purpose: the main thread asks for this
        chunk next and will handle a failure there, with a deadline and a
        fallback. Reporting it twice would be noise.
        """
        try:
            self._audio_for(text)
        except Exception:
            pass

    def _deadline(self):
        """How long one piece of a reply may take to make: short for a network, long for this PC."""
        return _LOCAL_SYNTHESIS_DEADLINE if self.engine in voices.LOCAL else _NEURAL_SYNTHESIS_DEADLINE

    def _speak_neural(self, text):
        # A reply already in the cache plays whole, at once; anything else
        # starts with its first sentence (_speakable_chunks).
        chunks = [text] if self._is_cached(text) else _speakable_chunks(text)

        if len(chunks) <= 1:
            data, samplerate, boundaries = self._audio_for_bounded(
                text,
                self._deadline(),
            )

            if not _stop.is_set():
                self._play_reactive(data, samplerate, boundaries)

            return

        global _neural_failed_at

        spoken_sentences = 0

        for index, chunk in enumerate(chunks):
            if _stop.is_set():
                return

            try:
                data, samplerate, boundaries = self._audio_for_bounded(
                    chunk,
                    self._deadline(),
                )
            except Exception as error:
                if _stop.is_set():
                    return

                # Only what is left goes to the fallback voice. Letting this
                # reach speak() would replay the whole answer from the top
                # in the other voice.
                _neural_failed_at = time.monotonic()

                print(
                    f"[JARVIS] neural voice unavailable ({error}); "
                    f"finishing in the fallback voice."
                )

                self._speak_fallback(" ".join(chunks[index:]))

                return

            # Start the next chunk now so its synthesis overlaps this
            # chunk's playback. Only the first chunk is ever waited for.
            if index + 1 < len(chunks):
                threading.Thread(
                    target=self._warm_quietly,
                    args=(chunks[index + 1],),
                    daemon=True,
                ).start()

            self._play_reactive(
                data,
                samplerate,
                boundaries,
                offset=spoken_sentences,
            )

            # The HUD counts sentences across the whole reply, not within
            # one chunk, so each chunk's indices continue from the last. A
            # chunk that is a sentence's first clause has not finished that
            # sentence: the next chunk carries on in it.
            finished = len(list(boundaries or ()))

            if finished and not chunk.rstrip().endswith((".", "!", "?")):
                finished -= 1

            spoken_sentences += finished

    def _play_reactive(self, data, samplerate, boundaries, offset=0):
        """Play continuously and advance the HUD at sentence boundaries.

        offset is how many sentences of this reply have already been
        spoken in earlier chunks, so the HUD keeps counting up instead of
        jumping back to the first sentence at every join.
        """
        _sounding()
        position = 0
        total = len(data)
        next_boundary = 0

        boundaries = list(boundaries or ())
        boundary_queue = queue.SimpleQueue()

        def queue_boundaries(seconds):
            nonlocal next_boundary

            while (
                next_boundary < len(boundaries)
                and boundaries[next_boundary]["offset"] <= seconds
            ):
                boundary_queue.put(next_boundary)
                next_boundary += 1

        def report_queued_boundaries():
            while True:
                try:
                    index = boundary_queue.get_nowait()
                except queue.Empty:
                    return

                self._report_sentence(offset + index)

        # The first sentence normally starts at zero.
        queue_boundaries(0.0)

        def callback(outdata, frames, time_info, status):
            nonlocal position

            if status:
                print(status)

            if _stop.is_set():
                outdata.fill(0)
                raise sd.CallbackStop

            # Checked after the stop, so a stop pressed while paused
            # still ends the utterance rather than being swallowed.
            if _paused.is_set():
                outdata.fill(0)
                return

            end = position + frames
            chunk = data[position:end]

            if len(chunk) < frames:
                outdata[:len(chunk), 0] = chunk
                outdata[len(chunk):, 0] = 0.0
            else:
                outdata[:, 0] = chunk

            if len(chunk):
                rms = float(
                    np.sqrt(
                        np.mean(
                            np.square(chunk)
                        )
                    )
                )

                self._report(
                    min(
                        1.0,
                        rms * _GAIN,
                    )
                )

            position = end

            # The audio callback only queues the boundary. GUI signalling
            # stays on the normal playback thread.
            queue_boundaries(position / samplerate)

            if position >= total:
                raise sd.CallbackStop

        opening = time.monotonic()

        stream = sd.OutputStream(
            samplerate=samplerate,
            channels=1,
            blocksize=_BLOCK,
            dtype="float32",
            callback=callback,
        )

        with stream:
            if TIMING:
                print(
                    f"[timing] audio device opened in "
                    f"{time.monotonic() - opening:.2f}s, "
                    f"playing "
                    f"{total / samplerate:.2f}s of speech",
                    flush=True,
                )

            playing = time.monotonic()

            while stream.active:
                report_queued_boundaries()
                sd.sleep(20)

            # Drain anything queued by the final audio callback.
            report_queued_boundaries()

            if TIMING:
                print(
                    f"[timing] playback took "
                    f"{time.monotonic() - playing:.2f}s"
                )

    def _speak_fallback(self, text):
        """Say [text] when the chosen voice cannot: Kokoro if it is loaded, Windows' own voice if not."""
        if _stop.is_set():
            return

        # Kokoro reading itself off the disk takes a second or two: worth
        # waiting for rather than starting the reply in another voice.
        stand_in = voices.local()

        if self.engine not in voices.LOCAL:
            # Loaded the first time it is needed, not at start-up: its import freezes the HUD for a second or
            # two, which is worth paying only when the network voice has actually failed.
            stand_in.load_in_background()
            stand_in.wait(_LOCAL_LOAD_WAIT)

        if self.engine not in voices.LOCAL and stand_in.ready():
            try:
                data, samplerate, boundaries = stand_in.audio(text)
            except Exception as error:
                print(f"[JARVIS] the Kokoro voice failed too ({error}); "
                      f"using the Windows voice.")
            else:
                if not _stop.is_set():
                    self._play_reactive(data, samplerate, boundaries)

                return

        self._speak_windows(text)

    def _speak_windows(self, text):
        """Windows' own SAPI voice: the last resort, given a deadline so it can never hold the speech lock.

        On 12 October 2026 the first sentence in it was said and the second
        never finished: stuck on SPEAKING, every later reply queued behind
        it. pyttsx3 keeps one engine per process and hands it to whichever
        thread speaks next, but a COM object belongs to the thread that made
        it -- the likeliest reason the second sentence, from another thread,
        never came back. pyttsx3 is gone: each sentence gets its own SAPI
        voice on its own thread, set up for COM there. The deadline stays
        for anything else that hangs: past it the voice is left behind and
        not used again that run.
        """
        if _windows_voice_hung.is_set():
            print(f"[JARVIS] no voice to say it with: {text[:60]!r}")
            return

        _sounding()

        done = threading.Event()

        def work():
            com = None

            try:
                # SAPI is COM, and COM wants each thread that uses it set up.
                try:
                    import pythoncom

                    pythoncom.CoInitialize()
                    com = pythoncom
                except ImportError:
                    pass

                voice = _sapi_voice()
                voice.Speak(text, _SVSF_ASYNC)

                while not voice.WaitUntilDone(50):
                    if _stop.is_set():
                        voice.Speak("", _SVSF_ASYNC | _SVSF_PURGE)
                        break

            except Exception as error:
                print(f"[JARVIS] the Windows voice failed: {error}")

            finally:
                if com is not None:
                    try:
                        com.CoUninitialize()
                    except Exception:
                        pass

                done.set()

        threading.Thread(target=work, daemon=True).start()

        deadline = time.monotonic() + _WINDOWS_VOICE_GRACE + _WINDOWS_VOICE_PER_CHAR * len(text)
        stopped_at = None

        while not done.wait(0.05):
            now = time.monotonic()

            if _stop.is_set():
                stopped_at = stopped_at or now

                # It stops at once; one that does not is not waited on.
                if now - stopped_at >= _WINDOWS_VOICE_STOP_GRACE:
                    return

            if now >= deadline:
                _windows_voice_hung.set()
                print("[JARVIS] the Windows voice stopped responding: it is left off "
                      "for the rest of this run, so JARVIS is not stuck speaking.")
                return


# SAPI's SpeakFlags: return at once, and drop whatever is still being said.
_SVSF_ASYNC = 1
_SVSF_PURGE = 2

WINDOWS_VOICE_ENV = "JARVIS_WINDOWS_VOICE"


def _language_code(voice=VOICE):
    """SAPI's Language attribute for [voice]'s language ("en-GB-RyanNeural" -> "809"), or None."""
    import locale

    wanted = "_".join(str(voice).split("-")[:2])

    for code, name in locale.windows_locale.items():
        if name.casefold() == wanted.casefold():
            return f"{code:x}"

    return None


def _choose_sapi_voice(voice, wanted=None, language=None):
    """The SAPI voice token to speak with, or None for Windows' default.

    [wanted] (JARVIS_WINDOWS_VOICE) is matched against the installed
    voices' names ("George"); without it, a man's voice in JARVIS's
    language is asked of SAPI, then any man's voice.
    """
    if wanted:
        tokens = voice.GetVoices()

        for index in range(tokens.Count):
            token = tokens.Item(index)

            if wanted.casefold() in str(token.GetDescription()).casefold():
                return token

        return None

    queries = ([f"Gender=Male;Language={language}"] if language else []) + ["Gender=Male"]

    for query in queries:
        found = voice.GetVoices(query)

        if found.Count:
            return found.Item(0)

    return None


def _sapi_voice():
    """A new SAPI voice for this thread, speaking as a man in JARVIS's language when Windows has one."""
    import win32com.client

    voice = win32com.client.Dispatch("SAPI.SpVoice")

    try:
        token = _choose_sapi_voice(voice, (os.getenv(WINDOWS_VOICE_ENV) or "").strip(), _language_code())

        if token is not None:
            voice.Voice = token
    except Exception as error:
        print(f"[JARVIS] could not choose a Windows voice ({error}); using its default.")

    return voice


speech = SpeechEngine()


# Phrases JARVIS repeats constantly. Synthesised once at startup, then instant.
COMMON_PHRASES = (
    "Yes, sir?",
    "Done, sir.",
    "Certainly, sir.",
    "I don't know how to do that yet.",
    "I couldn't find that out, sir.",
    "I couldn't open that, sir.",
    "I couldn't close that, sir.",
    "That didn't work, sir.",
    "Muting, sir.",
    "Unmuting, sir.",
    "Turning it up, sir.",
    "Turning it down, sir.",
    "Clearing the desktop, sir.",
    "Bringing them back, sir.",
    "That file exists. Overwrite it, sir?",
    "Very good, sir.",
    "Cancelled, sir.",
    "Noted, sir.",
    "Copied, sir.",
    "Added, sir.",
)


def _plain(text):
    """Text without a model's formatting marks (phrases.plain), for the voice."""
    import phrases

    return phrases.plain(text)


def speak(text):
    speech.speak(text)


def switch_voice(engine):
    """Change the voice now, and keep the choice for next time."""
    voices.choose(engine)
    speech.set_engine(engine)


def current_engine():
    return speech.engine


def play_file(path):
    """Play a recording. True if it finished, False if stopped."""
    return speech.play_file(path)


def audio_bytes(text):
    """Synthesised MP3 for text, without playing it on this machine."""
    return speech.audio_bytes(text)


# The most a quiet recording is raised to reach the peak asked for (to_pcm16): a whisper is not
# turned into a shout, nor near-silence into hiss.
_MOST_GAIN = 4.0


def to_pcm16(samples, samplerate, rate=16000, peak=None):
    """Float samples (-1 to 1, mono or not) as little-endian 16-bit mono PCM at [rate].

    Going down in rate, each output sample is first averaged over the input samples it stands
    for, so the voice's highest notes fold back as a faint hiss at most rather than as chirps;
    then read off by straight-line interpolation. numpy only. [peak], when given (0 to 1): the
    whole raised or lowered so its loudest moment reaches that, raised at most _MOST_GAIN times.
    """
    data = np.asarray(samples, dtype=np.float32)

    if data.ndim > 1:
        data = data.mean(axis=1)

    if not len(data):
        return b""

    if samplerate != rate:
        if samplerate > rate:
            width = max(1, int(round(samplerate / rate)))

            if width > 1:
                data = np.convolve(data, np.full(width, 1.0 / width, dtype=np.float32), mode="same")

        count = max(1, int(round(len(data) * rate / samplerate)))
        positions = np.arange(count, dtype=np.float64) * (samplerate / rate)
        data = np.interp(positions, np.arange(len(data), dtype=np.float64), data).astype(np.float32)

    if peak is not None:
        loudest = float(np.max(np.abs(data)))

        if loudest > 0.0:
            data = data * min(peak / loudest, _MOST_GAIN)

    return (np.clip(data, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def pcm16(text, rate=16000, peak=None):
    """Text spoken as raw 16-bit mono PCM at [rate], for a board's own speaker; [peak] as to_pcm16."""
    return speech.pcm16(text, rate, peak)


def invalidate(text):
    """Remove one phrase from the cache -- for fixing a bad entry (a
    wrong pronunciation, say) without clearing the whole cache."""
    return speech.invalidate(text)


def set_amplitude_listener(listener):
    speech.set_amplitude_listener(listener)


def set_sentence_listener(listener):
    speech.set_sentence_listener(listener)


def start_voice():
    """At start-up, say which voice speaks, and load it now only when it is the one that speaks.

    The local voice's load imports a model that holds Python's lock for a second or two; done at boot it
    froze the HUD mid boot animation (14 October 2026). So it is loaded here only when it is the chosen
    voice -- the greeting waits for it, as before, and a greeting that found it unloaded went to the Windows
    voice. When a network voice (Google, Azure, Edge) speaks, the local voice is only its stand-in, so it is
    loaded the first time it is needed (_speak_fallback): while the network voice works, never.
    """
    print(f"[JARVIS] voice: {voices.describe(speech.engine, speech.voice_name)}", flush=True)

    if speech.engine in voices.LOCAL:
        voices.local().load_in_background()


def prewarm(lines=None):
    """Warm the cache so the most common replies never wait on the network."""
    if lines is None:
        lines = list(COMMON_PHRASES)

        # Every wording variant, so no phrasing is slow the first time.
        try:
            import phrases as phrasebook

            lines.extend(phrasebook.every_fixed_line())
        except Exception as error:
            print(f"[JARVIS] could not list phrase variants: {error}")

    # When the local voice is the one that speaks, the phrases are made in it, so it is loaded first. When a
    # network voice speaks, the phrases are made in that, and its stand-in loads only when first needed.
    if speech.engine in voices.LOCAL:
        voices.local().load()

    speech.prewarm(tuple(dict.fromkeys(lines)))
