#include "mixer.h"
#include "render_pool.h"
#include "vst_host.h"

#include <algorithm>
#include <cmath>
#include <cstring>

namespace {

// Curve shape of the segment leaving point `a` (a.shape):
// 0 linear, 1 step (hold), 2 exponential ease-in, 3 bezier (smoothstep).
float shapeCurve(int shape, float t, float amount) {
    amount = std::clamp(amount, -1.f, 1.f);
    float shaped = t;
    switch (shape) {
    case 1:
        shaped = amount >= 0.f ? 0.f : 1.f;
        break;
    case 2:
        shaped = amount >= 0.f
            ? t * t * t
            : 1.f - (1.f - t) * (1.f - t) * (1.f - t);
        break;
    case 3: {
        const float smooth = t * t * (3.f - 2.f * t);
        shaped = amount >= 0.f ? smooth : 2.f * t - smooth;
        break;
    }
    default: return t;
    }
    return t + std::abs(amount) * (shaped - t);
}

float evalAutomationLane(const std::vector<AutomationPoint>& points,
                         int64_t frame) {
    if (points.empty()) return 0.f;
    if (frame <= points.front().frame) return points.front().value;
    if (frame >= points.back().frame) return points.back().value;
    const auto it = std::upper_bound(
        points.begin(), points.end(), frame,
        [](int64_t f, const AutomationPoint& p) { return f < p.frame; });
    const AutomationPoint& b = *it;
    const AutomationPoint& a = *(it - 1);
    if (b.frame <= a.frame) return a.value;
    float t = float(frame - a.frame) / float(b.frame - a.frame);
    t = shapeCurve(a.shape, t, a.curveAmount);
    return a.value + (b.value - a.value) * t;
}

bool trackUsesVst(const Track& t) {
    if (t.vstInstrument) return true;
    for (const auto& fx : t.effects) {
        if (std::dynamic_pointer_cast<VstPlugin>(fx))
            return true;
    }
    return false;
}

bool groupUsesVst(const Track& g) {
    for (const auto& fx : g.effects) {
        if (std::dynamic_pointer_cast<VstPlugin>(fx))
            return true;
    }
    return false;
}

void applyAutomationLanes(Track& t, int64_t playhead) {
    for (const auto& lane : t.autoLanes) {
        if (lane.points.empty()) continue;
        const float val = evalAutomationLane(lane.points, playhead);
        switch (lane.target) {
        case 0:
        case 1:
            break; // volume/pan applied after effects
        case 2:
            if (lane.slot >= 0 && size_t(lane.slot) < t.effects.size())
                t.effects[size_t(lane.slot)]->setParam(lane.param, val);
            break;
        case 3: {
            if (lane.slot < 0 || size_t(lane.slot) >= t.effects.size())
                break;
            auto vst = std::dynamic_pointer_cast<VstPlugin>(
                t.effects[size_t(lane.slot)]);
            if (vst)
                vst->setParamFromAudioThread(lane.param, val);
            break;
        }
        case 4:
            if (t.vstInstrument)
                t.vstInstrument->setParamFromAudioThread(lane.param, val);
            break;
        default:
            break;
        }
    }
}

float automatedGain(const Track& t, int64_t playhead) {
    float g = t.gain;
    for (const auto& lane : t.autoLanes) {
        if (lane.target == 0 && !lane.points.empty())
            g = evalAutomationLane(lane.points, playhead);
    }
    return g;
}

float automatedPan(const Track& t, int64_t playhead) {
    float p = t.pan;
    for (const auto& lane : t.autoLanes) {
        if (lane.target == 1 && !lane.points.empty())
            p = evalAutomationLane(lane.points, playhead);
    }
    return p;
}

float automatedWidth(const Track& t, int64_t playhead) {
    float w = t.stereoWidth;
    for (const auto& lane : t.autoLanes) {
        if (lane.target == 5 && !lane.points.empty())
            w = evalAutomationLane(lane.points, playhead);
    }
    return w;
}

} // namespace

Track* Mixer::findTrack(int trackId) {
    for (auto& t : tracks)
        if (t->id == trackId) return t.get();
    return nullptr;
}

Clip* Mixer::findClip(int trackId, int clipId, Track** outTrack) {
    Track* t = findTrack(trackId);
    if (!t) return nullptr;
    if (outTrack) *outTrack = t;
    for (auto& c : t->clips)
        if (c.id == clipId) return &c;
    return nullptr;
}

MidiClip* Mixer::findMidiClip(int trackId, int clipId, Track** outTrack) {
    Track* t = findTrack(trackId);
    if (!t) return nullptr;
    if (outTrack) *outTrack = t;
    for (auto& c : t->midiClips)
        if (c.id == clipId) return &c;
    return nullptr;
}

