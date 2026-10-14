#pragma once
#include "mixer.h"
#include "ring_buffer.h"
#include "vst_host.h"

#include <atomic>
#include <cstdint>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

class RtAudio;

// Info about an output device for the settings dialog.
struct DeviceInfo {
    unsigned int id = 0;
    std::string apiName;
    std::string apiDisplayName;
    std::string name;
    bool isDefault = false;
    std::vector<unsigned int> sampleRates;
};

// Commands passed from the UI thread into the audio thread.
struct Command {
    enum Type {
        AddTrack, RemoveTrack,
        AddClip, RemoveClip, MoveClip, TrimClip, SetClipFades, SetClipWarp,
        MoveClipToTrack,
        AddMidiClip, RemoveMidiClip, MoveMidiClip, ResizeMidiClip,
        AddNote, RemoveNote, UpdateNote,
        SetInstrument, PreviewNoteOn, PreviewNoteOff,
        AddEffect, RemoveEffect, SetEffectParam,
        SetVstInstrument, ClearVstInstrument,
        SetGain, SetPan, SetMute, SetSolo, SetTrackGroup, SetMasterGain,
        SetStereoWidth,
        SetTempo, SetMetronome, SetLoop, SetPdc, SetPdcMode,
        SetAutomationLane, RemoveAutomationLane,
        Play, Stop, Seek, ClearAll
    };
    Type type = Play;
    int trackId = 0;
    int clipId = 0;
    int aux = 0;           // note id / pitch / target track / effect slot
    int64_t i64 = 0;       // frame position
    int64_t i64b = 0;      // clip/note length
    int64_t srcOffset = 0; // offset into source file
    float f = 0.0f;        // gain/pan/velocity/param value
    double d = 0.0;        // bpm
    bool b = false;        // mute/solo/metronome flag
    MidiNote note;         // payload for note commands
    SynthParams synth;     // payload for SetInstrument
    // Shared ownership travels with the command so the audio thread never
    // needs to touch the file cache (and its mutex).
    AudioFilePtr file;
    EffectPtr effect;      // payload for AddEffect
    std::shared_ptr<VstPlugin> vstInstrument;
    std::shared_ptr<AutomationLane> autoLane;
};

// Pseudo track id addressing the master insert chain in the effect API.
constexpr int kMasterTrackId = -1;

class AudioEngine {
public:
    AudioEngine();
    ~AudioEngine();

    // Device -----------------------------------------------------------
    // deviceId 0 = default output device.
    bool start(uint32_t sampleRate = 48000, uint32_t bufferFrames = 512,
               unsigned int deviceId = 0, const std::string& apiName = "");
    void stop();
    // Full teardown for app exit: stop device, close VST editors, release
    // plugins. Safe to call multiple times. Does not clear file cache.
    void shutdown();
    bool isRunning() const { return running_; }
    uint32_t sampleRate() const { return sampleRate_; }
    uint32_t bufferFrames() const { return bufferFrames_; }
    unsigned int currentDeviceId() const { return deviceId_; }
    std::string currentApiName() const { return apiName_; }
    std::vector<std::pair<std::string, std::string>> listAudioApis() const;
    std::vector<DeviceInfo> listDevices(const std::string& apiName = "") const;

    // Files -------------------------------------------------------------
    // Loads (or returns cached) file, resampled to engine rate. Throws on error.
    int loadFile(const std::string& path);
    double fileDuration(int fileId) const;
    int64_t fileFrames(int fileId) const;
    std::vector<float> filePeaks(int fileId, uint32_t samplesPerPeak) const;

    // Tracks / audio clips (thread-safe, lock-free towards audio thread) --
    void addTrack(int trackId, int trackType = 0); // 0 audio, 1 midi, 2 group
    void removeTrack(int trackId);
    void addClip(int trackId, int clipId, int fileId,
                 int64_t startFrame, int64_t sourceOffset, int64_t length,
                 int warpMethod = 0, float stretchRatio = 1.f);
    void removeClip(int trackId, int clipId);
    void moveClip(int trackId, int clipId, int64_t startFrame);
    void trimClip(int trackId, int clipId, int64_t startFrame,
                  int64_t sourceOffset, int64_t length);
    void setClipFades(int trackId, int clipId, int64_t fadeIn, int64_t fadeOut);
    void setClipWarp(int trackId, int clipId, int warpMethod, float stretchRatio);
    // Move a clip (audio or midi depending on `isMidi`) to another track.
    void moveClipToTrack(int fromTrackId, int toTrackId, int clipId,
                         int64_t startFrame, bool isMidi);

