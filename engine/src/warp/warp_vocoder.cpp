#include "warp_windows_fix.h"
#include "warp_vocoder.h"

#include "warp_constants.h"
#include "warp_math.h"

#include <algorithm>
#include <cmath>

namespace pydaw::warp {

namespace {

float wrapPhase(float dphi) {
    const float twoPi = 2.f * float(M_PI);
    dphi -= twoPi * std::round(dphi / twoPi);
    return dphi;
}

}  // namespace

void stretchVocoderOffline(const float* src, int64_t frames, int channels,
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

    const int nWin = 2048;
    if (frames < nWin * 2) {
        stretchOlaOffline(src, frames, channels, ratio, out);
        return;
    }

    const int synHop = nWin / 4;
    const double anaHop = double(synHop) / double(ratio);
    std::vector<float> hann;
    makeHanning(nWin, hann);
    RealFft fft(nWin);
    const int nBins = nWin / 2 + 1;

    std::vector<float> freqScale;
    freqScale.resize(size_t(nBins));
    for (int k = 0; k < nBins; ++k)
        freqScale[k] = 2.f * float(M_PI) * float(k) * float(synHop) / float(nWin);

    const int64_t outFrames = std::max(int64_t(1),
                                       int64_t(std::llround(double(frames)
                                                            * double(ratio))));
    out.resize(size_t(outFrames * channels));
    std::vector<float> acc(size_t((outFrames + nWin) * channels), 0.f);
    std::vector<float> norm(size_t(outFrames + nWin), 0.f);

    std::vector<float> inGrain(size_t(nWin), 0.f);
    std::vector<float> outGrain(size_t(nWin), 0.f);
    std::vector<std::complex<float>> spectrum;
    std::vector<float> phaseAcc, prevPhase;

    for (int ch = 0; ch < channels; ++ch) {
        phaseAcc.clear();
        prevPhase.clear();
        bool havePhase = false;
        double pos = 0.0;
        int64_t outPos = 0;
        while (outPos < outFrames) {
            const int start = int(std::round(pos));
            std::fill(inGrain.begin(), inGrain.end(), 0.f);
            const int avail = int(std::min(int64_t(nWin), frames - start));
            if (avail > 0) {
                for (int i = 0; i < avail; ++i)
                    inGrain[i] = src[size_t((start + i) * channels + ch)];
            }
            for (int i = 0; i < nWin; ++i)
                inGrain[i] *= hann[i];

            fft.forward(inGrain.data(), spectrum);

            if (!havePhase) {
                phaseAcc.resize(size_t(nBins));
                prevPhase.resize(size_t(nBins));
                for (int k = 0; k < nBins; ++k) {
                    phaseAcc[k] = std::arg(spectrum[k]);
                    prevPhase[k] = phaseAcc[k];
                }
                havePhase = true;
            } else {
                for (int k = 0; k < nBins; ++k) {
                    const float mag = std::abs(spectrum[k]);
                    const float phase = std::arg(spectrum[k]);
                    float dphi = phase - prevPhase[k]
                                 - freqScale[k] * float(anaHop / synHop);
                    dphi = wrapPhase(dphi);
                    const float trueFreq = freqScale[k]
                        + (anaHop > 0 ? dphi * float(synHop) / float(anaHop)
                                      : 0.f);
                    phaseAcc[k] += trueFreq;
                    prevPhase[k] = phase;
                    spectrum[k] = {mag * std::cos(phaseAcc[k]),
                                   mag * std::sin(phaseAcc[k])};
                }
            }

            {
                float* outPtr = outGrain.data();
                fft.inverse(spectrum, outPtr);
            }
            for (int i = 0; i < nWin; ++i) {
                const int64_t o = outPos + i;
                if (o >= outFrames) break;
                const float w = hann[i];
                acc[size_t(o * channels + ch)] += outGrain[i] * w;
                if (ch == 0)
                    norm[size_t(o)] += w * w;
            }
            pos += anaHop;
            outPos += synHop;
        }
    }

    for (int64_t i = 0; i < outFrames; ++i) {
        const float n = std::max(norm[size_t(i)], 1e-6f);
        for (int ch = 0; ch < channels; ++ch)
            out[size_t(i * channels + ch)] =
                acc[size_t(i * channels + ch)] / n;
    }
}

void StreamingVocoder::configure(int64_t sourceFrames, float ratio) {
    ratio_ = clampRatio(ratio);
    sourceFrames_ = sourceFrames;
    nWin_ = 2048;
    useOlaFallback_ = sourceFrames < nWin_ * 2;
    if (useOlaFallback_) {
        olaFallback_.configure(sourceFrames, ratio_);
        return;
    }
    synHop_ = nWin_ / 4;
    anaHop_ = double(synHop_) / double(ratio_);
    makeHanning(nWin_, hann_);
    fft_ = std::make_unique<RealFft>(nWin_);
    const int nBins = nWin_ / 2 + 1;
    freqScale_.resize(size_t(nBins));
    for (int k = 0; k < nBins; ++k)
        freqScale_[k] = 2.f * float(M_PI) * float(k) * float(synHop_) / float(nWin_);
    phaseAccL_.assign(size_t(nBins), 0.f);
    phaseAccR_.assign(size_t(nBins), 0.f);
    prevPhaseL_.assign(size_t(nBins), 0.f);
    prevPhaseR_.assign(size_t(nBins), 0.f);
    inGrain_.assign(size_t(nWin_), 0.f);
    outGrain_.assign(size_t(nWin_), 0.f);
    outLimit_ = std::max(int64_t(1),
                         int64_t(std::llround(double(sourceFrames_)
                                              * double(ratio_))));
    ensureCapacity(int64_t(nWin_) * 2);
    resetToOutputPos(0);
}

void StreamingVocoder::ensureCapacity(int64_t frames) {
    const size_t need = size_t(std::max<int64_t>(frames, nWin_ * 2));
    if (outBuf_.size() < need * 2)
        outBuf_.resize(need * 2);
    if (normBuf_.size() < need)
        normBuf_.resize(need);
}

void StreamingVocoder::slideWindow(int64_t newOrigin) {
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

void StreamingVocoder::prepareWindow(int64_t relStart, int64_t needEnd) {
    const int64_t start = std::max<int64_t>(0, relStart);
    const int64_t end = std::min<int64_t>(outLimit_, needEnd + nWin_);
    ensureCapacity(std::max<int64_t>(1, end - start));
    if (start < bufferOrigin_ || end > bufferOrigin_ + int64_t(normBuf_.size()))
        slideWindow(start);
}

void StreamingVocoder::resetToOutputPos(int64_t outPos) {
    if (useOlaFallback_) {
        olaFallback_.resetToOutputPos(outPos);
        return;
    }
    outPos = std::max(int64_t(0), std::min(outPos, outLimit_));
    const int64_t hopOut =
        synHop_ > 0 ? (outPos / synHop_) * synHop_ : outPos;
    const int64_t nHops = synHop_ > 0 ? hopOut / synHop_ : 0;
    anaPos_ = double(nHops) * anaHop_;
    outPos_ = hopOut;
    bufferOrigin_ = hopOut;
    streamOutPos_ = outPos;
    havePhaseL_ = false;
    havePhaseR_ = false;
    ensureCapacity(int64_t(nWin_) * 2);
    std::fill(outBuf_.begin(), outBuf_.end(), 0.f);
    std::fill(normBuf_.begin(), normBuf_.end(), 0.f);
}

void StreamingVocoder::processChannelHop(const float* src, int64_t fileFrames,
                                         double srcBase, int ch,
                                         std::vector<float>& phaseAcc,
                                         std::vector<float>& prevPhase,
                                         bool& havePhase,
                                         float* outAcc, float* normAcc) {
    const int start = int(std::round(anaPos_));
    std::fill(inGrain_.begin(), inGrain_.end(), 0.f);
    for (int i = 0; i < nWin_; ++i) {
        const int64_t idx = int64_t(std::llround(srcBase)) + start + i;
        if (src && fileFrames > 0 && idx >= 0 && idx < fileFrames)
            inGrain_[i] = src[idx * 2 + ch];
        inGrain_[i] *= hann_[i];
    }

    fft_->forward(inGrain_.data(), spectrum_);
    const int nBins = nWin_ / 2 + 1;

    if (!havePhase) {
        for (int k = 0; k < nBins; ++k) {
            phaseAcc[k] = std::arg(spectrum_[k]);
            prevPhase[k] = phaseAcc[k];
        }
        havePhase = true;
    } else {
        for (int k = 0; k < nBins; ++k) {
            const float mag = std::abs(spectrum_[k]);
            const float phase = std::arg(spectrum_[k]);
            float dphi = phase - prevPhase[k]
                         - freqScale_[k] * float(anaHop_ / synHop_);
            dphi = wrapPhase(dphi);
            const float trueFreq = freqScale_[k]
                + (anaHop_ > 0 ? dphi * float(synHop_) / float(anaHop_) : 0.f);
            phaseAcc[k] += trueFreq;
            prevPhase[k] = phase;
            spectrum_[k] = {mag * std::cos(phaseAcc[k]),
                            mag * std::sin(phaseAcc[k])};
        }
    }

    {
        float* outPtr = outGrain_.data();
        fft_->inverse(spectrum_, outPtr);
    }
    for (int i = 0; i < nWin_; ++i) {
        const float w = hann_[i];
        const float v = outGrain_[i] * w;
        const int64_t o = outPos_ + i;
        if (o >= outLimit_) break;
        const int64_t bi = o - bufferOrigin_;
        if (bi < 0 || bi >= int64_t(normBuf_.size())) continue;
        outAcc[bi * 2 + ch] += v;
        if (ch == 0)
            normAcc[bi] += w * w;
    }
}

void StreamingVocoder::synthesizeHop(const float* src, int64_t fileFrames,
                                   double srcBase) {
    processChannelHop(src, fileFrames, srcBase, 0, phaseAccL_, prevPhaseL_,
                      havePhaseL_, outBuf_.data(), normBuf_.data());
    processChannelHop(src, fileFrames, srcBase, 1, phaseAccR_, prevPhaseR_,
                      havePhaseR_, outBuf_.data(), normBuf_.data());
    anaPos_ += anaHop_;
    outPos_ += synHop_;
}

void StreamingVocoder::runUntil(int64_t targetOutPos, const float* src,
                                int64_t fileFrames, double srcBase) {
    targetOutPos = std::min(targetOutPos, outLimit_);
    while (outPos_ < targetOutPos + nWin_ && outPos_ < outLimit_)
        synthesizeHop(src, fileFrames, srcBase);
}

void StreamingVocoder::processBlock(const float* src, int64_t fileFrames,
                                    double srcBaseFrame, int64_t relStart,
                                    int outFrames, float* dstInterleaved,
                                    int64_t dstFrameOffset) {
    if (useOlaFallback_) {
        olaFallback_.processBlock(src, fileFrames, srcBaseFrame, relStart,
                                  outFrames, dstInterleaved, dstFrameOffset);
        return;
    }
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