int64_t Mixer::totalLength() const {
    int64_t maxEnd = 0;
    for (const auto& t : tracks) {
        for (const auto& c : t->clips)
            maxEnd = std::max(maxEnd, c.endFrame());
        for (const auto& c : t->midiClips)
            maxEnd = std::max(maxEnd, c.endFrame());
    }
    return maxEnd;
}

void Clip::syncWarpProcessor() {
    const float ratio = stretchRatio;
    const int64_t srcSpan = std::max(
        int64_t(1),
        int64_t(std::llround(double(length) / std::max(double(ratio), 1e-9))));
    warpProc.configure(warp, ratio, srcSpan);
    lastMixPlayhead = -1;
}

void Mixer::invalidateWarpContinuity() {
    for (auto& t : tracks) {
        if (!t) continue;
        for (auto& c : t->clips)
            c.invalidateWarpContinuity();
    }
}

void Mixer::onLoopWrap(int64_t loopStart) {
    invalidateWarpContinuity();
    for (auto& t : tracks) {
        if (!t) continue;
        t->lastMidiBlockEnd = -1;
        if (t->vstInstrument) {
            t->vstInstrument->onTransportJump();
            t->vstInstrument->setTransport(bpm, true, loopStart);
        }
    }
}

void Mixer::resetTrackMidiTransport(Track& t, int64_t playhead, bool playing,
                                    bool killNotes) {
    t.lastMidiBlockEnd = -1;
    if (!t.vstInstrument) return;
    t.vstInstrument->setTransport(bpm, playing, playhead);
    if (killNotes)
        t.vstInstrument->allNotesOff();
}

namespace {

using pydaw::warp::ratioIsUnity;

float fadeEnv(const Clip& c, int64_t clipStart, int64_t clipEnd, int64_t pos) {
    float env = 1.f;
    const int64_t rel = pos - clipStart;
    if (c.fadeIn > 0 && rel < c.fadeIn)
        env *= float(rel) / float(c.fadeIn);
    const int64_t fromEnd = clipEnd - pos;
    if (c.fadeOut > 0 && fromEnd < c.fadeOut)
        env *= float(fromEnd) / float(c.fadeOut);
    return env;
}

}  // namespace

void Mixer::mixAudioTrack(Track& t, float* buf, uint32_t frames,
                          int64_t playhead) {
    const int64_t blockEnd = playhead + int64_t(frames);
    auto& scratch = t.warpScratch;
    scratch.reserve(size_t(frames) * 2);

    for (Clip& c : t.clips) {
        if (!c.file) continue;
        const int64_t clipStart = c.startFrame;
        const int64_t clipEnd = c.endFrame();
        if (clipEnd <= playhead || clipStart >= blockEnd) continue;

        int64_t from = std::max(clipStart, playhead);
        int64_t to = std::min(clipEnd, blockEnd);
        const float* src = c.file->data();
        const int64_t fileFrames = int64_t(c.file->frameCount());
        const float ratio = c.stretchRatio;

        if (ratioIsUnity(ratio)) {
            from = std::max(from, clipStart - c.sourceOffset);
            to = std::min(to, clipStart - c.sourceOffset + fileFrames);
            if (to <= from) continue;

            const int64_t fadeInEnd =
                c.fadeIn > 0 ? std::min(to, clipStart + c.fadeIn) : from;
            const int64_t fadeOutStart =
                c.fadeOut > 0 ? std::max(from, clipEnd - c.fadeOut) : to;
            const int64_t plainFrom = std::max(from, fadeInEnd);
            const int64_t plainTo =
                std::max(plainFrom, std::min(to, fadeOutStart));

            auto addWithEnv = [&](int64_t a, int64_t b) {
                for (int64_t pos = a; pos < b; ++pos) {
                    const float env = fadeEnv(c, clipStart, clipEnd, pos);
                    const int64_t dst = pos - playhead;
                    const int64_t rel = pos - clipStart;
                    const int64_t sf = c.sourceOffset + rel;
                    if (sf < 0 || sf >= fileFrames) continue;
                    buf[dst * 2] += src[sf * 2] * env;
                    buf[dst * 2 + 1] += src[sf * 2 + 1] * env;
                }
            };

            addWithEnv(from, plainFrom);

            if (plainTo > plainFrom) {
                float* dst = buf + (plainFrom - playhead) * 2;
                const float* s =
                    src + (c.sourceOffset + (plainFrom - clipStart)) * 2;
                const int64_t n = (plainTo - plainFrom) * 2;
                for (int64_t i = 0; i < n; ++i)
                    dst[i] += s[i];
            }

            addWithEnv(plainTo, to);
            continue;
        }

        // Realtime warp path (timeline coords; source via stretchRatio).
        const int64_t rel0 = from - clipStart;
        if (c.lastMixPlayhead < 0
            || (from != c.lastMixPlayhead + 1
                && from != c.lastMixPlayhead))
            c.warpProc.resetToTimelineRel(rel0);
        c.lastMixPlayhead = to - 1;

        auto mixWarpSegment = [&](int64_t a, int64_t b) {
            if (b <= a) return;
            const int outN = int(b - a);
            scratch.assign(size_t(outN) * 2, 0.f);
            const int64_t relStart = a - clipStart;
            c.warpProc.processBlock(src, fileFrames,
                                    double(c.sourceOffset), relStart, outN,
                                    scratch.data(), 0);
            for (int i = 0; i < outN; ++i) {
                const int64_t pos = a + i;
                const float env = fadeEnv(c, clipStart, clipEnd, pos);
                const int64_t dst = pos - playhead;
                buf[dst * 2] += scratch[size_t(i) * 2] * env;
                buf[dst * 2 + 1] += scratch[size_t(i) * 2 + 1] * env;
            }
        };

        const int64_t fadeInEnd =
            c.fadeIn > 0 ? std::min(to, clipStart + c.fadeIn) : from;
        const int64_t fadeOutStart =
            c.fadeOut > 0 ? std::max(from, clipEnd - c.fadeOut) : to;
        const int64_t plainFrom = std::max(from, fadeInEnd);
        const int64_t plainTo =
            std::max(plainFrom, std::min(to, fadeOutStart));

        mixWarpSegment(from, plainFrom);
        mixWarpSegment(plainFrom, plainTo);
        mixWarpSegment(plainTo, to);
    }
}