    // MIDI clips / notes ---------------------------------------------------
    void addMidiClip(int trackId, int clipId, int64_t startFrame, int64_t length);
    void removeMidiClip(int trackId, int clipId);
    void moveMidiClip(int trackId, int clipId, int64_t startFrame);
    void resizeMidiClip(int trackId, int clipId, int64_t length);
    void addNote(int trackId, int clipId, int noteId, int pitch,
                 float velocity, int64_t startFrame, int64_t length);
    void removeNote(int trackId, int clipId, int noteId);
    void updateNote(int trackId, int clipId, int noteId, int pitch,
                    float velocity, int64_t startFrame, int64_t length);

    // Instrument / preview ---------------------------------------------------
    void setInstrument(int trackId, int wave, float attack, float decay,
                       float sustain, float release, float gain);
    void previewNoteOn(int trackId, int pitch, float velocity);
    void previewNoteOff(int pitch);

    // Effects -----------------------------------------------------------------
    // Returns false if the effect type is unknown.
    bool addEffect(int trackId, const std::string& type);
    void removeEffect(int trackId, int slotIndex);
    void setEffectParam(int trackId, int slotIndex, int paramIndex, float value);

    // VST3 -------------------------------------------------------------------
    static std::vector<VstPluginInfo> scanVstPlugins(
        const std::vector<std::string>& dirs);
    bool addVstEffect(int trackId, const std::string& path,
                      const std::string& uid);
    bool setVstInstrument(int trackId, const std::string& path,
                          const std::string& uid);
    void clearVstInstrument(int trackId);

    bool vstHasEditor(int trackId, int slot);
    bool vstOpenEditor(int trackId, int slot, uintptr_t hwnd);
    void vstCloseEditor(int trackId, int slot);
    std::pair<int, int> vstEditorSize(int trackId, int slot);
    bool vstCheckResize(int trackId, int slot, int& w, int& h);
    bool vstEditorCanResize(int trackId, int slot);
    bool vstResizeEditor(int trackId, int slot, int w, int h);
    bool vstSetContentScale(int trackId, int slot, float scale);
    std::string vstDiagnostic(int trackId, int slot);
    int vstParamCount(int trackId, int slot);
    std::vector<std::tuple<int, std::string, float>> vstParams(int trackId,
                                                                int slot);
    void vstSetParam(int trackId, int slot, int paramIndex, float normalized,
                     bool markUserEdit = true);
    float vstGetParam(int trackId, int slot, int paramIndex);
    // Plugin-declared default normalized value ("reset to default" in UI).
    float vstGetParamDefault(int trackId, int slot, int paramIndex);
    // UI thread: mirror automation values applied on the audio thread into
    // every plugin's edit controller (native editors + generic panels).
    void vstSyncControllers();
    // UI thread: (trackId, slot, paramIndex) of the most recent user tweak
    // in any loaded VST (native editor or generic sliders); paramIndex = -1
    // when nothing was touched since the previous call. TWEAK mode poll.
    std::tuple<int, int, int> vstConsumeLastTweak();
    // UI thread: refresh parameter lists of plugins that reported
    // restartComponent (preset switch / dynamic GUI). Returns true when
    // at least one plugin's cached list was re-read - the UI should
    // rebuild its parameter views.
    bool vstRefreshChangedParams();
    // UI thread: true while the VST instance is still loading (subprocess
    // async load). Editor / param introspection should wait.
    bool vstIsLoading(int trackId, int slot);
    std::string vstGetState(int trackId, int slot);
    bool vstSetState(int trackId, int slot, const std::string& data);

    // Mixer params --------------------------------------------------------
    void setGain(int trackId, float gain);
    void setPan(int trackId, float pan);
    // Stereo width 0..2 (0 mono, 1 unchanged, 2 extra wide).
    void setStereoWidth(int trackId, float width);
    void setMute(int trackId, bool mute);
    void setSolo(int trackId, bool solo);
    void setTrackGroup(int trackId, int groupId);
    void setMasterGain(float gain);
    void setTempo(double bpm);
    void setMetronome(bool on);
    // Loop region in engine frames; enabled only if end > start.
    void setLoop(int64_t startFrame, int64_t endFrame, bool enabled);
    // Points: (frame, value, shape, curveAmount) where shape/amount describe
    // the curve towards the next point.
    // trackId == kMasterTrackId targets the master bus.
    void setAutomationLane(
        int trackId, int laneId, int target, int slot, int param,
        const std::vector<std::tuple<int64_t, float, int, float>>& points);
    void removeAutomationLane(int trackId, int laneId);
    void clearAll();
    // UI thread: wait until queued commands are applied and retired VST
    // instances are destroyed (safe before loading plugins after clear_all).
    void drainCommands();

