#pragma once
#include "audio_file.h"
#include "synth.h"
#include "warp/warp_constants.h"
#include "warp/warp_processor.h"

#include <atomic>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

class VstPlugin;

// Insert-effect interface. Built-in effects implement it now;
// future VST3 hosting plugs in here as well.
class EffectSlot {
public:
    virtual ~EffectSlot() = default;
    // In-place processing of interleaved stereo audio.
    virtual void process(float* interleaved, uint32_t frames) = 0;
    // Thread-safe parameter change (atomics inside implementations).
    virtual void setParam(int index, float value) = 0;
    virtual const char* name() const = 0;
    virtual void prepare(uint32_t sampleRate) = 0;
    // Processing latency reported by the effect (plugin delay
    // compensation). Must be audio-thread safe.
    virtual uint32_t latencySamples() const { return 0; }
};

using EffectPtr = std::shared_ptr<EffectSlot>;

// Lightweight MIDI event passed from mixer to VstPlugin (no VST headers
// involved). Defined here so Track can own a reusable per-track scratch
// buffer of events (no per-block allocation on the audio thread).
struct VstHostEvent {
    enum Type { NoteOn, NoteOff };
    Type type = NoteOn;
    int pitch = 60;
    float velocity = 1.0f;
    int32_t sampleOffset = 0;
};

// Factory for built-in effects: "delay", "filter". Returns nullptr on
// unknown type.
EffectPtr createEffect(const std::string& type);

struct Clip {
    int id = 0;
    AudioFilePtr file;
    // Position on the timeline, in engine samples.
    int64_t startFrame = 0;
    // Offset into the source file (for trimming), in samples.
    int64_t sourceOffset = 0;
    // Timeline length in samples. For warped clips this may exceed the raw
    // source span because playback maps timeline frames through stretchRatio.
    int64_t length = 0;
    // Linear fade in/out lengths, in samples (0 = none).
    int64_t fadeIn = 0;
    int64_t fadeOut = 0;
    // Realtime warp (timeline coords; source mapped via stretchRatio).
    pydaw::warp::WarpMethod warp = pydaw::warp::WarpMethod::Stretch;
    float stretchRatio = 1.f;
    pydaw::warp::WarpProcessor warpProc;
    int64_t lastMixPlayhead = -1;

    int64_t endFrame() const { return startFrame + length; }

    void syncWarpProcessor();
    void invalidateWarpContinuity() {
        lastMixPlayhead = -1;
    }
};

struct AutomationPoint {
    int64_t frame = 0;
    float value = 0.f;
    // Interpolation shape towards the NEXT point:
    // 0 linear, 1 step (hold), 2 exponential, 3 bezier (smoothstep).
    int shape = 0;
    // Signed intensity: +1 keeps the original shape, 0 is linear and
    // negative values bend the shape to the opposite side.
    float curveAmount = 1.f;
};

struct AutomationLane {
    int laneId = 0;
    // 0 volume, 1 pan, 2 builtin fx, 3 vst fx, 4 vst inst, 5 stereo width
    int target = 0;
    int slot = -1;    // engine effect chain index for targets 2/3
    int param = 0;
    std::vector<AutomationPoint> points; // sorted by frame, native units
};

enum class TrackType : int { Audio = 0, Midi = 1, Group = 2 };

struct Track {
    int id = 0;
    TrackType type = TrackType::Audio;
    int groupId = 0;  // 0 = not grouped; else id of the group track
    std::vector<Clip> clips;          // audio tracks
    std::vector<MidiClip> midiClips;  // midi tracks
    SynthParams synth;                // instrument for midi tracks
    std::shared_ptr<VstPlugin> vstInstrument;
    int64_t lastMidiBlockEnd = -1;
    std::vector<EffectPtr> effects;   // insert chain
    std::vector<AutomationLane> autoLanes;
    float gain = 1.0f;   // linear
    float pan = 0.0f;    // -1..+1
    // Stereo width: 0 = mono, 1 = unchanged stereo, 2 = extra wide.
    float stereoWidth = 1.0f;
    bool mute = false;
    bool solo = false;
    // Post-fader peak levels for UI metering.
    std::atomic<float> peakL{0.0f};
    std::atomic<float> peakR{0.0f};

    // Reusable scratch for VST note events built each block: capacity
    // persists between blocks, so the audio thread stops allocating a
    // fresh std::vector per block per VST instrument.
    std::vector<VstHostEvent> midiEvents;
    std::vector<float> warpScratch;

    // Delay-line PDC state (Mixer::pdcMode == 1): interleaved stereo
    // ring buffer holding (maxLatency - own path latency) frames.
    std::vector<float> pdcDelayBuf;
    size_t pdcDelayPos = 0;
    int64_t pdcDelayFrames = -1; // -1 = uninitialised
};

// Owns tracks/clips; mix() is called from the audio thread only.
// All mutation happens on the audio thread via applied commands.
class Mixer {
public:
    Track* findTrack(int trackId);
    Clip* findClip(int trackId, int clipId, Track** outTrack = nullptr);
    MidiClip* findMidiClip(int trackId, int clipId, Track** outTrack = nullptr);

    // Render `frames` stereo frames starting at timeline position `playhead`
    // into `out` (interleaved stereo, overwritten).
    void mix(float* out, uint32_t frames, int64_t playhead);

    // Add metronome clicks for [playhead, playhead+frames) into `out`.
    void renderMetronome(float* out, uint32_t frames, int64_t playhead);