void Mixer::mixMidiTrack(Track& t, float* buf, uint32_t frames,
                         int64_t playhead) {
    const int64_t blockEnd = playhead + int64_t(frames);

    if (t.vstInstrument) {
        // discontinuity: seek / loop wrap — kill stale voices first.
        // chaseNotes: also true on the first block after play/loop reset
        // (lastMidiBlockEnd < 0), e.g. PDC look-ahead skipping note-ons.
        const bool discontinuity = (t.lastMidiBlockEnd >= 0
                                    && playhead != t.lastMidiBlockEnd);
        const bool chaseNotes = discontinuity || t.lastMidiBlockEnd < 0;
        if (discontinuity)
            t.vstInstrument->onTransportJump();
        t.vstInstrument->setTransport(bpm, true, playhead);

        // Per-track scratch: clear() keeps capacity, so after warm-up
        // this loop performs no heap allocation on the audio thread.
        std::vector<VstHostEvent>& events = t.midiEvents;
        events.clear();
        if (events.capacity() < 64) events.reserve(64);

        for (const auto& clip : t.midiClips) {
            if (clip.endFrame() <= playhead || clip.startFrame >= blockEnd)
                continue;

            for (const auto& n : clip.notes) {
                if (n.startFrame >= clip.lengthFrames) continue;
                const int64_t len =
                    std::min(n.lengthFrames, clip.lengthFrames - n.startFrame);
                if (len <= 0) continue;

                const int64_t noteStart = clip.startFrame + n.startFrame;
                const int64_t noteEnd = noteStart + len;

                if (noteStart >= playhead && noteStart < blockEnd) {
                    VstHostEvent ev;
                    ev.type = VstHostEvent::NoteOn;
                    ev.pitch = n.pitch;
                    ev.velocity = n.velocity;
                    ev.sampleOffset = int32_t(noteStart - playhead);
                    events.push_back(ev);
                } else if (chaseNotes && noteStart < playhead
                           && noteEnd > playhead) {
                    // Note chasing: the note started before this block
                    // (start of playback, seek into the note, or the
                    // note-on fell inside the PDC look-ahead window).
                    // Retrigger it at the block start so it sounds.
                    VstHostEvent ev;
                    ev.type = VstHostEvent::NoteOn;
                    ev.pitch = n.pitch;
                    ev.velocity = n.velocity;
                    ev.sampleOffset = 0;
                    events.push_back(ev);
                }
                if (noteEnd > playhead && noteEnd <= blockEnd) {
                    VstHostEvent ev;
                    ev.type = VstHostEvent::NoteOff;
                    ev.pitch = n.pitch;
                    // Clamp into the block: offset == frames is outside
                    // the valid VST3 range [0, frames) and some plugins
                    // drop such events, leaving the note hanging - the
                    // next same-pitch note-on then voice-steals with a
                    // soft ramp ("faded" first note of the next clip).
                    ev.sampleOffset = int32_t(std::min<int64_t>(
                        noteEnd - playhead, int64_t(frames) - 1));
                    events.push_back(ev);
                }
            }
        }

        // Deliver note-offs before note-ons at the same sample offset:
        // back-to-back clips ending/starting the same pitch on the same
        // frame must release the old voice before retriggering it, or
        // the off kills the fresh note / the synth glides instead of
        // restarting the envelope. In-place sort: no allocation.
        std::sort(events.begin(), events.end(),
                  [](const VstHostEvent& a, const VstHostEvent& b) {
                      if (a.sampleOffset != b.sampleOffset)
                          return a.sampleOffset < b.sampleOffset;
                      if (a.type != b.type)
                          return a.type == VstHostEvent::NoteOff;
                      return a.pitch < b.pitch;
                  });

        t.vstInstrument->processWithEvents(buf, frames, events);
        t.lastMidiBlockEnd = blockEnd;
        return;
    }

    for (const auto& clip : t.midiClips) {
        // Release tails may ring past the clip end; use a generous bound.
        const int64_t tail = int64_t(t.synth.release * float(sampleRate)) + 1;
        if (clip.endFrame() + tail <= playhead || clip.startFrame >= blockEnd)
            continue;

        for (const auto& n : clip.notes) {
            // Clamp the note to the clip bounds.
            if (n.startFrame >= clip.lengthFrames) continue;
            const int64_t len =
                std::min(n.lengthFrames, clip.lengthFrames - n.startFrame);
            if (len <= 0) continue;

            const int64_t noteStart = clip.startFrame + n.startFrame;
            const int64_t noteEnd =
                noteStart + synth::noteTailFrames(t.synth, len, sampleRate);
            if (noteEnd <= playhead || noteStart >= blockEnd) continue;

            const int64_t age = playhead - noteStart; // may be negative
            synth::renderNote(t.synth, n.pitch, n.velocity, age, len,
                              sampleRate, buf, frames);
        }
    }
}

