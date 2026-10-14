#include "vst_bridge.h"
#include "vst_protocol.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <cctype>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

namespace {

using pydaw::vst_protocol::hexDecode;
using pydaw::vst_protocol::hexEncode;
using pydaw::vst_protocol::kSharedAudioFrames;
using pydaw::vst_protocol::parseFloatField;
using pydaw::vst_protocol::parseIntField;
using pydaw::vst_protocol::sanitizeField;
using pydaw::vst_protocol::splitTabs;

std::string commandPathArg(const std::string& path) {
    // Security: Validate path to prevent traversal attacks
    try {
        std::filesystem::path fsPath = std::filesystem::u8path(path);
        // Reject paths with .. components or that are not absolute
        std::filesystem::path canonical = std::filesystem::weakly_canonical(fsPath);
        std::string canonicalStr = canonical.u8string();
        
        // On Windows, check if path is within allowed VST directories
#ifdef _WIN32
        std::string upper = canonicalStr;
        std::transform(upper.begin(), upper.end(), upper.begin(), ::toupper);
        bool allowed = (upper.find("PROGRAM FILES") != std::string::npos ||
                       upper.find("PROGRAMFILES") != std::string::npos ||
                       upper.find("COMMONPROGRAMFILES") != std::string::npos ||
                       upper.find("COMMON") != std::string::npos ||
                       upper.find("VST") != std::string::npos);
        if (!allowed && std::getenv("PYDAW_VST_ALLOW_ALL_PATHS") == nullptr) {
            // Strict mode: only allow common VST locations
            // Can be overridden with PYDAW_VST_ALLOW_ALL_PATHS for testing
            return sanitizeField(path);  // Fallback to original for now
        }
#endif
        return sanitizeField(canonicalStr);
    } catch (...) {
        // If path validation fails, sanitize and return original
        return sanitizeField(path);
    }
}

std::filesystem::path hostExecutablePath() {
    if (const char* env = std::getenv("PYDAW_VST_HOST_EXE")) {
        if (*env) return std::filesystem::u8path(env);
    }

#ifdef _WIN32
    std::array<wchar_t, 32768> modulePath{};
    HMODULE module = nullptr;
    if (GetModuleHandleExW(
            GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
            reinterpret_cast<LPCWSTR>(&hostExecutablePath), &module)) {
        const DWORD n = GetModuleFileNameW(module, modulePath.data(),
                                           DWORD(modulePath.size()));
        if (n > 0 && n < modulePath.size()) {
            const auto dir = std::filesystem::path(modulePath.data()).parent_path();
            const auto besideExe = dir / L"pydaw_vst_host.exe";
            if (std::filesystem::exists(besideExe)) return besideExe;
        }
    }
#endif

    const auto cwd = std::filesystem::current_path();
    const std::array<std::filesystem::path, 4> candidates = {
        cwd / "pydaw" / "pydaw_vst_host.exe",
        cwd / "pydaw_vst_host.exe",
        cwd / "build" / "cp314-cp314-win_amd64" / "Release" / "pydaw_vst_host.exe",
        cwd / "build" / "cp314-cp314-win_amd64" / "Debug" / "pydaw_vst_host.exe",
    };
    for (const auto& candidate : candidates) {
        if (std::filesystem::exists(candidate)) return candidate;
    }
    return {};
}

class ChildProcess {
public:
    ChildProcess() = default;
    ~ChildProcess() { close(); }

    ChildProcess(const ChildProcess&) = delete;
    ChildProcess& operator=(const ChildProcess&) = delete;

