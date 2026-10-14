#include "audio_cache.h"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <system_error>
#include <thread>
#include <vector>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#else
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace fs = std::filesystem;

namespace audiocache {

namespace {

// FNV-1a 64-bit: stable, fast, good enough for cache keys.
uint64_t fnv1a(const void* data, size_t len, uint64_t seed = 1469598103934665603ull) {
    const auto* p = static_cast<const unsigned char*>(data);
    uint64_t h = seed;
    for (size_t i = 0; i < len; ++i) {
        h ^= p[i];
        h *= 1099511628211ull;
    }
    return h;
}

std::string toHex(uint64_t v) {
    char buf[17];
    std::snprintf(buf, sizeof(buf), "%016llx", (unsigned long long)v);
    return buf;
}

} // namespace

std::string cacheDir() {
    static std::string dir = [] {
        fs::path root;
        if (const char* env = std::getenv("PYDAW_CACHE_DIR"); env && env[0]) {
            root = fs::u8path(env);
        } else {
#ifdef _WIN32
            if (const char* la = std::getenv("LOCALAPPDATA"); la && la[0])
                root = fs::u8path(la) / "PyDAW" / "cache";
#else
            if (const char* home = std::getenv("HOME"); home && home[0])
                root = fs::u8path(home) / ".cache" / "pydaw";
#endif
        }
        if (root.empty()) {
            std::error_code ec;
            root = fs::temp_directory_path(ec) / "pydaw_cache";
            if (ec) return std::string{};
        }
        std::error_code ec;
        fs::create_directories(root, ec);
        if (ec && !fs::exists(root)) return std::string{};
        return root.u8string();
    }();
    return dir;
}

std::string fileKey(const std::string& path, uint32_t sampleRate) {
    std::error_code ec;
    const fs::path p = fs::u8path(path);
    const uint64_t size = fs::file_size(p, ec);
    if (ec) return {};
    const auto mtime = fs::last_write_time(p, ec);
    if (ec) return {};
    const int64_t mt = int64_t(mtime.time_since_epoch().count());

    uint64_t h = fnv1a(path.data(), path.size());
    h = fnv1a(&size, sizeof(size), h);
    h = fnv1a(&mt, sizeof(mt), h);
    h = fnv1a(&sampleRate, sizeof(sampleRate), h);
    return toHex(h);
}

bool writeFile(const std::string& path, const void* data, uint64_t bytes) {
    const std::string tmp = path + ".tmp";
    std::FILE* f = nullptr;
#ifdef _WIN32
    // Paths are UTF-8; fopen_s expects the ANSI codepage. Use the wide
    // variant so cache dirs with non-ASCII characters work.
    _wfopen_s(&f, fs::u8path(tmp).wstring().c_str(), L"wb");
#else
    f = std::fopen(tmp.c_str(), "wb");
#endif
    if (!f) return false;
    const size_t written = std::fwrite(data, 1, size_t(bytes), f);
    std::fclose(f);
    std::error_code ec;
    if (written != bytes) {
        fs::remove(fs::u8path(tmp), ec);
        return false;
    }
    fs::rename(fs::u8path(tmp), fs::u8path(path), ec);
    if (ec) {
        fs::remove(fs::u8path(tmp), ec);
        return false;
    }
    return true;
}

// LRU size limit -------------------------------------------------------------

uint64_t limitBytes() {
    static const uint64_t limit = [] {
        if (const char* env = std::getenv("PYDAW_CACHE_LIMIT_MB")) {
            char* end = nullptr;
            const long long mb = std::strtoll(env, &end, 10);
            if (end != env && mb >= 0)
                return uint64_t(mb) * 1024 * 1024;
        }
        return uint64_t(2048) * 1024 * 1024; // 2 GB default
    }();
    return limit;
}

void touchFile(const std::string& path) {
    std::error_code ec;
    fs::last_write_time(fs::u8path(path),
                        fs::file_time_type::clock::now(), ec);
}

uint64_t enforceLimit() {
    const uint64_t limit = limitBytes();
    const std::string root = cacheDir();
    if (limit == 0 || root.empty()) return 0;

    struct Entry {
        fs::path path;
        uint64_t size;
        fs::file_time_type mtime;
    };
    std::vector<Entry> entries;
    uint64_t total = 0;

    std::error_code ec;
    fs::recursive_directory_iterator it(fs::u8path(root), ec), end;
    if (ec) return 0;
    for (; it != end; it.increment(ec)) {
        if (ec) break;
        std::error_code fec;
        if (!it->is_regular_file(fec) || fec) continue;
        const uint64_t size = it->file_size(fec);
        if (fec) continue;
        const auto mtime = fs::last_write_time(it->path(), fec);
        if (fec) continue;
        entries.push_back({it->path(), size, mtime});
        total += size;
    }
    if (total <= limit) return 0;

    // Oldest mtime first = least recently used (readers touch entries).
    std::sort(entries.begin(), entries.end(),
              [](const Entry& a, const Entry& b) {
                  return a.mtime < b.mtime;
              });

    uint64_t freed = 0;
    for (const Entry& e : entries) {
        if (total - freed <= limit) break;
        std::error_code rec;
        if (fs::remove(e.path, rec) && !rec)
            freed += e.size; // in-use (mapped) files fail and are skipped
    }
    return freed;
}

void enforceLimitAsync() {
    std::thread([] { enforceLimit(); }).detach();
}

} // namespace audiocache

