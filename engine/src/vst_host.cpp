#include "vst_host.h"

#include "pluginterfaces/base/smartpointer.h"
#include "pluginterfaces/base/funknown.h"
#include "pluginterfaces/base/ibstream.h"
#include "pluginterfaces/base/ipluginbase.h"
#include "pluginterfaces/gui/iplugview.h"
#include "pluginterfaces/gui/iplugviewcontentscalesupport.h"
#include "pluginterfaces/vst/ivstaudioprocessor.h"
#include "pluginterfaces/vst/ivstcomponent.h"
#include "pluginterfaces/vst/ivsteditcontroller.h"
#include "pluginterfaces/vst/ivstevents.h"
#include "pluginterfaces/vst/ivsthostapplication.h"
#include "pluginterfaces/vst/ivstmessage.h"
#include "pluginterfaces/vst/ivstparameterchanges.h"
#include "pluginterfaces/vst/ivstpluginterfacesupport.h"
#include "pluginterfaces/vst/ivstprocesscontext.h"

#include <algorithm>
#include <atomic>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <map>
#include <mutex>
#include <set>
#include <vector>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <objbase.h>
#endif

namespace fs = std::filesystem;
using namespace Steinberg;
using namespace Steinberg::Vst;

// Diagnostic trace for plugin loading problems (stderr, cheap).
#define VST_LOG(...) do { \
    std::fprintf(stderr, "[vst] " __VA_ARGS__); \
    std::fputc('\n', stderr); \
    std::fflush(stderr); \
} while (0)