void Mixer::prepareGroupBuffers(uint32_t frames) {
    groupTrackIds_.clear();
    for (const auto& t : tracks)
        if (t->type == TrackType::Group)
            groupTrackIds_.push_back(t->id);

    const size_t bufSize = size_t(frames) * 2;
    const size_t total = groupTrackIds_.size() * bufSize;
    if (groupBufs_.size() < total)
        groupBufs_.resize(total);
    if (total > 0)
        std::memset(groupBufs_.data(), 0, total * sizeof(float));
}

float* Mixer::groupBuffer(int groupId, uint32_t frames) {
    const size_t bufSize = size_t(frames) * 2;
    for (size_t i = 0; i < groupTrackIds_.size(); ++i)
        if (groupTrackIds_[i] == groupId)
            return groupBufs_.data() + i * bufSize;
    return nullptr;
}

bool Mixer::groupHasSoloChild(int groupId) const {
    for (const auto& t : tracks)
        if (t->groupId == groupId && t->solo) return true;
    return false;
}

bool Mixer::trackSoloActive(const Track& t) {
    // A track counts as soloed if it is solo itself, or its parent
    // group is solo (soloing a group must keep its children audible).
    if (t.solo) return true;
    if (t.groupId != 0) {
        Track* g = findTrack(t.groupId);
        if (g && g->type == TrackType::Group && g->solo) return true;
    }
    return false;
}

bool Mixer::isGroupAudible(const Track& group, bool anySolo) const {
    if (group.mute) return false;
    if (!anySolo) return true;
    if (group.solo) return true;
    return groupHasSoloChild(group.id);
}

void Mixer::applyStereoWidth(float width, float* buf, uint32_t frames) {
    // Mid/side width control: width 0 = mono, 1 = unchanged, 2 = extra wide.
    width = std::max(0.0f, std::min(2.0f, width));
    if (std::fabs(width - 1.0f) < 1e-4f) return;
    for (uint32_t i = 0; i < frames; ++i) {
        const float l = buf[i * 2];
        const float r = buf[i * 2 + 1];
        const float m = (l + r) * 0.5f;
        const float s = (l - r) * 0.5f * width;
        buf[i * 2] = m + s;
        buf[i * 2 + 1] = m - s;
    }
}

void Mixer::applyGainPan(float gain, float pan, float* buf, uint32_t frames,
                         float& pkL, float& pkR) {
    const float gl = gain * std::cos((pan + 1.0f) * 0.25f * 3.14159265f);
    const float gr = gain * std::sin((pan + 1.0f) * 0.25f * 3.14159265f);
    pkL = 0.0f;
    pkR = 0.0f;
    for (uint32_t i = 0; i < frames; ++i) {
        const float l = buf[i * 2] * gl;
        const float r = buf[i * 2 + 1] * gr;
        buf[i * 2] = l;
        buf[i * 2 + 1] = r;
        pkL = std::max(pkL, std::fabs(l));
        pkR = std::max(pkR, std::fabs(r));
    }
}

