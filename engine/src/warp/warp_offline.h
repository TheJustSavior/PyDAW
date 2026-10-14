#pragma once

#include "warp_constants.h"

#include <cstdint>
#include <string>
#include <vector>

namespace pydaw::warp {

void stretchOffline(const float* src, int64_t frames, int channels,
                    float ratio, WarpMethod method, std::vector<float>& out);

// Render stretched WAV to cache; returns output path. Throws on failure.
std::string stretchFileToCache(const std::string& path, WarpMethod method,
                               float ratio, uint32_t sampleRate = 48000);

}  // namespace pydaw::warp
