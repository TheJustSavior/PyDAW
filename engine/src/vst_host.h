#pragma once

#include "mixer.h"

#include <cstdint>
#include <memory>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

struct VstPluginInfo {
    std::string path;
    std::string name;
    std::string vendor;
    std::string category;
    std::string subCategories;
    std::string uid;
    bool isInstrument = false;
};

class VstPlugin;

// VstHostEvent lives in mixer.h (Track owns a reusable scratch buffer).

std::vector<VstPluginInfo> scanVstDirectory(const std::string& dir);
std::vector<VstPluginInfo> scanVstPlugins(const std::vector<std::string>& dirs);

std::shared_ptr<VstPlugin> createVstPlugin(const std::string& path,
                                           const std::string& uid,
                                           bool instrument,
                                           uint32_t sampleRate);

// VST3 plugin hosted as EffectSlot (insert fx or instrument generator).
class VstPlugin : public EffectSlot {
public:
    ~VstPlugin() override;

    void process(float* interleaved, uint32_t frames) override;
    virtual void processWithEvents(float* interleaved, uint32_t frames,
                                   const std::vector<VstHostEvent>& events);

    void setParam(int index, float normalized) override;
    const char* name() const override;
    void prepare(uint32_t sampleRate) override;
    // Plugin-reported processing latency (cached; refreshed when the
    // plugin signals kLatencyChanged via restartComponent).
    uint32_t latencySamples() const override;

    virtual bool isInstrument() const { return instrument_; }

    // Parameter introspection (UI thread).
    virtual int paramCount() const;
    virtual std::tuple<int, std::string, float> getParamInfo(int index) const;
    // Plugin-declared default (normalized 0..1) for "reset to default".
    virtual float getParamDefault(int index) const;
    virtual float getParamNormalized(int index) const;
    // markUserEdit=false for programmatic writes (undo/redo, project
    // sync) that must not register as TWEAK-mode user touches.
    virtual void setParamNormalizedByIndex(int index, float normalized,
                                           bool markUserEdit = true);
    // Audio thread: queue param change without touching the controller.
    virtual void setParamFromAudioThread(int index, float normalized);
    // UI thread: push automation values applied on the audio thread into
    // the edit controller so plugin editors / generic UIs reflect them.
    virtual void syncControllerParams();

    // State persistence.
    virtual std::string getState() const;
    virtual bool setState(const std::string& data);

    // Editor (UI thread).
    virtual bool hasEditor() const;
    virtual bool openEditor(void* hwnd);
    virtual bool openEditorTopLevel(void* ownerHwnd = nullptr,
                                    const std::string& title = {});
    virtual void closeEditor();
    virtual std::pair<int, int> editorSize() const;
    virtual bool checkSizeChanged(int& w, int& h);
    virtual bool editorCanResize() const;
    virtual bool resizeEditor(int w, int h);
    // User zoom via IPlugViewContentScaleSupport. May be called before the
    // editor opens (the factor is stored and applied on createView).
    // Returns true when the plugin view accepted the factor.
    virtual bool setContentScale(float scale);

    // User-facing diagnostic for offline/fallback UI.
    virtual std::string diagnostic() const;

    // Preview / MIDI (audio thread via mixer).
    virtual void queuePreviewNoteOn(int pitch, float velocity);
    virtual void queuePreviewNoteOff(int pitch);
    virtual void allNotesOff();

    // Transport discontinuity helper.
    virtual void onTransportJump();

    // Host transport info for ProcessContext (audio thread, cheap writes).
    // projectSamplePos is the timeline position in engine samples.
    virtual void setTransport(double bpm, bool playing, int64_t projectSamplePos);

    virtual void hostPerformEdit(uint32_t paramId, double normalized);
    // IComponentHandler::restartComponent: parameter list/values may be
    // stale (preset switch, plugin GUI restructuring, ...).
    virtual void hostRestartComponent(int32_t flags);
    // UI thread: re-read the cached parameter list when the plugin
    // flagged it stale. Returns true when a refresh happened.
    virtual bool refreshParamsIfChanged();

    // UI thread: index of the parameter most recently tweaked by the user
    // (native editor performEdit or host generic sliders) since the last
    // call; -1 when none. Used by the TWEAK "touch a knob" mode.
    virtual int consumeLastUserEdit();

    // UI thread: true while a subprocess bridge plugin is still loading
    // (async constructor). Editor open / TWEAK must wait for this.
    virtual bool isLoading() const;

protected:
    VstPlugin();

private:
    friend std::shared_ptr<VstPlugin> createVstPlugin(const std::string&,
                                                      const std::string&,
                                                      bool, uint32_t);

    bool init(const std::string& path, const std::string& uid,
              bool instrument, uint32_t sampleRate);
    void shutdown();
    void processBlock(float* interleaved, uint32_t frames,
                      const std::vector<VstHostEvent>* events);

    struct Impl;
    std::unique_ptr<Impl> impl_;
    bool instrument_ = false;
    std::string displayName_{"VST3"};
};