void Mixer::applyGainPanBus(float gain, float pan, float* buf, uint32_t frames,
                            float& pkL, float& pkR) {
    const float gl = gain * (pan <= 0.f ? 1.f : 1.f - pan);
    const float gr = gain * (pan >= 0.f ? 1.f : 1.f + pan);
    pkL = 0.0f;
    pkR = 0.0f;
    for (uint32_t i = 0; i < frames; ++i) {
        const float l = buf[i * 2] * gl;
        const float r = buf[i * 2 + 1] * gr;
        buf[i * 2] = l;
        buf[i * 2 + 1] = r;
        pkL = std::max(pkL, std::fabs(l));
        pkR = std::max(pkR, std::fabs(r));
    }
}

uint32_t Mixer::chainLatency(const std::vector<EffectPtr>& effects) {
    uint32_t total = 0;
    for (const auto& fx : effects)
        if (fx) total += fx->latencySamples();
    return total;
}

int64_t Mixer::trackPathLatency(const Track& t) {
    uint32_t lat = chainLatency(t.effects);
    if (t.vstInstrument) lat += t.vstInstrument->latencySamples();
    if (t.groupId != 0) {
        for (const auto& g : tracks) {
            if (g->id == t.groupId && g->type == TrackType::Group) {
                lat += chainLatency(g->effects);
                break;
            }
        }
    }
    return int64_t(lat);
}

int64_t Mixer::maxPathLatency() {
    int64_t maxLat = 0;
    for (const auto& t : tracks)
        if (t->type != TrackType::Group)
            maxLat = std::max(maxLat, trackPathLatency(*t));
    return maxLat;
}

int64_t Mixer::trackCompensation(const Track& t, uint32_t masterLat) {
    if (!pdcEnabled) return 0;
    return trackPathLatency(t) + int64_t(masterLat);
}

void Mixer::applyPdcDelay(Track& t, float* buf, uint32_t frames,
                          int64_t delay) {
    if (delay <= 0) {
        t.pdcDelayFrames = delay;
        return;
    }
    // (Re)size only when the chain latency changed: an allocation on the
    // audio thread, but latency changes are rare host events anyway.
    if (t.pdcDelayFrames != delay) {
        t.pdcDelayBuf.assign(size_t(delay) * 2, 0.0f);
        t.pdcDelayPos = 0;
        t.pdcDelayFrames = delay;
    }
    for (uint32_t i = 0; i < frames; ++i) {
        float* slot = &t.pdcDelayBuf[t.pdcDelayPos * 2];
        const float l = buf[i * 2];
        const float r = buf[i * 2 + 1];
        buf[i * 2] = slot[0];
        buf[i * 2 + 1] = slot[1];
        slot[0] = l;
        slot[1] = r;
        if (++t.pdcDelayPos >= size_t(delay)) t.pdcDelayPos = 0;
    }
}

int64_t Mixer::trackPdcFrames(int trackId) {
    Track* t = findTrack(trackId);
    if (!t) return 0;
    const uint32_t masterLat = chainLatency(masterEffects);
    if (t->type == TrackType::Group)
        return int64_t(chainLatency(t->effects) + masterLat);
    return trackCompensation(*t, masterLat);
}

void Mixer::renderTrackBlock(Track& t, float* buf, uint32_t frames,
                             int64_t playhead) {
    std::memset(buf, 0, sizeof(float) * frames * 2);

    // Apply lane values before rendering so VST instruments see this
    // block's parameter changes (was after: one-block delay).
    applyAutomationLanes(t, playhead);

    if (t.type == TrackType::Audio)
        mixAudioTrack(t, buf, frames, playhead);
    else
        mixMidiTrack(t, buf, frames, playhead);

    for (auto& fx : t.effects)
        fx->process(buf, frames);

    applyStereoWidth(automatedWidth(t, playhead), buf, frames);

    float pkL = 0.0f, pkR = 0.0f;
    applyGainPan(automatedGain(t, playhead), automatedPan(t, playhead),
                 buf, frames, pkL, pkR);
    t.peakL.store(pkL, std::memory_order_relaxed);
    t.peakR.store(pkR, std::memory_order_relaxed);
}

void Mixer::clearPeaks() {
    masterPeakL.store(0.0f, std::memory_order_relaxed);
    masterPeakR.store(0.0f, std::memory_order_relaxed);
    for (auto& t : tracks) {
        t->peakL.store(0.0f, std::memory_order_relaxed);
        t->peakR.store(0.0f, std::memory_order_relaxed);
    }
}

