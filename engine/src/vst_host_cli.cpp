#include "vst_host.h"
#include "vst_protocol.h"

#include <algorithm>
#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <cstdlib>
#include <functional>
#include <iostream>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <objbase.h>
#endif

namespace {

using pydaw::vst_protocol::hexDecode;
using pydaw::vst_protocol::hexEncode;
using pydaw::vst_protocol::kSharedAudioFrames;
using pydaw::vst_protocol::parseDoubleField;
using pydaw::vst_protocol::parseFloatField;
using pydaw::vst_protocol::parseInt64Field;
using pydaw::vst_protocol::parseIntField;
using pydaw::vst_protocol::parseSizeField;
using pydaw::vst_protocol::parseUint32Field;
using pydaw::vst_protocol::parseUintPtrField;
using pydaw::vst_protocol::sanitizeField;
using pydaw::vst_protocol::splitTabs;

struct SharedAudio {
#ifdef _WIN32
    HANDLE mapping = nullptr;
#endif
    float* samples = nullptr;
    size_t frames = 0;
};

std::vector<VstHostEvent> parseEvents(const std::vector<std::string>& parts,
                                      size_t countIndex,
                                      size_t firstEventIndex) {
    std::vector<VstHostEvent> events;
    if (parts.size() <= countIndex) return events;
    const size_t eventCount = parseSizeField(parts[countIndex]);
    events.reserve(eventCount);
    for (size_t i = 0; i < eventCount && firstEventIndex + i < parts.size(); ++i) {
        const auto spec = parts[firstEventIndex + i];
        std::vector<std::string> fields;
        size_t start = 0;
        while (true) {
            const size_t pos = spec.find(',', start);
            if (pos == std::string::npos) {
                fields.push_back(spec.substr(start));
                break;
            }
            fields.push_back(spec.substr(start, pos - start));
            start = pos + 1;
        }
        if (fields.size() < 4) continue;
        VstHostEvent ev;
        ev.type = parseIntField(fields[0]) ? VstHostEvent::NoteOn
                                           : VstHostEvent::NoteOff;
        ev.pitch = parseIntField(fields[1]);
        ev.velocity = parseFloatField(fields[2]);
        ev.sampleOffset = int32_t(parseIntField(fields[3]));
        events.push_back(ev);
    }
    return events;
}

std::vector<std::pair<int, float>> parseParamUpdates(
    const std::vector<std::string>& parts, size_t countIndex) {
    std::vector<std::pair<int, float>> params;
    if (parts.size() <= countIndex) return params;
    const size_t count = parseSizeField(parts[countIndex]);
    params.reserve(count);
    for (size_t i = 0; i < count && countIndex + 1 + i < parts.size(); ++i) {
        const auto spec = parts[countIndex + 1 + i];
        const size_t comma = spec.find(',');
        if (comma == std::string::npos) continue;
        const int index = parseIntField(spec.substr(0, comma), -1);
        if (index < 0) continue;
        const float value = parseFloatField(spec.substr(comma + 1));
        params.emplace_back(index, std::clamp(value, 0.0f, 1.0f));
    }
    return params;
}

// Debug aid (PYDAW_VST_HOST_LOG): periodically report the child-side output
// peak so "plugin is silent" vs "audio lost in IPC" can be told apart.
void logAudioPeak(const float* samples, uint32_t frames, size_t events) {
    static const bool enabled = [] {
        const char* env = std::getenv("PYDAW_VST_HOST_LOG");
        return env && *env;
    }();
    if (!enabled) return;
    static uint64_t blocks = 0;
    static float peak = 0.f;
    static size_t eventsSeen = 0;
    for (uint32_t i = 0; i < frames * 2; ++i)
        peak = std::max(peak, std::abs(samples[i]));
    eventsSeen += events;
    if (++blocks % 200 == 0) {
        std::fprintf(stderr, "[vst] audio: blocks=%llu peak=%.4f events=%zu\n",
                     (unsigned long long)blocks, peak, eventsSeen);
        std::fflush(stderr);
        peak = 0.f;
        eventsSeen = 0;
    }
}

#ifdef _WIN32
// Marshals plugin GUI work (load / editor commands) onto the process main
// thread, which runs a continuous Windows message pump. VST3 editors must
// be created and serviced by a thread that dispatches window messages;
// the previous implementation only pumped between stdin commands, so
// heavyweight editors (Kontakt, ...) stalled during attach and timed out.
class GuiDispatcher {
public:
    GuiDispatcher() {
        wake_ = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    }
    ~GuiDispatcher() {
        if (wake_) CloseHandle(wake_);
    }

