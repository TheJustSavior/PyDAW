#pragma once

#include <cstdint>
#include <vector>

namespace pydaw::warp {

int chooseOlaWinLen(int64_t sourceFrames);

void stretchOlaOffline(const float* src, int64_t frames, int channels,
                       float ratio, std::vector<float>& out);

// Streaming pitch-preserving OLA for stereo interleaved source.
class StreamingOla {
public:
    void configure(int64_t sourceFrames, float ratio, int nWin = 0);
    void resetToOutputPos(int64_t outPos);

    // Produce outFrames samples starting at timeline relStart; accumulates
    // into dstInterleaved at dstFrameOffset (+= with env applied outside).
    void processBlock(const float* src, int64_t fileFrames,
                      double srcBaseFrame, int64_t relStart, int outFrames,
                      float* dstInterleaved, int64_t dstFrameOffset);

private:
    float ratio_ = 1.f;
    int nWin_ = 4096;
    int synHop_ = 1024;
    double anaHop_ = 1024.0;
    std::vector<float> hann_;
    std::vector<float> outBuf_;
    std::vector<float> normBuf_;
    int64_t sourceFrames_ = 0;
    int64_t outLimit_ = 0;
    int64_t bufferOrigin_ = 0;

    double anaPos_ = 0.0;
    int64_t outPos_ = 0;
    int64_t streamOutPos_ = 0;

    void ensureCapacity(int64_t frames);
    void slideWindow(int64_t newOrigin);
    void prepareWindow(int64_t relStart, int64_t needEnd);
    void synthesizeHop(const float* src, int64_t fileFrames, double srcBase);
    void runUntil(int64_t targetOutPos, const float* src, int64_t fileFrames,
                  double srcBase);
};

}  // namespace pydaw::warp
