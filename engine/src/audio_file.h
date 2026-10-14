#pragma once
#include "audio_cache.h"

#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

// Decoded audio held as interleaved stereo float32 at the engine sample
// rate. Small files live in RAM; large files are spilled to a disk cache
// and memory-mapped, so the OS streams pages in on demand (huge files no
// longer pin their full size in RAM, and reloads skip decoding entirely).
class AudioFile {
public:
    // Throws std::runtime_error on failure.
    AudioFile(const std::string& path, uint32_t targetSampleRate);

    const std::string& path() const { return path_; }
    uint32_t sampleRate() const { return sampleRate_; }
    // Number of stereo frames.
    uint64_t frameCount() const { return frames_; }
    double durationSeconds() const {
        return sampleRate_ ? double(frames_) / sampleRate_ : 0.0;
    }
    // True when the decoded data is memory-mapped from the disk cache
    // instead of held in RAM.
    bool isStreamed() const { return mapped_.isOpen(); }

    // Interleaved stereo samples, size == frames * 2. Immutable after
    // construction; safe to read from the audio thread.
    const float* data() const {
        return mapped_.isOpen()
                   ? static_cast<const float*>(mapped_.data())
                   : samples_.data();
    }

    // Min/max peaks per block for waveform drawing.
    // Returns pairs (min, max) of the mono mix,
    // length = ceil(frames / samplesPerPeak) * 2.
    //
    // Backed by a base-resolution peak pyramid that is computed once and
    // cached on disk; coarser zoom levels are derived from it instead of
    // rescanning the whole file. The pyramid build itself is guarded by
    // an internal mutex, so a background builder thread and the UI can
    // race safely (see buildPeaksAsync).
    std::vector<float> computePeaks(uint32_t samplesPerPeak) const;

    // Kick off the base peak pyramid on a background thread (import
    // time), so the first waveform paint finds it ready instead of
    // scanning the whole file synchronously. Safe to call many times.
    void buildPeaksAsync(std::shared_ptr<AudioFile> self) const;

    // Base resolution of the persistent peak cache (samples per block).
    static constexpr uint32_t kBasePeakSpp = 256;

private:
    void loadAndConvert(const std::string& path, uint32_t targetRate);
    void maybeSpillToCache();
    void ensureBasePeaks() const;
    std::vector<float> scanPeaks(uint32_t samplesPerPeak) const;
    static std::vector<float> resampleCubic(const std::vector<float>& in,
                                            uint64_t inFrames,
                                            uint32_t inRate,
                                            uint32_t outRate,
                                            uint64_t& outFrames);

    std::string path_;
    std::string cacheKey_;
    uint32_t sampleRate_ = 0;
    uint64_t frames_ = 0;
    std::vector<float> samples_; // interleaved stereo (RAM mode)
    MappedFile mapped_;          // decoded PCM cache (streaming mode)
    mutable std::mutex peaksMutex_;        // guards basePeaks_ build
    mutable std::vector<float> basePeaks_; // min/max pairs at kBasePeakSpp
    mutable bool peaksJobStarted_ = false; // background build kicked off
};

using AudioFilePtr = std::shared_ptr<AudioFile>;