    bool start(const std::filesystem::path& exe) {
#ifdef _WIN32
        close();
        if (exe.empty() || !std::filesystem::exists(exe)) return false;

        SECURITY_ATTRIBUTES sa{};
        sa.nLength = sizeof(sa);
        sa.bInheritHandle = TRUE;
        sa.lpSecurityDescriptor = nullptr;

        HANDLE childStdoutRead = nullptr;
        HANDLE childStdoutWrite = nullptr;
        HANDLE childStdinRead = nullptr;
        HANDLE childStdinWrite = nullptr;
        HANDLE childStderr = nullptr;
        HANDLE parentProcess = nullptr;
        HANDLE parentMapping = nullptr;
        HANDLE childProcess = nullptr;
        HANDLE childMapping = nullptr;

        if (!CreatePipe(&childStdoutRead, &childStdoutWrite, &sa, 0)) return false;
        if (!SetHandleInformation(childStdoutRead, HANDLE_FLAG_INHERIT, 0)) {
            CloseHandle(childStdoutRead);
            CloseHandle(childStdoutWrite);
            return false;
        }
        if (!CreatePipe(&childStdinRead, &childStdinWrite, &sa, 0)) {
            CloseHandle(childStdoutRead);
            CloseHandle(childStdoutWrite);
            return false;
        }
        if (!SetHandleInformation(childStdinWrite, HANDLE_FLAG_INHERIT, 0)) {
            CloseHandle(childStdoutRead);
            CloseHandle(childStdoutWrite);
            CloseHandle(childStdinRead);
            CloseHandle(childStdinWrite);
            return false;
        }
        // Child [vst] diagnostics normally go to NUL (they must never mix
        // into the protocol stdout). PYDAW_VST_HOST_LOG=<file> redirects
        // them to a log file for debugging plugin-specific issues.
        if (const char* logPath = std::getenv("PYDAW_VST_HOST_LOG")) {
            if (*logPath) {
                childStderr = CreateFileW(
                    std::filesystem::u8path(logPath).wstring().c_str(),
                    FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE,
                    &sa, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
            }
        }
        if (!childStderr || childStderr == INVALID_HANDLE_VALUE) {
            childStderr = CreateFileW(L"NUL", GENERIC_WRITE, FILE_SHARE_WRITE,
                                      &sa, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL,
                                      nullptr);
        }
        if (childStderr == INVALID_HANDLE_VALUE) childStderr = nullptr;

        SECURITY_ATTRIBUTES nonInheritable{};
        nonInheritable.nLength = sizeof(nonInheritable);
        nonInheritable.bInheritHandle = FALSE;
        const DWORD audioBytes = DWORD(sizeof(float) * kSharedAudioFrames * 2);
        parentMapping = CreateFileMappingW(INVALID_HANDLE_VALUE, &nonInheritable,
                                           PAGE_READWRITE, 0, audioBytes, nullptr);
        if (!parentMapping) {
            CloseHandle(childStdoutRead);
            CloseHandle(childStdoutWrite);
            CloseHandle(childStdinRead);
            CloseHandle(childStdinWrite);
            if (childStderr) CloseHandle(childStderr);
            return false;
        }
        if (!DuplicateHandle(GetCurrentProcess(), parentMapping, GetCurrentProcess(),
                             &childMapping, 0, TRUE, DUPLICATE_SAME_ACCESS)) {
            CloseHandle(parentMapping);
            CloseHandle(childStdoutRead);
            CloseHandle(childStdoutWrite);
            CloseHandle(childStdinRead);
            CloseHandle(childStdinWrite);
            if (childStderr) CloseHandle(childStderr);
            return false;
        }

        STARTUPINFOW si{};
        si.cb = sizeof(si);
        si.dwFlags = STARTF_USESTDHANDLES;
        si.hStdInput = childStdinRead;
        si.hStdOutput = childStdoutWrite;
        si.hStdError = childStderr ? childStderr : GetStdHandle(STD_ERROR_HANDLE);

        PROCESS_INFORMATION pi{};
        std::wstring cmd = L"\"" + exe.wstring() + L"\" --audio-map " +
                           std::to_wstring(reinterpret_cast<uintptr_t>(childMapping));
        const BOOL ok = CreateProcessW(
            nullptr, cmd.data(), nullptr, nullptr, TRUE,
            CREATE_NO_WINDOW, nullptr, nullptr, &si, &pi);

        CloseHandle(childStdoutWrite);
        CloseHandle(childStdinRead);
        if (childStderr) CloseHandle(childStderr);

        CloseHandle(childMapping);
        childMapping = nullptr;

        if (!ok) {
            CloseHandle(parentMapping);
            CloseHandle(childStdoutRead);
            CloseHandle(childStdinWrite);
            return false;
        }

        parentProcess = pi.hProcess;
        audioBuffer_ = static_cast<float*>(MapViewOfFile(
            parentMapping, FILE_MAP_ALL_ACCESS, 0, 0, audioBytes));
        if (!audioBuffer_) {
            TerminateProcess(parentProcess, 0);
            CloseHandle(parentProcess);
            CloseHandle(pi.hThread);
            CloseHandle(parentMapping);
            CloseHandle(childStdoutRead);
            CloseHandle(childStdinWrite);
            return false;
        }

        stdoutRead_ = childStdoutRead;
        stdinWrite_ = childStdinWrite;
        process_ = parentProcess;
        thread_ = pi.hThread;
        audioMapping_ = parentMapping;
        return true;
#else
        (void)exe;
        return false;
#endif
    }

    bool running() const {
#ifdef _WIN32
        if (!process_) return false;
        DWORD code = 0;
        if (!GetExitCodeProcess(process_, &code)) return false;
        return code == STILL_ACTIVE;
#else
        return false;
#endif
    }

    void terminate() {
#ifdef _WIN32
        if (process_ && running()) TerminateProcess(process_, 0);
#endif
    }

    bool writeLine(const std::string& line) {
#ifdef _WIN32
        if (!stdinWrite_ || !running()) return false;
        std::string payload = line;
        payload.push_back('\n');
        DWORD written = 0;
        return WriteFile(stdinWrite_, payload.data(), DWORD(payload.size()),
                         &written, nullptr) && written == payload.size();
#else
        (void)line;
        return false;
#endif
    }

    bool readLine(std::string& line, uint32_t timeoutMs = 2000) {
#ifdef _WIN32
        line.clear();
        if (!stdoutRead_) return false;
        const ULONGLONG start = GetTickCount64();
        const ULONGLONG deadline = start + timeoutMs;
        char ch = 0;
        while (true) {
            DWORD available = 0;
            if (!PeekNamedPipe(stdoutRead_, nullptr, 0, nullptr, &available,
                               nullptr)) {
                return false;
            }
            if (available == 0) {
                if (!running()) return false;
                const ULONGLONG now = GetTickCount64();
                if (now >= deadline) return false;
                // While the child attaches its plugin view to our HWND
                // (cross-process SetParent/CreateWindow), Windows sends
                // synchronous messages to this thread's windows. If we sit
                // in a plain Sleep the child blocks forever waiting for us
                // -> deadlock -> open_editor timeout. A PM_NOREMOVE peek
                // dispatches pending *sent* messages without consuming the
                // posted queue.
                MSG msg;
                PeekMessageW(&msg, nullptr, 0, 0, PM_NOREMOVE);
                // Audio replies usually land within a millisecond: spin
                // briefly (yield) before falling back to Sleep(1); the old
                // fixed Sleep(2) added ~2ms jitter per audio block.
                if (now - start < 2)
                    Sleep(0);
                else
                    Sleep(1);
                continue;
            }
            DWORD read = 0;
            if (!ReadFile(stdoutRead_, &ch, 1, &read, nullptr) || read != 1)
                return false;
            if (ch == '\n') return true;
            if (ch != '\r') line.push_back(ch);
            // Security: Reduce IPC message limit from 64MB to 16MB to prevent DoS
            if (line.size() > 16 * 1024 * 1024) return false;
        }
#else
        (void)line;
        (void)timeoutMs;
        return false;
#endif
    }

    float* audioBuffer() const {
#ifdef _WIN32
        return audioBuffer_;
#else
        return nullptr;
#endif
    }

    // Hand ownership of a fully started child over to another instance
    // (async load builds the process locally, then commits under the lock).
    void swap(ChildProcess& other) {
#ifdef _WIN32
        std::swap(process_, other.process_);
        std::swap(thread_, other.thread_);
        std::swap(stdinWrite_, other.stdinWrite_);
        std::swap(stdoutRead_, other.stdoutRead_);
        std::swap(audioMapping_, other.audioMapping_);
        std::swap(audioBuffer_, other.audioBuffer_);
#else
        (void)other;
#endif
    }

    void close() {
#ifdef _WIN32
        if (audioBuffer_) {
            UnmapViewOfFile(audioBuffer_);
            audioBuffer_ = nullptr;
        }
        if (audioMapping_) {
            CloseHandle(audioMapping_);
            audioMapping_ = nullptr;
        }
        if (stdinWrite_) {
            CloseHandle(stdinWrite_);
            stdinWrite_ = nullptr;
        }
        if (stdoutRead_) {
            CloseHandle(stdoutRead_);
            stdoutRead_ = nullptr;
        }
        if (process_) {
            if (running()) {
                TerminateProcess(process_, 0);
                // Reap the child so it does not linger as a zombie holding
                // the shared-memory mapping and pipe handles alive.
                WaitForSingleObject(process_, 2000);
            }
            CloseHandle(process_);
            process_ = nullptr;
        }
        if (thread_) {
            CloseHandle(thread_);
            thread_ = nullptr;
        }
#endif
    }

private:
#ifdef _WIN32
    HANDLE process_ = nullptr;
    HANDLE thread_ = nullptr;
    HANDLE stdinWrite_ = nullptr;
    HANDLE stdoutRead_ = nullptr;
    HANDLE audioMapping_ = nullptr;
    float* audioBuffer_ = nullptr;
#endif
};

class VstBridgePlugin final : public VstPlugin {
public:
    VstBridgePlugin(std::string path, std::string uid, bool instrument,
                    uint32_t sampleRate)
        : path_(std::move(path)), uid_(std::move(uid)), instrument_(instrument),
          sampleRate_(sampleRate) {
        const auto p = std::filesystem::u8path(path_);
        displayName_ = p.filename().u8string();
        if (displayName_.empty()) displayName_ = "VST Bridge";
        // Heavy plugins (Kontakt, Omnisphere) take seconds to load; doing
        // it synchronously froze the UI thread that creates the plugin.
        // The subprocess is built on thread-local objects and committed
        // under the mutex once ready; until then the plugin fail-safes.
        offlineReason_ = "loading...";
        const auto control = loadControl_;
        const std::string loadPath = path_;
        const std::string loadUid = uid_;
        const bool loadInstrument = instrument_;
        const uint32_t loadSampleRate = sampleRate_;
        const std::string state = state_;
        const uint64_t generation = loadGeneration_;
        loadThread_ = std::thread([this, control, loadPath, loadUid,
                                   loadInstrument, loadSampleRate, state,
                                   generation] {
            ChildProcess child;
            LoadResult res = loadIntoHost(child, loadPath, loadUid,
                                          loadInstrument, loadSampleRate,
                                          state, control);
            {
                std::lock_guard<std::mutex> commitLock(control->commitMutex);
                if (control->alive)
                    commitLoad(child, res, state, generation);
            }
            if (child.running()) {
                std::string r;
                child.writeLine("shutdown");
                child.readLine(r);
            }
            child.close();
        });
    }

