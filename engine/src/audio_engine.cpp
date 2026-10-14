#include "audio_engine.h"

#include "warp/warp_constants.h"
#include "vst_bridge.h"
#include "vst_host.h"

#include "RtAudio.h"
#include "render_pool.h"
#include "dr_wav.h"

#include <cstdio>
#include <cstdlib>

#if defined(__WINDOWS_ASIO__)
#include "asiodrivers.h"
#endif

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <stdexcept>
#include <thread>

namespace {

int rtCallback(void* outputBuffer, void* /*inputBuffer*/,
               unsigned int nFrames, double /*streamTime*/,
               RtAudioStreamStatus /*status*/, void* userData) {
    auto* engine = static_cast<AudioEngine*>(userData);
    return engine->process(static_cast<float*>(outputBuffer), nFrames);
}

int64_t maxTimelineFrameForSource(int64_t fileFrames, float ratio) {
    if (fileFrames <= 0) return 0;
    ratio = pydaw::warp::clampRatio(ratio);
    return std::max<int64_t>(0, int64_t(std::llround(
        double(fileFrames) * double(ratio))));
}

int64_t clampTimelineSourceOffset(int64_t sourceOffset, int64_t fileFrames,
                                  float ratio) {
    const int64_t maxSourceOffset = maxTimelineFrameForSource(fileFrames, ratio);
    return std::clamp<int64_t>(sourceOffset, 0, maxSourceOffset);
}

int64_t clampTimelineLength(int64_t length, int64_t sourceOffset,
                            int64_t fileFrames, float ratio) {
    const int64_t maxEnd = maxTimelineFrameForSource(fileFrames, ratio);
    const int64_t maxLength = std::max<int64_t>(0, maxEnd - sourceOffset);
    if (length <= 0)
        return maxLength;
    return std::clamp<int64_t>(length, 0, maxLength);
}

std::string normalizeApiName(const std::string& apiName) {
    std::string out;
    out.reserve(apiName.size());
    for (unsigned char ch : apiName) {
        out.push_back(static_cast<char>(std::tolower(ch)));
    }
    return out == "auto" ? std::string() : out;
}

RtAudio::Api apiFromName(const std::string& apiName) {
    const std::string normalized = normalizeApiName(apiName);
    if (normalized.empty()) {
#if defined(__WINDOWS_WASAPI__)
        return RtAudio::WINDOWS_WASAPI;
#elif defined(__MACOSX_CORE__)
        return RtAudio::MACOSX_CORE;
#elif defined(__LINUX_ALSA__)
        return RtAudio::LINUX_ALSA;
#else
        return RtAudio::UNSPECIFIED;
#endif
    }
    return RtAudio::getCompiledApiByName(normalized);
}

} // namespace

AudioEngine::AudioEngine() {
    // Trim the disk cache (PCM/peaks/stretch/join) to its LRU size
    // limit in the background; never blocks startup.
    audiocache::enforceLimitAsync();
}

AudioEngine::~AudioEngine() { shutdown(); }

bool AudioEngine::start(uint32_t sampleRate, uint32_t bufferFrames,
                        unsigned int deviceId, const std::string& apiName) {
    if (running_) return true;
    apiName_ = normalizeApiName(apiName);
    const RtAudio::Api api = apiFromName(apiName);
    if (!normalizeApiName(apiName).empty() && api == RtAudio::UNSPECIFIED) {
        return false;
    }
    try {
        dac_ = std::make_unique<RtAudio>(api);
    } catch (const std::exception&) {
        dac_.reset();
        return false;
    }
    if (dac_->getDeviceCount() == 0) {
        dac_.reset();
        return false;
    }

    RtAudio::StreamParameters params;
    params.deviceId = deviceId ? deviceId : dac_->getDefaultOutputDevice();
    params.nChannels = 2;
    params.firstChannel = 0;

    unsigned int frames = bufferFrames;
    sampleRate_ = sampleRate;

    RtAudio::StreamOptions options;
    options.streamName = "PyDAW";
    RtAudioErrorType err = dac_->openStream(
        &params, nullptr, RTAUDIO_FLOAT32, sampleRate, &frames,
        &rtCallback, this, &options);
    if (err != RTAUDIO_NO_ERROR) {
        dac_.reset();
        return false;
    }
    err = dac_->startStream();
    if (err != RTAUDIO_NO_ERROR) {
        dac_->closeStream();
        dac_.reset();
        return false;
    }
    running_ = true;
    bufferFrames_ = frames;
    deviceId_ = params.deviceId;
    apiName_ = RtAudio::getApiName(dac_->getCurrentApi());
    mixer_.sampleRate = sampleRate_;
    // Spawn render-pool workers now (not on the first mix callback) so
    // playback never stalls the driver with thread creation.
    RenderPool::instance().warmup();
    return true;
}

void AudioEngine::stop() {
    if (!dac_) return;
    if (dac_->isStreamRunning()) dac_->stopStream();
    if (dac_->isStreamOpen()) dac_->closeStream();
    {
        std::lock_guard<std::mutex> lock(mixMutex_);
        playing_.store(false, std::memory_order_relaxed);
        mixer_.clearPeaks();
    }
    dac_.reset();
    running_ = false;
}

void AudioEngine::shutdown() {
    if (shuttingDown_.exchange(true, std::memory_order_acq_rel))
        return;

    playing_.store(false, std::memory_order_relaxed);

    if (dac_ && dac_->isStreamRunning()) {
        dac_->stopStream();
        if (sampleRate_ > 0 && bufferFrames_ > 0) {
            const auto period = std::chrono::milliseconds(
                10 + (1000ull * bufferFrames_) / sampleRate_);
            std::this_thread::sleep_for(period);
        }
    }
    RenderPool::instance().shutdown();

    std::lock_guard<std::mutex> lock(mixMutex_);

    auto closePlugin = [](const std::shared_ptr<VstPlugin>& p) {
        if (!p) return;
        p->closeEditor();
        p->allNotesOff();
    };
    for (auto& entry : vstRegistry_)
        closePlugin(entry.second);
    for (auto& t : mixer_.tracks) {
        if (!t) continue;
        closePlugin(t->vstInstrument);
        for (auto& fx : t->effects) {
            if (auto v = std::dynamic_pointer_cast<VstPlugin>(fx))
                closePlugin(v);
        }
    }
    for (auto& fx : mixer_.masterEffects) {
        if (auto v = std::dynamic_pointer_cast<VstPlugin>(fx))
            closePlugin(v);
    }

    applyCommands();
    mixer_.tracks.clear();
    mixer_.masterEffects.clear();
    mixer_.masterAutoLanes.clear();
    playhead_.store(0, std::memory_order_relaxed);
    loopStart_ = 0;
    loopEnd_ = 0;
    loopOn_ = false;

    for (auto& [key, plug] : vstRegistry_) {
        (void)key;
        if (plug) vstGraveyard_.push_back(plug);
    }
    vstRegistry_.clear();
    trackEffectSlots_.clear();

    Command discarded;
    while (commands_.pop(discarded)) {}

    flushVstGraveyard();
    vstGraveyard_.clear();

    if (dac_) {
        if (dac_->isStreamOpen()) dac_->closeStream();
        dac_.reset();
    }
    running_ = false;
}