namespace {

constexpr uint32_t kMaxBlock = 4096;

// ---------------------------------------------------------------------------
// TUID / string helpers
// ---------------------------------------------------------------------------

std::string tuidToHex(const TUID uid) {
    static const char hex[] = "0123456789abcdef";
    std::string s(32, '0');
    for (int i = 0; i < 16; ++i) {
        s[size_t(i) * 2] = hex[(uid[i] >> 4) & 0x0f];
        s[size_t(i) * 2 + 1] = hex[uid[i] & 0x0f];
    }
    return s;
}

bool hexToTuid(const std::string& hex, TUID uid) {
    if (hex.size() != 32) return false;
    auto nib = [](char c) -> int {
        if (c >= '0' && c <= '9') return c - '0';
        if (c >= 'a' && c <= 'f') return c - 'a' + 10;
        if (c >= 'A' && c <= 'F') return c - 'A' + 10;
        return -1;
    };
    for (int i = 0; i < 16; ++i) {
        const int hi = nib(hex[size_t(i) * 2]);
        const int lo = nib(hex[size_t(i) * 2 + 1]);
        if (hi < 0 || lo < 0) return false;
        uid[i] = static_cast<uint8>(hi << 4 | lo);
    }
    return true;
}

std::string string128ToUtf8(const String128 s) {
    std::string out;
    out.reserve(64);
    for (int i = 0; i < 128 && s[i]; ++i) {
        const char16 c = s[i];
        if (c < 0x80) out.push_back(static_cast<char>(c));
        else if (c < 0x800) {
            out.push_back(static_cast<char>(0xC0 | (c >> 6)));
            out.push_back(static_cast<char>(0x80 | (c & 0x3F)));
        } else {
            out.push_back(static_cast<char>(0xE0 | (c >> 12)));
            out.push_back(static_cast<char>(0x80 | ((c >> 6) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | (c & 0x3F)));
        }
    }
    return out;
}

bool subCategoriesHasInstrument(const char* sub) {
    if (!sub || !sub[0]) return false;
    return std::strstr(sub, "Instrument") != nullptr;
}

// ---------------------------------------------------------------------------
// Host-side FUnknown helpers
// ---------------------------------------------------------------------------

template <typename Base>
class HostObject : public Base {
public:
    uint32_t PLUGIN_API addRef() override { return ++refs_; }
    uint32_t PLUGIN_API release() override {
        const uint32_t r = --refs_;
        if (r == 0) delete this;
        return r;
    }
    tresult PLUGIN_API queryInterface(const TUID iid, void** obj) override {
        if (FUnknownPrivate::iidEqual(iid, Base::iid)) {
            addRef();
            *obj = static_cast<Base*>(this);
            return kResultOk;
        }
        if (FUnknownPrivate::iidEqual(iid, FUnknown::iid)) {
            addRef();
            *obj = static_cast<FUnknown*>(static_cast<Base*>(this));
            return kResultOk;
        }
        *obj = nullptr;
        return kNoInterface;
    }

protected:
    uint32_t refs_ = 1;
};

// Attribute list for host-created IMessage objects. Serum 2 / iZotope &
// friends use component<->controller messages extensively; a host that
// returns kNoInterface from createInstance makes them crash.
class HostAttributeList : public HostObject<IAttributeList> {
public:
    tresult PLUGIN_API setInt(AttrID id, int64 value) override {
        if (!id) return kInvalidArgument;
        std::lock_guard<std::mutex> lock(mu_);
        ints_[id] = value;
        return kResultOk;
    }
    tresult PLUGIN_API getInt(AttrID id, int64& value) override {
        if (!id) return kInvalidArgument;
        std::lock_guard<std::mutex> lock(mu_);
        auto it = ints_.find(id);
        if (it == ints_.end()) return kResultFalse;
        value = it->second;
        return kResultOk;
    }
    tresult PLUGIN_API setFloat(AttrID id, double value) override {
        if (!id) return kInvalidArgument;
        std::lock_guard<std::mutex> lock(mu_);
        floats_[id] = value;
        return kResultOk;
    }
    tresult PLUGIN_API getFloat(AttrID id, double& value) override {
        if (!id) return kInvalidArgument;
        std::lock_guard<std::mutex> lock(mu_);
        auto it = floats_.find(id);
        if (it == floats_.end()) return kResultFalse;
        value = it->second;
        return kResultOk;
    }
    tresult PLUGIN_API setString(AttrID id, const TChar* string) override {
        if (!id || !string) return kInvalidArgument;
        std::lock_guard<std::mutex> lock(mu_);
        std::vector<TChar> s;
        for (const TChar* p = string; *p; ++p) s.push_back(*p);
        s.push_back(0);
        strings_[id] = std::move(s);
        return kResultOk;
    }
    tresult PLUGIN_API getString(AttrID id, TChar* string,
                                 uint32 sizeInBytes) override {
        if (!id || !string) return kInvalidArgument;
        std::lock_guard<std::mutex> lock(mu_);
        auto it = strings_.find(id);
        if (it == strings_.end()) return kResultFalse;
        const uint32 want = uint32(it->second.size() * sizeof(TChar));
        std::memcpy(string, it->second.data(),
                    std::min(sizeInBytes, want));
        return kResultOk;
    }
    tresult PLUGIN_API setBinary(AttrID id, const void* data,
                                 uint32 sizeInBytes) override {
        if (!id || (!data && sizeInBytes)) return kInvalidArgument;
        std::lock_guard<std::mutex> lock(mu_);
        auto& v = binaries_[id];
        v.assign(static_cast<const char*>(data),
                 static_cast<const char*>(data) + sizeInBytes);
        return kResultOk;
    }
    tresult PLUGIN_API getBinary(AttrID id, const void*& data,
                                 uint32& sizeInBytes) override {
        if (!id) return kInvalidArgument;
        std::lock_guard<std::mutex> lock(mu_);
        auto it = binaries_.find(id);
        if (it == binaries_.end()) return kResultFalse;
        data = it->second.data();
        sizeInBytes = uint32(it->second.size());
        return kResultOk;
    }

private:
    std::mutex mu_;
    std::map<std::string, int64> ints_;
    std::map<std::string, double> floats_;
    std::map<std::string, std::vector<TChar>> strings_;
    std::map<std::string, std::vector<char>> binaries_;
};

class HostMessage : public HostObject<IMessage> {
public:
    ~HostMessage() {
        if (attrs_) attrs_->release();
    }
    FIDString PLUGIN_API getMessageID() override {
        return id_.empty() ? nullptr : id_.c_str();
    }
    void PLUGIN_API setMessageID(FIDString id) override {
        id_ = id ? id : "";
    }
    IAttributeList* PLUGIN_API getAttributes() override {
        if (!attrs_) attrs_ = new HostAttributeList();
        return attrs_;
    }

private:
    std::string id_;
    HostAttributeList* attrs_ = nullptr;
};

class HostPlugInterfaceSupport : public HostObject<IPlugInterfaceSupport> {
public:
    tresult PLUGIN_API isPlugInterfaceSupported(const TUID iid) override {
        static const FUID supported[] = {
            IComponent::iid,
            IAudioProcessor::iid,
            IEditController::iid,
            IConnectionPoint::iid,
        };
        for (const auto& s : supported)
            if (FUnknownPrivate::iidEqual(iid, s)) return kResultTrue;
        return kResultFalse;
    }
    uint32_t PLUGIN_API release() override { return 100; }
};

static HostPlugInterfaceSupport gPlugInterfaceSupport;

class ImmortalHostObject : public HostObject<IHostApplication> {
public:
    tresult PLUGIN_API getName(String128 name) override {
        const char16 host[] = u"PyDAW";
        for (int i = 0; i < 6; ++i) name[i] = host[i];
        return kResultOk;
    }
    tresult PLUGIN_API createInstance(TUID cid, TUID iid, void** obj) override {
        if (!obj) return kInvalidArgument;
        auto wants = [&](const FUID& classId) {
            return FUnknownPrivate::iidEqual(cid, classId)
                && (FUnknownPrivate::iidEqual(iid, classId)
                    || FUnknownPrivate::iidEqual(iid, FUnknown::iid));
        };
        if (wants(IMessage::iid)) {
            *obj = static_cast<IMessage*>(new HostMessage());
            return kResultOk;
        }
        if (wants(IAttributeList::iid)) {
            *obj = static_cast<IAttributeList*>(new HostAttributeList());
            return kResultOk;
        }
        *obj = nullptr;
        return kNoInterface;
    }
    tresult PLUGIN_API queryInterface(const TUID iid, void** obj) override {
        if (FUnknownPrivate::iidEqual(iid, IPlugInterfaceSupport::iid)) {
            *obj = static_cast<IPlugInterfaceSupport*>(&gPlugInterfaceSupport);
            return kResultOk;
        }
        return HostObject<IHostApplication>::queryInterface(iid, obj);
    }
    uint32_t PLUGIN_API release() override { return 100; }
};

static ImmortalHostObject gHostApp;

// ---------------------------------------------------------------------------
// IBStream over std::string / vector
// ---------------------------------------------------------------------------

class MemoryStream : public HostObject<IBStream> {
public:
    explicit MemoryStream(std::string data = {}) : data_(std::move(data)) {}

    tresult PLUGIN_API read(void* buffer, int32 numBytes,
                            int32* numBytesRead) override {
        const int32 avail = int32(data_.size()) - pos_;
        const int32 n = std::min(numBytes, std::max(0, avail));
        if (n > 0) std::memcpy(buffer, data_.data() + pos_, size_t(n));
        pos_ += n;
        if (numBytesRead) *numBytesRead = n;
        return kResultOk;
    }

    tresult PLUGIN_API write(void* buffer, int32 numBytes,
                             int32* numBytesWritten) override {
        if (pos_ + numBytes > int32(data_.size()))
            data_.resize(size_t(pos_ + numBytes));
        std::memcpy(data_.data() + pos_, buffer, size_t(numBytes));
        pos_ += numBytes;
        if (numBytesWritten) *numBytesWritten = numBytes;
        return kResultOk;
    }

    tresult PLUGIN_API seek(int64 pos, int32 mode, int64* result) override {
        int64 np = pos_;
        if (mode == kIBSeekSet) np = pos;
        else if (mode == kIBSeekCur) np += pos;
        else if (mode == kIBSeekEnd) np = int64(data_.size()) + pos;
        if (np < 0) np = 0;
        pos_ = int32(np);
        if (result) *result = pos_;
        return kResultOk;
    }

    tresult PLUGIN_API tell(int64* pos) override {
        if (pos) *pos = pos_;
        return kResultOk;
    }

    std::string takeData() { return std::move(data_); }
    void resetRead() { pos_ = 0; }

private:
    std::string data_;
    int32 pos_ = 0;
};

// ---------------------------------------------------------------------------
// Parameter / event queues for process()
// ---------------------------------------------------------------------------

struct PendingParam {
    ParamID id = 0;
    ParamValue value = 0.0;
};

class BlockParamQueue : public HostObject<IParamValueQueue> {
public:
    explicit BlockParamQueue(ParamID id) : id_(id) {}

    ParamID PLUGIN_API getParameterId() override { return id_; }
    int32 PLUGIN_API getPointCount() override {
        return int32(points_.size());
    }
    tresult PLUGIN_API getPoint(int32 index, int32& sampleOffset,
                                 ParamValue& value) override {
        if (index < 0 || index >= int32(points_.size())) return kResultFalse;
        sampleOffset = points_[size_t(index)].offset;
        value = points_[size_t(index)].value;
        return kResultOk;
    }
    tresult PLUGIN_API addPoint(int32, ParamValue, int32&) override {
        return kResultFalse;
    }

    void add(int32 offset, ParamValue v) {
        points_.push_back({offset, v});
    }

private:
    struct Pt { int32 offset; ParamValue value; };
    ParamID id_;
    std::vector<Pt> points_;
};

class BlockParamChanges : public HostObject<IParameterChanges> {
public:
    int32 PLUGIN_API getParameterCount() override {
        return int32(queues_.size());
    }
    IParamValueQueue* PLUGIN_API getParameterData(int32 index) override {
        if (index < 0 || index >= int32(queues_.size())) return nullptr;
        return queues_[size_t(index)].get();
    }
    IParamValueQueue* PLUGIN_API addParameterData(const ParamID& id,
                                                    int32& index) override {
        index = int32(queues_.size());
        auto q = owned(new BlockParamQueue(id));
        queues_.push_back(q);
        return q;
    }

    void clear() { queues_.clear(); }

    void addParam(ParamID id, int32 offset, ParamValue value) {
        for (auto& q : queues_) {
            if (q->getParameterId() == id) {
                static_cast<BlockParamQueue*>(q.get())->add(offset, value);
                return;
            }
        }
        int32 idx = 0;
        auto* q = static_cast<BlockParamQueue*>(addParameterData(id, idx));
        q->add(offset, value);
    }

private:
    std::vector<IPtr<IParamValueQueue>> queues_;
};

class BlockEventList : public HostObject<IEventList> {
public:
    int32 PLUGIN_API getEventCount() override {
        return int32(events_.size());
    }
    tresult PLUGIN_API getEvent(int32 index, Event& e) override {
        if (index < 0 || index >= int32(events_.size())) return kResultFalse;
        e = events_[size_t(index)];
        return kResultOk;
    }
    tresult PLUGIN_API addEvent(Event& e) override {
        events_.push_back(e);
        return kResultOk;
    }

    void clear() { events_.clear(); }
    void append(const Event& e) { events_.push_back(e); }

private:
    std::vector<Event> events_;
};

// ---------------------------------------------------------------------------
// Component handler + plug frame
// ---------------------------------------------------------------------------

class VstComponentHandler : public HostObject<IComponentHandler> {
public:
    explicit VstComponentHandler(VstPlugin* owner) : owner_(owner) {}

    void detach() {
        std::lock_guard<std::mutex> lock(mutex_);
        owner_ = nullptr;
    }

    tresult PLUGIN_API beginEdit(ParamID) override { return kResultOk; }
    tresult PLUGIN_API performEdit(ParamID id,
                                   ParamValue valueNormalized) override;
    tresult PLUGIN_API endEdit(ParamID) override { return kResultOk; }
    tresult PLUGIN_API restartComponent(int32 flags) override;

private:
    VstPlugin* owner_;
    std::mutex mutex_;
};

class VstPlugFrame : public HostObject<IPlugFrame> {
public:
    tresult PLUGIN_API resizeView(IPlugView* view, ViewRect* newSize) override {
        if (newSize) {
            std::lock_guard<std::mutex> lock(mutex_);
            pendingW_ = newSize->getWidth();
            pendingH_ = newSize->getHeight();
            sizeChanged_ = true;
        }
        if (view) view->onSize(newSize);
        return kResultOk;
    }

    bool checkSizeChanged(int& w, int& h) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!sizeChanged_) return false;
        w = pendingW_;
        h = pendingH_;
        sizeChanged_ = false;
        return true;
    }

private:
    int pendingW_ = 0, pendingH_ = 0;
    bool sizeChanged_ = false;
    std::mutex mutex_;
};

