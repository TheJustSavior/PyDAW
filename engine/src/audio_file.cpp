#include "audio_file.h"

#define DR_WAV_IMPLEMENTATION
#include "dr_wav.h"
#define DR_FLAC_IMPLEMENTATION
#include "dr_flac.h"
#define DR_MP3_IMPLEMENTATION
#include "dr_mp3.h"

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <stdexcept>
#include <system_error>
#include <thread>

namespace fs = std::filesystem;

namespace {

std::string lowerExt(const std::string& path) {
    const size_t dot = path.find_last_of('.');
    if (dot == std::string::npos) return "";
    std::string ext = path.substr(dot + 1);
    std::transform(ext.begin(), ext.end(), ext.begin(),
                   [](unsigned char c) { return char(std::tolower(c)); });
    return ext;
}

// Convert arbitrary channel count to interleaved stereo.
std::vector<float> toStereo(std::vector<float>&& in, uint64_t frames,
                            uint32_t channels) {
    if (channels == 2) return std::move(in);
    std::vector<float> out(frames * 2);
    if (channels == 1) {
        for (uint64_t i = 0; i < frames; ++i) {
            out[i * 2] = in[i];
            out[i * 2 + 1] = in[i];
        }
    } else {
        // Downmix: take the first two channels, ignore the rest.
        for (uint64_t i = 0; i < frames; ++i) {
            out[i * 2] = in[i * channels];
            out[i * 2 + 1] = in[i * channels + 1];
        }
    }
    return out;
}

// Decoded files larger than this spill to the disk cache and are
// memory-mapped (OS pages stream in on demand). Override in MB via
// PYDAW_STREAM_THRESHOLD_MB; 0 disables streaming entirely.
uint64_t streamThresholdBytes() {
    static const uint64_t threshold = [] {
        if (const char* env = std::getenv("PYDAW_STREAM_THRESHOLD_MB")) {
            char* end = nullptr;
            const long mb = std::strtol(env, &end, 10);
            if (end != env && mb >= 0)
                return uint64_t(mb) * 1024 * 1024;
        }
        return uint64_t(64) * 1024 * 1024; // ~5.8 min stereo float @48k
    }();
    return threshold;
}

// Peak cache file header (v1).
struct PeaksHeader {
    char magic[8];       // "PDWPEAK1"
    uint64_t frames;     // source frame count
    uint32_t spp;        // samples per peak block
    uint32_t reserved;
};

constexpr char kPeaksMagic[8] = {'P', 'D', 'W', 'P', 'E', 'A', 'K', '1'};

#ifdef _WIN32
// Paths cross the pybind11 boundary as UTF-8, but fopen() on Windows
// expects the ANSI codepage - non-ASCII paths (e.g. a OneDrive
// "Документы" folder) fail to open. Convert to wide and use the *_w
// variants of the dr_libs open functions.
std::wstring widePath(const std::string& utf8) {
    return fs::u8path(utf8).wstring();
}
#endif

} // namespace

AudioFile::AudioFile(const std::string& path, uint32_t targetSampleRate)
    : path_(path) {
    cacheKey_ = audiocache::fileKey(path, targetSampleRate);

    // Fast path: decoded PCM already cached on disk -> map it, skip decode.
    if (!cacheKey_.empty() && !audiocache::cacheDir().empty()) {
        const std::string pcmPath =
            audiocache::cacheDir() + "/" + cacheKey_ + ".pcm";
        std::error_code ec;
        if (fs::exists(fs::u8path(pcmPath), ec) && !ec) {
            if (mapped_.open(pcmPath) && mapped_.size() >= sizeof(float) * 2) {
                frames_ = mapped_.size() / (sizeof(float) * 2);
                sampleRate_ = targetSampleRate;
                audiocache::touchFile(pcmPath); // mark as recently used
                return;
            }
            mapped_.close();
        }
    }

    loadAndConvert(path, targetSampleRate);
    maybeSpillToCache();
}

