#pragma once
#include <atomic>
#include <cstddef>
#include <vector>

// Single-producer single-consumer lock-free ring buffer.
// Used to pass commands from the Python/UI thread into the audio thread.
template <typename T>
class RingBuffer {
public:
    explicit RingBuffer(size_t capacity = 1024)
        : buffer_(capacity), capacity_(capacity) {}

    bool push(const T& item) {
        const size_t head = head_.load(std::memory_order_relaxed);
        const size_t next = (head + 1) % capacity_;
        if (next == tail_.load(std::memory_order_acquire))
            return false; // full
        buffer_[head] = item;
        head_.store(next, std::memory_order_release);
        return true;
    }

    bool pop(T& item) {
        const size_t tail = tail_.load(std::memory_order_relaxed);
        if (tail == head_.load(std::memory_order_acquire))
            return false; // empty
        item = buffer_[tail];
        tail_.store((tail + 1) % capacity_, std::memory_order_release);
        return true;
    }

private:
    std::vector<T> buffer_;
    size_t capacity_;
    std::atomic<size_t> head_{0};
    std::atomic<size_t> tail_{0};
};