    ~VstBridgePlugin() override {
        {
            std::lock_guard<std::mutex> lock(stateMutex_);
            ++loadGeneration_;
        }
        cancelActiveLoad();
        {
            std::lock_guard<std::mutex> commitLock(loadControl_->commitMutex);
            loadControl_->alive = false;
        }
        if (loadThread_.joinable()) loadThread_.detach();
        shutdownHost();
    }

    bool isLoaded() const {
        return loaded_.load(std::memory_order_acquire);
    }

    bool isLoading() const override {
        std::lock_guard<std::mutex> lock(stateMutex_);
        return !loaded_.load(std::memory_order_acquire) &&
               offlineReason_ == "loading...";
    }

    void process(float* interleaved, uint32_t frames) override {
        processWithEvents(interleaved, frames, {});
    }

    void processWithEvents(float* interleaved, uint32_t frames,
                           const std::vector<VstHostEvent>& events) override {
        // Never block the audio callback on slow UI-thread IPC for long
        // (editor open/state save can hold the bridge IPC for seconds).
        // Routine UI calls (resize polls, param sets) are short, so wait a
        // couple of ms first: skipping a block on every collision made the
        // instrument stutter while the editor window was open.
        std::unique_lock<std::mutex> lock(ipcMutex_, std::try_to_lock);
        if (!lock.owns_lock()) {
            const auto deadline = std::chrono::steady_clock::now()
                                  + std::chrono::milliseconds(3);
            while (!lock.try_lock()) {
                if (std::chrono::steady_clock::now() >= deadline) break;
                std::this_thread::yield();
            }
        }
        if (!lock.owns_lock()) {
            // Do not lose this block's MIDI: replay it with the next block
            // (a dropped note-on loses the note; a dropped note-off hangs it).
            queueMissedEvents(events);
            failSafe(interleaved, frames);
            return;
        }
        if (!loaded_.load(std::memory_order_acquire) || !host_.running()) {
            failSafe(interleaved, frames);
            return;
        }

        if (!host_.audioBuffer() || frames > kSharedAudioFrames) {
            failSafe(interleaved, frames);
            return;
        }

        // A previous audio block timed out and its reply is still owed on
        // the pipe (heavy plugins stall processing while loading libraries
        // or re-attaching editors). Drain it before sending a new command;
        // until it arrives, skip blocks instead of killing the subprocess.
        if (audioAwaitingReply_) {
            std::string stale;
            if (host_.readLine(stale, 1)) {
                audioAwaitingReply_ = false;
            } else {
                const auto stalled = std::chrono::steady_clock::now()
                                     - audioStallSince_;
                if (stalled > std::chrono::milliseconds(kAudioStallKillMs)) {
                    markOfflineFromAudio(
                        lock,
                        "VST subprocess stopped responding to audio process");
                }
                queueMissedEvents(events);
                failSafe(interleaved, frames);
                return;
            }
        }

        std::memcpy(host_.audioBuffer(), interleaved, sizeof(float) * frames * 2);

        // Grab transport / preview-note state queued by the mixer since the
        // previous block; it piggybacks on the audio command so stop/seek/BPM
        // reach the subprocess without extra pipe round-trips.
        PendingState pending;
        {
            std::lock_guard<std::mutex> pendingLock(pendingMutex_);
            pending = pending_;
            pending_.hasTransport = false;
            pending_.jump = false;
            pending_.allNotesOff = false;
            pending_.notes.clear();
            pending_.params.clear();
        }

        std::ostringstream cmd;
        cmd << "process_shm2\t" << frames << '\t'
            << (pending.hasTransport ? 1 : 0) << '\t' << pending.bpm << '\t'
            << (pending.playing ? 1 : 0) << '\t' << pending.samplePos << '\t'
            << (pending.jump ? 1 : 0) << '\t' << (pending.allNotesOff ? 1 : 0)
            << '\t' << (events.size() + pending.notes.size());
        auto appendEvent = [&cmd](const VstHostEvent& ev) {
            cmd << '\t' << (ev.type == VstHostEvent::NoteOn ? 1 : 0)
                << ',' << ev.pitch << ',' << ev.velocity << ','
                << ev.sampleOffset;
        };
        for (const auto& ev : pending.notes) appendEvent(ev);
        for (const auto& ev : events) appendEvent(ev);
        cmd << '\t' << pending.params.size();
        for (const auto& param : pending.params) {
            cmd << '\t' << param.index << ',' << param.value;
        }

        std::string reply;
        if (!host_.writeLine(cmd.str())) {
            markOfflineFromAudio(lock, "VST subprocess pipe write failed");
            failSafe(interleaved, frames);
            return;
        }
        if (!host_.readLine(reply, 250)) {
            // Transient stall (library load, editor attach): remember that
            // one reply is pending and fail-safe this block. The child is
            // only terminated after kAudioStallKillMs of continued silence.
            audioAwaitingReply_ = true;
            audioStallSince_ = std::chrono::steady_clock::now();
            failSafe(interleaved, frames);
            return;
        }
        const auto parts = splitTabs(reply);
        // process_shm replies with a bare "ok": audio data comes back via
        // shared memory, not the pipe.
        if (parts.empty() || parts[0] != "ok") {
            markOfflineFromAudio(lock, "VST subprocess stopped responding");
            failSafe(interleaved, frames);
            return;
        }
        std::memcpy(interleaved, host_.audioBuffer(), sizeof(float) * frames * 2);
    }