void AudioFile::loadAndConvert(const std::string& path, uint32_t targetRate) {
    const std::string ext = lowerExt(path);

    std::vector<float> raw;
    uint64_t frames = 0;
    uint32_t channels = 0;
    uint32_t rate = 0;

    if (ext == "wav" || ext == "wave" || ext == "aiff" || ext == "aif") {
        drwav wav;
#ifdef _WIN32
        const bool wavOk =
            drwav_init_file_w(&wav, widePath(path).c_str(), nullptr);
#else
        const bool wavOk = drwav_init_file(&wav, path.c_str(), nullptr);
#endif
        if (!wavOk)
            throw std::runtime_error("Cannot open WAV file: " + path);
        frames = wav.totalPCMFrameCount;
        channels = wav.channels;
        rate = wav.sampleRate;
        raw.resize(size_t(frames) * channels);
        const uint64_t got = drwav_read_pcm_frames_f32(&wav, frames, raw.data());
        drwav_uninit(&wav);
        if (got < frames) {
            // Truncated/corrupt file: keep what decoded instead of leaving
            // uninitialised garbage in the tail.
            frames = got;
            raw.resize(size_t(frames) * channels);
        }
    } else if (ext == "flac") {
#ifdef _WIN32
        drflac* flac = drflac_open_file_w(widePath(path).c_str(), nullptr);
#else
        drflac* flac = drflac_open_file(path.c_str(), nullptr);
#endif
        if (!flac)
            throw std::runtime_error("Cannot open FLAC file: " + path);
        channels = flac->channels;
        rate = flac->sampleRate;
        frames = flac->totalPCMFrameCount;
        raw.resize(size_t(frames) * channels);
        drflac_read_pcm_frames_f32(flac, frames, raw.data());
        drflac_close(flac);
    } else if (ext == "mp3") {
        drmp3 mp3;
#ifdef _WIN32
        const bool mp3Ok =
            drmp3_init_file_w(&mp3, widePath(path).c_str(), nullptr);
#else
        const bool mp3Ok = drmp3_init_file(&mp3, path.c_str(), nullptr);
#endif
        if (!mp3Ok)
            throw std::runtime_error("Cannot open MP3 file: " + path);
        channels = mp3.channels;
        rate = mp3.sampleRate;
        frames = drmp3_get_pcm_frame_count(&mp3);
        raw.resize(size_t(frames) * channels);
        drmp3_read_pcm_frames_f32(&mp3, frames, raw.data());
        drmp3_uninit(&mp3);
    } else {
        throw std::runtime_error("Unsupported audio format: ." + ext);
    }

    if (frames == 0 || channels == 0)
        throw std::runtime_error("Empty or corrupt audio file: " + path);

    std::vector<float> stereo = toStereo(std::move(raw), frames, channels);

    if (rate != targetRate) {
        uint64_t outFrames = 0;
        samples_ = resampleCubic(stereo, frames, rate, targetRate, outFrames);
        frames_ = outFrames;
    } else {
        samples_ = std::move(stereo);
        frames_ = frames;
    }
    sampleRate_ = targetRate;
}

void AudioFile::maybeSpillToCache() {
    const uint64_t bytes = uint64_t(samples_.size()) * sizeof(float);
    const uint64_t threshold = streamThresholdBytes();
    if (threshold == 0 || bytes < threshold) return;
    if (cacheKey_.empty() || audiocache::cacheDir().empty()) return;

    const std::string pcmPath =
        audiocache::cacheDir() + "/" + cacheKey_ + ".pcm";
    if (!audiocache::writeFile(pcmPath, samples_.data(), bytes)) return;
    if (!mapped_.open(pcmPath)) return;
    if (mapped_.size() != bytes) {
        mapped_.close();
        return;
    }
    // Release the in-RAM copy: reads now go through the mapping.
    samples_.clear();
    samples_.shrink_to_fit();
}

std::vector<float> AudioFile::resampleCubic(const std::vector<float>& in,
                                            uint64_t inFrames,
                                            uint32_t inRate,
                                            uint32_t outRate,
                                            uint64_t& outFrames) {
    // Catmull-Rom cubic interpolation: much lower aliasing than linear
    // for typical 44.1k <-> 48k conversions.
    const double ratio = double(outRate) / double(inRate);
    outFrames = uint64_t(double(inFrames) * ratio);
    std::vector<float> out(outFrames * 2);

    const float* src = in.data();
    const int64_t lastFrame = int64_t(inFrames) - 1;
    auto sampleAt = [&](int64_t frame, int c) -> float {
        frame = std::max<int64_t>(0, std::min<int64_t>(frame, lastFrame));
        return src[uint64_t(frame) * 2 + c];
    };

    for (uint64_t i = 0; i < outFrames; ++i) {
        const double srcPos = double(i) / ratio;
        const int64_t i1 = int64_t(srcPos);
        const float t = float(srcPos - double(i1));
        const float t2 = t * t;
        const float t3 = t2 * t;
        for (int c = 0; c < 2; ++c) {
            const float p0 = sampleAt(i1 - 1, c);
            const float p1 = sampleAt(i1, c);
            const float p2 = sampleAt(i1 + 1, c);
            const float p3 = sampleAt(i1 + 2, c);
            out[i * 2 + c] = 0.5f * ((2.0f * p1)
                + (-p0 + p2) * t
                + (2.0f * p0 - 5.0f * p1 + 4.0f * p2 - p3) * t2
                + (-p0 + 3.0f * p1 - 3.0f * p2 + p3) * t3);
        }
    }
    return out;
}

// Peaks -----------------------------------------------------------------------

std::vector<float> AudioFile::scanPeaks(uint32_t samplesPerPeak) const {
    if (samplesPerPeak == 0) samplesPerPeak = 1;
    const uint64_t blocks = (frames_ + samplesPerPeak - 1) / samplesPerPeak;
    std::vector<float> peaks(blocks * 2);
    const float* src = data();
    for (uint64_t b = 0; b < blocks; ++b) {
        float mn = 1.0f, mx = -1.0f;
        const uint64_t start = b * samplesPerPeak;
        const uint64_t end = std::min<uint64_t>(start + samplesPerPeak, frames_);
        for (uint64_t i = start; i < end; ++i) {
            const float mono = 0.5f * (src[i * 2] + src[i * 2 + 1]);
            mn = std::min(mn, mono);
            mx = std::max(mx, mono);
        }
        peaks[b * 2] = mn;
        peaks[b * 2 + 1] = mx;
    }
    return peaks;
}

