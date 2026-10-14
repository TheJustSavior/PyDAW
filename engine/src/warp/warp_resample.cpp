#include "warp_windows_fix.h"
#include "warp_resample.h"

#include "warp_constants.h"

#include <algorithm>
#include <cmath>

namespace pydaw::warp {

void resampleOffline(const float* src, int64_t frames, int channels,
                     float ratio, std::vector<float>& out) {
    ratio = clampRatio(ratio);
    if (frames <= 0 || channels <= 0) {
        out.clear();
        return;
    }
    if (ratioIsUnity(ratio)) {
        out.assign(src, src + size_t(frames * channels));
        return;
    }
    const int64_t outFrames = std::max(int64_t(1),
                                       int64_t(std::llround(double(frames)
                                                            * double(ratio))));
    out.resize(size_t(outFrames * channels));
    for (int64_t i = 0; i < outFrames; ++i) {
        const double srcPos = double(i) / double(ratio);
        const int64_t i0 = int64_t(std::floor(srcPos));
        const int64_t i1 = std::min(i0 + 1, frames - 1);
        const float frac = float(srcPos - double(i0));
        for (int ch = 0; ch < channels; ++ch) {
            const float s0 = src[size_t(i0 * channels + ch)];
            const float s1 = src[size_t(i1 * channels + ch)];
            out[size_t(i * channels + ch)] = s0 * (1.f - frac) + s1 * frac;
        }
    }
}

void sampleLinearStereo(const float* src, int64_t fileFrames,
                        double srcPos, float& outL, float& outR) {
    if (fileFrames <= 0 || !src) {
        outL = outR = 0.f;
        return;
    }
    if (srcPos < 0.0) {
        outL = outR = 0.f;
        return;
    }
    if (srcPos >= double(fileFrames - 1)) {
        if (srcPos >= double(fileFrames)) {
            outL = outR = 0.f;
            return;
        }
        outL = src[(fileFrames - 1) * 2];
        outR = src[(fileFrames - 1) * 2 + 1];
        return;
    }
    const int64_t i0 = int64_t(std::floor(srcPos));
    const int64_t i1 = std::min(i0 + 1, fileFrames - 1);
    const float frac = float(srcPos - double(i0));
    const float l0 = src[i0 * 2];
    const float r0 = src[i0 * 2 + 1];
    const float l1 = src[i1 * 2];
    const float r1 = src[i1 * 2 + 1];
    outL = l0 * (1.f - frac) + l1 * frac;
    outR = r0 * (1.f - frac) + r1 * frac;
}

void resampleBlock(const float* src, int64_t fileFrames,
                   double sourceOffset, double ratio,
                   int64_t relStart, int outFrames,
                   float* dstInterleaved, int64_t dstFrameOffset) {
    ratio = clampRatio(ratio);
    for (int i = 0; i < outFrames; ++i) {
        const double srcPos = (sourceOffset + double(relStart + i)) / ratio;
        float l, r;
        sampleLinearStereo(src, fileFrames, srcPos, l, r);
        const int64_t di = dstFrameOffset + i;
        dstInterleaved[di * 2] += l;
        dstInterleaved[di * 2 + 1] += r;
    }
}

}  // namespace pydaw::warp