    // Reader thread: run `fn` on the GUI thread and block until done.
    void run(const std::function<void()>& fn) {
        std::unique_lock<std::mutex> lock(mu_);
        task_ = fn;
        done_ = false;
        SetEvent(wake_);
        cv_.wait(lock, [this] { return done_; });
    }

    void quit() {
        quit_.store(true, std::memory_order_release);
        SetEvent(wake_);
    }

    // Main (GUI) thread: pump messages and execute marshaled tasks.
    void loop() {
        while (!quit_.load(std::memory_order_acquire)) {
            MsgWaitForMultipleObjects(1, &wake_, FALSE, INFINITE, QS_ALLINPUT);
            MSG msg;
            while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) {
                TranslateMessage(&msg);
                DispatchMessageW(&msg);
            }
            std::function<void()> task;
            {
                std::lock_guard<std::mutex> lock(mu_);
                task.swap(task_);
            }
            if (task) {
                // Always release the waiting reader thread, even when the
                // marshaled task throws: otherwise run() blocks forever.
                try {
                    task();
                } catch (...) {
                }
                std::lock_guard<std::mutex> lock(mu_);
                done_ = true;
                cv_.notify_all();
            }
        }
    }

private:
    HANDLE wake_ = nullptr;
    std::mutex mu_;
    std::condition_variable cv_;
    std::function<void()> task_;
    bool done_ = true;
    std::atomic<bool> quit_{false};
};
#endif

int runProbe(int argc, char** argv) {
#ifdef _WIN32
    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    OleInitialize(nullptr);
#endif
    if (argc < 6) return 2;
    const std::string path = argv[2];
    const std::string uid = argv[3];
    const bool instrument = std::string(argv[4]) == "1";
    const uint32_t sampleRate = parseUint32Field(argv[5]);
    auto plugin = createVstPlugin(path, uid, instrument, sampleRate);
    if (!plugin) {
#ifdef _WIN32
        OleUninitialize();
#endif
        return 3;
    }
    plugin->prepare(sampleRate);
    std::cout << "ok\t" << sanitizeField(plugin->name()) << '\t'
              << plugin->paramCount() << '\t' << plugin->latencySamples()
              << std::endl;
#ifdef _WIN32
    OleUninitialize();
#endif
    return 0;
}

#ifdef _WIN32
using GuiRunner = GuiDispatcher*;
#else
using GuiRunner = void*;
#endif

// Run `fn` on the GUI/main thread when a dispatcher is available;
// otherwise execute inline (POSIX stub build, probes).
void onGuiThread(GuiRunner gui, const std::function<void()>& fn) {
#ifdef _WIN32
    if (gui) {
        gui->run(fn);
        return;
    }
#endif
    fn();
}

