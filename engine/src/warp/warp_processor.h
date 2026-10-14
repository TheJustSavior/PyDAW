#pragma once

#include "warp_constants.h"
#include "warp_ola.h"
#include "warp_vocoder.h"

#include <cstdint>

namespace pydaw::warp {

class WarpProcessor {
public:
    void configure(WarpMethod method, float ratio, int64_t sourceSpanFrames);
    void resetToTimelineRel(int64_t rel);

    // Accumulates warped stereo into dstInterleaved at dstFrameOffset.
    void processBlock(const float* src, int64_t fileFrames,
                      double sourceOffsetTimeline, int64_t relStart,
                      int outFrames, float* dstInterleaved,
                      int64_t dstFrameOffset);

private:
    WarpMethod method_ = WarpMethod::Stretch;
    float ratio_ = 1.f;
    int64_t sourceSpanFrames_ = 0;
    StreamingOla ola_;
    StreamingVocoder vocoder_;

    double srcBaseFrame(double sourceOffsetTimeline) const;
    void reconfigureEngines();
};

}  // namespace pydaw::warp