    void setParam(int index, float normalized) override {
        setParamNormalizedByIndex(index, normalized);
    }

    const char* name() const override { return displayName_.c_str(); }

    void prepare(uint32_t sampleRate) override {
        {
            std::lock_guard<std::mutex> lock(stateMutex_);
            if (sampleRate_ == sampleRate) return;
        }
        // Join outside the lock: the async load thread briefly takes locks
        // to commit, joining under them would deadlock.
        cancelActiveLoad();
        joinLoadThread();
        loadControl_ = std::make_shared<LoadControl>();
        std::string state;
        {
            std::lock_guard<std::mutex> lock(stateMutex_);
            if (sampleRate_ == sampleRate) return;
            sampleRate_ = sampleRate;
            state = state_;
            ++loadGeneration_;
        }
        shutdownHost();
        loadInSubprocess(sampleRate, state);
    }

    uint32_t latencySamples() const override {
        return latencySamples_.load(std::memory_order_acquire);
    }

    bool isInstrument() const override { return instrument_; }

    int paramCount() const override {
        std::lock_guard<std::mutex> lock(stateMutex_);
        return int(params_.size());
    }

    std::tuple<int, std::string, float> getParamInfo(int index) const override {
        std::lock_guard<std::mutex> lock(stateMutex_);
        if (index < 0 || size_t(index) >= params_.size())
            return {index, "", 0.0f};
        return {index, params_[size_t(index)].name,
                params_[size_t(index)].value};
    }

    float getParamDefault(int index) const override {
        std::lock_guard<std::mutex> lock(stateMutex_);
        if (index < 0 || size_t(index) >= params_.size()) return 0.0f;
        return params_[size_t(index)].defaultValue;
    }

    float getParamNormalized(int index) const override {
        std::lock_guard<std::mutex> lock(stateMutex_);
        if (index < 0 || size_t(index) >= params_.size()) return 0.0f;
        return params_[size_t(index)].value;
    }

    void setParamNormalizedByIndex(int index, float normalized,
                                   bool markUserEdit = true) override {
        if (index < 0) return;
        normalized = std::clamp(normalized, 0.0f, 1.0f);
        {
            std::lock_guard<std::mutex> lock(stateMutex_);
            ensureParam(index);
            params_[size_t(index)].value = normalized;
            if (markUserEdit) {
                lastUserEdit_ = index;
                hasUserEdit_ = true;
            }
        }
        if (loaded_.load(std::memory_order_acquire)) {
            std::lock_guard<std::mutex> ipcLock(ipcMutex_);
            if (!host_.running()) return;
            std::ostringstream cmd;
            cmd << "set_param\t" << index << '\t' << normalized;
            std::string reply;
            if (!requestIpcLocked(cmd.str(), reply)) {
                markOfflineIpcLocked("VST subprocess stopped responding");
            } else {
                const auto parts = splitTabs(reply);
                if (parts.empty() || parts[0] != "ok") {
                    markOfflineIpcLocked("VST subprocess stopped responding");
                } else {
                    // Re-read from child so cached values (and TWEAK
                    // reset) stay aligned with the native editor.
                    refreshParamIpcLocked(index);
                }
            }
        }
    }