void Mixer::mix(float* out, uint32_t frames, int64_t playhead) {
    std::memset(out, 0, sizeof(float) * frames * 2);
    if (scratch_.size() < size_t(frames) * 2)
        scratch_.resize(size_t(frames) * 2);

    prepareGroupBuffers(frames);

    bool anySolo = false;
    for (const auto& t : tracks)
        if (t->solo) { anySolo = true; break; }

    const uint32_t masterLat = chainLatency(masterEffects);

    // Pass 1a: render every non-group track into its own buffer. Tracks
    // without VST plugins may run on the render pool; VST paths stay on
    // the audio callback thread (VST3 is not thread-safe for process()).
    renderJobs_.clear();
    for (auto& t : tracks)
        if (t->type != TrackType::Group)
            renderJobs_.push_back(t.get());

    const size_t bufSize = size_t(frames) * 2;
    if (trackBufs_.size() < renderJobs_.size() * bufSize)
        trackBufs_.resize(renderJobs_.size() * bufSize);

    const bool delayMode = pdcEnabled && pdcMode == 1;
    const int64_t maxLat = delayMode ? maxPathLatency() : 0;

    parallelRenderIndices_.clear();
    for (size_t i = 0; i < renderJobs_.size(); ++i) {
        if (!trackUsesVst(*renderJobs_[i]))
            parallelRenderIndices_.push_back(int(i));
    }

    auto renderTrackAt = [&](size_t i) {
        Track& t = *renderJobs_[i];
        float* buf = trackBufs_.data() + i * bufSize;
        if (delayMode) {
            renderTrackBlock(t, buf, frames, playhead);
            const int64_t d = maxLat - trackPathLatency(t);
            applyPdcDelay(t, buf, frames, d);
        } else {
            const int64_t comp = trackCompensation(t, masterLat);
            renderTrackBlock(t, buf, frames, playhead + comp);
        }
    };

    if (!parallelRenderIndices_.empty()) {
        MixJobCtx ctx{this, frames, playhead, masterLat, maxLat, delayMode,
                      parallelRenderIndices_.data()};
        RenderPool::instance().run(
            int(parallelRenderIndices_.size()),
            [](void* p, int ji) {
                auto* c = static_cast<MixJobCtx*>(p);
                const size_t i = size_t(c->jobIndices[ji]);
                Track& t = *c->self->renderJobs_[i];
                float* buf = c->self->trackBufs_.data()
                             + i * size_t(c->frames) * 2;
                if (c->delayMode) {
                    c->self->renderTrackBlock(t, buf, c->frames, c->playhead);
                    const int64_t d =
                        c->maxLat - c->self->trackPathLatency(t);
                    c->self->applyPdcDelay(t, buf, c->frames, d);
                } else {
                    const int64_t comp =
                        c->self->trackCompensation(t, c->masterLat);
                    c->self->renderTrackBlock(t, buf, c->frames,
                                              c->playhead + comp);
                }
            },
            &ctx);
    }

    for (size_t i = 0; i < renderJobs_.size(); ++i) {
        if (!trackUsesVst(*renderJobs_[i])) continue;
        renderTrackAt(i);
    }

    // Pass 1b: route rendered buffers to master or group accumulators
    // (serial: sums must stay deterministic).
    for (size_t i = 0; i < renderJobs_.size(); ++i) {
        Track* t = renderJobs_[i];
        const bool audible = !t->mute && (!anySolo || trackSoloActive(*t));
        if (!audible) continue;
        const float* buf = trackBufs_.data() + i * bufSize;

        Track* grp = nullptr;
        if (t->groupId != 0)
            grp = findTrack(t->groupId);
        if (grp && grp->type == TrackType::Group) {
            float* gbuf = groupBuffer(grp->id, frames);
            if (gbuf) {
                for (uint32_t k = 0; k < frames * 2; ++k)
                    gbuf[k] += buf[k];
            }
        } else {
            for (uint32_t k = 0; k < frames * 2; ++k)
                out[k] += buf[k];
        }
    }

    // Pass 2a (parallel): each group bus processes its accumulated
    // child sum in place. Groups cannot be nested, so every bus is
    // independent of the others (a flat "topological level"); each job
    // touches only its own group buffer / effects / peak atomics.
    groupJobs_.clear();
    for (auto& t : tracks)
        if (t->type == TrackType::Group)
            groupJobs_.push_back(t.get());

    parallelGroupIndices_.clear();
    for (size_t i = 0; i < groupJobs_.size(); ++i) {
        if (!groupUsesVst(*groupJobs_[i]))
            parallelGroupIndices_.push_back(int(i));
    }

    auto renderGroupAt = [&](size_t i) {
        Track& g = *groupJobs_[i];
        float* buf = groupBuffer(g.id, frames);
        if (!buf) return;
        applyAutomationLanes(g, playhead);
        for (auto& fx : g.effects)
            fx->process(buf, frames);
        applyStereoWidth(automatedWidth(g, playhead), buf, frames);
        float pkL = 0.0f, pkR = 0.0f;
        applyGainPanBus(automatedGain(g, playhead), automatedPan(g, playhead),
                        buf, frames, pkL, pkR);
        g.peakL.store(pkL, std::memory_order_relaxed);
        g.peakR.store(pkR, std::memory_order_relaxed);
    };

    if (!parallelGroupIndices_.empty()) {
        MixJobCtx gctx{this, frames, playhead, masterLat, 0, false,
                       parallelGroupIndices_.data()};
        RenderPool::instance().run(
            int(parallelGroupIndices_.size()),
            [](void* p, int ji) {
                auto* c = static_cast<MixJobCtx*>(p);
                const size_t i = size_t(c->jobIndices[ji]);
                Track& g = *c->self->groupJobs_[i];
                float* buf = c->self->groupBuffer(g.id, c->frames);
                if (!buf) return;

                applyAutomationLanes(g, c->playhead);

                for (auto& fx : g.effects)
                    fx->process(buf, c->frames);

                c->self->applyStereoWidth(
                    automatedWidth(g, c->playhead), buf, c->frames);

                float pkL = 0.0f, pkR = 0.0f;
                c->self->applyGainPanBus(
                    automatedGain(g, c->playhead),
                    automatedPan(g, c->playhead), buf, c->frames,
                    pkL, pkR);
                g.peakL.store(pkL, std::memory_order_relaxed);
                g.peakR.store(pkR, std::memory_order_relaxed);
            },
            &gctx);
    }

    for (size_t i = 0; i < groupJobs_.size(); ++i) {
        if (!groupUsesVst(*groupJobs_[i])) continue;
        renderGroupAt(i);
    }

    // Pass 2b (serial): sum audible group busses into the master in
    // track order — deterministic, bit-identical to the serial path.
    for (auto& t : tracks) {
        if (t->type != TrackType::Group) continue;
        const float* gbuf = groupBuffer(t->id, frames);
        if (!gbuf) continue;
        if (isGroupAudible(*t, anySolo)) {
            for (uint32_t i = 0; i < frames * 2; ++i)
                out[i] += gbuf[i];
        }
    }

    applyMaster(out, frames, playhead);
}