int serveCommands(const SharedAudio& shared, GuiRunner gui) {
    std::shared_ptr<VstPlugin> plugin;
    std::string line;
    while (std::getline(std::cin, line)) {
        const auto parts = splitTabs(line);
        if (parts.empty()) continue;
        const std::string& cmd = parts[0];
        try {
            if (cmd == "shutdown") {
                std::cout << "ok" << std::endl;
                return 0;
            } else if (cmd == "load" && parts.size() >= 5) {
                const bool instrument = parts[3] == "1";
                const uint32_t sr = parseUint32Field(parts[4]);
                std::string reply = "err\tload";
                // Load on the GUI thread: plugin construction may create
                // hidden windows / COM objects tied to the pumping thread.
                onGuiThread(gui, [&] {
                    plugin = createVstPlugin(parts[1], parts[2], instrument, sr);
                    if (!plugin) return;
                    plugin->prepare(sr);
                    std::ostringstream out;
                    out << "ok\t" << sanitizeField(plugin->name()) << '\t'
                        << plugin->paramCount() << '\t'
                        << plugin->latencySamples();
                    reply = out.str();
                });
                std::cout << reply << std::endl;
            } else if (cmd == "param_count") {
                std::cout << "ok\t" << (plugin ? plugin->paramCount() : 0)
                          << std::endl;
            } else if (cmd == "param" && parts.size() >= 2) {
                const int idx = parseIntField(parts[1], -1);
                if (!plugin) {
                    std::cout << "err\tnot_loaded" << std::endl;
                } else {
                    auto info = plugin->getParamInfo(idx);
                    std::cout << "ok\t" << std::get<0>(info) << '\t'
                              << sanitizeField(std::get<1>(info)) << '\t'
                              << std::get<2>(info) << '\t'
                              << plugin->getParamDefault(idx) << std::endl;
                }
            } else if (cmd == "set_param" && parts.size() >= 3) {
                if (plugin) plugin->setParamNormalizedByIndex(
                    parseIntField(parts[1], -1), parseFloatField(parts[2]), false);
                std::cout << "ok" << std::endl;
            } else if (cmd == "consume_tweak") {
                // TWEAK mode: index of the parameter last touched by the
                // user in the native editor (hostPerformEdit), -1 when none.
                std::cout << "ok\t"
                          << (plugin ? plugin->consumeLastUserEdit() : -1)
                          << std::endl;
            } else if (cmd == "sync_controllers") {
                if (plugin) plugin->syncControllerParams();
                std::cout << "ok" << std::endl;
            } else if (cmd == "get_state") {
                std::cout << "ok\t" << (plugin ? hexEncode(plugin->getState()) : "")
                          << std::endl;
            } else if (cmd == "set_state" && parts.size() >= 2) {
                const bool ok = plugin && plugin->setState(hexDecode(parts[1]));
                std::cout << (ok ? "ok" : "err\tstate") << std::endl;
            } else if (cmd == "has_editor") {
                bool has = false;
                onGuiThread(gui, [&] { has = plugin && plugin->hasEditor(); });
                std::cout << "ok\t" << (has ? 1 : 0) << std::endl;
            } else if (cmd == "open_editor" && parts.size() >= 2) {
                uintptr_t hwnd = parseUintPtrField(parts[1]);
                bool ok = false;
                onGuiThread(gui, [&] {
                    ok = plugin && plugin->openEditor(
                        reinterpret_cast<void*>(hwnd));
                });
                std::cout << (ok ? "ok" : "err\teditor") << std::endl;
            } else if (cmd == "open_editor_top_level") {
                bool ok = false;
                uintptr_t owner = parts.size() >= 2 ? parseUintPtrField(parts[1]) : 0;
                std::string title = parts.size() >= 3 ? hexDecode(parts[2]) : "";
                onGuiThread(gui, [&] {
                    ok = plugin && plugin->openEditorTopLevel(
                        reinterpret_cast<void*>(owner), title);
                });
                std::cout << (ok ? "ok" : "err\teditor") << std::endl;
            } else if (cmd == "close_editor") {
                onGuiThread(gui, [&] { if (plugin) plugin->closeEditor(); });
                std::cout << "ok" << std::endl;
            } else if (cmd == "editor_size") {
                // Cheap cached read; answered on the reader thread. The UI
                // polls these every ~100ms — marshaling to the GUI thread
                // (busy pumping heavyweight plugin UIs like Kontakt) made
                // each poll slow and starved the audio protocol behind it.
                auto size = std::pair<int, int>{0, 0};
                if (plugin) size = plugin->editorSize();
                std::cout << "ok\t" << size.first << '\t' << size.second
                          << std::endl;
            } else if (cmd == "check_resize") {
                int w = 0;
                int h = 0;
                const bool changed = plugin && plugin->checkSizeChanged(w, h);
                std::cout << "ok\t" << (changed ? 1 : 0) << '\t' << w << '\t'
                          << h << std::endl;
            } else if (cmd == "editor_can_resize") {
                // Calls into IPlugView: keep on the GUI thread (only used
                // once when the editor opens, not polled).
                bool can = false;
                onGuiThread(gui, [&] {
                    can = plugin && plugin->editorCanResize();
                });
                std::cout << "ok\t" << (can ? 1 : 0) << std::endl;
            } else if (cmd == "resize_editor" && parts.size() >= 3) {
                bool ok = false;
                onGuiThread(gui, [&] {
                    ok = plugin && plugin->resizeEditor(
                        parseIntField(parts[1]), parseIntField(parts[2]));
                });
                std::cout << (ok ? "ok" : "err\tresize") << std::endl;
            } else if (cmd == "set_content_scale" && parts.size() >= 2) {
                bool ok = false;
                onGuiThread(gui, [&] {
                    ok = plugin && plugin->setContentScale(
                        parseFloatField(parts[1], 1.0f));
                });
                std::cout << (ok ? "ok" : "err\tscale") << std::endl;
            } else if (cmd == "process_shm2" && parts.size() >= 8) {
                // process_shm2\tframes\thasTransport\tbpm\tplaying\tpos\t
                // jump\tallOff\tnEvents\tevents...\tnParams\tparams... —
                // audio via shared memory plus transport, preview notes and
                // automation values queued since the last block.
                const uint32_t frames = parseUint32Field(parts[1]);
                if (plugin && shared.samples && frames <= shared.frames) {
                    if (parts[6] == "1") plugin->onTransportJump();
                    if (parts[7] == "1") plugin->allNotesOff();
                    if (parts[2] == "1") {
                        const double bpm = parseDoubleField(parts[3], 120.0);
                        const bool playing = parts[4] == "1";
                        const int64_t pos = parseInt64Field(parts[5]);
                        plugin->setTransport(bpm, playing, pos);
                    }
                    auto events = parseEvents(parts, 8, 9);
                    const size_t paramCountIndex = 9 + events.size();
                    auto params = parseParamUpdates(parts, paramCountIndex);
                    for (const auto& p : params)
                        plugin->setParamFromAudioThread(p.first, p.second);
                    plugin->processWithEvents(shared.samples, frames, events);
                    logAudioPeak(shared.samples, frames, events.size());
                }
                std::cout << "ok" << std::endl;
            } else if (cmd == "process_shm" && parts.size() >= 3) {
                const uint32_t frames = parseUint32Field(parts[1]);
                if (plugin && shared.samples && frames <= shared.frames) {
                    auto events = parseEvents(parts, 2, 3);
                    plugin->processWithEvents(shared.samples, frames, events);
                }
                std::cout << "ok" << std::endl;
            } else if (cmd == "process" && parts.size() >= 3) {
                const uint32_t frames = parseUint32Field(parts[1]);
                std::string raw = hexDecode(parts[2]);
                if (raw.size() == sizeof(float) * frames * 2 && plugin) {
                    plugin->process(reinterpret_cast<float*>(raw.data()), frames);
                }
                std::cout << "ok\t" << hexEncode(raw) << std::endl;
            } else if (cmd == "process_events" && parts.size() >= 4) {
                const uint32_t frames = parseUint32Field(parts[1]);
                std::string raw = hexDecode(parts[2]);
                std::vector<VstHostEvent> events = parseEvents(parts, 3, 4);
                if (raw.size() == sizeof(float) * frames * 2 && plugin) {
                    plugin->processWithEvents(reinterpret_cast<float*>(raw.data()),
                                              frames, events);
                }
                std::cout << "ok\t" << hexEncode(raw) << std::endl;
            } else {
                std::cout << "err\tunknown" << std::endl;
            }
        } catch (const std::exception& exc) {
            std::cout << "err\t" << sanitizeField(exc.what()) << std::endl;
        }
    }
    return 0;
}