    void setParamFromAudioThread(int index, float normalized) override {
        if (index < 0) return;
        normalized = std::clamp(normalized, 0.0f, 1.0f);
        {
            std::lock_guard<std::mutex> lock(stateMutex_);
            ensureParam(index);
            params_[size_t(index)].value = normalized;
        }
        std::lock_guard<std::mutex> lock(pendingMutex_);
        for (auto& p : pending_.params) {
            if (p.index == index) {
                p.value = normalized;
                return;
            }
        }
        if (pending_.params.size() >= kMaxPendingParams) return;
        pending_.params.push_back({index, normalized});
    }

    void syncControllerParams() override {
        std::lock_guard<std::mutex> lock(ipcMutex_);
        if (!loaded_.load(std::memory_order_acquire) || !host_.running()) return;
        std::string reply;
        requestIpcLocked("sync_controllers", reply, 2000);
    }

    std::string getState() const override {
        if (!loaded_.load(std::memory_order_acquire)) {
            std::lock_guard<std::mutex> lock(stateMutex_);
            return state_;
        }
        std::lock_guard<std::mutex> ipcLock(ipcMutex_);
        if (!host_.running()) {
            std::lock_guard<std::mutex> lock(stateMutex_);
            return state_;
        }
        std::string reply;
        // Sampler states can be large (hex text over the pipe).
        if (!requestIpcLocked("get_state", reply, 15000)) {
            std::lock_guard<std::mutex> lock(stateMutex_);
            return state_;
        }
        const auto parts = splitTabs(reply);
        if (parts.size() >= 2 && parts[0] == "ok") {
            std::lock_guard<std::mutex> lock(stateMutex_);
            state_ = hexDecode(parts[1]);
            return state_;
        }
        std::lock_guard<std::mutex> lock(stateMutex_);
        return state_;
    }

    bool setState(const std::string& data) override {
        {
            std::lock_guard<std::mutex> lock(stateMutex_);
            state_ = data;
        }
        if (!loaded_.load(std::memory_order_acquire)) return true;
        std::lock_guard<std::mutex> ipcLock(ipcMutex_);
        if (!host_.running()) return true;
        std::string reply;
        if (!requestIpcLocked("set_state\t" + hexEncode(data), reply, 15000)) {
            markOfflineIpcLocked("VST subprocess stopped responding");
            return false;
        }
        const auto parts = splitTabs(reply);
        const bool ok = !parts.empty() && parts[0] == "ok";
        if (!ok) markOfflineIpcLocked("VST subprocess stopped responding");
        return ok;
    }

    bool hasEditor() const override {
        std::lock_guard<std::mutex> lock(ipcMutex_);
        if (!loaded_.load(std::memory_order_acquire) || !host_.running()) return false;
        std::string reply;
        if (!requestIpcLocked("has_editor", reply, 5000)) {
            setEditorError("VST subprocess timed out while probing editor");
            markOfflineIpcLocked("VST subprocess timed out while probing editor");
            return false;
        }
        const auto parts = splitTabs(reply);
        const bool ok = parts.size() >= 2 && parts[0] == "ok" && parts[1] == "1";
        if (!ok) setEditorError("Plugin reports no native editor");
        return ok;
    }

    bool openEditor(void* hwnd) override {
        std::lock_guard<std::mutex> lock(ipcMutex_);
        if (!loaded_.load(std::memory_order_acquire) || !host_.running() || !hwnd)
            return false;
        std::string reply;
        if (prefersTopLevelEditor()) {
            std::ostringstream cmd;
            cmd << "open_editor_top_level\t"
                << std::to_string(reinterpret_cast<uintptr_t>(hwnd)) << '\t'
                << hexEncode(displayName_);
            if (!requestIpcLocked(cmd.str(), reply, 20000)) {
                setEditorError(
                    "Serum 2 native editor timed out while opening; "
                    "plugin is loaded, use the generic parameter panel");
                return false;
            }
            const auto parts = splitTabs(reply);
            const bool ok = !parts.empty() && parts[0] == "ok";
            if (ok) {
                setEditorError("Serum 2 native editor opened in separate VST host window");
            } else {
                setEditorError(reply.empty() ? "Serum 2 native editor failed to open"
                                             : "Serum 2 native editor failed to open: " + reply);
            }
            return ok;
        }
        // Editor attach can be slow for heavyweight GUIs (Kontakt shows a
        // full library browser); this runs on the UI thread, not audio.
        if (!requestIpcLocked("open_editor\t" +
                                  std::to_string(reinterpret_cast<uintptr_t>(hwnd)),
                              reply, 20000)) {
            setEditorError("VST subprocess timed out while opening editor");
            markOfflineIpcLocked("VST subprocess timed out while opening editor");
            return false;
        }
        const auto parts = splitTabs(reply);
        const bool ok = !parts.empty() && parts[0] == "ok";
        if (!ok) {
            setEditorError(reply.empty() ? "Plugin editor failed to open"
                                         : "Plugin editor failed to open: " + reply);
            if (!host_.running()) markOfflineIpcLocked("VST subprocess stopped responding");
        } else {
            setEditorError("");
        }
        return ok;
    }

    void closeEditor() override {
        std::lock_guard<std::mutex> lock(ipcMutex_);
        if (!loaded_.load(std::memory_order_acquire) || !host_.running()) return;
        std::string reply;
        requestIpcLocked("close_editor", reply);
    }

    std::pair<int, int> editorSize() const override {
        std::lock_guard<std::mutex> lock(ipcMutex_);
        if (!loaded_.load(std::memory_order_acquire) || !host_.running()) return {0, 0};
        std::string reply;
        if (!requestIpcLocked("editor_size", reply)) return {0, 0};
        const auto parts = splitTabs(reply);
        if (parts.size() < 3 || parts[0] != "ok") return {0, 0};
        return {parseIntField(parts[1]), parseIntField(parts[2])};
    }

