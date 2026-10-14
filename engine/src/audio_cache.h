#pragma once
#include <cstdint>
#include <string>

// Disk cache shared by streamed PCM data and waveform peaks.
//
// Layout: <cacheDir>/<key>.pcm   raw interleaved stereo float32 frames
//         <cacheDir>/<key>.peaks base-resolution min/max peak pairs
// The key is a content hash of (source path, size, mtime, engine rate),
// so edits to the source file invalidate the cache automatically.
namespace audiocache {

// Cache root: PYDAW_CACHE_DIR env override, else %LOCALAPPDATA%/PyDAW/cache
// (or the system temp dir as a last resort). Created on demand; empty
// string when no writable location exists.
std::string cacheDir();

// Stable content key for a source file at a given engine sample rate.
std::string fileKey(const std::string& path, uint32_t sampleRate);

// Atomic-ish write: data is written to a temp file and renamed into place.
// Returns false on any I/O failure (cache is best-effort).
bool writeFile(const std::string& path, const void* data, uint64_t bytes);

// Cache size limit in bytes: PYDAW_CACHE_LIMIT_MB env override
// (default 2048 MB; 0 disables trimming).
uint64_t limitBytes();

// Bump a cache entry's mtime so LRU trimming sees it as recently used.
void touchFile(const std::string& path);

// LRU trim: if the cache directory (recursively, including the Python
// stretch/join sub-caches) exceeds limitBytes(), delete oldest-mtime
// files until it fits. Best-effort: in-use (memory-mapped) files that
// refuse deletion are skipped. Returns bytes freed.
uint64_t enforceLimit();

// Run enforceLimit() on a detached background thread (engine startup).
void enforceLimitAsync();

} // namespace audiocache

// Read-only memory-mapped file. The OS pages data in on demand, which
// gives streaming behaviour for large decoded files without a prefetch
// thread: untouched regions cost no physical RAM.
class MappedFile {
public:
    MappedFile() = default;
    ~MappedFile();
    MappedFile(const MappedFile&) = delete;
    MappedFile& operator=(const MappedFile&) = delete;

    bool open(const std::string& path);
    void close();
    bool isOpen() const { return data_ != nullptr; }
    const void* data() const { return data_; }
    uint64_t size() const { return size_; }

private:
    void* data_ = nullptr;
    uint64_t size_ = 0;
#ifdef _WIN32
    void* fileHandle_ = nullptr;
    void* mapHandle_ = nullptr;
#else
    int fd_ = -1;
#endif
};