std::vector<std::pair<std::string, std::string>> AudioEngine::listAudioApis() const {
    std::vector<std::pair<std::string, std::string>> result;
    std::vector<RtAudio::Api> apis;
    RtAudio::getCompiledApi(apis);
    for (RtAudio::Api api : apis) {
        result.emplace_back(RtAudio::getApiName(api),
                            RtAudio::getApiDisplayName(api));
    }
    return result;
}

std::vector<DeviceInfo> AudioEngine::listDevices(const std::string& apiName) const {
    std::vector<DeviceInfo> result;
    const RtAudio::Api api = apiFromName(apiName);
    if (!normalizeApiName(apiName).empty() && api == RtAudio::UNSPECIFIED) {
        return result;
    }
    // RtAudio's ASIO backend refuses to probe devices from a second instance
    // while another ASIO stream is open (streamOpen guard), which would make
    // the list come back empty when the engine is already running on ASIO.
    // Reuse the live instance when it matches the requested API.
    RtAudio* rt = nullptr;
    std::unique_ptr<RtAudio> probe;
    if (dac_ && dac_->getCurrentApi() == api) {
        rt = dac_.get();
    } else {
        try {
            probe = std::make_unique<RtAudio>(api);
        } catch (const std::exception&) {
            return result;
        }
        rt = probe.get();
    }
    const std::string currentApi = RtAudio::getApiName(rt->getCurrentApi());
    const std::string currentApiDisplay =
        RtAudio::getApiDisplayName(rt->getCurrentApi());
    for (unsigned int id : rt->getDeviceIds()) {
        RtAudio::DeviceInfo info = rt->getDeviceInfo(id);
        if (info.outputChannels < 2) continue;
        DeviceInfo d;
        d.id = id;
        d.apiName = currentApi;
        d.apiDisplayName = currentApiDisplay;
        d.name = info.name;
        d.isDefault = info.isDefaultOutput;
        d.sampleRates.assign(info.sampleRates.begin(), info.sampleRates.end());
        result.push_back(std::move(d));
    }
    return result;
}

// Files ------------------------------------------------------------------

int AudioEngine::loadFile(const std::string& path) {
    std::lock_guard<std::mutex> lock(filesMutex_);
    for (const auto& [id, f] : files_)
        if (f->path() == path && f->sampleRate() == sampleRate_) return id;
    auto file = std::make_shared<AudioFile>(path, sampleRate_);
    // Build the waveform peak pyramid in the background right at import,
    // so the first paint of a large file doesn't scan it synchronously.
    file->buildPeaksAsync(file);
    const int id = nextFileId_++;
    files_[id] = std::move(file);
    return id;
}

double AudioEngine::fileDuration(int fileId) const {
    std::lock_guard<std::mutex> lock(filesMutex_);
    auto it = files_.find(fileId);
    return it != files_.end() ? it->second->durationSeconds() : 0.0;
}

int64_t AudioEngine::fileFrames(int fileId) const {
    std::lock_guard<std::mutex> lock(filesMutex_);
    auto it = files_.find(fileId);
    return it != files_.end() ? int64_t(it->second->frameCount()) : 0;
}

std::vector<float> AudioEngine::filePeaks(int fileId, uint32_t samplesPerPeak) const {
    std::lock_guard<std::mutex> lock(filesMutex_);
    auto it = files_.find(fileId);
    if (it == files_.end()) return {};
    return it->second->computePeaks(samplesPerPeak);
}

// Command plumbing -----------------------------------------------------------