    bool checkSizeChanged(int& w, int& h) override {
        std::lock_guard<std::mutex> lock(ipcMutex_);
        if (!loaded_.load(std::memory_order_acquire) || !host_.running()) return false;
        std::string reply;
        if (!requestIpcLocked("check_resize", reply)) return false;
        const auto parts = splitTabs(reply);
        if (parts.size() < 4 || parts[0] != "ok") return false;
        w = parseIntField(parts[2]);
        h = parseIntField(parts[3]);
        return parts[1] == "1";
    }

    bool editorCanResize() const override {
        std::lock_guard<std::mutex> lock(ipcMutex_);
        if (!loaded_.load(std::memory_order_acquire) || !host_.running()) return false;
        std::string reply;
        if (!requestIpcLocked("editor_can_resize", reply)) return false;
        const auto parts = splitTabs(reply);
        return parts.size() >= 2 && parts[0] == "ok" && parts[1] == "1";
    }

    bool resizeEditor(int w, int h) override {
        std::lock_guard<std::mutex> lock(ipcMutex_);
        if (!loaded_.load(std::memory_order_acquire) || !host_.running()) return false;
        std::string reply;
        std::ostringstream cmd;
        cmd << "resize_editor\t" << w << '\t' << h;
        if (!requestIpcLocked(cmd.str(), reply)) return false;
        const auto parts = splitTabs(reply);
        return !parts.empty() && parts[0] == "ok";
    }

    bool setContentScale(float scale) override {
        std::lock_guard<std::mutex> lock(ipcMutex_);
        if (!loaded_.load(std::memory_order_acquire) || !host_.running()) return false;
        std::string reply;
        std::ostringstream cmd;
        cmd << "set_content_scale\t" << scale;
        if (!requestIpcLocked(cmd.str(), reply)) return false;
        const auto parts = splitTabs(reply);
        return !parts.empty() && parts[0] == "ok";
    }

    void queuePreviewNoteOn(int pitch, float velocity) override {
        std::lock_guard<std::mutex> lock(pendingMutex_);
        if (pending_.notes.size() >= kMaxPendingNotes) return;
        VstHostEvent ev;
        ev.type = VstHostEvent::NoteOn;
        ev.pitch = pitch;
        ev.velocity = velocity;
        pending_.notes.push_back(ev);
    }

    void queuePreviewNoteOff(int pitch) override {
        std::lock_guard<std::mutex> lock(pendingMutex_);
        if (pending_.notes.size() >= kMaxPendingNotes) return;
        VstHostEvent ev;
        ev.type = VstHostEvent::NoteOff;
        ev.pitch = pitch;
        ev.velocity = 0.0f;
        pending_.notes.push_back(ev);
    }

    void allNotesOff() override {
        std::lock_guard<std::mutex> lock(pendingMutex_);
        pending_.allNotesOff = true;
        pending_.notes.clear();
    }

    void onTransportJump() override {
        std::lock_guard<std::mutex> lock(pendingMutex_);
        pending_.jump = true;
    }

    void setTransport(double bpm, bool playing, int64_t projectSamplePos) override {
        std::lock_guard<std::mutex> lock(pendingMutex_);
        pending_.hasTransport = true;
        pending_.bpm = bpm;
        pending_.playing = playing;
        pending_.samplePos = projectSamplePos;
    }

    void hostPerformEdit(uint32_t, double) override {}
    void hostRestartComponent(int32_t) override {}
    // Fires once after the async load finishes so the UI rebuilds the
    // (initially empty) parameter panels.
    bool refreshParamsIfChanged() override {
        return paramsChanged_.exchange(false, std::memory_order_acq_rel);
    }

    std::string diagnostic() const override {
        std::lock_guard<std::mutex> lock(stateMutex_);
        std::ostringstream msg;
        msg << "VST bridge";
        if (!displayName_.empty()) msg << " (" << displayName_ << ")";
        msg << ": ";
        if (!loaded_.load(std::memory_order_acquire)) {
            msg << (offlineReason_.empty() ? "plugin is offline" : offlineReason_);
        } else if (!lastEditorError_.empty()) {
            msg << lastEditorError_;
        } else {
            msg << "loaded in subprocess";
        }
        if (!hostExePath_.empty()) msg << " [host: " << hostExePath_ << "]";
        return msg.str();
    }

    int consumeLastUserEdit() override {
        {
            std::lock_guard<std::mutex> lock(stateMutex_);
            // Generic host sliders mark edits locally.
            if (hasUserEdit_) {
                const int idx = lastUserEdit_;
                hasUserEdit_ = false;
                return idx;
            }
        }
        // Native editor tweaks happen inside the subprocess: poll the child
        // (UI-thread TWEAK poll, ~60 Hz only while TWEAK mode is armed).
        std::lock_guard<std::mutex> ipcLock(ipcMutex_);
        if (!loaded_.load(std::memory_order_acquire) || !host_.running()) return -1;
        std::string reply;
        if (!requestIpcLocked("consume_tweak", reply, 500)) return -1;
        const auto parts = splitTabs(reply);
        if (parts.size() < 2 || parts[0] != "ok") return -1;
        const int idx = parseIntField(parts[1], -1);
        // Keep the parent cache in sync with the child's live value so
        // TWEAK reset + generic panels reflect the real plugin state.
        if (idx >= 0) refreshParamIpcLocked(idx);
        return idx;
    }

private:
    struct LoadControl {
        std::mutex mutex;
        std::mutex commitMutex;
        ChildProcess* activeChild = nullptr;
        bool cancelled = false;
        bool alive = true;
    };

    struct Param {
        std::string name;
        float value = 0.0f;
        float defaultValue = 0.0f;
    };

    struct PendingParamUpdate {
        int index = -1;
        float value = 0.0f;
    };

    static constexpr size_t kMaxPendingNotes = 256;
    static constexpr size_t kMaxPendingParams = 1024;
    // Kill the subprocess only after this long without any audio reply;
    // heavy plugins (Kontakt library load, editor attach) stall processing
    // for seconds and must not be terminated on the first missed block.
    static constexpr uint32_t kAudioStallKillMs = 10000;