// ---------------------------------------------------------------------------
// Module loading (Windows)
// ---------------------------------------------------------------------------

#ifdef _WIN32

using InitModuleProc = bool (PLUGIN_API*)();
using ExitModuleProc = bool (PLUGIN_API*)();
using GetFactoryProc = IPluginFactory* (PLUGIN_API*)();

std::wstring utf8ToWide(const std::string& s) {
    if (s.empty()) return {};
    const int n = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
                                      s.c_str(), -1, nullptr, 0);
    if (n <= 0) return {};
    std::wstring w(size_t(n - 1), L'\0');
    MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s.c_str(), -1, w.data(), n);
    return w;
}

std::wstring editorWindowTitle(const std::string& title,
                               const std::string& fallback) {
    std::wstring w = utf8ToWide(title.empty() ? fallback : title);
    if (w.empty()) w = L"VST Editor";
    return w;
}

HWND editorOwnerWindow(void* ownerHwnd) {
    HWND owner = reinterpret_cast<HWND>(ownerHwnd);
    if (!owner || !IsWindow(owner)) return nullptr;
    HWND root = GetAncestor(owner, GA_ROOT);
    return root && IsWindow(root) ? root : owner;
}

LRESULT CALLBACK vstEditorWndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    if (msg == WM_CLOSE) {
        auto* plugin = reinterpret_cast<VstPlugin*>(
            GetWindowLongPtrW(hwnd, GWLP_USERDATA));
        if (plugin) {
            plugin->closeEditor();
            return 0;
        }
    } else if (msg == WM_NCDESTROY) {
        SetWindowLongPtrW(hwnd, GWLP_USERDATA, 0);
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

std::string pathToUtf8(const fs::path& p) {
#if defined(_WIN32)
    const auto u8 = p.u8string();
    return std::string(u8.begin(), u8.end());
#else
    return p.string();
#endif
}

std::string resolveVst3DllPath(const std::string& pathUtf8) {
    std::error_code ec;
    fs::path path = fs::u8path(pathUtf8);
    if (!fs::exists(path, ec)) return {};
    if (fs::is_regular_file(path, ec)) return pathToUtf8(path);
    if (!fs::is_directory(path, ec)) return {};
    const fs::path contents = path / "Contents" / "x86_64-win";
    if (!fs::is_directory(contents, ec)) return {};
    for (const auto& e : fs::directory_iterator(contents, ec)) {
        if (ec) break;
        if (e.path().extension() == ".vst3")
            return pathToUtf8(e.path());
    }
    return {};
}

class VstModule : public std::enable_shared_from_this<VstModule> {
public:
    ~VstModule() { unload(); }

    bool load(const std::string& path) {
        unload();
        dllPath_ = resolveVst3DllPath(path);
        if (dllPath_.empty()) {
            VST_LOG("module: cannot resolve dll in '%s'", path.c_str());
            return false;
        }
        module_ = LoadLibraryW(utf8ToWide(dllPath_).c_str());
        if (!module_) {
            VST_LOG("module: LoadLibrary failed (err=%lu) '%s'",
                    GetLastError(), dllPath_.c_str());
            return false;
        }

        if (auto init = reinterpret_cast<InitModuleProc>(
                GetProcAddress(module_, "InitDll")))
            init();

        auto getFactory = reinterpret_cast<GetFactoryProc>(
            GetProcAddress(module_, "GetPluginFactory"));
        if (!getFactory) {
            VST_LOG("module: no GetPluginFactory '%s'", dllPath_.c_str());
            unload();
            return false;
        }
        factory_ = getFactory();
        if (!factory_) {
            VST_LOG("module: GetPluginFactory returned null '%s'",
                    dllPath_.c_str());
            unload();
            return false;
        }
        factory_->addRef();

        // Many commercial plugins (iZotope, Xfer, ...) expect the host
        // context on the factory before any createInstance call.
        IPluginFactory3* f3 = nullptr;
        if (factory_->queryInterface(
                IPluginFactory3::iid,
                reinterpret_cast<void**>(&f3)) == kResultOk && f3) {
            f3->setHostContext(&gHostApp);
            f3->release();
        }

        bundlePath_ = path;
        return true;
    }

    void unload() {
        if (factory_) {
            factory_->release();
            factory_ = nullptr;
        }
        // Do not call ExitDll / FreeLibrary. Many VST3 plugins (FabFilter,
        // Kontakt, ...) crash on unload when their GUI ran in-process;
        // leaking the module until process exit is the standard host tradeoff.
        module_ = nullptr;
        dllPath_.clear();
        bundlePath_.clear();
    }

    IPluginFactory* factory() const { return factory_; }
    const std::string& bundlePath() const { return bundlePath_; }

private:
    HMODULE module_ = nullptr;
    IPluginFactory* factory_ = nullptr;
    std::string dllPath_;
    std::string bundlePath_;
};

std::shared_ptr<VstModule> getModule(const std::string& path) {
    static std::mutex mu;
    static std::map<std::string, std::weak_ptr<VstModule>> cache;
    std::lock_guard<std::mutex> lock(mu);
    auto it = cache.find(path);
    if (it != cache.end()) {
        if (auto m = it->second.lock()) return m;
    }
    auto mod = std::make_shared<VstModule>();
    if (!mod->load(path)) return nullptr;
    cache[path] = mod;
    return mod;
}

std::string sanitizeUtf8(std::string s) {
    std::string out;
    out.reserve(s.size());
    for (size_t i = 0; i < s.size(); ) {
        const unsigned char c = static_cast<unsigned char>(s[i]);
        if (c < 0x80) { out.push_back(static_cast<char>(c)); ++i; continue; }
        size_t len = 1;
        if ((c & 0xE0) == 0xC0) len = 2;
        else if ((c & 0xF0) == 0xE0) len = 3;
        else if ((c & 0xF8) == 0xF0) len = 4;
        if (i + len > s.size()) break;
        out.append(s, i, len);
        i += len;
    }
    return out;
}

void scanVst3Entries(const std::string& entryPath,
                     std::vector<VstPluginInfo>& out) {
    try {
    auto mod = std::make_shared<VstModule>();
    if (!mod->load(entryPath)) return;

    IPluginFactory* factory = mod->factory();
    IPluginFactory2* factory2 = nullptr;
    factory->queryInterface(IPluginFactory2::iid,
                            reinterpret_cast<void**>(&factory2));

    const int32 n = factory->countClasses();
    for (int32 i = 0; i < n; ++i) {
        VstPluginInfo info;
        info.path = sanitizeUtf8(entryPath);

        if (factory2) {
            PClassInfo2 ci{};
            if (factory2->getClassInfo2(i, &ci) != kResultOk) continue;
            if (std::strcmp(ci.category, kVstAudioEffectClass) != 0) continue;
            info.name = sanitizeUtf8(ci.name ? ci.name : "");
            info.vendor = sanitizeUtf8(ci.vendor ? ci.vendor : "");
            info.category = sanitizeUtf8(ci.category ? ci.category : "");
            info.subCategories = sanitizeUtf8(ci.subCategories ? ci.subCategories : "");
            info.uid = tuidToHex(ci.cid);
            info.isInstrument = subCategoriesHasInstrument(ci.subCategories);
        } else {
            PClassInfo ci{};
            if (factory->getClassInfo(i, &ci) != kResultOk) continue;
            if (std::strcmp(ci.category, kVstAudioEffectClass) != 0) continue;
            info.name = sanitizeUtf8(ci.name ? ci.name : "");
            info.category = sanitizeUtf8(ci.category ? ci.category : "");
            info.uid = tuidToHex(ci.cid);
        }

        PFactoryInfo fi{};
        if (factory->getFactoryInfo(&fi) == kResultOk && info.vendor.empty())
            info.vendor = sanitizeUtf8(fi.vendor);

        out.push_back(std::move(info));
    }
    if (factory2) factory2->release();
    mod->unload();
    } catch (...) {}
}

void scanDirectoryRecursive(const fs::path& dir,
                            std::vector<VstPluginInfo>& out) {
    std::error_code ec;
    if (!fs::is_directory(dir, ec)) return;
    try {
    for (const auto& e : fs::directory_iterator(dir, ec)) {
        if (ec) break;
        const auto p = e.path();
        const std::string name = p.filename().string();
        if (name.size() >= 5 &&
            name.compare(name.size() - 5, 5, ".vst3") == 0) {
            scanVst3Entries(pathToUtf8(p), out);
            continue;
        }
        if (fs::is_directory(p, ec))
            scanDirectoryRecursive(p, out);
    }
    } catch (...) {}
}

#endif // _WIN32

} // namespace

