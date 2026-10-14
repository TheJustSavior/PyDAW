#include "warp_windows_fix.h"
#include "warp_ola.h"

#include "warp_constants.h"
#include "warp_math.h"

#include <algorithm>
#include <cmath>

namespace pydaw::warp {

int chooseOlaWinLen(int64_t sourceFrames) {
    int nWin = 4096;
    if (sourceFrames < nWin * 2) {
        const int log2v = std::max(2, int(std::log2(
            std::max(int64_t(4), sourceFrames / 4))));
        nWin = std::max(256, 1 << log2v);
    }
    return nWin;
}

void stretchOlaOffline(const float* src, int64_t frames, int channels,
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

    const int nWin = chooseOlaWinLen(frames);
    const int synHop = nWin / 4;
    const double anaHop = double(synHop) / double(ratio);
    std::vector<float> hann;
    makeHanning(nWin, hann);

    const int64_t outFrames = std::max(int64_t(1),
                                       int64_t(std::llround(double(frames)
                                                            * double(ratio))));
    const int64_t bufLen = outFrames + nWin;
    std::vector<float> acc(size_t(bufLen * channels), 0.f);
    std::vector<float> norm(size_t(bufLen), 0.f);

    double pos = 0.0;
    int64_t outPos = 0;
    std::vector<float> chunk(size_t(nWin * channels), 0.f);

    while (outPos < outFrames) {
        const int start = int(std::round(pos));
        std::fill(chunk.begin(), chunk.end(), 0.f);
        const int avail = int(std::min(int64_t(nWin), frames - start));
        if (avail > 0) {
            for (int i = 0; i < avail; ++i) {
                for (int ch = 0; ch < channels; ++ch)
                    chunk[size_t(i * channels + ch)] =
                        src[size_t((start + i) * channels + ch)];
            }
        }
        for (int i = 0; i < nWin; ++i) {
            const float w = hann[i];
            for (int ch = 0; ch < channels; ++ch) {
                acc[size_t((outPos + i) * channels + ch)] +=
                    chunk[size_t(i * channels + ch)] * w;
            }
            norm[size_t(outPos + i)] += w;
        }
        pos += anaHop;
        outPos += synHop;
    }

    out.resize(size_t(outFrames * channels));
    for (int64_t i = 0; i < outFrames; ++i) {
        const float n = std::max(norm[size_t(i)], 1e-6f);
        for (int ch = 0; ch < channels; ++ch)
            out[size_t(i * channels + ch)] =
                acc[size_t(i * channels + ch)] / n;
    }
}

void StreamingOla::configure(int64_t sourceFrames, float ratio, int nWin) {
    ratio_ = clampRatio(ratio);
    sourceFrames_ = sourceFrames;
    nWin_ = nWin > 0 ? nWin : chooseOlaWinLen(sourceFrames);
    synHop_ = nWin_ / 4;
    anaHop_ = double(synHop_) / double(ratio_);
    makeHanning(nWin_, hann_);
    outLimit_ = std::max(int64_t(1),
                         int64_t(std::llround(double(sourceFrames)
                                              * double(ratio_))));
    ensureCapacity(int64_t(nWin_) * 2);
    resetToOutputPos(0);
}

void StreamingOla::ensureCapacity(int64_t frames) {
    const size_t need = size_t(std::max<int64_t>(frames, nWin_ * 2));
    if (outBuf_.size() < need * 2)
        outBuf_.resize(need * 2);
    if (normBuf_.size() < need)
        normBuf_.resize(need);
}

void StreamingOla::slideWindow(int64_t newOrigin) {
    if (newOrigin == bufferOrigin_) return;
    std::vector<float> newOut(outBuf_.size(), 0.f);
    std::vector<float> newNorm(normBuf_.size(), 0.f);
    const int64_t oldOrigin = bufferOrigin_;
    const int64_t oldEnd = oldOrigin + int64_t(normBuf_.size());
    const int64_t newEnd = newOrigin + int64_t(newNorm.size());
    const int64_t copyStart = std::max(oldOrigin, newOrigin);
    const int64_t copyEnd = std::min(oldEnd, newEnd);
    for (int64_t pos = copyStart; pos < copyEnd; ++pos) {
        const int64_t oldIdx = pos - oldOrigin;
        const int64_t newIdx = pos - newOrigin;
        newNorm[size_t(newIdx)] = normBuf_[size_t(oldIdx)];
        newOut[size_t(newIdx * 2)] = outBuf_[size_t(oldIdx * 2)];
        newOut[size_t(newIdx * 2 + 1)] = outBuf_[size_t(oldIdx * 2 + 1)];
    }
    outBuf_.swap(newOut);
    normBuf_.swap(newNorm);
    bufferOrigin_ = newOrigin;
}

void StreamingOla::prepareWindow(int64_t relStart, int64_t needEnd) {
    const int64_t start = std::max<int64_t>(0, relStart);
    const int64_t end = std::min<int64_t>(outLimit_, needEnd + nWin_);
    ensureCapacity(std::max<int64_t>(1, end - start));
    if (start < bufferOrigin_ || end > bufferOrigin_ + int64_t(normBuf_.size()))
        slideWindow(start);
}

void StreamingOla::resetToOutputPos(int64_t outPos) {
    outPos = std::max(int64_t(0), std::min(outPos, outLimit_));
    const int64_t hopOut =
        synHop_ > 0 ? (outPos / synHop_) * synHop_ : outPos;
    const int64_t nHops = synHop_ > 0 ? hopOut / synHop_ : 0;
    anaPos_ = double(nHops) * anaHop_;
    outPos_ = hopOut;
    bufferOrigin_ = hopOut;
    streamOutPos_ = outPos;
    ensureCapacity(int64_t(nWin_) * 2);
    std::fill(outBuf_.begin(), outBuf_.end(), 0.f);
    std::fill(normBuf_.begin(), normBuf_.end(), 0.f);
}

void StreamingOla::synthesizeHop(const float* src, int64_t fileFrames,
                                 double srcBase) {
    const int start = int(std::round(anaPos_));
    for (int i = 0; i < nWin_; ++i) {
        const float w = hann_[i];
        float l = 0.f, r = 0.f;
        const int64_t srcIdx = int64_t(std::llround(srcBase)) + start + i;
        if (src && fileFrames > 0 && srcIdx >= 0 && srcIdx < fileFrames) {
            l = src[srcIdx * 2];
            r = src[srcIdx * 2 + 1];
        }
        const int64_t o = outPos_ + i;
        if (o >= outLimit_ + nWin_) break;
        const int64_t bi = o - bufferOrigin_;
        if (bi < 0 || bi >= int64_t(normBuf_.size())) continue;
        outBuf_[size_t(bi * 2)] += l * w;
        outBuf_[size_t(bi * 2 + 1)] += r * w;
        normBuf_[size_t(bi)] += w;
    }
    anaPos_ += anaHop_;
    outPos_ += synHop_;
}

void StreamingOla::runUntil(int64_t targetOutPos, const float* src,
                            int64_t fileFrames, double srcBase) {
    targetOutPos = std::min(targetOutPos, outLimit_);
    while (outPos_ < targetOutPos + nWin_ && outPos_ < outLimit_)
        synthesizeHop(src, fileFrames, srcBase);
}

void StreamingOla::processBlock(const float* src, int64_t fileFrames,
                                double srcBaseFrame, int64_t relStart,
                                int outFrames, float* dstInterleaved,
                                int64_t dstFrameOffset) {
    if (outFrames <= 0 || ratioIsUnity(ratio_)) return;

    if (relStart != streamOutPos_)
        resetToOutputPos(relStart);

    const int64_t needEnd = relStart + outFrames;
    prepareWindow(relStart, needEnd);
    runUntil(needEnd, src, fileFrames, srcBaseFrame);
    streamOutPos_ = needEnd;

    for (int i = 0; i < outFrames; ++i) {
        const int64_t op = relStart + i;
        if (op >= outLimit_) break;
        const int64_t bi = op - bufferOrigin_;
        if (bi < 0 || bi >= int64_t(normBuf_.size())) continue;
        const float n = std::max(normBuf_[size_t(bi)], 1e-6f);
        const float l = outBuf_[size_t(bi * 2)] / n;
        const float r = outBuf_[size_t(bi * 2 + 1)] / n;
        const int64_t di = dstFrameOffset + i;
        dstInterleaved[di * 2] += l;
        dstInterleaved[di * 2 + 1] += r;
    }
}

}  // namespace pydaw::warp
