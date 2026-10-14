#include "warp_windows_fix.h"
#include "warp_offline.h"

#include "audio_file.h"
#include "warp_ola.h"
#include "warp_resample.h"
#include "warp_vocoder.h"

#include "dr_wav.h"

#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <vector>

namespace fs = std::filesystem;

namespace pydaw::warp {

namespace {

std::string sha1Hex24(const std::string& data) {
    // Minimal SHA-1 (public-domain style) for cache keys only.
    uint32_t h0 = 0x67452301, h1 = 0xEFCDAB89, h2 = 0x98BADCFE;
    uint32_t h3 = 0x10325476, h4 = 0xC3D2E1F0;
    std::vector<uint8_t> msg(data.begin(), data.end());
    const uint64_t bitLen = msg.size() * 8;
    msg.push_back(0x80);
    while ((msg.size() % 64) != 56) msg.push_back(0);
    for (int i = 7; i >= 0; --i)
        msg.push_back(uint8_t((bitLen >> (i * 8)) & 0xff));

    auto rol = [](uint32_t x, int n) { return (x << n) | (x >> (32 - n)); };

    for (size_t off = 0; off < msg.size(); off += 64) {
        uint32_t w[80];
        for (int i = 0; i < 16; ++i) {
            w[i] = (uint32_t(msg[off + i * 4]) << 24)
                 | (uint32_t(msg[off + i * 4 + 1]) << 16)
                 | (uint32_t(msg[off + i * 4 + 2]) << 8)
                 | uint32_t(msg[off + i * 4 + 3]);
        }
        for (int i = 16; i < 80; ++i)
            w[i] = rol(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1);

        uint32_t a = h0, b = h1, c = h2, d = h3, e = h4;
        for (int i = 0; i < 80; ++i) {
            uint32_t f, k;
            if (i < 20) { f = (b & c) | ((~b) & d); k = 0x5A827999; }
            else if (i < 40) { f = b ^ c ^ d; k = 0x6ED9EBA1; }
            else if (i < 60) { f = (b & c) | (b & d) | (c & d); k = 0x8F1BBCDC; }
            else { f = b ^ c ^ d; k = 0xCA62C1D6; }
            const uint32_t temp = rol(a, 5) + f + e + k + w[i];
            e = d; d = c; c = rol(b, 30); b = a; a = temp;
        }
        h0 += a; h1 += b; h2 += c; h3 += d; h4 += e;
    }

    std::ostringstream oss;
    oss << std::hex << std::setfill('0');
    oss << std::setw(8) << h0 << std::setw(8) << h1 << std::setw(8) << h2;
    std::string s = oss.str();
    return s.substr(0, 24);
}

std::string cacheDir() {
    if (const char* env = std::getenv("PYDAW_CACHE_DIR"))
        return (fs::path(env) / "stretch").string();
#ifdef _WIN32
    if (const char* local = std::getenv("LOCALAPPDATA"))
        return (fs::path(local) / "PyDAW" / "cache" / "stretch").string();
#endif
    return (fs::temp_directory_path() / "pydaw_cache" / "stretch").string();
}

const char* methodTag(WarpMethod m) {
    switch (m) {
    case WarpMethod::StretchHd: return "stretch_hd";
    case WarpMethod::Resample: return "resample";
    default: return "stretch";
    }
}

std::string renderKey(const std::string& path, WarpMethod method, float ratio) {
    std::error_code ec;
    const fs::path p = fs::absolute(path, ec);
    std::ostringstream sig;
    sig << p.string();
    if (!ec && fs::exists(p, ec)) {
        sig << '|' << fs::file_size(p, ec);
        const auto ftime = fs::last_write_time(p, ec);
        const auto sctp = std::chrono::time_point_cast<std::chrono::seconds>(
            ftime - fs::file_time_type::clock::now()
            + std::chrono::system_clock::now());
        const auto mt = std::chrono::system_clock::to_time_t(sctp);
        sig << '|' << int64_t(mt);
    }
    sig << '|' << methodTag(method) << '|' << std::fixed
        << std::setprecision(6) << ratio;
    return sha1Hex24(sig.str());
}

#ifdef _WIN32
#include <windows.h>
static std::wstring widePath(const std::string& utf8) {
    if (utf8.empty()) return L"";
    const int n = MultiByteToWideChar(CP_UTF8, 0, utf8.c_str(), -1, nullptr, 0);
    std::wstring w(size_t(n), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, utf8.c_str(), -1, w.data(), n);
    if (!w.empty() && w.back() == L'\0') w.pop_back();
    return w;
}
#endif

}  // namespace

void stretchOffline(const float* src, int64_t frames, int channels,
                    float ratio, WarpMethod method, std::vector<float>& out) {
    switch (method) {
    case WarpMethod::Resample:
        resampleOffline(src, frames, channels, ratio, out);
        break;
    case WarpMethod::StretchHd:
        stretchVocoderOffline(src, frames, channels, ratio, out);
        break;
    default:
        stretchOlaOffline(src, frames, channels, ratio, out);
        break;
    }
}

std::string stretchFileToCache(const std::string& path, WarpMethod method,
                               float ratio, uint32_t sampleRate) {
    ratio = clampRatio(ratio);
    if (ratioIsUnity(ratio))
        return path;

    const std::string outPath =
        (fs::path(cacheDir()) / (renderKey(path, method, ratio) + ".wav")).string();
    if (fs::exists(outPath))
        return outPath;

    fs::create_directories(cacheDir());

    AudioFile file(path, sampleRate);
    const int64_t frames = int64_t(file.frameCount());
    const float* stereo = file.data();
    std::vector<float> stretched;
    stretchOffline(stereo, frames, 2, ratio, method, stretched);
    const uint64_t outFrames = stretched.size() / 2;

    const std::string tmp = outPath + ".tmp";
    drwav_data_format fmt{};
    fmt.container = drwav_container_riff;
    fmt.format = DR_WAVE_FORMAT_IEEE_FLOAT;
    fmt.channels = 2;
    fmt.sampleRate = sampleRate;
    fmt.bitsPerSample = 32;

    drwav outWav;
#ifdef _WIN32
    if (!drwav_init_file_write_w(&outWav, widePath(tmp).c_str(), &fmt, nullptr))
#else
    if (!drwav_init_file_write(&outWav, tmp.c_str(), &fmt, nullptr))
#endif
        throw std::runtime_error("stretchFileToCache: cannot write " + tmp);

    drwav_write_pcm_frames(&outWav, outFrames, stretched.data());
    drwav_uninit(&outWav);

    fs::rename(tmp, outPath);
    return outPath;
}

}  // namespace pydaw::warp