// ---------------------------------------------------------------------------
// VstPlugin::Impl
// ---------------------------------------------------------------------------

struct VstPlugin::Impl {
    // Keeps the plugin DLL loaded for the plugin's lifetime. Declared first
    // so it is destroyed last (after all plugin interfaces are released).
    std::shared_ptr<void> module;

    IComponent* component = nullptr;
    IAudioProcessor* processor = nullptr;
    IEditController* controller = nullptr;
    IPlugView* view = nullptr;
    VstComponentHandler* handler = nullptr;
    VstPlugFrame* frame = nullptr;
#ifdef _WIN32
    HWND editorWindow = nullptr;
#endif

    bool instrument = false;
    // Single-component plugins expose IEditController on the component
    // itself; then we must not initialize/terminate it separately.
    bool controllerIsComponent = false;
    uint32_t sampleRate = 48000;

    // The VST3 spec requires ProcessData to carry buffers for ALL buses,
    // not just active ones. Bus 0 points at our stereo buffers, the rest
    // are declared with their channel counts but null channel pointers.
    std::vector<AudioBusBuffers> inBuses, outBuses;

    // Scratch storage for secondary buses (sidechain ins, multi-outs).
    std::vector<std::vector<std::vector<float>>> extraIn, extraOut;
    std::vector<std::vector<float*>> extraInPtrs, extraOutPtrs;

    ProcessContext context{};
    int64_t samplePos = 0;
    int64_t continuousPos = 0;
    double tempo = 120.0;
    bool hostPlaying = false;
    std::atomic<bool> shuttingDown{false};

    BlockParamChanges paramChanges;
    BlockEventList eventList;

    std::mutex paramMutex;
    std::vector<PendingParam> pendingParams;
    // Last parameter touched by the user (editor performEdit / host UI),
    // consumed by the TWEAK mode poll. Guarded by paramMutex.
    uint32_t lastUserEditId = 0;
    bool hasUserEdit = false;
    // Automation values applied on the audio thread, waiting to be
    // mirrored into the edit controller from the UI thread (deduped by id).
    std::vector<PendingParam> controllerPending;

    std::mutex eventMutex;
    std::vector<VstHostEvent> pendingEvents;

    // Serializes IAudioProcessor::process vs IEditController access
    // (VST3 is not safe concurrently; pool workers must not race the UI).
    mutable std::mutex processMutex_;

    std::set<int> activeNotes;
    int editorW = 0, editorH = 0;
    bool editorOpen = false;
    // Cached hasEditor() probe: creating a plugin view just to test for
    // an editor makes some plugins flash tiny top-level windows, so the
    // probe runs at most once per plugin instance. -1 = not probed yet.
    int hasEditorCache = -1;
    float contentScale = 1.0f;

    // Cached plugin latency for delay compensation. Refreshed on the
    // audio thread when latencyDirty is raised by restartComponent.
    std::atomic<uint32_t> latency{0};
    std::atomic<bool> latencyDirty{false};

    std::vector<ParameterInfo> paramInfos;
    std::vector<ParamID> paramIds;
    // Set by IComponentHandler::restartComponent (preset switch, plugin
    // GUI restructuring, ...): the cached parameter list/values may be
    // stale and should be re-read on the UI thread.
    std::atomic<bool> paramsDirty{false};
};

VstPlugin::VstPlugin() = default;

tresult VstComponentHandler::performEdit(ParamID id,
                                           ParamValue valueNormalized) {
    VstPlugin* owner = nullptr;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        owner = owner_;
    }
    if (owner) owner->hostPerformEdit(id, valueNormalized);
    return kResultOk;
}

tresult VstComponentHandler::restartComponent(int32 flags) {
    VstPlugin* owner = nullptr;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        owner = owner_;
    }
    if (owner) owner->hostRestartComponent(int32_t(flags));
    return kResultOk;
}

// ---------------------------------------------------------------------------
// Public scan API
// ---------------------------------------------------------------------------

std::vector<VstPluginInfo> scanVstDirectory(const std::string& dir) {
#ifdef _WIN32
    std::vector<VstPluginInfo> out;
    scanDirectoryRecursive(fs::u8path(dir), out);
    return out;
#else
    (void)dir;
    return {};
#endif
}

std::vector<VstPluginInfo> scanVstPlugins(
    const std::vector<std::string>& dirs) {
    std::vector<VstPluginInfo> out;
    for (const auto& d : dirs) {
        auto part = scanVstDirectory(d);
        out.insert(out.end(), part.begin(), part.end());
    }
    return out;
}

// ---------------------------------------------------------------------------
// VstPlugin lifecycle
// ---------------------------------------------------------------------------

std::shared_ptr<VstPlugin> createVstPlugin(const std::string& path,
                                             const std::string& uid,
                                             bool instrument,
                                             uint32_t sampleRate) {
#ifdef _WIN32
    auto plug = std::shared_ptr<VstPlugin>(new VstPlugin());
    if (!plug->init(path, uid, instrument, sampleRate))
        return nullptr;
    return plug;
#else
    (void)path; (void)uid; (void)instrument; (void)sampleRate;
    return nullptr;
#endif
}

#ifdef _WIN32