// MappedFile -----------------------------------------------------------------

MappedFile::~MappedFile() { close(); }

#ifdef _WIN32

bool MappedFile::open(const std::string& path) {
    close();
    const std::wstring wpath = fs::u8path(path).wstring();
    HANDLE file = CreateFileW(wpath.c_str(), GENERIC_READ, FILE_SHARE_READ,
                              nullptr, OPEN_EXISTING,
                              FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) return false;
    LARGE_INTEGER sz{};
    if (!GetFileSizeEx(file, &sz) || sz.QuadPart <= 0) {
        CloseHandle(file);
        return false;
    }
    HANDLE map = CreateFileMappingW(file, nullptr, PAGE_READONLY, 0, 0,
                                    nullptr);
    if (!map) {
        CloseHandle(file);
        return false;
    }
    void* view = MapViewOfFile(map, FILE_MAP_READ, 0, 0, 0);
    if (!view) {
        CloseHandle(map);
        CloseHandle(file);
        return false;
    }
    fileHandle_ = file;
    mapHandle_ = map;
    data_ = view;
    size_ = uint64_t(sz.QuadPart);
    return true;
}

void MappedFile::close() {
    if (data_) UnmapViewOfFile(data_);
    if (mapHandle_) CloseHandle(static_cast<HANDLE>(mapHandle_));
    if (fileHandle_) CloseHandle(static_cast<HANDLE>(fileHandle_));
    data_ = nullptr;
    mapHandle_ = nullptr;
    fileHandle_ = nullptr;
    size_ = 0;
}

#else

bool MappedFile::open(const std::string& path) {
    close();
    fd_ = ::open(path.c_str(), O_RDONLY);
    if (fd_ < 0) return false;
    struct stat st{};
    if (fstat(fd_, &st) != 0 || st.st_size <= 0) {
        ::close(fd_);
        fd_ = -1;
        return false;
    }
    void* view = mmap(nullptr, size_t(st.st_size), PROT_READ, MAP_SHARED,
                      fd_, 0);
    if (view == MAP_FAILED) {
        ::close(fd_);
        fd_ = -1;
        return false;
    }
    data_ = view;
    size_ = uint64_t(st.st_size);
    return true;
}

void MappedFile::close() {
    if (data_) munmap(data_, size_t(size_));
    if (fd_ >= 0) ::close(fd_);
    data_ = nullptr;
    size_ = 0;
    fd_ = -1;
}

#endif
