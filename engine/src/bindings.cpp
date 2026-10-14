#include "audio_engine.h"
#include "warp/warp_constants.h"
#include "warp/warp_offline.h"

#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;
using pydaw::warp::WarpMethod;

namespace {

using pydaw::warp::warpMethodFromName;

std::vector<float> stretchBufferPy(py::array data, int64_t frames,
                                   int channels, float ratio,
                                   const std::string& method) {
    auto buf = data.request();
    if (buf.ndim != 2)
        throw std::runtime_error("stretch_buffer expects 2-D array");
    const int64_t f = buf.shape[0];
    const int ch = int(buf.shape[1]);
    if (f != frames || ch != channels)
        throw std::runtime_error("stretch_buffer shape mismatch");
    std::vector<float> out;
    {
        py::gil_scoped_release release;
        pydaw::warp::stretchOffline(static_cast<const float*>(buf.ptr), f, ch,
                                    ratio, warpMethodFromName(method), out);
    }
    return out;
}

}  // namespace

PYBIND11_MODULE(pydaw_engine, m) {
    m.doc() = "PyDAW C++ audio engine (RtAudio + dr_libs + built-in synth)";

    py::class_<DeviceInfo>(m, "DeviceInfo")
        .def_readonly("id", &DeviceInfo::id)
        .def_readonly("api_name", &DeviceInfo::apiName)
        .def_readonly("api_display_name", &DeviceInfo::apiDisplayName)
        .def_readonly("name", &DeviceInfo::name)
        .def_readonly("is_default", &DeviceInfo::isDefault)
        .def_readonly("sample_rates", &DeviceInfo::sampleRates);

    py::class_<VstPluginInfo>(m, "VstPluginInfo")
        .def_readonly("path", &VstPluginInfo::path)
        .def_readonly("name", &VstPluginInfo::name)
        .def_readonly("vendor", &VstPluginInfo::vendor)
        .def_readonly("category", &VstPluginInfo::category)
        .def_readonly("sub_categories", &VstPluginInfo::subCategories)
        .def_readonly("uid", &VstPluginInfo::uid)
        .def_readonly("is_instrument", &VstPluginInfo::isInstrument);

    py::class_<AudioEngine>(m, "AudioEngine")
        .def(py::init<>())
        .def_static("scan_vst_plugins", &AudioEngine::scanVstPlugins,
                    py::arg("dirs"),
                    py::call_guard<py::gil_scoped_release>())
        // Device
        .def("start", &AudioEngine::start,
             py::arg("sample_rate") = 48000, py::arg("buffer_frames") = 512,
             py::arg("device_id") = 0, py::arg("api_name") = "")
        .def("stop", &AudioEngine::stop)
        .def("shutdown", &AudioEngine::shutdown,
             py::call_guard<py::gil_scoped_release>())
        .def("is_running", &AudioEngine::isRunning)
        .def("sample_rate", &AudioEngine::sampleRate)
        .def("buffer_frames", &AudioEngine::bufferFrames)
        .def("current_device_id", &AudioEngine::currentDeviceId)
        .def("current_api_name", &AudioEngine::currentApiName)
        .def("list_audio_apis", &AudioEngine::listAudioApis)
        .def("list_devices", &AudioEngine::listDevices,
             py::arg("api_name") = "")
        // Files
        .def("load_file", &AudioEngine::loadFile, py::arg("path"),
             py::call_guard<py::gil_scoped_release>())
        .def("file_duration", &AudioEngine::fileDuration, py::arg("file_id"))
        .def("file_frames", &AudioEngine::fileFrames, py::arg("file_id"))
        .def("file_peaks", &AudioEngine::filePeaks,
             py::arg("file_id"), py::arg("samples_per_peak"),
             py::call_guard<py::gil_scoped_release>())
        // Tracks / audio clips
        .def("add_track", &AudioEngine::addTrack, py::arg("track_id"),
             py::arg("track_type") = 0)
        .def("remove_track", &AudioEngine::removeTrack, py::arg("track_id"))
        .def("add_clip", &AudioEngine::addClip,
             py::arg("track_id"), py::arg("clip_id"), py::arg("file_id"),
             py::arg("start_frame"), py::arg("source_offset") = 0,
             py::arg("length") = 0, py::arg("warp_method") = 0,
             py::arg("stretch_ratio") = 1.0f)
        .def("remove_clip", &AudioEngine::removeClip,
             py::arg("track_id"), py::arg("clip_id"))
        .def("move_clip", &AudioEngine::moveClip,
             py::arg("track_id"), py::arg("clip_id"), py::arg("start_frame"))
        .def("trim_clip", &AudioEngine::trimClip,
             py::arg("track_id"), py::arg("clip_id"), py::arg("start_frame"),
             py::arg("source_offset"), py::arg("length"))
        .def("set_clip_fades", &AudioEngine::setClipFades,
             py::arg("track_id"), py::arg("clip_id"),
             py::arg("fade_in"), py::arg("fade_out"))
        .def("set_clip_warp", &AudioEngine::setClipWarp,
             py::arg("track_id"), py::arg("clip_id"),
             py::arg("warp_method"), py::arg("stretch_ratio"))
        .def("move_clip_to_track", &AudioEngine::moveClipToTrack,
             py::arg("from_track_id"), py::arg("to_track_id"),
             py::arg("clip_id"), py::arg("start_frame"), py::arg("is_midi"))
        // MIDI clips / notes
        .def("add_midi_clip", &AudioEngine::addMidiClip,
             py::arg("track_id"), py::arg("clip_id"),
             py::arg("start_frame"), py::arg("length"))
        .def("remove_midi_clip", &AudioEngine::removeMidiClip,
             py::arg("track_id"), py::arg("clip_id"))
        .def("move_midi_clip", &AudioEngine::moveMidiClip,
             py::arg("track_id"), py::arg("clip_id"), py::arg("start_frame"))
        .def("resize_midi_clip", &AudioEngine::resizeMidiClip,
             py::arg("track_id"), py::arg("clip_id"), py::arg("length"))
        .def("add_note", &AudioEngine::addNote,
             py::arg("track_id"), py::arg("clip_id"), py::arg("note_id"),
             py::arg("pitch"), py::arg("velocity"),
             py::arg("start_frame"), py::arg("length"))
        .def("remove_note", &AudioEngine::removeNote,
             py::arg("track_id"), py::arg("clip_id"), py::arg("note_id"))
        .def("update_note", &AudioEngine::updateNote,
             py::arg("track_id"), py::arg("clip_id"), py::arg("note_id"),
             py::arg("pitch"), py::arg("velocity"),
             py::arg("start_frame"), py::arg("length"))
        // Instrument / preview
        .def("set_instrument", &AudioEngine::setInstrument,
             py::arg("track_id"), py::arg("wave"),
             py::arg("attack"), py::arg("decay"), py::arg("sustain"),
             py::arg("release"), py::arg("gain"))
        .def("preview_note_on", &AudioEngine::previewNoteOn,
             py::arg("track_id"), py::arg("pitch"), py::arg("velocity") = 1.0f)
        .def("preview_note_off", &AudioEngine::previewNoteOff, py::arg("pitch"))
        // Effects
        .def("add_effect", &AudioEngine::addEffect,
             py::arg("track_id"), py::arg("type"))
        .def("remove_effect", &AudioEngine::removeEffect,
             py::arg("track_id"), py::arg("slot_index"))
        .def("set_effect_param", &AudioEngine::setEffectParam,
             py::arg("track_id"), py::arg("slot_index"),
             py::arg("param_index"), py::arg("value"))
        // Mixer params
        .def("set_gain", &AudioEngine::setGain, py::arg("track_id"), py::arg("gain"))
        .def("set_pan", &AudioEngine::setPan, py::arg("track_id"), py::arg("pan"))
        .def("set_stereo_width", &AudioEngine::setStereoWidth,
             py::arg("track_id"), py::arg("width"))
        .def("set_mute", &AudioEngine::setMute, py::arg("track_id"), py::arg("mute"))
        .def("set_solo", &AudioEngine::setSolo, py::arg("track_id"), py::arg("solo"))
        .def("set_track_group", &AudioEngine::setTrackGroup,
             py::arg("track_id"), py::arg("group_id"))
        .def("set_master_gain", &AudioEngine::setMasterGain, py::arg("gain"))
        .def("set_tempo", &AudioEngine::setTempo, py::arg("bpm"))
        .def("set_metronome", &AudioEngine::setMetronome, py::arg("on"))
        .def("set_loop", &AudioEngine::setLoop,
             py::arg("start_frame"), py::arg("end_frame"), py::arg("enabled"))
        .def("set_automation_lane", &AudioEngine::setAutomationLane,
             py::arg("track_id"), py::arg("lane_id"), py::arg("target"),
             py::arg("slot"), py::arg("param"), py::arg("points"))
        .def("remove_automation_lane", &AudioEngine::removeAutomationLane,
             py::arg("track_id"), py::arg("lane_id"))
        .def("clear_all", &AudioEngine::clearAll)
        .def("drain_commands", &AudioEngine::drainCommands,
             py::call_guard<py::gil_scoped_release>())
        // Transport
        .def("play", &AudioEngine::play)
        .def("pause", &AudioEngine::pause)
        .def("seek", &AudioEngine::seek, py::arg("frame"))
        .def("is_playing", &AudioEngine::isPlaying)
        .def("playhead_frame", &AudioEngine::playheadFrame)
        .def("playhead_seconds", &AudioEngine::playheadSeconds)
        // Metering
        .def("master_peaks", [](AudioEngine& e) {
            return std::make_pair(e.masterPeakL(), e.masterPeakR());
        })
        .def("track_peaks", &AudioEngine::trackPeaks, py::arg("track_id"))
        // Export
        .def("export_wav", &AudioEngine::exportWav, py::arg("path"),
             py::arg("start_frame") = int64_t(0),
             py::arg("end_frame") = int64_t(-1),
             py::call_guard<py::gil_scoped_release>())
        .def("export_progress", &AudioEngine::exportProgress)
        // Debug: run the realtime callback offline and return interleaved
        // stereo floats (honors transport/loop). Only safe when no audio
        // device is running (engine not started).
        .def("debug_process", [](AudioEngine& e, uint32_t frames) {
            if (e.isRunning())
                throw std::runtime_error(
                    "debug_process requires a stopped engine (no audio "
                    "device stream running)");
            std::vector<float> buf(size_t(frames) * 2, 0.f);
            uint32_t done = 0;
            const uint32_t block = 512;
            while (done < frames) {
                const uint32_t n = std::min(block, frames - done);
                e.process(buf.data() + size_t(done) * 2, n);
                done += n;
            }
            return buf;
        }, py::arg("frames"))
        .def("cancel_export", &AudioEngine::cancelExport)
        .def("export_was_cancelled", &AudioEngine::exportWasCancelled)
        // Performance / latency compensation
        .def("set_pdc_enabled", &AudioEngine::setPdcEnabled, py::arg("on"))
        .def("pdc_enabled", &AudioEngine::pdcEnabled)
        .def("set_pdc_mode", &AudioEngine::setPdcMode, py::arg("mode"))
        .def("pdc_mode", &AudioEngine::pdcMode)
        .def("track_latency", &AudioEngine::trackLatency, py::arg("track_id"))
        .def_static("mix_threads", &AudioEngine::mixThreads)
        // VST3
        .def("add_vst_effect", &AudioEngine::addVstEffect,
             py::arg("track_id"), py::arg("path"), py::arg("uid"))
        .def("set_vst_instrument", &AudioEngine::setVstInstrument,
             py::arg("track_id"), py::arg("path"), py::arg("uid"))
        .def("clear_vst_instrument", &AudioEngine::clearVstInstrument,
             py::arg("track_id"))
        .def("vst_has_editor", &AudioEngine::vstHasEditor,
             py::arg("track_id"), py::arg("slot"))
        .def("vst_open_editor", &AudioEngine::vstOpenEditor,
             py::arg("track_id"), py::arg("slot"), py::arg("hwnd"))
        .def("vst_close_editor", &AudioEngine::vstCloseEditor,
             py::arg("track_id"), py::arg("slot"))
        .def("vst_editor_size", &AudioEngine::vstEditorSize,
             py::arg("track_id"), py::arg("slot"))
        .def("vst_check_resize", [](AudioEngine& e, int trackId, int slot) {
            int w = 0, h = 0;
            const bool changed = e.vstCheckResize(trackId, slot, w, h);
            return py::make_tuple(changed, w, h);
        }, py::arg("track_id"), py::arg("slot"))
        .def("vst_editor_can_resize", &AudioEngine::vstEditorCanResize,
             py::arg("track_id"), py::arg("slot"))
        .def("vst_resize_editor", &AudioEngine::vstResizeEditor,
             py::arg("track_id"), py::arg("slot"), py::arg("w"), py::arg("h"))
        .def("vst_set_content_scale", &AudioEngine::vstSetContentScale,
             py::arg("track_id"), py::arg("slot"), py::arg("scale"))
        .def("vst_diagnostic", &AudioEngine::vstDiagnostic,
             py::arg("track_id"), py::arg("slot"))
        .def("vst_param_count", &AudioEngine::vstParamCount,
             py::arg("track_id"), py::arg("slot"))
        .def("vst_params", &AudioEngine::vstParams,
             py::arg("track_id"), py::arg("slot"))
        .def("vst_set_param", &AudioEngine::vstSetParam,
             py::arg("track_id"), py::arg("slot"), py::arg("param_index"),
             py::arg("normalized"), py::arg("mark_user_edit") = true)
        .def("vst_get_param", &AudioEngine::vstGetParam,
             py::arg("track_id"), py::arg("slot"), py::arg("param_index"))
        .def("vst_get_param_default", &AudioEngine::vstGetParamDefault,
             py::arg("track_id"), py::arg("slot"), py::arg("param_index"))
        .def("vst_sync_controllers", &AudioEngine::vstSyncControllers)
        .def("vst_consume_last_tweak", &AudioEngine::vstConsumeLastTweak)
        .def("vst_refresh_changed_params",
             &AudioEngine::vstRefreshChangedParams)
        .def("vst_is_loading", &AudioEngine::vstIsLoading,
             py::arg("track_id"), py::arg("slot"))
        .def("vst_get_state", [](AudioEngine& e, int trackId, int slot) {
            return py::bytes(e.vstGetState(trackId, slot));
        }, py::arg("track_id"), py::arg("slot"))
        .def("vst_set_state", [](AudioEngine& e, int trackId, int slot,
                                 py::bytes data) {
            return e.vstSetState(trackId, slot, std::string(data));
        }, py::arg("track_id"), py::arg("slot"), py::arg("data"));

    m.def("stretch_buffer", &stretchBufferPy,
          py::arg("data"), py::arg("frames"), py::arg("channels"),
          py::arg("ratio"), py::arg("method"));
    m.def("stretch_file_to_cache",
          [](const std::string& path, const std::string& method, float ratio,
             uint32_t sampleRate) {
              std::string outPath;
              {
                  py::gil_scoped_release release;
                  outPath = pydaw::warp::stretchFileToCache(
                      path, warpMethodFromName(method), ratio, sampleRate);
              }
              return outPath;
          },
          py::arg("path"), py::arg("method"), py::arg("ratio"),
          py::arg("sample_rate") = 48000);
    m.def("warp_method_code", [](const std::string& name) {
        return int(warpMethodFromName(name));
    }, py::arg("method"));
}