bool VstPlugin::init(const std::string& path, const std::string& uid,
                     bool instrument, uint32_t sampleRate) {
    impl_ = std::make_unique<Impl>();
    impl_->instrument = instrument;
    impl_->sampleRate = sampleRate;

    TUID cid{};
    if (!hexToTuid(uid, cid)) {
        VST_LOG("init: bad uid '%s'", uid.c_str());
        return false;
    }

    auto mod = getModule(path);
    if (!mod) {
        VST_LOG("init: module load failed '%s'", path.c_str());
        return false;
    }
    impl_->module = mod; // pin the DLL in memory while the plugin lives
    VST_LOG("init: module loaded '%s'", path.c_str());

    void* obj = nullptr;
    const tresult cr = mod->factory()->createInstance(cid, IComponent::iid,
                                                      &obj);
    if (cr != kResultOk || !obj) {
        VST_LOG("init: createInstance failed (%d) '%s'", int(cr), path.c_str());
        return false;
    }
    impl_->component = static_cast<IComponent*>(obj);
    VST_LOG("init: component created");

    // Per VST3 spec setIoMode must be called before initialize.
    if (instrument)
        impl_->component->setIoMode(kSimple);

    if (impl_->component->initialize(&gHostApp) != kResultOk) {
        VST_LOG("init: component initialize failed '%s'", path.c_str());
        shutdown();
        return false;
    }
    VST_LOG("init: component initialized");

    if (impl_->component->queryInterface(IAudioProcessor::iid,
            reinterpret_cast<void**>(&impl_->processor)) != kResultOk
        || !impl_->processor) {
        VST_LOG("init: no IAudioProcessor '%s'", path.c_str());
        shutdown();
        return false;
    }

    TUID ctrlCid{};
    if (impl_->component->getControllerClassId(ctrlCid) == kResultOk) {
        void* ctrlObj = nullptr;
        if (mod->factory()->createInstance(ctrlCid, IEditController::iid,
                                           &ctrlObj) == kResultOk && ctrlObj) {
            impl_->controller = static_cast<IEditController*>(ctrlObj);
            impl_->controller->initialize(&gHostApp);
            VST_LOG("init: separate controller initialized");
        }
    }
    if (!impl_->controller) {
        // Single-component plugin: controller lives on the component.
        void* ctrlObj = nullptr;
        if (impl_->component->queryInterface(
                IEditController::iid,
                reinterpret_cast<void**>(&ctrlObj)) == kResultOk && ctrlObj) {
            impl_->controller = static_cast<IEditController*>(ctrlObj);
            impl_->controllerIsComponent = true;
        }
    }
    if (impl_->controller) {
        impl_->handler = new VstComponentHandler(this);
        impl_->controller->setComponentHandler(impl_->handler);

        if (!impl_->controllerIsComponent) {
            IConnectionPoint* compCp = nullptr;
            IConnectionPoint* ctrlCp = nullptr;
            if (impl_->component->queryInterface(
                    IConnectionPoint::iid,
                    reinterpret_cast<void**>(&compCp)) == kResultOk
                && impl_->controller->queryInterface(
                       IConnectionPoint::iid,
                       reinterpret_cast<void**>(&ctrlCp)) == kResultOk) {
                compCp->connect(ctrlCp);
                ctrlCp->connect(compCp);
                compCp->release();
                ctrlCp->release();
                VST_LOG("init: connection points linked");
            }

            MemoryStream state;
            if (impl_->component->getState(&state) == kResultOk) {
                state.resetRead();
                impl_->controller->setComponentState(&state);
                VST_LOG("init: component state -> controller");
            }
        }
    } else {
        VST_LOG("init: no IEditController '%s'", path.c_str());
    }

    ProcessSetup setup{};
    setup.processMode = kRealtime;
    setup.symbolicSampleSize = kSample32;
    setup.maxSamplesPerBlock = kMaxBlock;
    setup.sampleRate = sampleRate;
    if (impl_->processor->setupProcessing(setup) != kResultOk) {
        VST_LOG("init: setupProcessing failed '%s'", path.c_str());
        shutdown();
        return false;
    }
    impl_->latency.store(impl_->processor->getLatencySamples(),
                         std::memory_order_relaxed);
    VST_LOG("init: setupProcessing ok (sr=%u, latency=%u)", sampleRate,
            impl_->latency.load(std::memory_order_relaxed));

    // Bus configuration. ProcessData must describe ALL declared audio buses
    // (plugins index into inputs[]/outputs[] up to their own bus count), so
    // allocate buffers for every bus even if only the main one is active.
    const int32 numInBuses = impl_->component->getBusCount(kAudio, kInput);
    const int32 numOutBuses = impl_->component->getBusCount(kAudio, kOutput);
    VST_LOG("init: buses in=%d out=%d", int(numInBuses), int(numOutBuses));

    // Try to request stereo on the main buses; ignore failure (plugins keep
    // their default arrangement, which we query below via getBusInfo).
    {
        SpeakerArrangement stereo = SpeakerArr::kStereo;
        std::vector<SpeakerArrangement> ins(size_t(std::max(numInBuses, 1)),
                                            stereo);
        std::vector<SpeakerArrangement> outs(size_t(std::max(numOutBuses, 1)),
                                             stereo);
        impl_->processor->setBusArrangements(
            numInBuses > 0 ? ins.data() : nullptr, numInBuses,
            numOutBuses > 0 ? outs.data() : nullptr, numOutBuses);
    }

    auto setupBuses = [&](BusDirection dir, int32 count,
                          std::vector<AudioBusBuffers>& buses,
                          std::vector<std::vector<std::vector<float>>>& store,
                          std::vector<std::vector<float*>>& ptrs) {
        buses.resize(size_t(count));
        store.resize(size_t(count));
        ptrs.resize(size_t(count));
        for (int32 b = 0; b < count; ++b) {
            BusInfo bi{};
            int32 channels = 2;
            if (impl_->component->getBusInfo(kAudio, dir, b, bi) == kResultOk)
                channels = std::max<int32>(bi.channelCount, 0);
            store[size_t(b)].assign(size_t(channels),
                                    std::vector<float>(kMaxBlock, 0.f));
            ptrs[size_t(b)].resize(size_t(channels));
            for (int32 ch = 0; ch < channels; ++ch)
                ptrs[size_t(b)][size_t(ch)] = store[size_t(b)][size_t(ch)].data();
            buses[size_t(b)].numChannels = channels;
            buses[size_t(b)].channelBuffers32 =
                channels > 0 ? ptrs[size_t(b)].data() : nullptr;
            buses[size_t(b)].silenceFlags = 0;
        }
    };
    setupBuses(kInput, numInBuses, impl_->inBuses, impl_->extraIn,
               impl_->extraInPtrs);
    setupBuses(kOutput, numOutBuses, impl_->outBuses, impl_->extraOut,
               impl_->extraOutPtrs);

    // Activate every audio bus (simplest safe choice) + event input.
    for (int32 b = 0; b < numInBuses; ++b)
        impl_->component->activateBus(kAudio, kInput, b, static_cast<TBool>(true));
    for (int32 b = 0; b < numOutBuses; ++b)
        impl_->component->activateBus(kAudio, kOutput, b, static_cast<TBool>(true));
    if (impl_->component->getBusCount(kEvent, kInput) > 0)
        impl_->component->activateBus(kEvent, kInput, 0, static_cast<TBool>(true));

    impl_->context.sampleRate = double(sampleRate);

    if (impl_->component->setActive(static_cast<TBool>(true)) != kResultOk) {
        VST_LOG("init: setActive failed '%s'", path.c_str());
        shutdown();
        return false;
    }
    VST_LOG("init: setActive ok");
    // Some plugins return kNotImplemented here yet still process fine;
    // only a hard failure is treated as fatal.
    impl_->processor->setProcessing(static_cast<TBool>(true));

    // Cache parameter info.
    if (impl_->controller) {
        const int32 n = impl_->controller->getParameterCount();
        impl_->paramInfos.resize(size_t(n));
        impl_->paramIds.resize(size_t(n));
        for (int32 i = 0; i < n; ++i) {
            ParameterInfo pi{};
            if (impl_->controller->getParameterInfo(i, pi) == kResultOk) {
                impl_->paramInfos[size_t(i)] = pi;
                impl_->paramIds[size_t(i)] = pi.id;
            }
        }
    }

    instrument_ = instrument;
    displayName_.clear();
    if (IPluginFactory2* factory2 = nullptr;
        mod->factory()->queryInterface(IPluginFactory2::iid,
            reinterpret_cast<void**>(&factory2)) == kResultOk && factory2) {
        const int32 n = factory2->countClasses();
        for (int32 i = 0; i < n; ++i) {
            PClassInfo2 ci{};
            if (factory2->getClassInfo2(i, &ci) == kResultOk
                    && std::memcmp(ci.cid, cid, sizeof(TUID)) == 0) {
                displayName_ = sanitizeUtf8(ci.name ? ci.name : "");
                break;
            }
        }
        factory2->release();
    }
    if (displayName_.empty()) {
        displayName_ = mod->bundlePath();
        const auto slash = displayName_.find_last_of("/\\");
        if (slash != std::string::npos)
            displayName_ = displayName_.substr(slash + 1);
    }

    VST_LOG("init: done '%s' (params=%d)", displayName_.c_str(),
            int(impl_->paramIds.size()));
    return true;
}

#endif // _WIN32

void VstPlugin::shutdown() {
    if (!impl_) return;
    impl_->shuttingDown.store(true, std::memory_order_release);

    closeEditor();

    if (impl_->processor)
        impl_->processor->setProcessing(static_cast<TBool>(false));
    if (impl_->component)
        impl_->component->setActive(static_cast<TBool>(false));

    if (impl_->handler)
        impl_->handler->detach();

    // Controller first (it may reference the component via connection points).
    if (impl_->controller) {
        impl_->controller->setComponentHandler(nullptr);
        if (!impl_->controllerIsComponent)
            impl_->controller->terminate();
        impl_->controller->release();
        impl_->controller = nullptr;
    }
    if (impl_->processor) {
        impl_->processor->release();
        impl_->processor = nullptr;
    }
    if (impl_->component) {
        impl_->component->terminate();
        impl_->component->release();
        impl_->component = nullptr;
    }
    if (impl_->handler) {
        impl_->handler->release();
        impl_->handler = nullptr;
    }
    if (impl_->frame) {
        impl_->frame->release();
        impl_->frame = nullptr;
    }
}

VstPlugin::~VstPlugin() { shutdown(); }

void VstPlugin::prepare(uint32_t sampleRate) {
    if (!impl_) return;
    impl_->sampleRate = sampleRate;
#ifdef _WIN32
    if (impl_->processor) {
        impl_->processor->setProcessing(static_cast<TBool>(false));
        ProcessSetup setup{};
        setup.processMode = kRealtime;
        setup.symbolicSampleSize = kSample32;
        setup.maxSamplesPerBlock = kMaxBlock;
        setup.sampleRate = sampleRate;
        impl_->processor->setupProcessing(setup);
        impl_->latency.store(impl_->processor->getLatencySamples(),
                             std::memory_order_relaxed);
        impl_->processor->setProcessing(static_cast<TBool>(true));
    }
#endif
}

uint32_t VstPlugin::latencySamples() const {
    return impl_ ? impl_->latency.load(std::memory_order_relaxed) : 0;
}