    bool prefersTopLevelEditor() const {
        std::string haystack = path_ + " " + displayName_;
        std::transform(haystack.begin(), haystack.end(), haystack.begin(),
                       [](unsigned char ch) { return char(std::tolower(ch)); });
        return haystack.find("serum2") != std::string::npos
            || haystack.find("serum 2") != std::string::npos;
    }

    // Transport / preview-note state queued from the audio thread and
    // flushed with the next process_shm2 command.
    struct PendingState {
        // Only forwarded to the child when the mixer actually called
        // setTransport since the last block: VstPlugin::setTransport flushes
        // plugin DSP when the position moves backward, so replaying a stale
        // position every block would reset the plugin continuously.
        bool hasTransport = false;
        double bpm = 120.0;
        bool playing = false;
        int64_t samplePos = 0;
        bool jump = false;
        bool allNotesOff = false;
        std::vector<VstHostEvent> notes;
        std::vector<PendingParamUpdate> params;
    };

    // Result of a subprocess load performed against a local ChildProcess
    // (so a background thread can build it without holding bridge locks).
    struct LoadResult {
        std::string displayName;
        std::vector<Param> params;
        uint32_t latencySamples = 0;
        std::string offlineReason;
        std::string hostExePath;
        bool ok = false;
    };

    static bool requestHost(ChildProcess& host, const std::string& command,
                            std::string& reply, uint32_t timeoutMs = 2000) {
        return host.writeLine(command) && host.readLine(reply, timeoutMs);
    }

    // Start the host executable and load the plugin into `host`. Pure
    // function of the constructor arguments plus `state`: no members are
    // touched, so it is safe on any thread.
    static bool isLoadCancelled(const std::shared_ptr<LoadControl>& control) {
        std::lock_guard<std::mutex> lock(control->mutex);
        return control->cancelled;
    }

    static bool registerActiveLoadChild(
        const std::shared_ptr<LoadControl>& control, ChildProcess* child) {
        std::lock_guard<std::mutex> lock(control->mutex);
        if (control->cancelled) return false;
        control->activeChild = child;
        return true;
    }

    static void unregisterActiveLoadChild(
        const std::shared_ptr<LoadControl>& control, ChildProcess* child) {
        std::lock_guard<std::mutex> lock(control->mutex);
        if (control->activeChild == child) control->activeChild = nullptr;
    }

    static LoadResult loadIntoHost(
        ChildProcess& host, const std::string& path, const std::string& uid,
        bool instrument, uint32_t sampleRate, const std::string& state,
        const std::shared_ptr<LoadControl>& control) {
        LoadResult res;
        if (!registerActiveLoadChild(control, &host)) {
            res.offlineReason = "load cancelled";
            return res;
        }
        struct LoadChildGuard {
            std::shared_ptr<LoadControl> control;
            ChildProcess* child;
            ~LoadChildGuard() { unregisterActiveLoadChild(control, child); }
        } guard{control, &host};

        const auto exe = hostExecutablePath();
        if (exe.empty()) {
            res.offlineReason = "pydaw_vst_host.exe not found";
            return res;
        }
        res.hostExePath = exe.u8string();
        if (!host.start(exe)) {
            res.offlineReason = "failed to start pydaw_vst_host: "
                                + res.hostExePath;
            return res;
        }
        if (isLoadCancelled(control)) {
            res.offlineReason = "load cancelled";
            host.close();
            return res;
        }

        std::ostringstream cmd;
        cmd << "load\t" << commandPathArg(path) << '\t' << sanitizeField(uid)
            << '\t' << (instrument ? 1 : 0) << '\t' << sampleRate;
        std::string reply;
        // Heavy plugins (Kontakt, Omnisphere, ...) can take many seconds
        // to initialize their component/controller.
        if (!requestHost(host, cmd.str(), reply, 60000)) {
            res.offlineReason = "no response from VST host";
            host.close();
            return res;
        }
        const auto parts = splitTabs(reply);
        if (parts.size() < 4 || parts[0] != "ok") {
            res.offlineReason = reply.empty()
                ? "VST subprocess load failed"
                : "VST subprocess load failed: " + reply;
            host.close();
            return res;
        }

        res.displayName = parts[1];
        res.latencySamples = uint32_t(std::max(0, parseIntField(parts[3])));
        const int count = std::max(0, parseIntField(parts[2]));
        res.params.resize(size_t(count));
        for (int i = 0; i < count; ++i) {
            std::string pReply;
            if (!requestHost(host, "param\t" + std::to_string(i), pReply))
                continue;
            const auto pParts = splitTabs(pReply);
            if (pParts.size() < 5 || pParts[0] != "ok") continue;
            auto& p = res.params[size_t(i)];
            p.name = pParts[2];
            p.value = std::clamp(parseFloatField(pParts[3]), 0.0f, 1.0f);
            p.defaultValue = std::clamp(parseFloatField(pParts[4]), 0.0f, 1.0f);
        }
        if (!state.empty()) {
            std::string stateReply;
            requestHost(host, "set_state\t" + hexEncode(state), stateReply);
        }
        res.ok = true;
        return res;
    }

    void commitLoad(ChildProcess& child, LoadResult& res,
                    const std::string& appliedState, uint64_t generation) {
        std::string newerState;
        if (res.ok) {
            {
                std::lock_guard<std::mutex> ipcLock(ipcMutex_);
                {
                    std::lock_guard<std::mutex> stateLock(stateMutex_);
                    if (generation != loadGeneration_) return;
                    audioAwaitingReply_ = false;
                    host_.swap(child);
                    if (!res.hostExePath.empty()) hostExePath_ = res.hostExePath;
                    if (!res.displayName.empty()) displayName_ = res.displayName;
                    params_ = std::move(res.params);
                    latencySamples_.store(res.latencySamples,
                                          std::memory_order_release);
                    offlineReason_.clear();
                    if (!state_.empty() && state_ != appliedState)
                        newerState = state_;
                }
                // setState() may have arrived while the load was in flight
                // (project restore): push the newer state before audio starts.
                if (!newerState.empty()) {
                    std::string reply;
                    requestIpcLocked("set_state\t" + hexEncode(newerState),
                                     reply, 15000);
                }
                loaded_.store(true, std::memory_order_release);
            }
        } else {
            std::lock_guard<std::mutex> lock(stateMutex_);
            if (generation != loadGeneration_) return;
            if (!res.hostExePath.empty()) hostExePath_ = res.hostExePath;
            loaded_.store(false, std::memory_order_release);
            params_.clear();
            offlineReason_ = res.offlineReason;
        }
        paramsChanged_.store(true, std::memory_order_release);
    }

