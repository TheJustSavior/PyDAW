#pragma once

#include <cstdint>
#include <string>

namespace pydaw::warp {

constexpr float kMinRatio = 0.1f;
constexpr float kMaxRatio = 10.0f;

enum class WarpMethod : uint8_t {
    Stretch = 0,
    StretchHd = 1,
    Resample = 2,
};

inline float clampRatio(float ratio) {
    if (ratio < kMinRatio) return kMinRatio;
    if (ratio > kMaxRatio) return kMaxRatio;
    return ratio;
}

inline bool ratioIsUnity(float ratio) {
    return ratio >= 0.999999f && ratio <= 1.000001f;
}

inline WarpMethod warpMethodFromInt(int v) {
    if (v == 1) return WarpMethod::StretchHd;
    if (v == 2) return WarpMethod::Resample;
    return WarpMethod::Stretch;
}

inline WarpMethod warpMethodFromName(const std::string& name) {
    if (name == "stretch_hd") return WarpMethod::StretchHd;
    if (name == "resample") return WarpMethod::Resample;
    return WarpMethod::Stretch;
}

}  // namespace pydaw::warp