const char* VstPlugin::name() const { return displayName_.c_str(); }

void VstPlugin::setParam(int index, float normalized) {
    setParamNormalizedByIndex(index, normalized);
}

static Event makeNoteOn(int pitch, float vel, int32_t offset) {
    Event e{};
    e.type = Event::kNoteOnEvent;
    e.sampleOffset = offset;
    e.noteOn.channel = 0;
    e.noteOn.pitch = static_cast<int16>(pitch);
    e.noteOn.velocity = vel;
    e.noteOn.length = 0;
    e.noteOn.noteId = -1;
    return e;
}

static Event makeNoteOff(int pitch, int32_t offset) {
    Event e{};
    e.type = Event::kNoteOffEvent;
    e.sampleOffset = offset;
    e.noteOff.channel = 0;
    e.noteOff.pitch = static_cast<int16>(pitch);
    e.noteOff.velocity = 0.f;
    e.noteOff.noteId = -1;
    return e;
}

void VstPlugin::queuePreviewNoteOn(int pitch, float velocity) {
    if (!impl_) return;
    std::lock_guard<std::mutex> lock(impl_->eventMutex);
    impl_->pendingEvents.push_back(
        {VstHostEvent::NoteOn, pitch, velocity, 0});
}

void VstPlugin::queuePreviewNoteOff(int pitch) {
    if (!impl_) return;
    std::lock_guard<std::mutex> lock(impl_->eventMutex);
    impl_->pendingEvents.push_back(
        {VstHostEvent::NoteOff, pitch, 0.f, 0});
}

void VstPlugin::allNotesOff() {
    if (!impl_) return;
    std::lock_guard<std::mutex> lock(impl_->eventMutex);
    for (int p : impl_->activeNotes)
        impl_->pendingEvents.push_back(
            {VstHostEvent::NoteOff, p, 0.f, 0});
    impl_->activeNotes.clear();
}

void VstPlugin::hostPerformEdit(uint32_t paramId, double normalized) {
    if (!impl_) return;
    std::lock_guard<std::mutex> lock(impl_->paramMutex);
    impl_->pendingParams.push_back({paramId, normalized});
    impl_->lastUserEditId = paramId;
    impl_->hasUserEdit = true;
}

int VstPlugin::consumeLastUserEdit() {
    if (!impl_) return -1;
    uint32_t id = 0;
    {
        std::lock_guard<std::mutex> lock(impl_->paramMutex);
        if (!impl_->hasUserEdit) return -1;
        impl_->hasUserEdit = false;
        id = impl_->lastUserEditId;
    }
    for (size_t i = 0; i < impl_->paramIds.size(); ++i)
        if (uint32_t(impl_->paramIds[i]) == id) return int(i);
    return -1;
}

bool VstPlugin::isLoading() const { return false; }

void VstPlugin::hostRestartComponent(int32_t flags) {
    if (!impl_) return;
    // kReloadComponent (1<<0) and kParamTitlesChanged (1<<4) mean the
    // parameter list/labels may have changed (preset switch, dynamic
    // plugin GUIs). kParamValuesChanged (1<<2) is included so generic
    // panels re-read values after a full preset load.
    constexpr int32_t kInteresting = (1 << 0) | (1 << 2) | (1 << 4);
    if (flags & kInteresting)
        impl_->paramsDirty.store(true, std::memory_order_relaxed);
    // kLatencyChanged: re-read on the audio thread before the next block.
    if (flags & (1 << 1))
        impl_->latencyDirty.store(true, std::memory_order_relaxed);
}

bool VstPlugin::refreshParamsIfChanged() {
    if (!impl_ || !impl_->controller) return false;
    if (!impl_->paramsDirty.exchange(false, std::memory_order_relaxed))
        return false;
    std::lock_guard<std::mutex> procLock(impl_->processMutex_);
    const int32 n = impl_->controller->getParameterCount();
    std::vector<ParameterInfo> infos(static_cast<size_t>(n));
    std::vector<ParamID> ids(static_cast<size_t>(n));
    for (int32 i = 0; i < n; ++i) {
        ParameterInfo pi{};
        if (impl_->controller->getParameterInfo(i, pi) == kResultOk) {
            infos[size_t(i)] = pi;
            ids[size_t(i)] = pi.id;
        }
    }
    // Swap under paramMutex: the audio thread resolves lane indices
    // through paramIds (setParamFromAudioThread).
    std::lock_guard<std::mutex> lock(impl_->paramMutex);
    impl_->paramInfos.swap(infos);
    impl_->paramIds.swap(ids);
    return true;
}

void VstPlugin::onTransportJump() { allNotesOff(); }

void VstPlugin::setTransport(double bpm, bool playing,
                            int64_t projectSamplePos) {
    if (!impl_) return;
    const int64_t newPos = std::max<int64_t>(0, projectSamplePos);
    impl_->tempo = bpm > 0.0 ? bpm : 120.0;
    impl_->hostPlaying = playing;
#ifdef _WIN32
    // Timeline moved backward while playing (loop wrap / seek): flush the
    // plugin DSP state. continuousPos stays monotonic; only
    // projectTimeSamples jumps. When stopped, just move the position:
    // flushing here would cut preview-note tails.
    if (playing && impl_->processor && newPos < impl_->samplePos) {
        impl_->processor->setProcessing(static_cast<TBool>(false));
        impl_->samplePos = newPos;
        impl_->processor->setProcessing(static_cast<TBool>(true));
        return;
    }
#endif
    impl_->samplePos = newPos;
}

void VstPlugin::process(float* interleaved, uint32_t frames) {
    processWithEvents(interleaved, frames, {});
}

void VstPlugin::processWithEvents(float* interleaved, uint32_t frames,
                                  const std::vector<VstHostEvent>& events) {
    if (!impl_ || impl_->shuttingDown.load(std::memory_order_acquire) ||
        !impl_->processor) {
        std::memset(interleaved, 0, sizeof(float) * frames * 2);
        return;
    }
    uint32_t done = 0;
    while (done < frames) {
        const uint32_t chunk = std::min(frames - done, kMaxBlock);
        const std::vector<VstHostEvent>* ev = (done == 0) ? &events : nullptr;
        processBlock(interleaved + size_t(done) * 2, chunk, ev);
        done += chunk;
    }
}