    // Render live preview voices (piano roll auditioning); advances voices.
    void renderPreview(float* out, uint32_t frames);
    bool hasPreviewVoices() const { return !previewVoices_.empty(); }

    // Keep VST instruments processing while the transport is stopped
    // (piano-roll preview notes, release tails). Adds into `out`.
    void renderVstIdle(float* out, uint32_t frames, int64_t playhead);

    // Reset UI meter atomics when transport stops or the project is cleared.
    void clearPeaks();

    // Master automation + insert chain + gain + soft clip + peak metering.
    void applyMaster(float* out, uint32_t frames, int64_t playhead = -1);

    void previewNoteOn(int pitch, float velocity, const SynthParams& params);
    void previewNoteOff(int pitch);

    // Timeline length = end of the last clip (audio or midi).
    int64_t totalLength() const;

    // Full latency-compensation offset for a track: its own insert chain
    // + instrument + its group bus chain + the master chain. 0 when the
    // track is unknown. Audio-thread only.
    int64_t trackPdcFrames(int trackId);

    void invalidateWarpContinuity();

    // Loop seam: reset warp/MIDI state before rendering from loopStart.
    void onLoopWrap(int64_t loopStart);

    // Reset per-track MIDI/VST transport after seek/stop/clip removal.
    // killNotes sends note-offs for notes still held in VST instruments.
    void resetTrackMidiTransport(Track& t, int64_t playhead, bool playing,
                                 bool killNotes);

    std::vector<std::unique_ptr<Track>> tracks;
    // Master insert chain, processed before master gain / soft clip.
    std::vector<EffectPtr> masterEffects;
    // Automation lanes for the master bus (gain / master fx params).
    std::vector<AutomationLane> masterAutoLanes;
    std::atomic<float> masterGain{1.0f};
    std::atomic<float> masterPeakL{0.0f};
    std::atomic<float> masterPeakR{0.0f};

    uint32_t sampleRate = 48000;
    double bpm = 120.0;
    bool metronomeOn = false;
    // Plugin delay compensation: tracks render "ahead" by their chain
    // latency so plugin-delayed output lands exactly on the timeline.
    bool pdcEnabled = true;
    // PDC strategy:
    //   0 = look-ahead (default): tracks render at playhead + latency;
    //       zero added monitoring latency, ideal for timeline playback.
    //   1 = delay lines (classic): every track is delayed by
    //       (maxLatency - own path latency) so all tracks align at the
    //       same output time. Suited for future live-input monitoring,
    //       where a live signal cannot be read "ahead".
    int pdcMode = 0;

private:
    void mixAudioTrack(Track& t, float* buf, uint32_t frames, int64_t playhead);
    void mixMidiTrack(Track& t, float* buf, uint32_t frames, int64_t playhead);
    // Render one non-group track into `buf` (zeroing it first): clips or
    // MIDI, insert chain, stereo width, gain/pan and peak metering.
    // Thread-safe against other tracks (touches only `t` + shared consts).
    void renderTrackBlock(Track& t, float* buf, uint32_t frames,
                          int64_t playhead);
    static uint32_t chainLatency(const std::vector<EffectPtr>& effects);
    int64_t trackCompensation(const Track& t, uint32_t masterLat);
    // Latency of a track's whole path to the master sum: own insert
    // chain + instrument + parent group bus chain (master chain is
    // common to all paths and thus excluded).
    int64_t trackPathLatency(const Track& t);
    // Max path latency across all non-group tracks (delay-line PDC).
    int64_t maxPathLatency();
    // Classic PDC: run `buf` through the track's delay line so its
    // output lands `delay` frames late (aligning all tracks).
    void applyPdcDelay(Track& t, float* buf, uint32_t frames,
                       int64_t delay);
    void prepareGroupBuffers(uint32_t frames);
    float* groupBuffer(int groupId, uint32_t frames);
    bool groupHasSoloChild(int groupId) const;
    bool trackSoloActive(const Track& t);
    bool isGroupAudible(const Track& group, bool anySolo) const;
    void applyStereoWidth(float width, float* buf, uint32_t frames);
    void applyGainPan(float gain, float pan, float* buf, uint32_t frames,
                      float& pkL, float& pkR);
    // Group bus: pan at center leaves stereo image unchanged (unity).
    void applyGainPanBus(float gain, float pan, float* buf, uint32_t frames,
                         float& pkL, float& pkR);

    struct ActivePreview {
        PreviewVoice voice;
        SynthParams params;
    };
    std::vector<ActivePreview> previewVoices_;
    std::vector<float> scratch_;     // shared buffer (group pass / idle)
    std::vector<float> groupBufs_;   // one stereo buffer per group track
    std::vector<int> groupTrackIds_; // ids parallel to groupBufs_

    // Parallel pass-1 state: one render buffer per non-group track plus
    // the job list (raw pointers valid for the duration of one mix()).
    std::vector<float> trackBufs_;
    std::vector<Track*> renderJobs_;
    // Parallel pass-2 state: group tracks whose busses are processed in
    // parallel (groups cannot nest, so they are always independent).
    std::vector<Track*> groupJobs_;
    std::vector<int> parallelRenderIndices_;
    std::vector<int> parallelGroupIndices_;
    struct MixJobCtx {
        Mixer* self;
        uint32_t frames;
        int64_t playhead;
        uint32_t masterLat;
        int64_t maxLat;    // delay-line PDC only
        bool delayMode;
        const int* jobIndices = nullptr;
    };
};