    // Transport ----------------------------------------------------------
    void play();
    void pause();
    void seek(int64_t frame);
    bool isPlaying() const { return playing_.load(std::memory_order_relaxed); }
    int64_t playheadFrame() const { return playhead_.load(std::memory_order_relaxed); }
    double playheadSeconds() const {
        return sampleRate_ ? double(playheadFrame()) / sampleRate_ : 0.0;
    }

    // Metering -------------------------------------------------------------
    float masterPeakL() const { return mixer_.masterPeakL.load(std::memory_order_relaxed); }
    float masterPeakR() const { return mixer_.masterPeakR.load(std::memory_order_relaxed); }
    std::pair<float, float> trackPeaks(int trackId);

    // Offline export: render the timeline (or a frame range) to a WAV file.
    // endFrame < 0 = end of content. Safe to call from a worker thread;
    // poll exportProgress() / call cancelExport() from any other thread.
    bool exportWav(const std::string& path, int64_t startFrame = 0,
                   int64_t endFrame = -1);
    // 0..1 progress of the export currently running (1 when idle/done).
    double exportProgress() const {
        return exportProgress_.load(std::memory_order_relaxed);
    }
    // Ask a running export to stop; the partial file is removed and
    // exportWav returns false with wasCancelled() == true.
    void cancelExport() {
        exportCancel_.store(true, std::memory_order_relaxed);
    }
    bool exportWasCancelled() const {
        return exportCancelled_.load(std::memory_order_relaxed);
    }

    // Plugin delay compensation ------------------------------------------
    void setPdcEnabled(bool on);
    bool pdcEnabled() const { return pdcEnabled_; }
    // PDC strategy: 0 = look-ahead reads (timeline, zero added latency),
    // 1 = classic delay lines (future live-input monitoring).
    void setPdcMode(int mode);
    int pdcMode() const { return pdcMode_; }
    // Current PDC offset of a track (frames). UI thread; approximate when
    // commands are still queued.
    int64_t trackLatency(int trackId);
    // Render-pool worker threads (excluding the audio thread).
    static int mixThreads();

    // Called by the RtAudio callback.
    int process(float* out, uint32_t frames);

private:
    void pushCommand(const Command& cmd);
    void applyCommands();
    void flushVstGraveyard();
    std::shared_ptr<VstPlugin> findVst(int trackId, int slot);
    int nextEffectSlot(int trackId);
    void shiftVstFxSlots(int trackId, int removedSlot);
    void removeVstKeysForTrack(int trackId);

    std::unique_ptr<RtAudio> dac_;
    Mixer mixer_;
    RingBuffer<Command> commands_{2048};
    std::mutex commandMutex_;

    std::atomic<bool> playing_{false};
    // Set while exportWav renders offline; the realtime callback outputs
    // silence instead of racing the export thread on mixer_.
    std::atomic<bool> exporting_{false};
    std::atomic<double> exportProgress_{1.0};
    std::atomic<bool> exportCancel_{false};
    std::atomic<bool> exportCancelled_{false};
    bool pdcEnabled_ = true;
    int pdcMode_ = 0;
    std::atomic<int64_t> playhead_{0};
    // Loop state: mutated only via commands on the audio thread.
    int64_t loopStart_ = 0;
    int64_t loopEnd_ = 0;
    bool loopOn_ = false;
    bool running_ = false;
    std::atomic<bool> shuttingDown_{false};
    uint32_t sampleRate_ = 48000;
    uint32_t bufferFrames_ = 512;
    unsigned int deviceId_ = 0;
    std::string apiName_;

    // File cache owned by the engine; audio thread sees raw pointers only.
    mutable std::mutex filesMutex_;
    // Guards mixer_ against concurrent mix()/renderVstIdle() from the
    // realtime callback and offline exportWav() on another thread.
    std::mutex mixMutex_;
    std::map<int, AudioFilePtr> files_;
    int nextFileId_ = 1;

    // UI-thread VST registry; slot -1 = instrument.
    std::map<std::pair<int, int>, std::shared_ptr<VstPlugin>> vstRegistry_;
    std::map<int, int> trackEffectSlots_;
    std::vector<std::shared_ptr<VstPlugin>> vstGraveyard_;
};