void VstPlugin::processBlock(float* interleaved, uint32_t frames,
                             const std::vector<VstHostEvent>* clipEvents) {
#ifdef _WIN32
    if (!impl_ || impl_->shuttingDown.load(std::memory_order_acquire) ||
        !impl_->processor) {
        std::memset(interleaved, 0, sizeof(float) * frames * 2);
        return;
    }

    std::lock_guard<std::mutex> procLock(impl_->processMutex_);

    if (impl_->latencyDirty.exchange(false, std::memory_order_relaxed))
        impl_->latency.store(impl_->processor->getLatencySamples(),
                             std::memory_order_relaxed);

    // Deinterleave input into the main input bus (effects). Secondary
    // buses (sidechains) stay silent. Mono main input gets an L+R mix.
    if (!impl_->inBuses.empty()) {
        auto& main = impl_->extraIn[0];
        if (main.size() >= 2) {
            for (uint32_t i = 0; i < frames; ++i) {
                main[0][i] = interleaved[i * 2];
                main[1][i] = interleaved[i * 2 + 1];
            }
        } else if (main.size() == 1) {
            for (uint32_t i = 0; i < frames; ++i)
                main[0][i] = 0.5f * (interleaved[i * 2] +
                                     interleaved[i * 2 + 1]);
        }
    }
    if (!impl_->outBuses.empty()) {
        for (auto& ch : impl_->extraOut[0])
            std::fill(ch.begin(), ch.begin() + frames, 0.f);
    }

    impl_->paramChanges.clear();
    impl_->eventList.clear();

    if (impl_->paramMutex.try_lock()) {
        for (const auto& p : impl_->pendingParams)
            impl_->paramChanges.addParam(p.id, 0, p.value);
        impl_->pendingParams.clear();
        impl_->paramMutex.unlock();
    }

    auto addHostEvent = [&](const VstHostEvent& he) {
        if (he.type == VstHostEvent::NoteOn) {
            impl_->eventList.append(
                makeNoteOn(he.pitch, he.velocity, he.sampleOffset));
            impl_->activeNotes.insert(he.pitch);
        } else {
            impl_->eventList.append(
                makeNoteOff(he.pitch, he.sampleOffset));
            impl_->activeNotes.erase(he.pitch);
        }
    };

    // Pending events first: transport-jump / stop note-offs must precede
    // the note-ons of this block, otherwise a chased note that restarts
    // at offset 0 would be killed by its own stale note-off.
    {
        std::lock_guard<std::mutex> evLock(impl_->eventMutex);
        for (const auto& he : impl_->pendingEvents)
            addHostEvent(he);
        impl_->pendingEvents.clear();
    }

    if (clipEvents) {
        for (const auto& he : *clipEvents)
            addHostEvent(he);
    }

    impl_->context.projectTimeSamples = impl_->samplePos;
    impl_->context.continousTimeSamples = impl_->continuousPos;
    impl_->context.tempo = impl_->tempo;
    impl_->context.projectTimeMusic =
        double(impl_->samplePos) / double(impl_->sampleRate)
        * impl_->tempo / 60.0;
    impl_->context.state = ProcessContext::kTempoValid
        | ProcessContext::kProjectTimeMusicValid
        | ProcessContext::kContTimeValid
        | (impl_->hostPlaying ? uint32(ProcessContext::kPlaying) : 0u);

    ProcessData data{};
    data.processMode = kRealtime;
    data.symbolicSampleSize = kSample32;
    data.numSamples = int32(frames);
    data.inputParameterChanges = &impl_->paramChanges;
    data.processContext = &impl_->context;
    data.numInputs = int32(impl_->inBuses.size());
    data.inputs = impl_->inBuses.empty() ? nullptr : impl_->inBuses.data();
    data.numOutputs = int32(impl_->outBuses.size());
    data.outputs = impl_->outBuses.empty() ? nullptr : impl_->outBuses.data();
    if (impl_->instrument)
        data.inputEvents = &impl_->eventList;

    impl_->processor->process(data);
    impl_->samplePos += frames;
    impl_->continuousPos += int64_t(frames);

    // Re-interleave from the main output bus (mono output duplicated).
    if (!impl_->outBuses.empty() && !impl_->extraOut[0].empty()) {
        const auto& main = impl_->extraOut[0];
        const auto& l = main[0];
        const auto& r = main.size() >= 2 ? main[1] : main[0];
        for (uint32_t i = 0; i < frames; ++i) {
            interleaved[i * 2] = l[i];
            interleaved[i * 2 + 1] = r[i];
        }
    } else {
        std::memset(interleaved, 0, sizeof(float) * frames * 2);
    }
#else
    (void)interleaved; (void)frames; (void)clipEvents;
#endif
}

int VstPlugin::paramCount() const {
    return impl_ ? int(impl_->paramIds.size()) : 0;
}

std::tuple<int, std::string, float> VstPlugin::getParamInfo(int index) const {
    if (!impl_ || index < 0 || index >= int(impl_->paramInfos.size()))
        return {0, "", 0.f};
    const auto& pi = impl_->paramInfos[size_t(index)];
    float v = 0.f;
    if (impl_->controller)
        v = float(impl_->controller->getParamNormalized(pi.id));
    return {int(pi.id), string128ToUtf8(pi.title), v};
}

float VstPlugin::getParamDefault(int index) const {
    if (!impl_ || index < 0 || index >= int(impl_->paramInfos.size()))
        return 0.f;
    return float(impl_->paramInfos[size_t(index)].defaultNormalizedValue);
}

float VstPlugin::getParamNormalized(int index) const {
    if (!impl_ || !impl_->controller || index < 0
        || index >= int(impl_->paramIds.size()))
        return 0.f;
    std::lock_guard<std::mutex> procLock(impl_->processMutex_);
    return float(impl_->controller->getParamNormalized(
        impl_->paramIds[size_t(index)]));
}

void VstPlugin::setParamNormalizedByIndex(int index, float normalized,
                                          bool markUserEdit) {
    if (!impl_ || !impl_->controller || index < 0
        || index >= int(impl_->paramIds.size()))
        return;
    const ParamID id = impl_->paramIds[size_t(index)];
    {
        std::lock_guard<std::mutex> procLock(impl_->processMutex_);
        impl_->controller->setParamNormalized(id, normalized);
    }
    {
        std::lock_guard<std::mutex> lock(impl_->paramMutex);
        impl_->pendingParams.push_back({id, normalized});
        if (markUserEdit) {
            // Generic host sliders count as user tweaks too (TWEAK mode);
            // undo/redo and other programmatic writes must not.
            impl_->lastUserEditId = uint32_t(id);
            impl_->hasUserEdit = true;
        }
    }
}

void VstPlugin::setParamFromAudioThread(int index, float normalized) {
    if (!impl_ || index < 0) return;
    normalized = std::max(0.f, std::min(1.f, normalized));
    // paramIds may be swapped by refreshParamsIfChanged (UI thread), so
    // both the bounds check and the lookup happen under paramMutex.
    // Never block the render pool: if the UI holds the lock (editor /
    // syncControllers), skip this block's automation sample.
    if (!impl_->paramMutex.try_lock()) return;
    if (index >= int(impl_->paramIds.size())) {
        impl_->paramMutex.unlock();
        return;
    }
    const ParamID id = impl_->paramIds[size_t(index)];
    impl_->pendingParams.push_back({id, normalized});
    for (auto& cp : impl_->controllerPending) {
        if (cp.id == id) {
            cp.value = normalized;
            impl_->paramMutex.unlock();
            return;
        }
    }
    impl_->controllerPending.push_back({id, normalized});
    impl_->paramMutex.unlock();
}

void VstPlugin::syncControllerParams() {
    if (!impl_ || !impl_->controller) return;
    std::vector<PendingParam> tmp;
    {
        std::lock_guard<std::mutex> lock(impl_->paramMutex);
        if (impl_->controllerPending.empty()) return;
        tmp.swap(impl_->controllerPending);
    }
    std::lock_guard<std::mutex> procLock(impl_->processMutex_);
    for (const auto& p : tmp)
        impl_->controller->setParamNormalized(p.id, p.value);
}

std::string VstPlugin::getState() const {
#ifdef _WIN32
    if (!impl_ || !impl_->component) return {};
    MemoryStream stream;
    if (impl_->component->getState(&stream) != kResultOk) return {};
    return stream.takeData();
#else
    return {};
#endif
}

bool VstPlugin::setState(const std::string& data) {
#ifdef _WIN32
    if (!impl_ || !impl_->component) return false;
    MemoryStream stream(data);
    if (impl_->component->setState(&stream) != kResultOk) return false;
    if (impl_->controller) {
        stream.resetRead();
        impl_->controller->setComponentState(&stream);
    }
    return true;
#else
    (void)data;
    return false;
#endif
}

bool VstPlugin::hasEditor() const {
#ifdef _WIN32
    if (!impl_ || !impl_->controller) return false;
    if (impl_->editorOpen) return true;
    if (impl_->hasEditorCache >= 0) return impl_->hasEditorCache != 0;
    IPlugView* v = impl_->controller->createView(ViewType::kEditor);
    if (v) v->release();
    impl_->hasEditorCache = v ? 1 : 0;
    return v != nullptr;
#else
    return false;
#endif
}

