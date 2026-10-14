#include "render_pool.h"

#include <algorithm>
#include <cstdlib>

namespace {

int configuredWorkers() {
    const unsigned hw = std::max(1u, std::thread::hardware_concurrency());
    // Leave one core for the audio callback / UI; cap to avoid
    // oversubscription on many-core machines (per-block work is small).
    int workers = int(hw) - 1;
    if (const char* env = std::getenv("PYDAW_MIX_THREADS")) {
        char* end = nullptr;
        const long n = std::strtol(env, &end, 10);
        // env counts total mixing threads incl. the audio thread.
        if (end != env && n >= 0) workers = int(n) - 1;
    }
    return std::max(0, std::min(workers, 7));
}

} // namespace

RenderPool& RenderPool::instance() {
    static RenderPool pool;
    return pool;
}

RenderPool::~RenderPool() {
    quit_.store(true, std::memory_order_release);
    {
        std::lock_guard<std::mutex> lock(mutex_);
        generation_.fetch_add(1, std::memory_order_release);
    }
    cv_.notify_all();
    for (auto& t : threads_)
        if (t.joinable()) t.join();
}

void RenderPool::ensureStarted() {
    if (started_) return;
    quit_.store(false, std::memory_order_release);
    started_ = true;
    const int n = configuredWorkers();
    threads_.reserve(size_t(n));
    for (int i = 0; i < n; ++i)
        threads_.emplace_back([this] { workerLoop(); });
}

int RenderPool::workerCount() {
    std::lock_guard<std::mutex> lock(mutex_);
    ensureStarted();
    return int(threads_.size());
}

void RenderPool::warmup() {
    std::lock_guard<std::mutex> lock(mutex_);
    ensureStarted();
}

void RenderPool::shutdown() {
    std::lock_guard<std::mutex> runLock(runMutex_);
    {
        std::lock_guard<std::mutex> lock(mutex_);
        quit_.store(true, std::memory_order_release);
        generation_.fetch_add(1, std::memory_order_release);
    }
    cv_.notify_all();

    for (auto& t : threads_) {
        if (t.joinable()) t.join();
    }
    threads_.clear();
    started_ = false;
    fn_ = nullptr;
    ctx_ = nullptr;
    jobs_ = 0;
    pending_.store(0, std::memory_order_release);
}

void RenderPool::workerLoop() {
    uint64_t seenGen = 0;
    for (;;) {
        {
            std::unique_lock<std::mutex> lock(mutex_);
            cv_.wait(lock, [&] {
                return quit_.load(std::memory_order_acquire)
                       || generation_.load(std::memory_order_acquire)
                              != seenGen;
            });
        }
        if (quit_.load(std::memory_order_acquire)) return;
        seenGen = generation_.load(std::memory_order_acquire);

        for (;;) {
            const int j = nextJob_.fetch_add(1, std::memory_order_relaxed);
            if (j >= jobs_) break;
            // Always decrement, even if a job throws: otherwise run()'s
            // completion wait spins forever on the audio thread.
            try {
                fn_(ctx_, j);
            } catch (...) {
            }
            pending_.fetch_sub(1, std::memory_order_release);
        }
    }
}

void RenderPool::run(int jobs, void (*fn)(void*, int), void* ctx) {
    if (jobs <= 0) return;

    // Serialize callers (realtime callback + offline export thread) so
    // shared job state (fn_, ctx_, pending_, …) is never torn down
    // while another thread is still executing or waiting inside run().
    std::lock_guard<std::mutex> runLock(runMutex_);

    bool parallel = false;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        ensureStarted();
        if (!quit_.load(std::memory_order_acquire)
            && !threads_.empty() && jobs > 1) {
            fn_ = fn;
            ctx_ = ctx;
            jobs_ = jobs;
            nextJob_.store(0, std::memory_order_relaxed);
            pending_.store(jobs, std::memory_order_relaxed);
            generation_.fetch_add(1, std::memory_order_release);
            parallel = true;
        }
    }

    if (!parallel) {
        for (int j = 0; j < jobs; ++j) fn(ctx, j);
        return;
    }

    cv_.notify_all();

    // The caller works too: claim jobs until the counter runs out.
    for (;;) {
        const int j = nextJob_.fetch_add(1, std::memory_order_relaxed);
        if (j >= jobs_) break;
        try {
            fn(ctx, j);
        } catch (...) {
        }
        pending_.fetch_sub(1, std::memory_order_release);
    }
    // Spin-yield for stragglers (bounded: per-track render of one block).
    while (pending_.load(std::memory_order_acquire) > 0)
        std::this_thread::yield();
}
