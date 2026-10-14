#include "warp_windows_fix.h"
#include "warp_processor.h"

#include "warp_resample.h"

namespace pydaw::warp {

void WarpProcessor::configure(WarpMethod method, float ratio,
                              int64_t sourceSpanFrames) {
    method_ = method;
    ratio_ = clampRatio(ratio);
    sourceSpanFrames_ = std::max(int64_t(1), sourceSpanFrames);
    reconfigureEngines();
    resetToTimelineRel(0);
}

void WarpProcessor::reconfigureEngines() {
    if (method_ == WarpMethod::Stretch) {
        ola_.configure(sourceSpanFrames_, ratio_);
    } else if (method_ == WarpMethod::StretchHd) {
        vocoder_.configure(sourceSpanFrames_, ratio_);
    }
}

double WarpProcessor::srcBaseFrame(double sourceOffsetTimeline) const {
    return sourceOffsetTimeline / double(ratio_);
}

void WarpProcessor::resetToTimelineRel(int64_t rel) {
    rel = std::max(int64_t(0), rel);
    if (method_ == WarpMethod::Stretch)
        ola_.resetToOutputPos(rel);
    else if (method_ == WarpMethod::StretchHd)
        vocoder_.resetToOutputPos(rel);
}

void WarpProcessor::processBlock(const float* src, int64_t fileFrames,
                                 double sourceOffsetTimeline, int64_t relStart,
                                 int outFrames, float* dstInterleaved,
                                 int64_t dstFrameOffset) {
    if (outFrames <= 0) return;

    if (method_ == WarpMethod::Resample || ratioIsUnity(ratio_)) {
        if (ratioIsUnity(ratio_) && method_ != WarpMethod::Resample) {
            for (int i = 0; i < outFrames; ++i) {
                const int64_t rel = relStart + i;
                const double srcPos = sourceOffsetTimeline + double(rel);
                const int64_t sf = int64_t(std::round(srcPos));
                if (sf < 0 || sf >= fileFrames) continue;
                const int64_t di = dstFrameOffset + i;
                dstInterleaved[di * 2] += src[sf * 2];
                dstInterleaved[di * 2 + 1] += src[sf * 2 + 1];
            }
            return;
        }
        resampleBlock(src, fileFrames, sourceOffsetTimeline, ratio_,
                      relStart, outFrames, dstInterleaved, dstFrameOffset);
        return;
    }

    const double srcBase = srcBaseFrame(sourceOffsetTimeline);
    if (method_ == WarpMethod::StretchHd) {
        vocoder_.processBlock(src, fileFrames, srcBase, relStart, outFrames,
                              dstInterleaved, dstFrameOffset);
    } else {
        ola_.processBlock(src, fileFrames, srcBase, relStart, outFrames,
                          dstInterleaved, dstFrameOffset);
    }
}

}  // namespace pydaw::warp