void AudioFile::buildPeaksAsync(std::shared_ptr<AudioFile> self) const {
    {
        std::lock_guard<std::mutex> lock(peaksMutex_);
        if (peaksJobStarted_ || !basePeaks_.empty()) return;
        peaksJobStarted_ = true;
    }
    // The shared_ptr keeps the file alive for the duration of the build
    // even if the engine drops it; writeFile is atomic (tmp + rename),
    // so a build interrupted by process exit leaves no bad cache entry.
    std::thread([self] { self->ensureBasePeaks(); }).detach();
}

void AudioFile::ensureBasePeaks() const {
    // Serialize builders (background import thread vs. first UI paint):
    // whoever comes second finds the pyramid ready and returns at once.
    std::lock_guard<std::mutex> lock(peaksMutex_);
    if (!basePeaks_.empty()) return;

    const std::string dir = audiocache::cacheDir();
    const std::string peaksPath =
        (!cacheKey_.empty() && !dir.empty())
            ? dir + "/" + cacheKey_ + ".peaks"
            : std::string{};

    // Try the on-disk cache first.
    if (!peaksPath.empty()) {
        MappedFile mf;
        if (mf.open(peaksPath) && mf.size() >= sizeof(PeaksHeader)) {
            const auto* hdr = static_cast<const PeaksHeader*>(mf.data());
            const uint64_t blocks =
                (frames_ + kBasePeakSpp - 1) / kBasePeakSpp;
            const uint64_t expect =
                sizeof(PeaksHeader) + blocks * 2 * sizeof(float);
            if (std::memcmp(hdr->magic, kPeaksMagic, 8) == 0
                    && hdr->frames == frames_
                    && hdr->spp == kBasePeakSpp
                    && mf.size() == expect) {
                const float* body = reinterpret_cast<const float*>(
                    static_cast<const char*>(mf.data())
                    + sizeof(PeaksHeader));
                basePeaks_.assign(body, body + blocks * 2);
                mf.close();
                audiocache::touchFile(peaksPath); // LRU: recently used
                return;
            }
        }
    }

    // Compute once and persist for future sessions.
    basePeaks_ = scanPeaks(kBasePeakSpp);
    if (!peaksPath.empty() && !basePeaks_.empty()) {
        std::vector<char> blob(sizeof(PeaksHeader)
                               + basePeaks_.size() * sizeof(float));
        PeaksHeader hdr{};
        std::memcpy(hdr.magic, kPeaksMagic, 8);
        hdr.frames = frames_;
        hdr.spp = kBasePeakSpp;
        std::memcpy(blob.data(), &hdr, sizeof(hdr));
        std::memcpy(blob.data() + sizeof(hdr), basePeaks_.data(),
                    basePeaks_.size() * sizeof(float));
        audiocache::writeFile(peaksPath, blob.data(), blob.size());
    }
}

std::vector<float> AudioFile::computePeaks(uint32_t samplesPerPeak) const {
    if (samplesPerPeak == 0) samplesPerPeak = 1;

    // Zoomed in tighter than the pyramid base: scan the samples directly
    // (cheap: only happens at extreme zoom where the UI shows a small span).
    if (samplesPerPeak < kBasePeakSpp)
        return scanPeaks(samplesPerPeak);

    // Derive from the base pyramid: each output block folds the base
    // blocks it covers. O(baseBlocks) instead of O(frames) per zoom level.
    ensureBasePeaks();
    const uint64_t baseBlocks = basePeaks_.size() / 2;
    if (baseBlocks == 0) return {};

    const uint64_t blocks = (frames_ + samplesPerPeak - 1) / samplesPerPeak;
    std::vector<float> peaks(blocks * 2);
    for (uint64_t b = 0; b < blocks; ++b) {
        const uint64_t startSample = b * samplesPerPeak;
        const uint64_t endSample =
            std::min<uint64_t>(startSample + samplesPerPeak, frames_);
        const uint64_t b0 = startSample / kBasePeakSpp;
        const uint64_t b1 = std::min<uint64_t>(
            baseBlocks, (endSample + kBasePeakSpp - 1) / kBasePeakSpp);
        float mn = 1.0f, mx = -1.0f;
        for (uint64_t i = b0; i < b1; ++i) {
            mn = std::min(mn, basePeaks_[i * 2]);
            mx = std::max(mx, basePeaks_[i * 2 + 1]);
        }
        if (mn > mx) { mn = 0.f; mx = 0.f; }
        peaks[b * 2] = mn;
        peaks[b * 2 + 1] = mx;
    }
    return peaks;
}
