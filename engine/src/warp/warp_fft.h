#pragma once

#include <complex>
#include <cstdint>
#include <vector>

namespace pydaw::warp {

// In-place real FFT (forward) and inverse for power-of-two sizes.
class RealFft {
public:
    explicit RealFft(int size);

    int size() const { return size_; }

    // Input: real time-domain frame (length size). Output: nBins complex
    // spectrum (size/2 + 1), matching numpy rfft layout.
    void forward(const float* frame, std::vector<std::complex<float>>& spec);

    // Input: spectrum (nBins). Output: real frame (length size).
    void inverse(const std::vector<std::complex<float>>& spec, float* frame);

private:
    int size_;
    int nBins_;
    std::vector<std::complex<float>> twiddle_;
    std::vector<int> bitRev_;
    std::vector<std::complex<float>> work_;

    void fftInPlace(std::complex<float>* data, bool inverse);
};

}  // namespace pydaw::warp
