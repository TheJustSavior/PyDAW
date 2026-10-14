#pragma once
#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <mutex>
#include <thread>
#include <vector>

// Persistent worker pool for parallel track rendering.
//
// Design constraints (audio thread friendliness):
//  - no allocations in run(): jobs are described by a plain function
//    pointer + context, workers claim indices from an atomic counter;
//  - the calling (audio) thread participates in the work and then
//    spin-yields until the last worker finishes (no blocking waits);
//  - workers sleep on a condition variable between blocks, so an idle
//    engine costs nothing.
class RenderPool {
public:
    // Global pool shared by all mixers (one audio callback at a time).
    static RenderPool& instance();

    ~RenderPool();

    // Worker threads (excluding the caller). 0 = parallelism disabled
    // (PYDAW_MIX_THREADS=0/1 or a single-core machine).
    int workerCount();

    // Start worker threads during engine startup so the first audio
    // callback never pays thread-spawn cost (which can xrun the driver).
    void warmup();
    // Stop worker threads during engine shutdown (before VST teardown).
    void shutdown();

    // Execute fn(ctx, i) for every i in [0, jobs). Blocks until all jobs
    // are done. Serialized globally; not reentrant on the same thread.
    void run(int jobs, void (*fn)(void*, int), void* ctx);

private:
    RenderPool() = default;
    void ensureStarted();
    void workerLoop();

    std::vector<std::thread> threads_;
    std::mutex mutex_;     // protects cv / generation handshake
    std::mutex runMutex_;  // one run() at a time (audio + export threads)
    std::condition_variable cv_;
    std::atomic<uint64_t> generation_{0};
    std::atomic<int> nextJob_{0};
    std::atomic<int> pending_{0};
    void (*fn_)(void*, int) = nullptr;
    void* ctx_ = nullptr;
    int jobs_ = 0;
    std::atomic<bool> quit_{false};
    bool started_ = false;
};