int runServer(const SharedAudio& shared) {
#ifdef _WIN32
    GuiDispatcher gui;
    int rc = 0;
    // stdin protocol (including realtime audio) runs on a worker thread;
    // the process main thread stays a responsive GUI message pump.
    std::thread reader([&] {
        rc = serveCommands(shared, &gui);
        gui.quit();
    });
    gui.loop();
    reader.join();
    return rc;
#else
    return serveCommands(shared, nullptr);
#endif
}

} // namespace

int main(int argc, char** argv) {
#ifdef _WIN32
    // The plugin view lives in this process but is parented into the
    // DPI-aware Qt main process. Without matching awareness Windows
    // DPI-virtualizes our child window, which renders plugin GUIs
    // scaled/clipped on high-DPI displays.
    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
#endif
    if (argc >= 2 && std::string(argv[1]) == "--probe")
        return runProbe(argc, argv);

#ifdef _WIN32
    OleInitialize(nullptr);
#endif

    SharedAudio shared;
#ifdef _WIN32
    for (int i = 1; i + 1 < argc; ++i) {
        if (std::string(argv[i]) == "--audio-map") {
            shared.mapping = reinterpret_cast<HANDLE>(
                parseUintPtrField(argv[i + 1]));
            shared.frames = kSharedAudioFrames;
            shared.samples = static_cast<float*>(MapViewOfFile(
                shared.mapping, FILE_MAP_ALL_ACCESS, 0, 0,
                sizeof(float) * kSharedAudioFrames * 2));
            break;
        }
    }
#endif

    const int rc = runServer(shared);
#ifdef _WIN32
    if (shared.samples) UnmapViewOfFile(shared.samples);
    if (shared.mapping) CloseHandle(shared.mapping);
    OleUninitialize();
#endif
    return rc;
}
