#include "warp_windows_fix.h"
#include "warp_fft.h"

#include "warp_math.h"

#include <cmath>
#include <stdexcept>

namespace pydaw::warp {

namespace {

bool isPowerOfTwo(int n) {
    return n > 0 && (n & (n - 1)) == 0;
}

}  // namespace

RealFft::RealFft(int size) : size_(size), nBins_(size / 2 + 1) {
    if (!isPowerOfTwo(size) || size < 4)
        throw std::invalid_argument("RealFft size must be power of 2 >= 4");

    bitRev_.resize(size_);
    const int log2n = int(std::log2(size_));
    for (int i = 0; i < size_; ++i) {
        int j = 0;
        for (int b = 0, x = i; b < log2n; ++b) {
            j = (j << 1) | (x & 1);
            x >>= 1;
        }
        bitRev_[i] = j;
    }

    twiddle_.resize(size_ / 2);
    for (int k = 0; k < size_ / 2; ++k) {
        const float ang = -2.f * float(M_PI) * float(k) / float(size_);
        twiddle_[k] = {std::cos(ang), std::sin(ang)};
    }

    work_.resize(size_);
}

void RealFft::fftInPlace(std::complex<float>* data, bool inverse) {
    for (int i = 0; i < size_; ++i) {
        if (i < bitRev_[i])
            std::swap(data[i], data[bitRev_[i]]);
    }

    for (int len = 2; len <= size_; len <<= 1) {
        const int half = len / 2;
        const int step = size_ / len;
        for (int i = 0; i < size_; i += len) {
            for (int j = 0; j < half; ++j) {
                const auto t = twiddle_[j * step];
                const auto u = data[i + j];
                const auto v = data[i + j + half] * (inverse ? std::conj(t) : t);
                data[i + j] = u + v;
                data[i + j + half] = u - v;
            }
        }
    }

    if (inverse) {
        const float scale = 1.f / float(size_);
        for (int i = 0; i < size_; ++i)
            data[i] *= scale;
    }
}

void RealFft::forward(const float* frame,
                      std::vector<std::complex<float>>& spec) {
    for (int i = 0; i < size_; ++i)
        work_[i] = {frame[i], 0.f};

    fftInPlace(work_.data(), false);

    spec.resize(size_t(nBins_));
    spec[0] = work_[0];
    for (int k = 1; k < nBins_ - 1; ++k)
        spec[k] = work_[k];
    if (nBins_ > 1)
        spec[nBins_ - 1] = work_[size_ / 2];
}

void RealFft::inverse(const std::vector<std::complex<float>>& spec,
                      float* frame) {
    work_[0] = spec[0];
    for (int k = 1; k < size_ / 2; ++k)
        work_[k] = spec[k];
    work_[size_ / 2] = spec[nBins_ - 1];
    for (int k = size_ / 2 + 1; k < size_; ++k)
        work_[k] = std::conj(work_[size_ - k]);

    fftInPlace(work_.data(), true);

    for (int i = 0; i < size_; ++i)
        frame[i] = work_[i].real();
}

}  // namespace pydaw::warp
