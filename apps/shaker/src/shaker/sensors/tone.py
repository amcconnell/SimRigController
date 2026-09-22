"""Narrowband detection of a single drive tone in accelerometer samples.

The crosstalk measurement reads broadband RMS over a window, which works only
while the shaker is much louder than everything else. It stopped working: with
the seat on isolation mounts the front pod delivers 0.146 g against a 0.056 g
ambient floor, and the SNR guard — rightly — refuses to report a ratio built on
that. Turning the drive up is not the answer, because the interesting
measurements are the quiet ones.

The fix is to stop measuring everything. We know exactly what frequency was
played, so the detector only has to look there. This is a lock-in amplifier:
multiply the samples by a cosine and a sine at the drive frequency, average
each over the window, and the in-phase and quadrature means recover the tone's
amplitude and phase while anything at another frequency averages towards zero.
The effective bandwidth is about 1/T — roughly 1.2 Hz for a 0.8 s window
against a 390 Hz Nyquist — so broadband ambient is suppressed by something like
15-20 dB before the ratio is ever computed.

That narrowness is also the catch. The pods run from their own oscillator at a
nominal 800 Hz and actually deliver 779 and 789, and the figure drifts with
temperature. A 1 percent error in the assumed sample rate makes the demodulated
phase rotate through a full turn during the window at 120 Hz, which averages
the tone away to nothing — the detector would report silence from a shaker that
was working perfectly. So rather than trusting a single frequency, the tone is
sought across a small span around it and the strongest answer wins. The search
is over the *assumed* frequency, which absorbs sample-rate error, pitch error
and the pod's clock drift together without having to attribute the discrepancy
to any one of them.
"""

from __future__ import annotations

import numpy as np

# Half-width of the frequency search, as a fraction of the target. Covers the
# ~2.5% spread already observed between the two pods' actual sample rates with
# room for drift, and stays far narrower than the spacing between sweep points.
SEARCH_FRAC = 0.04

# Trial frequencies across that span. Spaced to land within a fraction of the
# ~1/T detector bandwidth of the true tone, so the peak is never missed between
# two trials.
SEARCH_STEPS = 21

# Below this many samples the window is too short for the average to mean
# anything and the result would be dominated by whatever phase it started at.
_MIN_SAMPLES = 64


def detect(
    x: list[float] | np.ndarray,
    y: list[float] | np.ndarray,
    z: list[float] | np.ndarray,
    rate_hz: float,
    freq_hz: float,
    search_frac: float = SEARCH_FRAC,
    steps: int = SEARCH_STEPS,
) -> tuple[float, float]:
    """Return (RMS g at the drive frequency, the frequency it was found at).

    Reported as RMS rather than peak so it sits on the same scale as every
    other vibration figure in the app — a reading here is directly comparable
    with a crosstalk number or a live meter, which it would not be if one were
    peak and the others RMS.

    The three axes are combined in power, giving the magnitude of the vector
    motion at that frequency. That keeps the answer independent of how a pod
    happens to be bolted in, which matters because the two are mounted in
    different orientations under different parts of the rig.
    """
    n = min(len(x), len(y), len(z))
    if n < _MIN_SAMPLES or rate_hz <= 0.0 or freq_hz <= 0.0:
        return (0.0, freq_hz)
    # Above Nyquist the trial cosine is an alias of something else entirely and
    # a confident number could be reported for a tone that was never sampled.
    if freq_hz * (1.0 + search_frac) >= 0.5 * rate_hz:
        return (0.0, freq_hz)

    # Hann rather than a plain average. The ambient floor at a rig is not
    # white — it is dominated by whatever is happening below 10 Hz — and a
    # rectangular window's sidelobes decay too slowly to keep that out of a
    # 40 Hz bin.
    w = np.hanning(n)
    w_sum = float(w.sum())
    if w_sum <= 0.0:
        return (0.0, freq_hz)

    t = np.arange(n, dtype=np.float64) / rate_hz
    trials = freq_hz * (1.0 + np.linspace(-search_frac, search_frac, steps))
    angle = 2.0 * np.pi * np.outer(trials, t)
    cos_w = np.cos(angle) * w
    sin_w = np.sin(angle) * w

    # Sum the per-axis power at each trial frequency, then pick the trial that
    # found the most. Taking the maximum biases the answer slightly upward when
    # there is no tone at all, since it is the largest of several noise draws —
    # which is why the caller compares against an ambient floor measured
    # through this same detector rather than against zero.
    power = np.zeros(steps, dtype=np.float64)
    for axis in (x, y, z):
        a = np.asarray(axis, dtype=np.float64)[:n]
        i = cos_w @ a * (2.0 / w_sum)
        q = sin_w @ a * (2.0 / w_sum)
        power += i * i + q * q

    best = int(np.argmax(power))
    peak_g = float(np.sqrt(power[best]))
    return (peak_g / np.sqrt(2.0), float(trials[best]))