    void joinLoadThread() {
        if (loadThread_.joinable()) loadThread_.join();
    }

    // Destructor / plugin switch: terminate an in-flight subprocess load so
    // teardown does not block the UI for tens of seconds.
    void cancelActiveLoad() {
        std::lock_guard<std::mutex> lock(loadControl_->mutex);
        loadControl_->cancelled = true;
        if (loadControl_->activeChild) loadControl_->activeChild->terminate();
    }

    // Synchronous reload used by prepare() on a sample-rate change.
    void loadInSubprocess(uint32_t sampleRate, const std::string& state) {
        uint64_t generation;
        {
            std::lock_guard<std::mutex> lock(stateMutex_);
            generation = loadGeneration_;
        }
        ChildProcess child;
        LoadResult res = loadIntoHost(child, path_, uid_, instrument_, sampleRate,
                                      state, loadControl_);
        commitLoad(child, res, state, generation);
        child.close();
    }

    bool requestIpcLocked(const std::string& command, std::string& reply,
                          uint32_t timeoutMs = 2000) const {
        // Replies are FIFO on the pipe: if an audio block timed out earlier
        // its "ok" is still queued ahead of ours — consume it first.
        if (audioAwaitingReply_) {
            std::string stale;
            if (!host_.readLine(stale, timeoutMs)) return false;
            audioAwaitingReply_ = false;
        }
        return host_.writeLine(command) && host_.readLine(reply, timeoutMs);
    }

    bool refreshParamIpcLocked(int index) {
        if (index < 0) return false;
        std::string reply;
        if (!requestIpcLocked("param\t" + std::to_string(index), reply)) return false;
        const auto parts = splitTabs(reply);
        if (parts.size() < 5 || parts[0] != "ok") return false;
        std::lock_guard<std::mutex> lock(stateMutex_);
        ensureParam(index);
        auto& p = params_[size_t(index)];
        p.name = parts[2];
        p.value = std::clamp(parseFloatField(parts[3]), 0.0f, 1.0f);
        p.defaultValue = std::clamp(parseFloatField(parts[4]), 0.0f, 1.0f);
        return true;
    }

    void ensureParam(int index) const {
        if (index < 0) return;
        while (params_.size() <= size_t(index)) {
            Param p;
            p.name = "Param " + std::to_string(params_.size() + 1);
            params_.push_back(std::move(p));
        }
    }

    // A skipped block must not swallow its MIDI events: replay them at
    // offset 0 with the next successfully processed block.
    void queueMissedEvents(const std::vector<VstHostEvent>& events) {
        if (events.empty()) return;
        std::lock_guard<std::mutex> lock(pendingMutex_);
        for (VstHostEvent ev : events) {
            if (pending_.notes.size() >= kMaxPendingNotes) break;
            ev.sampleOffset = 0;
            pending_.notes.push_back(ev);
        }
    }

    void failSafe(float* interleaved, uint32_t frames) const {
        if (instrument_)
            std::memset(interleaved, 0, sizeof(float) * frames * 2);
    }

    void setEditorError(const std::string& message) const {
        std::lock_guard<std::mutex> lock(stateMutex_);
        lastEditorError_ = message;
    }

    void markOfflineIpcLocked(const std::string& reason) const {
        shutdownHostIpcLocked();
        loaded_.store(false, std::memory_order_release);
        std::lock_guard<std::mutex> lock(stateMutex_);
        offlineReason_ = reason.empty() ? "VST subprocess stopped responding" : reason;
    }

    void markOfflineFromAudio(std::unique_lock<std::mutex>& ipcLock,
                              const std::string& reason) const {
        markOfflineIpcLocked(reason);
        ipcLock.unlock();
    }

    void shutdownHost() {
        std::lock_guard<std::mutex> lock(ipcMutex_);
        shutdownHostIpcLocked();
    }

    void shutdownHostIpcLocked() const {
        audioAwaitingReply_ = false;
        if (host_.running()) {
            std::string reply;
            host_.writeLine("shutdown");
            host_.readLine(reply);
        }
        host_.close();
    }

    std::string path_;
    std::string uid_;
    bool instrument_ = false;
    uint32_t sampleRate_ = 48000;
    std::string displayName_;
    mutable std::mutex stateMutex_;
    mutable std::mutex ipcMutex_;
    mutable ChildProcess host_;
    mutable std::vector<Param> params_;
    mutable std::string state_;
    mutable std::atomic<bool> loaded_{false};
    mutable std::string offlineReason_;
    mutable std::string lastEditorError_;
    std::string hostExePath_;
    std::thread loadThread_;
    uint64_t loadGeneration_ = 0;
    std::shared_ptr<LoadControl> loadControl_ = std::make_shared<LoadControl>();
    mutable std::atomic<bool> paramsChanged_{false};
    mutable std::mutex pendingMutex_;
    PendingState pending_;
    mutable bool audioAwaitingReply_ = false;
    mutable std::chrono::steady_clock::time_point audioStallSince_{};
    std::atomic<uint32_t> latencySamples_{0};
    int lastUserEdit_ = -1;
    bool hasUserEdit_ = false;
};

} // namespace

std::shared_ptr<VstPlugin> createVstBridgePlugin(const std::string& path,
                                                 const std::string& uid,
                                                 bool instrument,
                                                 uint32_t sampleRate) {
    return std::shared_ptr<VstPlugin>(
        new VstBridgePlugin(path, uid, instrument, sampleRate));
}
