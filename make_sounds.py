#!/usr/bin/env python3
"""Synthesizes the Quest Log card sounds into sounds/*.wav (original sounds, no game audio).

The palette aims at the feel of a Sheikah-tech tablet menu: glassy bell partials, a soft air sweep
for opening/closing, and very short high blips for the cursor.

  open    card appears: air sweep + low thum + rising A-major bell arpeggio + shimmer
  move    cursor moves to another quest (hover, ↑/↓)
  scroll  the quest list scrolls by about one row
  select  confirm: open a quest or the board
  done    quest ticked: ascending D-major chime
  undo    a change is undone: two falling notes
  cancel  a menu or detail view is closed without a choice
  track   a quest is tracked: rising lock-on blips
  untrack tracking removed: falling blip
  close   card closes: falling sweep + descending blips

Run: python3 make_sounds.py   (pure Python, no dependencies)
"""
import math
import os
import random
import struct
import wave

SR = 44100
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "sounds")
random.seed(7)

BELL = [(1.0, 1.0), (2.0, 0.22), (3.01, 0.10), (4.17, 0.05), (5.43, 0.02)]
BLIP = [(1.0, 1.0), (2.0, 0.12)]


def silence(seconds):
    return [0.0] * int(SR * seconds)


def tone(buf, t0, freq, dur, amp, attack=0.003, tau=0.25, partials=BELL, glide_to=None, detune=0.0):
    """Adds a decaying tone; glide_to bends the pitch over the first 40 ms."""
    start = int(t0 * SR)
    n = min(int(dur * SR), len(buf) - start)
    phases = [0.0] * len(partials)
    phases2 = [0.0] * len(partials)
    for i in range(n):
        t = i / SR
        f = freq
        if glide_to is not None:
            k = min(1.0, t / 0.04)
            f = freq + (glide_to - freq) * k
        env = (t / attack if t < attack else 1.0) * math.exp(-max(0.0, t - attack) / tau)
        s = 0.0
        for j, (ratio, a) in enumerate(partials):
            # higher partials die faster, like a struck glass bar
            pe = math.exp(-t * (ratio - 1.0) * 6.0)
            phases[j] += 2 * math.pi * f * ratio / SR
            s += a * pe * math.sin(phases[j])
            if detune:
                phases2[j] += 2 * math.pi * (f + detune) * ratio / SR
                s += a * pe * 0.5 * math.sin(phases2[j])
        buf[start + i] += amp * env * s


def sweep(buf, t0, dur, amp, f0, f1, q=2.5):
    """Band-passed noise whose centre moves from f0 to f1 (state-variable filter), rise-fall envelope."""
    start = int(t0 * SR)
    n = min(int(dur * SR), len(buf) - start)
    low = band = 0.0
    for i in range(n):
        x = random.uniform(-1, 1)
        k = i / max(1, n - 1)
        fc = f0 * (f1 / f0) ** k
        f = 2 * math.sin(math.pi * min(fc, SR / 6) / SR)
        low += f * band
        high = x - low - band / q
        band += f * high
        env = math.sin(math.pi * k) ** 1.5
        buf[start + i] += amp * env * band


def click(buf, t0, amp, dur=0.003):
    start = int(t0 * SR)
    prev = 0.0
    for i in range(int(dur * SR)):
        x = random.uniform(-1, 1)
        y = x - prev   # crude high-pass
        prev = x
        buf[start + i] += amp * y * (1 - i / (dur * SR))


def reverb(buf, mix=0.25, room=0.78):
    """Small Schroeder reverb (4 combs + 2 all-passes)."""
    out = [0.0] * len(buf)
    for d in (1116, 1188, 1277, 1356):
        line = [0.0] * d
        idx = 0
        filt = 0.0
        for i, x in enumerate(buf):
            y = line[idx]
            filt = y * 0.6 + filt * 0.4
            line[idx] = x + filt * room
            idx = (idx + 1) % d
            out[i] += y * 0.25
    for d in (556, 441):
        line = [0.0] * d
        idx = 0
        for i in range(len(out)):
            b = line[idx]
            y = -out[i] + b
            line[idx] = out[i] + b * 0.5
            idx = (idx + 1) % d
            out[i] = y
    return [x * (1 - mix) + y * mix for x, y in zip(buf, out)]


