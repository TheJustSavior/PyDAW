#pragma once
#include <cstdint>
#include <vector>

// Simple built-in polyphonic synthesizer for MIDI tracks.
//
// Note rendering is stateless/deterministic: oscillator phase and envelope
// are computed from "frames since note start", so seeking anywhere on the
// timeline produces correct audio without voice bookkeeping.

enum class Waveform : int {
    Sine = 0,
    Square = 1,
    Saw = 2,
    Triangle = 3,
};

struct SynthParams {
    Waveform wave = Waveform::Saw;
    float attack = 0.005f;   // seconds
    float decay = 0.08f;     // seconds
    float sustain = 0.75f;   // 0..1
    float release = 0.20f;   // seconds
    float gain = 0.5f;       // pre-fader instrument gain
};

struct MidiNote {
    int id = 0;
    int pitch = 60;          // MIDI note number 0..127
    float velocity = 1.0f;   // 0..1
    int64_t startFrame = 0;  // relative to the clip start
    int64_t lengthFrames = 0;
};

struct MidiClip {
    int id = 0;
    int64_t startFrame = 0;   // position on the timeline
    int64_t lengthFrames = 0; // clip length (notes outside are clipped)
    std::vector<MidiNote> notes;

    int64_t endFrame() const { return startFrame + lengthFrames; }
};

// A live voice used for piano-roll note preview (works while stopped).
struct PreviewVoice {
    int pitch = 60;
    float velocity = 1.0f;
    int64_t age = 0;          // frames since note-on
    int64_t releasedAt = -1;  // age at which note-off happened, -1 = held
};

namespace synth {

float noteFrequency(int pitch);

// Render one note additively into an interleaved stereo buffer.
// `noteAge` is the position (in frames since the note started) that
// corresponds to buf[0]; frames past noteLen enter the release phase.
void renderNote(const SynthParams& p, int pitch, float velocity,
                int64_t noteAge, int64_t noteLen,
                uint32_t sampleRate, float* buf, uint32_t frames);

// Total frames a note keeps sounding after its start (length + release tail).
int64_t noteTailFrames(const SynthParams& p, int64_t noteLen,
                       uint32_t sampleRate);

} // namespace synth