void Mixer::applyMaster(float* out, uint32_t frames, int64_t playhead) {
    // Master automation (gain lane + master fx params), then the insert
    // chain, then gain + soft clip (tanh limiter).
    float mg = masterGain.load(std::memory_order_relaxed);
    if (playhead >= 0) {
        for (const auto& lane : masterAutoLanes) {
            if (lane.points.empty()) continue;
            const float val = evalAutomationLane(lane.points, playhead);
            switch (lane.target) {
            case 0:
                mg = val;
                break;
            case 2:
                if (lane.slot >= 0
                        && size_t(lane.slot) < masterEffects.size())
                    masterEffects[size_t(lane.slot)]->setParam(lane.param,
                                                               val);
                break;
            case 3: {
                if (lane.slot < 0
                        || size_t(lane.slot) >= masterEffects.size())
                    break;
                auto vst = std::dynamic_pointer_cast<VstPlugin>(
                    masterEffects[size_t(lane.slot)]);
                if (vst)
                    vst->setParamFromAudioThread(lane.param, val);
                break;
            }
            default:
                break;
            }
        }
    }
    for (auto& fx : masterEffects)
        fx->process(out, frames);
    float mpL = 0.0f, mpR = 0.0f;
    for (uint32_t i = 0; i < frames * 2; ++i) {
        float s = out[i] * mg;
        if (s > 0.95f || s < -0.95f)
            s = std::tanh(s);
        out[i] = s;
        const float a = std::fabs(s);
        if (i % 2 == 0) mpL = std::max(mpL, a);
        else mpR = std::max(mpR, a);
    }
    masterPeakL.store(mpL, std::memory_order_relaxed);
    masterPeakR.store(mpR, std::memory_order_relaxed);
}