def finish(buf, name, peak):
    m = max(1e-9, max(abs(x) for x in buf))
    fade = int(0.02 * SR)
    data = bytearray()
    for i, x in enumerate(buf):
        g = peak / m
        if i > len(buf) - fade:
            g *= (len(buf) - i) / fade
        data += struct.pack("<h", int(max(-1, min(1, x * g)) * 32767))
    os.makedirs(OUT, exist_ok=True)
    with wave.open(os.path.join(OUT, name + ".wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(bytes(data))
    print(f"{name}.wav  {len(buf) / SR:.2f}s")


def make_open():
    b = silence(1.3)
    sweep(b, 0.0, 0.32, 0.20, 700, 6500)
    tone(b, 0.0, 196, 0.5, 0.30, attack=0.01, tau=0.16, partials=[(1, 1), (2, 0.3)], glide_to=294)
    for t, f, a in ((0.05, 880.0, 0.34), (0.11, 1318.5, 0.30), (0.17, 1760.0, 0.27), (0.24, 2217.5, 0.22)):
        tone(b, t, f, 1.0, a, tau=0.30, detune=1.8)
    tone(b, 0.22, 3520, 0.8, 0.035, attack=0.08, tau=0.25, partials=[(1, 1)], detune=14)
    finish(reverb(b, 0.32), "open", 0.82)


def make_move():
    b = silence(0.09)
    tone(b, 0.0, 2093, 0.09, 0.6, attack=0.0008, tau=0.018, partials=BLIP, glide_to=1975.5)
    click(b, 0.0, 0.08)
    finish(b, "move", 0.42)


def make_scroll():
    b = silence(0.06)
    tone(b, 0.0, 1480, 0.06, 0.6, attack=0.0008, tau=0.011, partials=BLIP)
    finish(b, "scroll", 0.26)


def make_select():
    b = silence(0.32)
    tone(b, 0.0, 1318.5, 0.3, 0.5, tau=0.06, partials=BELL[:3])
    tone(b, 0.045, 1975.5, 0.3, 0.55, tau=0.09, partials=BELL[:3])
    finish(reverb(b, 0.18), "select", 0.55)


def make_done():
    b = silence(1.1)
    tone(b, 0.0, 293.7, 0.4, 0.22, attack=0.006, tau=0.12, partials=[(1, 1), (2, 0.4)])
    for t, f, a in ((0.0, 1174.7, 0.32), (0.055, 1480.0, 0.30), (0.11, 1760.0, 0.28), (0.17, 2349.3, 0.26)):
        tone(b, t, f, 1.0, a, tau=0.34, detune=1.5)
    finish(reverb(b, 0.34), "done", 0.62)


def make_undo():
    b = silence(0.24)
    tone(b, 0.0, 1480.0, 0.2, 0.5, tau=0.045, partials=BELL[:3])
    tone(b, 0.06, 1108.7, 0.2, 0.5, tau=0.06, partials=BELL[:3])
    finish(reverb(b, 0.12), "undo", 0.42)


def make_cancel():
    b = silence(0.22)
    tone(b, 0.0, 1318.5, 0.2, 0.45, tau=0.04, partials=BELL[:2])
    tone(b, 0.05, 987.8, 0.2, 0.4, tau=0.05, partials=BELL[:2])
    finish(reverb(b, 0.1), "cancel", 0.38)


def make_track():
    b = silence(0.6)
    for t, f in ((0.0, 1046.5), (0.07, 1568.0), (0.14, 2093.0)):
        tone(b, t, f, 0.5, 0.36, tau=0.12, partials=BELL[:3], detune=1.2)
    finish(reverb(b, 0.25), "track", 0.5)


def make_untrack():
    b = silence(0.3)
    tone(b, 0.0, 1568.0, 0.25, 0.45, tau=0.05, partials=BELL[:2])
    tone(b, 0.06, 1046.5, 0.25, 0.4, tau=0.06, partials=BELL[:2])
    finish(reverb(b, 0.12), "untrack", 0.38)


def make_close():
    b = silence(0.55)
    sweep(b, 0.0, 0.24, 0.16, 5200, 800)
    for t, f in ((0.0, 1760.0), (0.045, 1318.5), (0.09, 880.0)):
        tone(b, t, f, 0.4, 0.32, tau=0.05, partials=BELL[:3])
    finish(reverb(b, 0.2), "close", 0.45)


if __name__ == "__main__":
    for make in (make_open, make_move, make_scroll, make_select, make_done, make_undo, make_cancel, make_track, make_untrack, make_close):
        make()
