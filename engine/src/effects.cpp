// Built-in insert effects implementing EffectSlot.
// Parameters are atomics so the UI thread can change them while the
// audio thread is processing.

#include "mixer.h"

#include <atomic>
#include <cmath>
#include <cstring>
#include <string>
#include <vector>

namespace {

constexpr float kPi = 3.14159265358979f;

// Stereo delay: params [0]=time seconds (0.02..2), [1]=feedback (0..0.95),
// [2]=wet mix (0..1).
class DelayEffect : public EffectSlot {
public:
    void prepare(uint32_t sampleRate) override {
        sampleRate_ = sampleRate;
        buffer_.assign(size_t(sampleRate) * 4, 0.0f); // 2 s stereo
        writePos_ = 0;
    }

    void setParam(int index, float value) override {
        switch (index) {
        case 0: time_.store(value, std::memory_order_relaxed); break;
        case 1: feedback_.store(value, std::memory_order_relaxed); break;
        case 2: mix_.store(value, std::memory_order_relaxed); break;
        }
    }

    const char* name() const override { return "delay"; }

    void process(float* buf, uint32_t frames) override {
        if (buffer_.empty()) prepare(sampleRate_);
        const float time = std::min(1.9f, std::max(0.02f,
            time_.load(std::memory_order_relaxed)));
        const float fb = std::min(0.95f, std::max(0.0f,
            feedback_.load(std::memory_order_relaxed)));
        const float wet = std::min(1.0f, std::max(0.0f,
            mix_.load(std::memory_order_relaxed)));

        const size_t delayFrames = size_t(time * float(sampleRate_));
        const size_t bufFrames = buffer_.size() / 2;

        for (uint32_t i = 0; i < frames; ++i) {
            const size_t readPos =
                (writePos_ + bufFrames - delayFrames) % bufFrames;
            for (int c = 0; c < 2; ++c) {
                const float dry = buf[i * 2 + c];
                const float echo = buffer_[readPos * 2 + c];
                buffer_[writePos_ * 2 + c] = dry + echo * fb;
                buf[i * 2 + c] = dry + echo * wet;
            }
            writePos_ = (writePos_ + 1) % bufFrames;
        }
    }

private:
    uint32_t sampleRate_ = 48000;
    std::vector<float> buffer_;
    size_t writePos_ = 0;
    std::atomic<float> time_{0.35f};
    std::atomic<float> feedback_{0.35f};
    std::atomic<float> mix_{0.3f};
};

// One-pole state-variable style biquad low/high-pass.
// Params: [0]=cutoff Hz (40..18000), [1]=mode (0=lowpass, 1=highpass).
class FilterEffect : public EffectSlot {
public:
    void prepare(uint32_t sampleRate) override {
        sampleRate_ = sampleRate;
        std::memset(state_, 0, sizeof(state_));
    }

    void setParam(int index, float value) override {
        switch (index) {
        case 0: cutoff_.store(value, std::memory_order_relaxed); break;
        case 1: mode_.store(int(value), std::memory_order_relaxed); break;
        }
    }

    const char* name() const override { return "filter"; }

    void process(float* buf, uint32_t frames) override {
        const float cutoff = std::min(18000.0f, std::max(40.0f,
            cutoff_.load(std::memory_order_relaxed)));
        const int highpass = mode_.load(std::memory_order_relaxed);

        // Biquad Butterworth coefficients (RBJ cookbook, Q = 0.7071).
        const float w0 = 2.0f * kPi * cutoff / float(sampleRate_);
        const float cw = std::cos(w0), sw = std::sin(w0);
        const float alpha = sw / (2.0f * 0.70710678f);
        const float a0 = 1.0f + alpha;
        float b0, b1, b2;
        if (highpass) {
            b0 = (1.0f + cw) * 0.5f; b1 = -(1.0f + cw); b2 = b0;
        } else {
            b0 = (1.0f - cw) * 0.5f; b1 = 1.0f - cw; b2 = b0;
        }
        const float a1 = -2.0f * cw, a2 = 1.0f - alpha;
        const float nb0 = b0 / a0, nb1 = b1 / a0, nb2 = b2 / a0;
        const float na1 = a1 / a0, na2 = a2 / a0;

        for (int c = 0; c < 2; ++c) {
            float x1 = state_[c][0], x2 = state_[c][1];
            float y1 = state_[c][2], y2 = state_[c][3];
            for (uint32_t i = 0; i < frames; ++i) {
                const float x = buf[i * 2 + c];
                const float y = nb0 * x + nb1 * x1 + nb2 * x2
                              - na1 * y1 - na2 * y2;
                x2 = x1; x1 = x;
                y2 = y1; y1 = y;
                buf[i * 2 + c] = y;
            }
            state_[c][0] = x1; state_[c][1] = x2;
            state_[c][2] = y1; state_[c][3] = y2;
        }
    }

private:
    uint32_t sampleRate_ = 48000;
    float state_[2][4] = {};
    std::atomic<float> cutoff_{2000.0f};
    std::atomic<int> mode_{0};
};

} // namespace

EffectPtr createEffect(const std::string& type) {
    if (type == "delay") return std::make_shared<DelayEffect>();
    if (type == "filter") return std::make_shared<FilterEffect>();
    return nullptr;
}