void AudioEngine::pushCommand(const Command& cmd) {
    std::lock_guard<std::mutex> producerLock(commandMutex_);
    if (shuttingDown_.load(std::memory_order_acquire))
        return;

    // If the audio thread is not running, apply directly (e.g. offline mode).
    if (!running_) {
        Command c = cmd;
        if (!commands_.push(c))
            return;
        std::lock_guard<std::mutex> mixLock(mixMutex_);
        applyCommands();
        return;
    }
    while (!commands_.push(cmd)) {
        if (shuttingDown_.load(std::memory_order_acquire))
            return;
        // Queue full (extremely unlikely): sleep one scheduling quantum
        // instead of burning a core; the audio thread drains ~every block.
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
}

// Tracks / audio clips ---------------------------------------------------------

void AudioEngine::addTrack(int trackId, int trackType) {
    if (!trackEffectSlots_.count(trackId))
        trackEffectSlots_[trackId] = 0;
    Command c; c.type = Command::AddTrack; c.trackId = trackId;
    c.aux = trackType;
    pushCommand(c);
}

void AudioEngine::removeTrack(int trackId) {
    removeVstKeysForTrack(trackId);
    trackEffectSlots_.erase(trackId);
    Command c; c.type = Command::RemoveTrack; c.trackId = trackId;
    pushCommand(c);
}

void AudioEngine::addClip(int trackId, int clipId, int fileId,
                          int64_t startFrame, int64_t sourceOffset, int64_t length,
                          int warpMethod, float stretchRatio) {
    AudioFilePtr file;
    {
        std::lock_guard<std::mutex> lock(filesMutex_);
        auto it = files_.find(fileId);
        if (it == files_.end()) return;
        file = it->second;
    }
    const int64_t fileFrames = int64_t(file->frameCount());
    sourceOffset = clampTimelineSourceOffset(sourceOffset, fileFrames,
                                             stretchRatio);
    length = clampTimelineLength(length, sourceOffset, fileFrames,
                                 stretchRatio);
    if (length <= 0)
        return;

    Command c; c.type = Command::AddClip;
    c.trackId = trackId; c.clipId = clipId;
    c.i64 = startFrame; c.i64b = length;
    c.srcOffset = sourceOffset;
    c.aux = warpMethod;
    c.f = stretchRatio;
    c.file = std::move(file);
    pushCommand(c);
}

void AudioEngine::removeClip(int trackId, int clipId) {
    Command c; c.type = Command::RemoveClip; c.trackId = trackId; c.clipId = clipId;
    pushCommand(c);
}

void AudioEngine::moveClip(int trackId, int clipId, int64_t startFrame) {
    Command c; c.type = Command::MoveClip; c.trackId = trackId; c.clipId = clipId;
    c.i64 = startFrame;
    pushCommand(c);
}

void AudioEngine::trimClip(int trackId, int clipId, int64_t startFrame,
                           int64_t sourceOffset, int64_t length) {
    Command c; c.type = Command::TrimClip;
    c.trackId = trackId; c.clipId = clipId;
    c.i64 = startFrame; c.i64b = length; c.srcOffset = sourceOffset;
    pushCommand(c);
}

void AudioEngine::setClipFades(int trackId, int clipId,
                               int64_t fadeIn, int64_t fadeOut) {
    Command c; c.type = Command::SetClipFades;
    c.trackId = trackId; c.clipId = clipId;
    c.i64 = fadeIn; c.i64b = fadeOut;
    pushCommand(c);
}

void AudioEngine::setClipWarp(int trackId, int clipId, int warpMethod,
                              float stretchRatio) {
    Command c; c.type = Command::SetClipWarp;
    c.trackId = trackId; c.clipId = clipId;
    c.aux = warpMethod;
    c.f = stretchRatio;
    pushCommand(c);
}

void AudioEngine::moveClipToTrack(int fromTrackId, int toTrackId, int clipId,
                                  int64_t startFrame, bool isMidi) {
    Command c; c.type = Command::MoveClipToTrack;
    c.trackId = fromTrackId; c.aux = toTrackId; c.clipId = clipId;
    c.i64 = startFrame; c.b = isMidi;
    pushCommand(c);
}

// MIDI clips / notes ------------------------------------------------------------

void AudioEngine::addMidiClip(int trackId, int clipId,
                              int64_t startFrame, int64_t length) {
    Command c; c.type = Command::AddMidiClip;
    c.trackId = trackId; c.clipId = clipId;
    c.i64 = startFrame; c.i64b = std::max<int64_t>(0, length);
    pushCommand(c);
}

void AudioEngine::removeMidiClip(int trackId, int clipId) {
    Command c; c.type = Command::RemoveMidiClip;
    c.trackId = trackId; c.clipId = clipId;
    pushCommand(c);
}

void AudioEngine::moveMidiClip(int trackId, int clipId, int64_t startFrame) {
    Command c; c.type = Command::MoveMidiClip;
    c.trackId = trackId; c.clipId = clipId; c.i64 = startFrame;
    pushCommand(c);
}

void AudioEngine::resizeMidiClip(int trackId, int clipId, int64_t length) {
    Command c; c.type = Command::ResizeMidiClip;
    c.trackId = trackId; c.clipId = clipId; c.i64b = length;
    pushCommand(c);
}

void AudioEngine::addNote(int trackId, int clipId, int noteId, int pitch,
                          float velocity, int64_t startFrame, int64_t length) {
    Command c; c.type = Command::AddNote;
    c.trackId = trackId; c.clipId = clipId;
    c.note.id = noteId; c.note.pitch = pitch; c.note.velocity = velocity;
    c.note.startFrame = startFrame; c.note.lengthFrames = length;
    pushCommand(c);
}

void AudioEngine::removeNote(int trackId, int clipId, int noteId) {
    Command c; c.type = Command::RemoveNote;
    c.trackId = trackId; c.clipId = clipId; c.aux = noteId;
    pushCommand(c);
}

void AudioEngine::updateNote(int trackId, int clipId, int noteId, int pitch,
                             float velocity, int64_t startFrame, int64_t length) {
    Command c; c.type = Command::UpdateNote;
    c.trackId = trackId; c.clipId = clipId;
    c.note.id = noteId; c.note.pitch = pitch; c.note.velocity = velocity;
    c.note.startFrame = startFrame; c.note.lengthFrames = length;
    pushCommand(c);
}

// Instrument / preview -----------------------------------------------------------

void AudioEngine::setInstrument(int trackId, int wave, float attack,
                                float decay, float sustain, float release,
                                float gain) {
    Command c; c.type = Command::SetInstrument; c.trackId = trackId;
    c.synth.wave = Waveform(wave);
    c.synth.attack = attack; c.synth.decay = decay;
    c.synth.sustain = sustain; c.synth.release = release;
    c.synth.gain = gain;
    pushCommand(c);
}

void AudioEngine::previewNoteOn(int trackId, int pitch, float velocity) {
    Command c; c.type = Command::PreviewNoteOn;
    c.trackId = trackId; c.aux = pitch; c.f = velocity;
    pushCommand(c);
}

void AudioEngine::previewNoteOff(int pitch) {
    Command c; c.type = Command::PreviewNoteOff; c.aux = pitch;
    pushCommand(c);
}

// Effects ---------------------------------------------------------------------

bool AudioEngine::addEffect(int trackId, const std::string& type) {
    EffectPtr fx = createEffect(type);
    if (!fx) return false;
    fx->prepare(sampleRate_);
    const int slot = nextEffectSlot(trackId);
    Command c; c.type = Command::AddEffect; c.trackId = trackId;
    c.effect = std::move(fx);
    pushCommand(c);
    trackEffectSlots_[trackId] = slot + 1;
    return true;
}

void AudioEngine::removeEffect(int trackId, int slotIndex) {
    auto it = vstRegistry_.find({trackId, slotIndex});
    if (it != vstRegistry_.end()) {
        vstGraveyard_.push_back(it->second);
        vstRegistry_.erase(it);
    }
    shiftVstFxSlots(trackId, slotIndex);
    if (trackEffectSlots_.count(trackId) && trackEffectSlots_[trackId] > 0)
        --trackEffectSlots_[trackId];
    Command c; c.type = Command::RemoveEffect;
    c.trackId = trackId; c.aux = slotIndex;
    pushCommand(c);
}

void AudioEngine::setEffectParam(int trackId, int slotIndex,
                                 int paramIndex, float value) {
    Command c; c.type = Command::SetEffectParam;
    c.trackId = trackId; c.aux = slotIndex; c.clipId = paramIndex; c.f = value;
    pushCommand(c);
}

// Mixer params -------------------------------------------------------------

void AudioEngine::setGain(int trackId, float gain) {
    Command c; c.type = Command::SetGain; c.trackId = trackId; c.f = gain;
    pushCommand(c);
}

void AudioEngine::setPan(int trackId, float pan) {
    Command c; c.type = Command::SetPan; c.trackId = trackId; c.f = pan;
    pushCommand(c);
}

void AudioEngine::setStereoWidth(int trackId, float width) {
    Command c; c.type = Command::SetStereoWidth;
    c.trackId = trackId; c.f = width;
    pushCommand(c);
}

void AudioEngine::setMute(int trackId, bool mute) {
    Command c; c.type = Command::SetMute; c.trackId = trackId; c.b = mute;
    pushCommand(c);
}

void AudioEngine::setSolo(int trackId, bool solo) {
    Command c; c.type = Command::SetSolo; c.trackId = trackId; c.b = solo;
    pushCommand(c);
}

void AudioEngine::setTrackGroup(int trackId, int groupId) {
    Command c; c.type = Command::SetTrackGroup;
    c.trackId = trackId; c.aux = groupId;
    pushCommand(c);
}

void AudioEngine::setMasterGain(float gain) {
    Command c; c.type = Command::SetMasterGain; c.f = gain;
    pushCommand(c);
}

void AudioEngine::setTempo(double bpm) {
    Command c; c.type = Command::SetTempo; c.d = bpm;
    pushCommand(c);
}

void AudioEngine::setMetronome(bool on) {
    Command c; c.type = Command::SetMetronome; c.b = on;
    pushCommand(c);
}

void AudioEngine::setPdcEnabled(bool on) {
    pdcEnabled_ = on;
    Command c; c.type = Command::SetPdc; c.b = on;
    pushCommand(c);
}

void AudioEngine::setPdcMode(int mode) {
    pdcMode_ = (mode == 1) ? 1 : 0;
    Command c; c.type = Command::SetPdcMode; c.aux = pdcMode_;
    pushCommand(c);
}

int64_t AudioEngine::trackLatency(int trackId) {
    std::lock_guard<std::mutex> lock(mixMutex_);
    return mixer_.trackPdcFrames(trackId);
}

int AudioEngine::mixThreads() {
    return RenderPool::instance().workerCount();
}

void AudioEngine::setLoop(int64_t startFrame, int64_t endFrame, bool enabled) {
    Command c; c.type = Command::SetLoop;
    c.i64 = startFrame; c.i64b = endFrame; c.b = enabled;
    pushCommand(c);
}

void AudioEngine::setAutomationLane(
    int trackId, int laneId, int target, int slot, int param,
    const std::vector<std::tuple<int64_t, float, int, float>>& points) {
    auto lane = std::make_shared<AutomationLane>();
    lane->laneId = laneId;
    lane->target = target;
    lane->slot = slot;
    lane->param = param;
    lane->points.reserve(points.size());
    for (const auto& p : points)
        lane->points.push_back({std::get<0>(p), std::get<1>(p),
                                std::get<2>(p), std::get<3>(p)});
    std::sort(lane->points.begin(), lane->points.end(),
              [](const AutomationPoint& a, const AutomationPoint& b) {
                  return a.frame < b.frame;
              });
    Command c;
    c.type = Command::SetAutomationLane;
    c.trackId = trackId;
    c.autoLane = std::move(lane);
    pushCommand(c);
}

void AudioEngine::removeAutomationLane(int trackId, int laneId) {
    Command c;
    c.type = Command::RemoveAutomationLane;
    c.trackId = trackId;
    c.aux = laneId;
    pushCommand(c);
}

void AudioEngine::clearAll() {
    flushVstGraveyard();
    for (auto& [key, plug] : vstRegistry_) {
        (void)key;
        if (plug) {
            plug->closeEditor();
            vstGraveyard_.push_back(plug);
        }
    }
    vstRegistry_.clear();
    trackEffectSlots_.clear();
    Command c; c.type = Command::ClearAll;
    pushCommand(c);
}

void AudioEngine::drainCommands() {
    std::lock_guard<std::mutex> lock(mixMutex_);
    applyCommands();
    flushVstGraveyard();
}

void AudioEngine::play() {
    Command c; c.type = Command::Play;
    pushCommand(c);
}

void AudioEngine::pause() {
    Command c; c.type = Command::Stop;
    pushCommand(c);
}

void AudioEngine::seek(int64_t frame) {
    Command c; c.type = Command::Seek; c.i64 = std::max<int64_t>(0, frame);
    pushCommand(c);
}

std::pair<float, float> AudioEngine::trackPeaks(int trackId) {
    std::lock_guard<std::mutex> lock(mixMutex_);
    Track* t = mixer_.findTrack(trackId);
    if (!t) return {0.0f, 0.0f};
    return {t->peakL.load(std::memory_order_relaxed),
            t->peakR.load(std::memory_order_relaxed)};
}

// Command application (audio thread) -----------------------------------------

void AudioEngine::applyCommands() {
    Command c;
    while (commands_.pop(c)) {
        switch (c.type) {
        case Command::AddTrack: {
            if (!mixer_.findTrack(c.trackId)) {
                auto t = std::make_unique<Track>();
                t->id = c.trackId;
                if (c.aux == 1)
                    t->type = TrackType::Midi;
                else if (c.aux == 2)
                    t->type = TrackType::Group;
                else
                    t->type = TrackType::Audio;
                mixer_.tracks.push_back(std::move(t));
            }
            break;
        }
        case Command::RemoveTrack: {
            auto& v = mixer_.tracks;
            v.erase(std::remove_if(v.begin(), v.end(),
                        [&](const auto& t) { return t->id == c.trackId; }),
                    v.end());
            break;
        }
        case Command::AddClip: {
            Track* t = mixer_.findTrack(c.trackId);
            if (t && c.file) {
                Clip clip;
                clip.id = c.clipId;
                clip.file = c.file;
                clip.startFrame = c.i64;
                clip.sourceOffset = c.srcOffset;
                clip.length = c.i64b;
                clip.warp = pydaw::warp::warpMethodFromInt(c.aux);
                clip.stretchRatio = c.f;
                clip.syncWarpProcessor();
                t->clips.push_back(std::move(clip));
            }
            break;
        }
        case Command::RemoveClip: {
            Track* t = mixer_.findTrack(c.trackId);
            if (t) {
                auto& v = t->clips;
                v.erase(std::remove_if(v.begin(), v.end(),
                            [&](const Clip& cl) { return cl.id == c.clipId; }),
                        v.end());
            }
            break;
        }
        case Command::MoveClip: {
            Clip* cl = mixer_.findClip(c.trackId, c.clipId);
            if (cl) {
                cl->startFrame = c.i64;
                cl->invalidateWarpContinuity();
            }
            break;
        }
        case Command::TrimClip: {
            Clip* cl = mixer_.findClip(c.trackId, c.clipId);
            if (cl && cl->file) {
                const int64_t fileFrames = int64_t(cl->file->frameCount());
                const int64_t sourceOffset =
                    clampTimelineSourceOffset(c.srcOffset, fileFrames,
                                              cl->stretchRatio);
                const int64_t length =
                    clampTimelineLength(c.i64b, sourceOffset, fileFrames,
                                        cl->stretchRatio);
                if (length <= 0)
                    break;
                cl->startFrame = c.i64;
                cl->sourceOffset = sourceOffset;
                cl->length = length;
                cl->syncWarpProcessor();
            }
            break;
        }
        case Command::SetClipFades: {
            Clip* cl = mixer_.findClip(c.trackId, c.clipId);
            if (cl) { cl->fadeIn = c.i64; cl->fadeOut = c.i64b; }
            break;
        }
        case Command::SetClipWarp: {
            Clip* cl = mixer_.findClip(c.trackId, c.clipId);
            if (cl) {
                cl->warp = pydaw::warp::warpMethodFromInt(c.aux);
                cl->stretchRatio = c.f;
                cl->syncWarpProcessor();
            }
            break;
        }
        case Command::MoveClipToTrack: {
            Track* from = mixer_.findTrack(c.trackId);
            Track* to = mixer_.findTrack(c.aux);
            if (!from || !to || from == to) break;
            if (c.b) { // midi clip
                auto& v = from->midiClips;
                auto it = std::find_if(v.begin(), v.end(),
                    [&](const MidiClip& mc) { return mc.id == c.clipId; });
                if (it != v.end() && to->type == TrackType::Midi) {
                    MidiClip mc = std::move(*it);
                    v.erase(it);
                    mc.startFrame = c.i64;
                    to->midiClips.push_back(std::move(mc));
                }
            } else {
                auto& v = from->clips;
                auto it = std::find_if(v.begin(), v.end(),
                    [&](const Clip& cl) { return cl.id == c.clipId; });
                if (it != v.end() && to->type == TrackType::Audio) {
                    Clip cl = std::move(*it);
                    v.erase(it);
                    cl.startFrame = c.i64;
                    to->clips.push_back(std::move(cl));
                }
            }
            break;
        }
        case Command::AddMidiClip: {
            Track* t = mixer_.findTrack(c.trackId);
            if (t && !mixer_.findMidiClip(c.trackId, c.clipId)) {
                MidiClip mc;
                mc.id = c.clipId;
                mc.startFrame = c.i64;
                mc.lengthFrames = c.i64b;
                t->midiClips.push_back(std::move(mc));
            }
            break;
        }
        case Command::RemoveMidiClip: {
            Track* t = mixer_.findTrack(c.trackId);
            if (t) {
                auto& v = t->midiClips;
                v.erase(std::remove_if(v.begin(), v.end(),
                            [&](const MidiClip& mc) { return mc.id == c.clipId; }),
                        v.end());
                if (t->vstInstrument)
                    mixer_.resetTrackMidiTransport(*t,
                        playhead_.load(std::memory_order_relaxed),
                        playing_.load(std::memory_order_relaxed), true);
            }
            break;
        }
        case Command::MoveMidiClip: {
            MidiClip* mc = mixer_.findMidiClip(c.trackId, c.clipId);
            if (mc) mc->startFrame = c.i64;
            break;
        }
        case Command::ResizeMidiClip: {
            MidiClip* mc = mixer_.findMidiClip(c.trackId, c.clipId);
            if (mc) mc->lengthFrames = std::max<int64_t>(0, c.i64b);
            break;
        }
        case Command::AddNote: {
            MidiClip* mc = mixer_.findMidiClip(c.trackId, c.clipId);
            if (mc) mc->notes.push_back(c.note);
            break;
        }
        case Command::RemoveNote: {
            MidiClip* mc = mixer_.findMidiClip(c.trackId, c.clipId);
            if (mc) {
                auto& v = mc->notes;
                v.erase(std::remove_if(v.begin(), v.end(),
                            [&](const MidiNote& n) { return n.id == c.aux; }),
                        v.end());
            }
            break;
        }
        case Command::UpdateNote: {
            MidiClip* mc = mixer_.findMidiClip(c.trackId, c.clipId);
            if (mc) {
                for (auto& n : mc->notes)
                    if (n.id == c.note.id) { n = c.note; break; }
            }
            break;
        }
        case Command::SetInstrument: {
            Track* t = mixer_.findTrack(c.trackId);
            if (t) t->synth = c.synth;
            break;
        }
        case Command::PreviewNoteOn: {
            Track* t = mixer_.findTrack(c.trackId);
            if (t && t->vstInstrument)
                t->vstInstrument->queuePreviewNoteOn(c.aux, c.f);
            else
                mixer_.previewNoteOn(c.aux, c.f, t ? t->synth : SynthParams{});
            break;
        }
        case Command::PreviewNoteOff: {
            bool routed = false;
            for (auto& tr : mixer_.tracks) {
                if (tr->vstInstrument) {
                    tr->vstInstrument->queuePreviewNoteOff(c.aux);
                    routed = true;
                }
            }
            if (!routed) mixer_.previewNoteOff(c.aux);
            break;
        }
        case Command::AddEffect: {
            if (c.trackId == kMasterTrackId) {
                if (c.effect) mixer_.masterEffects.push_back(c.effect);
                break;
            }
            Track* t = mixer_.findTrack(c.trackId);
            if (t && c.effect) t->effects.push_back(c.effect);
            break;
        }
        case Command::RemoveEffect: {
            if (c.trackId == kMasterTrackId) {
                auto& v = mixer_.masterEffects;
                if (c.aux >= 0 && size_t(c.aux) < v.size())
                    v.erase(v.begin() + c.aux);
                break;
            }
            Track* t = mixer_.findTrack(c.trackId);
            if (t && c.aux >= 0 && size_t(c.aux) < t->effects.size())
                t->effects.erase(t->effects.begin() + c.aux);
            break;
        }
        case Command::SetEffectParam: {
            if (c.trackId == kMasterTrackId) {
                auto& v = mixer_.masterEffects;
                if (c.aux >= 0 && size_t(c.aux) < v.size())
                    v[size_t(c.aux)]->setParam(c.clipId, c.f);
                break;
            }
            Track* t = mixer_.findTrack(c.trackId);
            if (t && c.aux >= 0 && size_t(c.aux) < t->effects.size())
                t->effects[size_t(c.aux)]->setParam(c.clipId, c.f);
            break;
        }
        case Command::SetVstInstrument: {
            Track* t = mixer_.findTrack(c.trackId);
            if (t && c.vstInstrument) {
                t->vstInstrument = c.vstInstrument;
                t->lastMidiBlockEnd = -1;
            }
            break;
        }
        case Command::ClearVstInstrument: {
            Track* t = mixer_.findTrack(c.trackId);
            if (t) {
                t->vstInstrument.reset();
                t->lastMidiBlockEnd = -1;
            }
            break;
        }
        case Command::SetGain: {
            Track* t = mixer_.findTrack(c.trackId);
            if (t) t->gain = c.f;
            break;
        }
        case Command::SetPan: {
            Track* t = mixer_.findTrack(c.trackId);
            if (t) t->pan = c.f;
            break;
        }
        case Command::SetStereoWidth: {
            Track* t = mixer_.findTrack(c.trackId);
            if (t) t->stereoWidth = c.f;
            break;
        }
        case Command::SetMute: {
            Track* t = mixer_.findTrack(c.trackId);
            if (t) t->mute = c.b;
            break;
        }
        case Command::SetSolo: {
            Track* t = mixer_.findTrack(c.trackId);
            if (t) t->solo = c.b;
            break;
        }
        case Command::SetTrackGroup: {
            Track* t = mixer_.findTrack(c.trackId);
            if (t && t->type != TrackType::Group)
                t->groupId = c.aux;
            break;
        }
        case Command::SetMasterGain:
            mixer_.masterGain.store(c.f, std::memory_order_relaxed);
            break;
        case Command::SetTempo:
            mixer_.bpm = c.d;
            break;
        case Command::SetMetronome:
            mixer_.metronomeOn = c.b;
            break;
        case Command::SetPdc:
            mixer_.pdcEnabled = c.b;
            break;
        case Command::SetPdcMode:
            mixer_.pdcMode = c.aux;
            break;
        case Command::SetLoop:
            loopStart_ = c.i64;
            loopEnd_ = c.i64b;
            loopOn_ = c.b && c.i64b > c.i64;
            break;
        case Command::SetAutomationLane: {
            if (!c.autoLane) break;
            std::vector<AutomationLane>* lanesPtr = nullptr;
            if (c.trackId == kMasterTrackId) {
                lanesPtr = &mixer_.masterAutoLanes;
            } else {
                Track* t = mixer_.findTrack(c.trackId);
                if (!t) break;
                lanesPtr = &t->autoLanes;
            }
            auto& lanes = *lanesPtr;
            const int id = c.autoLane->laneId;
            auto it = std::find_if(lanes.begin(), lanes.end(),
                                   [id](const AutomationLane& l) {
                                       return l.laneId == id;
                                   });
            if (it != lanes.end())
                *it = *c.autoLane;
            else
                lanes.push_back(*c.autoLane);
            break;
        }
        case Command::RemoveAutomationLane: {
            std::vector<AutomationLane>* lanesPtr = nullptr;
            if (c.trackId == kMasterTrackId) {
                lanesPtr = &mixer_.masterAutoLanes;
            } else {
                Track* t = mixer_.findTrack(c.trackId);
                if (!t) break;
                lanesPtr = &t->autoLanes;
            }
            auto& lanes = *lanesPtr;
            lanes.erase(std::remove_if(lanes.begin(), lanes.end(),
                                         [&](const AutomationLane& l) {
                                             return l.laneId == c.aux;
                                         }),
                          lanes.end());
            break;
        }
        case Command::Play: {
            playing_.store(true, std::memory_order_relaxed);
            const int64_t ph = playhead_.load(std::memory_order_relaxed);
            for (auto& tr : mixer_.tracks)
                mixer_.resetTrackMidiTransport(*tr, ph, true, false);
            break;
        }
        case Command::Stop: {
            playing_.store(false, std::memory_order_relaxed);
            mixer_.invalidateWarpContinuity();
            const int64_t ph = playhead_.load(std::memory_order_relaxed);
            for (auto& tr : mixer_.tracks)
                mixer_.resetTrackMidiTransport(*tr, ph, false, true);
            mixer_.clearPeaks();
            break;
        }
        case Command::Seek: {
            playhead_.store(c.i64, std::memory_order_relaxed);
            mixer_.invalidateWarpContinuity();
            const bool playing = playing_.load(std::memory_order_relaxed);
            for (auto& tr : mixer_.tracks)
                mixer_.resetTrackMidiTransport(*tr, c.i64, playing, true);
            break;
        }
        case Command::ClearAll:
            mixer_.tracks.clear();
            mixer_.masterEffects.clear();
            mixer_.masterAutoLanes.clear();
            mixer_.clearPeaks();
            playing_.store(false, std::memory_order_relaxed);
            playhead_.store(0, std::memory_order_relaxed);
            // A cleared project must not keep the previous loop region:
            // otherwise the next play() wraps at a stale loop end.
            loopStart_ = 0;
            loopEnd_ = 0;
            loopOn_ = false;
            break;
        }
    }
}

// VST3 helpers / API ---------------------------------------------------------

void AudioEngine::flushVstGraveyard() {
    vstGraveyard_.erase(
        std::remove_if(vstGraveyard_.begin(), vstGraveyard_.end(),
                       [](const std::shared_ptr<VstPlugin>& p) {
                           if (!p || p.use_count() > 1) return false;
                           // Replacing a VST while the old instance is still
                           // loading would join the load thread on the UI
                           // thread; defer teardown until loading finishes.
                           if (p->isLoading()) return false;
                           return true;
                       }),
        vstGraveyard_.end());
}

std::shared_ptr<VstPlugin> AudioEngine::findVst(int trackId, int slot) {
    flushVstGraveyard();
    auto it = vstRegistry_.find({trackId, slot});
    return it != vstRegistry_.end() ? it->second : nullptr;
}

int AudioEngine::nextEffectSlot(int trackId) {
    return trackEffectSlots_.count(trackId) ? trackEffectSlots_[trackId] : 0;
}

void AudioEngine::shiftVstFxSlots(int trackId, int removedSlot) {
    std::map<std::pair<int, int>, std::shared_ptr<VstPlugin>> next;
    for (auto& [k, v] : vstRegistry_) {
        if (k.first != trackId) {
            next[k] = v;
            continue;
        }
        if (k.second < 0) {
            next[k] = v;
            continue;
        }
        if (k.second < removedSlot)
            next[k] = v;
        else if (k.second > removedSlot)
            next[{trackId, k.second - 1}] = v;
        else
            vstGraveyard_.push_back(v);
    }
    vstRegistry_ = std::move(next);
}

void AudioEngine::removeVstKeysForTrack(int trackId) {
    for (auto it = vstRegistry_.begin(); it != vstRegistry_.end(); ) {
        if (it->first.first == trackId) {
            vstGraveyard_.push_back(it->second);
            it = vstRegistry_.erase(it);
        } else {
            ++it;
        }
    }
    flushVstGraveyard();
}

std::vector<VstPluginInfo> AudioEngine::scanVstPlugins(
    const std::vector<std::string>& dirs) {
    return ::scanVstPlugins(dirs);
}

bool AudioEngine::addVstEffect(int trackId, const std::string& path,
                               const std::string& uid) {
    if (!std::filesystem::exists(std::filesystem::u8path(path)))
        return false;
    auto plug = std::getenv("PYDAW_VST_IN_PROCESS")
        ? createVstPlugin(path, uid, false, sampleRate_)
        : createVstBridgePlugin(path, uid, false, sampleRate_);
    if (!plug) return false;
    const int slot = nextEffectSlot(trackId);
    plug->prepare(sampleRate_);
    Command c;
    c.type = Command::AddEffect;
    c.trackId = trackId;
    c.effect = plug;
    pushCommand(c);
    vstRegistry_[{trackId, slot}] = plug;
    trackEffectSlots_[trackId] = slot + 1;
    return true;
}

bool AudioEngine::setVstInstrument(int trackId, const std::string& path,
                                   const std::string& uid) {
    auto it = vstRegistry_.find({trackId, -1});
    if (it != vstRegistry_.end()) {
        vstGraveyard_.push_back(it->second);
        vstRegistry_.erase(it);
    }
    if (!std::filesystem::exists(std::filesystem::u8path(path)))
        return false;
    auto plug = std::getenv("PYDAW_VST_IN_PROCESS")
        ? createVstPlugin(path, uid, true, sampleRate_)
        : createVstBridgePlugin(path, uid, true, sampleRate_);
    if (!plug) return false;
    plug->prepare(sampleRate_);
    Command c;
    c.type = Command::SetVstInstrument;
    c.trackId = trackId;
    c.vstInstrument = plug;
    pushCommand(c);
    vstRegistry_[{trackId, -1}] = plug;
    return true;
}

void AudioEngine::clearVstInstrument(int trackId) {
    auto it = vstRegistry_.find({trackId, -1});
    if (it != vstRegistry_.end()) {
        vstGraveyard_.push_back(it->second);
        vstRegistry_.erase(it);
    }
    Command c;
    c.type = Command::ClearVstInstrument;
    c.trackId = trackId;
    pushCommand(c);
}

bool AudioEngine::vstHasEditor(int trackId, int slot) {
    auto p = findVst(trackId, slot);
    return p && p->hasEditor();
}

bool AudioEngine::vstOpenEditor(int trackId, int slot, uintptr_t hwnd) {
    auto p = findVst(trackId, slot);
    return p && p->openEditor(reinterpret_cast<void*>(hwnd));
}

void AudioEngine::vstCloseEditor(int trackId, int slot) {
    if (shuttingDown_.load(std::memory_order_acquire)) return;
    if (auto p = findVst(trackId, slot)) p->closeEditor();
}

std::pair<int, int> AudioEngine::vstEditorSize(int trackId, int slot) {
    auto p = findVst(trackId, slot);
    return p ? p->editorSize() : std::make_pair(0, 0);
}

bool AudioEngine::vstCheckResize(int trackId, int slot, int& w, int& h) {
    auto p = findVst(trackId, slot);
    return p && p->checkSizeChanged(w, h);
}

bool AudioEngine::vstEditorCanResize(int trackId, int slot) {
    auto p = findVst(trackId, slot);
    return p && p->editorCanResize();
}

bool AudioEngine::vstResizeEditor(int trackId, int slot, int w, int h) {
    auto p = findVst(trackId, slot);
    return p && p->resizeEditor(w, h);
}

bool AudioEngine::vstSetContentScale(int trackId, int slot, float scale) {
    auto p = findVst(trackId, slot);
    return p && p->setContentScale(scale);
}

std::string AudioEngine::vstDiagnostic(int trackId, int slot) {
    auto p = findVst(trackId, slot);
    return p ? p->diagnostic() : std::string{"VST instance not found"};
}

int AudioEngine::vstParamCount(int trackId, int slot) {
    auto p = findVst(trackId, slot);
    return p ? p->paramCount() : 0;
}

std::vector<std::tuple<int, std::string, float>> AudioEngine::vstParams(
    int trackId, int slot) {
    std::vector<std::tuple<int, std::string, float>> out;
    auto p = findVst(trackId, slot);
    if (!p) return out;
    const int n = p->paramCount();
    out.reserve(size_t(n));
    for (int i = 0; i < n; ++i)
        out.push_back(p->getParamInfo(i));
    return out;
}

void AudioEngine::vstSetParam(int trackId, int slot, int paramIndex,
                              float normalized, bool markUserEdit) {
    if (auto p = findVst(trackId, slot))
        p->setParamNormalizedByIndex(paramIndex, normalized, markUserEdit);
}

float AudioEngine::vstGetParam(int trackId, int slot, int paramIndex) {
    auto p = findVst(trackId, slot);
    return p ? p->getParamNormalized(paramIndex) : 0.f;
}

float AudioEngine::vstGetParamDefault(int trackId, int slot, int paramIndex) {
    auto p = findVst(trackId, slot);
    return p ? p->getParamDefault(paramIndex) : 0.f;
}

void AudioEngine::vstSyncControllers() {
    for (auto& entry : vstRegistry_)
        if (entry.second)
            entry.second->syncControllerParams();
}

bool AudioEngine::vstRefreshChangedParams() {
    bool changed = false;
    for (auto& entry : vstRegistry_)
        if (entry.second && entry.second->refreshParamsIfChanged())
            changed = true;
    return changed;
}

bool AudioEngine::vstIsLoading(int trackId, int slot) {
    auto p = findVst(trackId, slot);
    return p && p->isLoading();
}

std::tuple<int, int, int> AudioEngine::vstConsumeLastTweak() {
    std::tuple<int, int, int> found{0, 0, -1};
    // Drain every plugin so stale edits don't linger; report the last hit.
    for (auto& entry : vstRegistry_) {
        if (!entry.second) continue;
        const int idx = entry.second->consumeLastUserEdit();
        if (idx >= 0)
            found = {entry.first.first, entry.first.second, idx};
    }
    return found;
}

std::string AudioEngine::vstGetState(int trackId, int slot) {
    auto p = findVst(trackId, slot);
    return p ? p->getState() : std::string{};
}

bool AudioEngine::vstSetState(int trackId, int slot, const std::string& data) {
    auto p = findVst(trackId, slot);
    return p && p->setState(data);
}

int AudioEngine::process(float* out, uint32_t frames) {
    if (shuttingDown_.load(std::memory_order_acquire)) {
        std::memset(out, 0, sizeof(float) * frames * 2);
        return 0;
    }
    // While an offline export renders on another thread, keep the device
    // stream alive but silent: touching mixer_ from two threads corrupts the
    // render and can crackle/drop out the driver.
    if (exporting_.load(std::memory_order_acquire)) {
        std::memset(out, 0, sizeof(float) * frames * 2);
        return 0;
    }

    std::lock_guard<std::mutex> lock(mixMutex_);
    applyCommands();

    const bool playing = playing_.load(std::memory_order_relaxed);
    if (playing) {
        int64_t pos = playhead_.load(std::memory_order_relaxed);

        if (loopOn_) {
            // Render in segments, wrapping back to loopStart_ at loopEnd_.
            // The seam loopEnd -> loopStart is a hard discontinuity for
            // both audio clips (waveform jump) and synth voices (chase
            // retrigger). Positional declick: fade to zero over ~5 ms
            // before loopEnd and back in over ~5 ms after loopStart
            // with a smoothstep shape.
            const int64_t fade = std::min<int64_t>(
                int64_t(sampleRate_) / 200, // 5 ms
                (loopEnd_ - loopStart_) / 2);
            auto smooth = [](float t) { return t * t * (3.0f - 2.0f * t); };
            uint32_t done = 0;
            while (done < frames) {
                if (pos >= loopEnd_) {
                    mixer_.onLoopWrap(loopStart_);
                    pos = loopStart_;
                }
                const uint32_t n = uint32_t(std::min<int64_t>(
                    frames - done, loopEnd_ - pos));
                float* seg = out + size_t(done) * 2;
                mixer_.mix(seg, n, pos);
                mixer_.renderMetronome(seg, n, pos);
                if (fade > 0) {
                    for (uint32_t i = 0; i < n; ++i) {
                        const int64_t p = pos + int64_t(i);
                        float g = 1.0f;
                        if (p >= loopEnd_ - fade)
                            g = smooth(float(loopEnd_ - 1 - p)
                                       / float(fade));
                        else if (p < loopStart_ + fade && p >= loopStart_)
                            g = smooth(float(p - loopStart_) / float(fade));
                        if (g < 1.0f) {
                            seg[size_t(i) * 2] *= g;
                            seg[size_t(i) * 2 + 1] *= g;
                        }
                    }
                }
                pos += n;
                done += n;
            }
            if (pos >= loopEnd_) pos = loopStart_;
            playhead_.store(pos, std::memory_order_relaxed);
        } else {
            mixer_.mix(out, frames, pos);
            mixer_.renderMetronome(out, frames, pos);
            playhead_.store(pos + int64_t(frames), std::memory_order_relaxed);
            // No auto-stop: like FL/Ableton, the transport keeps running
            // past the end of content until the user stops it.
        }
    } else {
        std::memset(out, 0, sizeof(float) * frames * 2);
        // VST instruments must keep processing while stopped so piano-roll
        // preview notes and release tails are audible.
        mixer_.renderVstIdle(out, frames,
                             playhead_.load(std::memory_order_relaxed));
        mixer_.applyMaster(out, frames);
    }

    // Piano-roll note auditioning works even while stopped.
    if (mixer_.hasPreviewVoices())
        mixer_.renderPreview(out, frames);
    return 0;
}

// Offline export -------------------------------------------------------------

bool AudioEngine::exportWav(const std::string& path, int64_t startFrame,
                            int64_t endFrame) {
    exportCancel_.store(false, std::memory_order_relaxed);
    exportCancelled_.store(false, std::memory_order_relaxed);
    exportProgress_.store(0.0, std::memory_order_relaxed);

    // Offline render runs on the caller's thread and shares mixer_ state with
    // the realtime callback. Do NOT stop/restart the device stream here:
    // reopening the device afterwards can fail (e.g. FlexASIO re-initializes
    // WASAPI and hits AUDCLNT_E_DEVICE_IN_USE, 0x8889000A). Instead raise an
    // atomic flag that makes the callback output silence without touching the
    // mixer, then wait one buffer period so any in-flight callback finishes.
    exporting_.store(true, std::memory_order_release);
    if (dac_ && dac_->isStreamRunning() && sampleRate_ > 0) {
        const auto period = std::chrono::milliseconds(
            10 + (1000ull * bufferFrames_) / sampleRate_);
        std::this_thread::sleep_for(period);
    }

    struct ExportGuard {
        std::atomic<bool>& exporting;
        std::atomic<double>& progress;
        bool ok = false;
        ExportGuard(std::atomic<bool>& e, std::atomic<double>& p)
            : exporting(e), progress(p) {}
        ~ExportGuard() {
            progress.store(1.0, std::memory_order_relaxed);
            exporting.store(false, std::memory_order_release);
        }
    } guard(exporting_, exportProgress_);

    std::lock_guard<std::mutex> lock(mixMutex_);
    applyCommands(); // drain queued model changes before offline render

    const int64_t total = mixer_.totalLength();
    const int64_t from = std::max<int64_t>(0, startFrame);
    const int64_t to = endFrame < 0 ? total : std::min(endFrame, total);
    if (to <= from)
        return guard.ok = false;

    drwav_data_format fmt{};
    fmt.container = drwav_container_riff;
    fmt.format = DR_WAVE_FORMAT_IEEE_FLOAT;
    fmt.channels = 2;
    fmt.sampleRate = sampleRate_;
    fmt.bitsPerSample = 32;

    drwav wav;
#ifdef _WIN32
    // UTF-8 path -> wide: plain drwav_init_file_write uses ANSI fopen
    // and fails on non-ASCII export folders (e.g. "Документы").
    const bool wavOk = drwav_init_file_write_w(
        &wav, std::filesystem::u8path(path).wstring().c_str(), &fmt,
        nullptr);
#else
    const bool wavOk = drwav_init_file_write(&wav, path.c_str(), &fmt,
                                             nullptr);
#endif
    if (!wavOk)
        return guard.ok = false;

    const uint32_t block = 4096;
    std::vector<float> buf(block * 2);
    const double span = double(to - from);
    int64_t pos = from;
    while (pos < to) {
        if (exportCancel_.load(std::memory_order_relaxed)) {
            drwav_uninit(&wav);
            std::error_code rmEc;
            std::filesystem::remove(std::filesystem::u8path(path), rmEc);
            exportCancelled_.store(true, std::memory_order_relaxed);
            return guard.ok = false;
        }
        const uint32_t n = uint32_t(std::min<int64_t>(block, to - pos));
        mixer_.mix(buf.data(), n, pos);
        drwav_write_pcm_frames(&wav, n, buf.data());
        pos += n;
        exportProgress_.store(double(pos - from) / span,
                              std::memory_order_relaxed);
    }
    drwav_uninit(&wav);
    return guard.ok = true;
}