bool VstPlugin::openEditor(void* hwnd) {
#ifdef _WIN32
    if (!impl_ || !impl_->controller || impl_->editorOpen) return false;
    std::lock_guard<std::mutex> procLock(impl_->processMutex_);
    VST_LOG("editor: createView '%s'", displayName_.c_str());
    impl_->view = impl_->controller->createView(ViewType::kEditor);
    if (!impl_->view) {
        VST_LOG("editor: createView returned null");
        return false;
    }
    if (impl_->view->isPlatformTypeSupported(kPlatformTypeHWND) != kResultTrue) {
        VST_LOG("editor: HWND platform type not supported");
        impl_->view->release();
        impl_->view = nullptr;
        return false;
    }
    if (!impl_->frame) impl_->frame = new VstPlugFrame();
    // Frame must be set before attaching (plugins may resize during attach).
    impl_->view->setFrame(impl_->frame);
    // Re-apply the user zoom factor before attach so the plugin lays the
    // view out at the right scale from the start.
    if (impl_->contentScale != 1.0f) {
        IPlugViewContentScaleSupport* scaleSupport = nullptr;
        if (impl_->view->queryInterface(
                IPlugViewContentScaleSupport::iid,
                reinterpret_cast<void**>(&scaleSupport)) == kResultOk
            && scaleSupport) {
            scaleSupport->setContentScaleFactor(impl_->contentScale);
            scaleSupport->release();
        }
    }
    ViewRect rect{};
    if (impl_->view->getSize(&rect) == kResultOk) {
        impl_->editorW = rect.getWidth();
        impl_->editorH = rect.getHeight();
    }
    VST_LOG("editor: attaching (%dx%d)", impl_->editorW, impl_->editorH);
    if (impl_->view->attached(reinterpret_cast<void*>(hwnd), kPlatformTypeHWND)
        != kResultOk) {
        VST_LOG("editor: attached() failed");
        impl_->view->setFrame(nullptr);
        impl_->view->release();
        impl_->view = nullptr;
        return false;
    }
    if (impl_->view->getSize(&rect) == kResultOk) {
        impl_->editorW = rect.getWidth();
        impl_->editorH = rect.getHeight();
    }
    // Cross-process attach: read the actual child HWND size. Some plugins
    // (Diva, Kontakt, ...) report getSize() in a different space than the
    // HWND they create when parent/child DPI awareness differs.
    {
        const HWND parent = reinterpret_cast<HWND>(hwnd);
        const HWND child = GetWindow(parent, GW_CHILD);
        if (child) {
            RECT cr{};
            if (GetClientRect(child, &cr)) {
                const int cw = cr.right - cr.left;
                const int ch = cr.bottom - cr.top;
                if (cw > 0 && ch > 0) {
                    impl_->editorW = cw;
                    impl_->editorH = ch;
                }
            }
        }
    }
    impl_->editorOpen = true;
    VST_LOG("editor: attached ok (%dx%d)", impl_->editorW, impl_->editorH);
    return true;
#else
    (void)hwnd;
    return false;
#endif
}

bool VstPlugin::openEditorTopLevel(void* ownerHwnd, const std::string& title) {
#ifdef _WIN32
    if (!impl_ || !impl_->controller) return false;
    if (impl_->editorOpen && impl_->editorWindow && IsWindow(impl_->editorWindow)) {
        ShowWindow(impl_->editorWindow, SW_SHOWNORMAL);
        BringWindowToTop(impl_->editorWindow);
        SetForegroundWindow(impl_->editorWindow);
        return true;
    }
    if (impl_->editorOpen) return true;

    static const wchar_t kClassName[] = L"PyDAWVstEditorHost";
    static std::once_flag registerOnce;
    std::call_once(registerOnce, [] {
        WNDCLASSW wc{};
        wc.lpfnWndProc = vstEditorWndProc;
        wc.hInstance = GetModuleHandleW(nullptr);
        wc.lpszClassName = kClassName;
        wc.hCursor = LoadCursorW(nullptr, MAKEINTRESOURCEW(32512));
        RegisterClassW(&wc);
    });

    const DWORD style = WS_OVERLAPPEDWINDOW | WS_CLIPCHILDREN | WS_CLIPSIBLINGS;
    const HWND owner = editorOwnerWindow(ownerHwnd);
    const auto windowTitle = editorWindowTitle(title, displayName_);
    HWND wnd = CreateWindowExW(
        0, kClassName, windowTitle.c_str(), style,
        CW_USEDEFAULT, CW_USEDEFAULT, 900, 650,
        owner, nullptr, GetModuleHandleW(nullptr), nullptr);
    if (!wnd && owner) {
        wnd = CreateWindowExW(
            0, kClassName, windowTitle.c_str(), style,
            CW_USEDEFAULT, CW_USEDEFAULT, 900, 650,
            nullptr, nullptr, GetModuleHandleW(nullptr), nullptr);
    }
    if (!wnd) return false;
    SetWindowLongPtrW(wnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(this));

    if (!openEditor(reinterpret_cast<void*>(wnd))) {
        SetWindowLongPtrW(wnd, GWLP_USERDATA, 0);
        DestroyWindow(wnd);
        return false;
    }
    if (impl_->editorW > 0 && impl_->editorH > 0) {
        RECT wr{0, 0, impl_->editorW, impl_->editorH};
        AdjustWindowRectEx(&wr, style, FALSE, 0);
        SetWindowPos(wnd, nullptr, 0, 0, wr.right - wr.left, wr.bottom - wr.top,
                     SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE);
    }
    impl_->editorWindow = wnd;
    ShowWindow(wnd, SW_SHOWNORMAL);
    BringWindowToTop(wnd);
    SetForegroundWindow(wnd);
    UpdateWindow(wnd);
    return true;
#else
    (void)ownerHwnd;
    (void)title;
    return false;
#endif
}

void VstPlugin::closeEditor() {
#ifdef _WIN32
    if (!impl_) return;
    std::lock_guard<std::mutex> procLock(impl_->processMutex_);
    if (impl_->view) {
        impl_->view->removed();
        impl_->view->setFrame(nullptr);
        impl_->view->release();
        impl_->view = nullptr;
    }
    impl_->editorOpen = false;
    if (impl_->editorWindow) {
        HWND wnd = impl_->editorWindow;
        impl_->editorWindow = nullptr;
        SetWindowLongPtrW(wnd, GWLP_USERDATA, 0);
        DestroyWindow(wnd);
    }
#endif
}

std::pair<int, int> VstPlugin::editorSize() const {
    if (!impl_) return {0, 0};
    return {impl_->editorW, impl_->editorH};
}

bool VstPlugin::checkSizeChanged(int& w, int& h) {
#ifdef _WIN32
    if (!impl_ || !impl_->frame) return false;
    if (impl_->frame->checkSizeChanged(w, h)) {
        impl_->editorW = w;
        impl_->editorH = h;
        return true;
    }
#endif
    return false;
}

bool VstPlugin::editorCanResize() const {
#ifdef _WIN32
    if (!impl_ || !impl_->view || !impl_->editorOpen) return false;
    return impl_->view->canResize() == kResultTrue;
#else
    return false;
#endif
}

bool VstPlugin::resizeEditor(int w, int h) {
#ifdef _WIN32
    if (!impl_ || !impl_->view || !impl_->editorOpen) return false;
    if (w <= 0 || h <= 0) return false;
    ViewRect rect(0, 0, w, h);
    if (impl_->view->checkSizeConstraint(&rect) != kResultOk)
        return false;
    if (impl_->view->onSize(&rect) != kResultOk)
        return false;
    impl_->editorW = rect.getWidth();
    impl_->editorH = rect.getHeight();
    return true;
#else
    (void)w;
    (void)h;
    return false;
#endif
}

std::string VstPlugin::diagnostic() const {
    return {};
}

bool VstPlugin::setContentScale(float scale) {
#ifdef _WIN32
    if (!impl_ || scale <= 0.f) return false;
    impl_->contentScale = scale;
    if (!impl_->view) return true; // stored; applied on next openEditor
    IPlugViewContentScaleSupport* scaleSupport = nullptr;
    if (impl_->view->queryInterface(
            IPlugViewContentScaleSupport::iid,
            reinterpret_cast<void**>(&scaleSupport)) != kResultOk
        || !scaleSupport) {
        VST_LOG("editor: no IPlugViewContentScaleSupport '%s'",
                displayName_.c_str());
        return false;
    }
    const tresult res = scaleSupport->setContentScaleFactor(scale);
    scaleSupport->release();
    if (res != kResultTrue) {
        VST_LOG("editor: setContentScaleFactor(%.2f) rejected '%s'",
                double(scale), displayName_.c_str());
        return false;
    }
    // The plugin adjusts its size and reports it back via getSize
    // (and/or IPlugFrame::resizeView, which the poll loop picks up).
    ViewRect rect{};
    if (impl_->editorOpen && impl_->view->getSize(&rect) == kResultOk) {
        impl_->editorW = rect.getWidth();
        impl_->editorH = rect.getHeight();
    }
    VST_LOG("editor: content scale %.2f applied (%dx%d) '%s'",
            double(scale), impl_->editorW, impl_->editorH,
            displayName_.c_str());
    return true;
#else
    (void)scale;
    return false;
#endif
}

#ifndef _WIN32

bool VstPlugin::init(const std::string&, const std::string&, bool, uint32_t) {
    return false;
}

void VstPlugin::processBlock(float*, uint32_t,
                             const std::vector<VstHostEvent>*) {}

#endif