void Mixer::renderVstIdle(float* out, uint32_t frames, int64_t playhead) {
    if (scratch_.size() < size_t(frames) * 2)
        scratch_.resize(size_t(frames) * 2);

    prepareGroupBuffers(frames);

    bool anySolo = false;
    for (const auto& t : tracks)
        if (t->solo) { anySolo = true; break; }

    // Pass 1: idle VST output from grouped/non-grouped MIDI tracks.
    for (auto& t : tracks) {
        if (t->type != TrackType::Midi || !t->vstInstrument) continue;

        float* buf = scratch_.data();
        // No setTransport here: the stopped state was already set on Stop.
        // Re-sending a constant playhead every idle block reads as a
        // backward jump once the plugin's own sample position advances,
        // and the resulting per-block DSP flush kills preview notes.
        t->vstInstrument->processWithEvents(buf, frames, {});

        for (auto& fx : t->effects)
            fx->process(buf, frames);

        applyStereoWidth(t->stereoWidth, buf, frames);

        const bool audible = !t->mute && (!anySolo || trackSoloActive(*t));
        float pkL = 0.0f, pkR = 0.0f;
        applyGainPan(t->gain, t->pan, buf, frames, pkL, pkR);
        t->peakL.store(pkL, std::memory_order_relaxed);
        t->peakR.store(pkR, std::memory_order_relaxed);

        if (!audible) continue;

        Track* grp = nullptr;
        if (t->groupId != 0)
            grp = findTrack(t->groupId);
        if (grp && grp->type == TrackType::Group) {
            float* gbuf = groupBuffer(grp->id, frames);
            if (gbuf) {
                for (uint32_t i = 0; i < frames * 2; ++i)
                    gbuf[i] += buf[i];
            }
        } else {
            for (uint32_t i = 0; i < frames * 2; ++i)
                out[i] += buf[i];
        }
    }

    // Pass 2: group tracks (full effect chain + gain/pan, same as mix()).
    for (auto& t : tracks) {
        if (t->type != TrackType::Group) continue;

        float* gbuf = groupBuffer(t->id, frames);
        float* buf = scratch_.data();
        if (gbuf)
            std::memcpy(buf, gbuf, sizeof(float) * frames * 2);
        else
            std::memset(buf, 0, sizeof(float) * frames * 2);

        for (auto& fx : t->effects)
            fx->process(buf, frames);

        applyStereoWidth(t->stereoWidth, buf, frames);

        float pkL = 0.0f, pkR = 0.0f;
        applyGainPanBus(t->gain, t->pan, buf, frames, pkL, pkR);
        t->peakL.store(pkL, std::memory_order_relaxed);
        t->peakR.store(pkR, std::memory_order_relaxed);

        if (isGroupAudible(*t, anySolo)) {
            for (uint32_t i = 0; i < frames * 2; ++i)
                out[i] += buf[i];
        }
    }
}

// Metronome ------------------------------------------------------------------

void Mixer::renderMetronome(float* out, uint32_t frames, int64_t playhead) {
    if (!metronomeOn || bpm <= 0.0) return;
    const double framesPerBeat = double(sampleRate) * 60.0 / bpm;
    const int64_t clickLen = int64_t(double(sampleRate) * 0.03); // 30 ms

    for (uint32_t i = 0; i < frames; ++i) {
        const double pos = double(playhead + int64_t(i));
        if (pos < 0.0) continue;
        const int64_t beat = int64_t(pos / framesPerBeat);
        const int64_t clickAge = int64_t(pos - double(beat) * framesPerBeat);
        if (clickAge >= clickLen) continue;

        const bool accent = (beat % 4 == 0);
        const float freq = accent ? 1600.0f : 1100.0f;
        const float env = 1.0f - float(clickAge) / float(clickLen);
        const float s = std::sin(2.0f * 3.14159265f * freq *
                                 float(clickAge) / float(sampleRate)) *
                        env * env * 0.35f;
        out[i * 2] += s;
        out[i * 2 + 1] += s;
    }
}

// Preview voices (piano roll auditioning) --------------------------------------

void Mixer::previewNoteOn(int pitch, float velocity,
                          const SynthParams& params) {
    // Retrigger if the same pitch is already sounding.
    for (auto& v : previewVoices_) {
        if (v.voice.pitch == pitch && v.voice.releasedAt < 0) {
            v.voice.age = 0;
            v.voice.velocity = velocity;
            v.params = params;
            return;
        }
    }
    if (previewVoices_.size() >= 16) previewVoices_.erase(previewVoices_.begin());
    ActivePreview ap;
    ap.voice.pitch = pitch;
    ap.voice.velocity = velocity;
    ap.params = params;
    previewVoices_.push_back(ap);
}

void Mixer::previewNoteOff(int pitch) {
    for (auto& v : previewVoices_)
        if (v.voice.pitch == pitch && v.voice.releasedAt < 0)
            v.voice.releasedAt = v.voice.age;
}

void Mixer::renderPreview(float* out, uint32_t frames) {
    for (auto& v : previewVoices_) {
        // "Held length": until released the note is still in sustain.
        const int64_t len = v.voice.releasedAt >= 0
                                ? v.voice.releasedAt
                                : v.voice.age + int64_t(frames) + 1;
        synth::renderNote(v.params, v.voice.pitch, v.voice.velocity,
                          v.voice.age, len, sampleRate, out, frames);
        v.voice.age += int64_t(frames);
    }
    // Drop finished voices (released and past the release tail).
    previewVoices_.erase(
        std::remove_if(previewVoices_.begin(), previewVoices_.end(),
                       [&](const ActivePreview& v) {
                           if (v.voice.releasedAt < 0) return false;
                           const int64_t end = synth::noteTailFrames(
                               v.params, v.voice.releasedAt, sampleRate);
                           return v.voice.age > end;
                       }),
        previewVoices_.end());
}
