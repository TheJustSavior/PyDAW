#pragma once

#include <cstdint>
#include <vector>

namespace pydaw::warp {

// Linear-interpolation resample: output length = round(n * ratio).
void resampleOffline(const float* src, int64_t frames, int channels,
                     float ratio, std::vector<float>& out);

// Sample stereo interleaved source at fractional frame position.
void sampleLinearStereo(const float* src, int64_t fileFrames,
                        double srcPos, float& outL, float& outR);

// Resample a timeline segment [relStart, relStart+outFrames) into dst
// (interleaved stereo). srcStart = (sourceOffset + rel) / ratio mapping.
void resampleBlock(const float* src, int64_t fileFrames,
                   double sourceOffset, double ratio,
                   int64_t relStart, int outFrames,
                   float* dstInterleaved, int64_t dstFrameOffset);

}  // namespace pydaw::warp
