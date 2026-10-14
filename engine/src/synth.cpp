#include "synth.h"

#include <algorithm>
#include <cmath>

namespace {

constexpr float kTwoPi = 6.28318530717958647692f;

inline float oscSample(Waveform w, float phase01) {
    switch (w) {
    case Waveform::Sine:
        return std::sin(phase01 * kTwoPi);
    case Waveform::Square:
        return phase01 < 0.5f ? 1.0f : -1.0f;
    case Waveform::Saw:
        return 2.0f * phase01 - 1.0f;
    case Waveform::Triangle:
        return phase01 < 0.5f ? 4.0f * phase01 - 1.0f
                              : 3.0f - 4.0f * phase01;
    }
    return 0.0f;
}

// Envelope value at `age` frames for a note held for `len` frames.
inline float envelopeAt(const SynthParams& p, int64_t age, int64_t len,
                        float sr) {
    const float aF = std::max(1.0f, p.attack * sr);
    const float dF = std::max(1.0f, p.decay * sr);
    const float rF = std::max(1.0f, p.release * sr);

    auto held = [&](float t) -> float {
        if (t < aF) return t / aF;
        if (t < aF + dF) return 1.0f - (1.0f - p.sustain) * (t - aF) / dF;
        return p.sustain;
    };

    const float t = float(age);
    if (age < len) return held(t);
    const float rel = (t - float(len)) / rF;
    if (rel >= 1.0f) return 0.0f;
    return held(float(len)) * (1.0f - rel);
}

} // namespace

namespace synth {

float noteFrequency(int pitch) {
    return 440.0f * std::pow(2.0f, (pitch - 69) / 12.0f);
}

int64_t noteTailFrames(const SynthParams& p, int64_t noteLen,
                       uint32_t sampleRate) {
    return noteLen + int64_t(p.release * float(sampleRate)) + 1;
}

void renderNote(const SynthParams& p, int pitch, float velocity,
                int64_t noteAge, int64_t noteLen,
                uint32_t sampleRate, float* buf, uint32_t frames) {
    const float sr = float(sampleRate);
    const float freq = noteFrequency(pitch);
    const float phaseInc = freq / sr;
    const float amp = p.gain * std::max(0.0f, std::min(1.0f, velocity));

    // Deterministic phase from note age, so seek/loop stays glitch-free.
    double phase = double(phaseInc) * double(noteAge);
    phase -= std::floor(phase);

    for (uint32_t i = 0; i < frames; ++i) {
        const int64_t age = noteAge + int64_t(i);
        if (age < 0) continue;
        const float env = envelopeAt(p, age, noteLen, sr);
        if (env <= 0.0f && age >= noteLen) break;
        const float s = oscSample(p.wave, float(phase)) * env * amp;
        buf[i * 2] += s;
        buf[i * 2 + 1] += s;
        phase += phaseInc;
        if (phase >= 1.0) phase -= 1.0;
    }
}

} // namespace synth
