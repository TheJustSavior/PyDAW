#pragma once

#include "warp_fft.h"
#include "warp_ola.h"

#include <cstdint>
#include <memory>
#include <vector>

namespace pydaw::warp {

void stretchVocoderOffline(const float* src, int64_t frames, int channels,
                           float ratio, std::vector<float>& out);

class StreamingVocoder {
public:
    void configure(int64_t sourceFrames, float ratio);
    void resetToOutputPos(int64_t outPos);

    void processBlock(const float* src, int64_t fileFrames,
                      double srcBaseFrame, int64_t relStart, int outFrames,
                      float* dstInterleaved, int64_t dstFrameOffset);

private:
    float ratio_ = 1.f;
    int nWin_ = 2048;
    int synHop_ = 512;
    double anaHop_ = 512.0;
    int64_t sourceFrames_ = 0;
    int64_t outLimit_ = 0;
    int64_t bufferOrigin_ = 0;
    bool useOlaFallback_ = false;

    StreamingOla olaFallback_;

    std::vector<float> hann_;
    std::vector<float> outBuf_;
    std::vector<float> normBuf_;
    std::vector<float> freqScale_;
    std::vector<float> inGrain_;
    std::vector<float> outGrain_;
    std::vector<std::complex<float>> spectrum_;
    std::vector<float> phaseAccL_, phaseAccR_, prevPhaseL_, prevPhaseR_;
    bool havePhaseL_ = false;
    bool havePhaseR_ = false;

    double anaPos_ = 0.0;
    int64_t outPos_ = 0;
    int64_t streamOutPos_ = 0;

    std::unique_ptr<RealFft> fft_;

    void ensureCapacity(int64_t frames);
    void slideWindow(int64_t newOrigin);
    void prepareWindow(int64_t relStart, int64_t needEnd);
    void synthesizeHop(const float* src, int64_t fileFrames, double srcBase);
    void runUntil(int64_t targetOutPos, const float* src, int64_t fileFrames,
                  double srcBase);
    void processChannelHop(const float* src, int64_t fileFrames, double srcBase,
                           int ch, std::vector<float>& phaseAcc,
                           std::vector<float>& prevPhase, bool& havePhase,
                           float* outAcc, float* normAcc);
};

}  // namespace pydaw::warp
