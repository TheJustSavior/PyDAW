"""Bridge between the Python project model and the C++ engine.

All engine access goes through this module so the UI never touches
pydaw_engine directly. If the compiled module is missing, a silent
stub is used so the UI can still be developed/tested.

MIDI clips/notes are stored in beats in the model; this bridge converts
them to engine frames using the project BPM.
"""

from __future__ import annotations

import os
import sys

from .model.project import (AUTOMATION_SHAPE_CODES, AutomationLane, Clip,
                            EFFECT_DEFS, Effect, Instrument, MidiClip,
                            MidiNote, Project, Track)

# Pseudo track id addressing the master insert chain in the engine.
MASTER_FX_ID = -1

try:
    from pydaw import pydaw_engine  # compiled extension inside the package
    ENGINE_AVAILABLE = True
except ImportError:
    try:
        import pydaw_engine  # fallback: top-level module
        ENGINE_AVAILABLE = True
    except ImportError:
        pydaw_engine = None
        ENGINE_AVAILABLE = False


class _StubEngine:
    """No-audio fallback used when the C++ module is not built."""

    @staticmethod
    def scan_vst_plugins(dirs: list[str]) -> list:
        return []

    def __getattr__(self, name):
        def _noop(*args, **kwargs):
            if name == "load_file":
                return 0
            if name in ("is_running", "is_playing"):
                return False
            if name in ("playhead_frame", "file_frames", "buffer_frames",
                        "current_device_id"):
                return 0
            if name in ("playhead_seconds", "file_duration"):
                return 0.0
            if name == "export_progress":
                return 1.0
            if name in ("track_latency", "mix_threads", "pdc_mode"):
                return 0
            if name in ("pdc_enabled",):
                return True
            if name in ("export_was_cancelled",):
                return False
            if name == "sample_rate":
                return 48000
            if name == "current_api_name":
                return ""
            if name in ("master_peaks", "track_peaks"):
                return (0.0, 0.0)
            if name in ("file_peaks", "list_devices"):
                return []
            if name == "list_audio_apis":
                return []
            if name in ("start", "export_wav", "add_effect",
                        "add_vst_effect", "set_vst_instrument",
                        "vst_has_editor", "vst_open_editor", "vst_set_state",
                        "vst_editor_can_resize", "vst_resize_editor",
                        "vst_set_content_scale"):
                return False
            if name in ("vst_param_count",):
                return 0
            if name in ("vst_get_param", "vst_get_param_default"):
                return 0.0
            if name == "vst_params":
                return []
            if name == "vst_editor_size":
                return (0, 0)
            if name == "vst_check_resize":
                return (False, 0, 0)
            if name == "vst_get_state":
                return b""
            return None
        return _noop


class EngineBridge:
    TARGET_CODES = {"volume": 0, "pan": 1, "inst": 4, "width": 5}

    def __init__(self, sample_rate: int = 48000, buffer_frames: int = 512,
                 device_id: int = 0, api_name: str = "") -> None:
        if ENGINE_AVAILABLE:
            self.engine = pydaw_engine.AudioEngine()
        else:
            self.engine = _StubEngine()
        self.audio_ok = bool(self.engine.start(sample_rate, buffer_frames,
                                               device_id, api_name))
        # Saved device/API can disappear or be busy (e.g. an ASIO device
        # held by another app). Without a running stream the audio
        # callback never fires and the playhead freezes - fall back to
        # the default device so the app stays usable.
        self.used_fallback_device = False
        if not self.audio_ok and (device_id or api_name):
            self.audio_ok = bool(self.engine.start(sample_rate,
                                                   buffer_frames, 0, ""))
            self.used_fallback_device = self.audio_ok
        self.sample_rate = int(self.engine.sample_rate()) or sample_rate
        self.buffer_frames = int(self.engine.buffer_frames()) or buffer_frames
        self.api_name = str(self.engine.current_api_name() or api_name or "")
        self._peak_cache: dict[tuple[int, int], list[float]] = {}
        self.last_sync_errors: list[str] = []
        self._fx_slot_map: dict[tuple[int, int], int] = {}
        self._fx_engine_count: dict[int, int] = {}
        self._shutting_down = False

    def shutdown(self) -> None:
        self._shutting_down = True
        if hasattr(self.engine, "shutdown"):
            self.engine.shutdown()
        else:
            self.engine.stop()

    # -- VST plugin scan -------------------------------------------------

    def scan_vst_plugins(self, dirs: list[str]) -> list:
        """Scan VST3 directories; returns list of VstPluginInfo objects.

        By default the native scanner runs in a short-lived subprocess so
        buggy plugin DLLs cannot crash the main PyDAW process. Set
        PYDAW_VST_SCAN_IN_PROCESS=1 to use the old in-process path for
        debugging.
        """
        if not ENGINE_AVAILABLE:
            return _StubEngine.scan_vst_plugins(dirs)
        if (os.environ.get("PYDAW_DEBUG") == "1"
                and os.environ.get("PYDAW_VST_SCAN_IN_PROCESS") == "1"):
            return list(pydaw_engine.AudioEngine.scan_vst_plugins(dirs))

        from .vst_scan_subprocess import scan_vst_plugins_subprocess

        result = scan_vst_plugins_subprocess(dirs)
        if result.error:
            print(f"[vst-scan] subprocess scan failed: {result.error}",
                  file=sys.stderr)
        return list(result.plugins)

    # -- devices ---------------------------------------------------------

    def list_audio_apis(self) -> list[tuple[str, str]]:
        """Compiled RtAudio APIs as (name, display_name)."""
        return list(self.engine.list_audio_apis())

    def list_devices(self, api_name: str | None = None) -> list:
        """Available stereo output devices (id, name, is_default, rates)."""
        return list(self.engine.list_devices(api_name or ""))

    def restart(self, sample_rate: int, buffer_frames: int,
                device_id: int = 0, api_name: str = "") -> bool:
        """Reopen the audio device with new settings.

        The caller must re-load files and re-sync the project afterwards
        (files are resampled to the engine rate at load time).
        """
        self.engine.stop()
        self.audio_ok = bool(self.engine.start(sample_rate, buffer_frames,
                                               device_id, api_name))
        self.sample_rate = int(self.engine.sample_rate()) or sample_rate
        self.buffer_frames = int(self.engine.buffer_frames()) or buffer_frames
        self.api_name = str(self.engine.current_api_name() or api_name or "")
        self._peak_cache.clear()
        return self.audio_ok

    # -- files ---------------------------------------------------------

    def load_file(self, path: str) -> tuple[int, int]:
        """Load audio file; returns (file_id, frame_count). Raises on error."""
        file_id = self.engine.load_file(path)
        return file_id, int(self.engine.file_frames(file_id))

    def file_peaks(self, file_id: int, samples_per_peak: int) -> list[float]:
        key = (file_id, samples_per_peak)
        if key not in self._peak_cache:
            self._peak_cache[key] = list(
                self.engine.file_peaks(file_id, samples_per_peak))
        return self._peak_cache[key]

    # -- FX slot mapping (model slot -> engine slot) -----------------------

    def resolve_fx_slot(self, track_id: int, model_slot: int) -> int | None:
        """Map a model effect index to the engine chain slot, if loaded."""
        return self._fx_slot_map.get((track_id, model_slot))

    def _clear_fx_maps(self) -> None:
        self._fx_slot_map.clear()
        self._fx_engine_count.clear()

    def _apply_effects(self, track_id: int, effects: list[Effect]) -> None:
        """Push model effects into the engine, building the slot map."""
        self._fx_engine_count[track_id] = 0
        keys = [k for k in self._fx_slot_map if k[0] == track_id]
        for key in keys:
            del self._fx_slot_map[key]

        for model_slot, fx in enumerate(effects):
            if fx.type == "vst":
                ok = bool(self.engine.add_vst_effect(
                    track_id, fx.path, fx.uid))
                if not ok:
                    self.last_sync_errors.append(
                        fx.name or fx.path or "VST effect")
                    continue
            else:
                ok = bool(self.engine.add_effect(track_id, fx.type))
                if not ok:
                    continue

            eng_slot = self._fx_engine_count[track_id]
            self._fx_slot_map[(track_id, model_slot)] = eng_slot
            self._fx_engine_count[track_id] = eng_slot + 1

            if fx.type == "vst":
                if fx.state:
                    self.engine.vst_set_state(track_id, eng_slot, fx.state)
            else:
                for i, value in enumerate(fx.params):
                    self.engine.set_effect_param(
                        track_id, eng_slot, i, value)

    def _apply_track_effects(self, track: Track) -> None:
        self._apply_effects(track.id, track.effects)

    def _resync_fx(self, track_id: int, effects: list[Effect]) -> None:
        """Remove and re-add all effects for one chain (after edit)."""
        count = self._fx_engine_count.get(track_id, 0)
        for _ in range(count):
            self.engine.remove_effect(track_id, 0)
        self._apply_effects(track_id, effects)

    def _resync_track_fx(self, track: Track) -> None:
        self._resync_fx(track.id, track.effects)

    # -- model sync ------------------------------------------------------

    @staticmethod
    def _track_type_code(track: Track) -> int:
        if track.is_midi:
            return 1
        if track.is_group:
            return 2
        return 0

    def _automation_target_code(self, track: Track, lane: AutomationLane) -> int:
        if lane.target == "fx":
            if 0 <= lane.fx_slot < len(track.effects):
                if track.effects[lane.fx_slot].type == "vst":
                    return 3
            return 2
        return self.TARGET_CODES.get(lane.target, 0)

    @staticmethod
    def _native_automation_value(track: Track, lane: AutomationLane,
                                 normalized: float) -> float:
        v = max(0.0, min(1.0, normalized))
        if lane.target == "volume":
            return v * 1.5
        if lane.target == "pan":
            return v * 2.0 - 1.0
        if lane.target == "width":
            return v * 2.0
        if lane.target == "fx" and 0 <= lane.fx_slot < len(track.effects):
            fx = track.effects[lane.fx_slot]
            if fx.type in EFFECT_DEFS and lane.param < len(EFFECT_DEFS[fx.type][1]):
                lo, hi = EFFECT_DEFS[fx.type][1][lane.param][1:3]
                return lo + (hi - lo) * v
        return v

    def sync_automation_lane(self, project: Project, track: Track,
                             lane: AutomationLane) -> None:
        target = self._automation_target_code(track, lane)
        slot = -1
        if lane.target == "fx":
            eng_slot = self.resolve_fx_slot(track.id, lane.fx_slot)
            if eng_slot is None:
                return
            slot = eng_slot
        elif lane.target == "inst":
            slot = -1
        points = []
        for pt in lane.points:
            frame = project.beats_to_frames(pt.beat)
            value = self._native_automation_value(track, lane, pt.value)
            shape = AUTOMATION_SHAPE_CODES.get(pt.shape, 0)
            points.append((frame, value, shape, pt.curve_amount))
        self.engine.set_automation_lane(
            track.id, lane.id, target, slot, lane.param, points)

    def _master_proxy_track(self, project: Project) -> Track:
        return Track(id=MASTER_FX_ID, name="Master",
                     effects=project.master_effects,
                     automation_lanes=project.master_automation_lanes)

    def sync_master_automation_lane(self, project: Project,
                                    lane: AutomationLane) -> None:
        self.sync_automation_lane(project, self._master_proxy_track(project),
                                  lane)

    def remove_automation_lane(self, track_id: int, lane_id: int) -> None:
        self.engine.remove_automation_lane(track_id, lane_id)

    def add_track(self, track: Track, project: Project | None = None) -> None:
        self.engine.add_track(track.id, self._track_type_code(track))
        self.engine.set_gain(track.id, track.gain)
        self.engine.set_pan(track.id, track.pan)
        self.engine.set_stereo_width(track.id, track.stereo_width)
        self.engine.set_mute(track.id, track.mute)
        self.engine.set_solo(track.id, track.solo)
        if track.group_id != 0:
            self.set_track_group(track.id, track.group_id)
        if track.is_midi:
            inst = track.instrument
            if inst.vst_path:
                if self.set_vst_instrument(track.id, inst.vst_path,
                                           inst.vst_uid):
                    if inst.vst_state:
                        self.vst_set_state(track.id, -1, inst.vst_state)
                else:
                    self.last_sync_errors.append(
                        inst.vst_name or inst.vst_path or "VST instrument")
                    self.set_instrument(track.id, inst)
            else:
                self.set_instrument(track.id, inst)
        self._apply_track_effects(track)
        if project is not None:
            for lane in track.automation_lanes:
                self.sync_automation_lane(project, track, lane)

    def remove_track(self, track_id: int) -> None:
        self.engine.remove_track(track_id)
        keys = [k for k in self._fx_slot_map if k[0] == track_id]
        for key in keys:
            del self._fx_slot_map[key]
        self._fx_engine_count.pop(track_id, None)

    def add_clip(self, track_id: int, clip: Clip) -> None:
        warp_code = self._warp_method_code(clip.warp)
        self.engine.add_clip(track_id, clip.id, clip.file_id,
                             clip.start_frame, clip.source_offset, clip.length,
                             warp_code, float(clip.stretch_ratio))
        if clip.fade_in or clip.fade_out:
            self.engine.set_clip_fades(track_id, clip.id,
                                       clip.fade_in, clip.fade_out)

    def set_clip_warp(self, track_id: int, clip: Clip) -> None:
        """Push warp method and stretch ratio to the realtime engine."""
        if hasattr(self.engine, "set_clip_warp"):
            self.engine.set_clip_warp(
                track_id, clip.id, self._warp_method_code(clip.warp),
                float(clip.stretch_ratio))

    @staticmethod
    def _warp_method_code(warp: str) -> int:
        if ENGINE_AVAILABLE and hasattr(pydaw_engine, "warp_method_code"):
            return int(pydaw_engine.warp_method_code(warp))
        return {"stretch": 0, "stretch_hd": 1, "resample": 2}.get(warp, 0)

    def remove_clip(self, track_id: int, clip_id: int) -> None:
        self.engine.remove_clip(track_id, clip_id)

    def move_clip(self, track_id: int, clip_id: int, start_frame: int) -> None:
        self.engine.move_clip(track_id, clip_id, start_frame)

    def trim_clip(self, track_id: int, clip: Clip) -> None:
        self.set_clip_warp(track_id, clip)
        self.engine.trim_clip(track_id, clip.id, clip.start_frame,
                              clip.source_offset, clip.length)

    def set_clip_fades(self, track_id: int, clip: Clip) -> None:
        self.engine.set_clip_fades(track_id, clip.id,
                                   clip.fade_in, clip.fade_out)

    def move_clip_to_track(self, from_track: int, to_track: int,
                           clip_id: int, start_frame: int,
                           is_midi: bool) -> None:
        self.engine.move_clip_to_track(from_track, to_track, clip_id,
                                       start_frame, is_midi)

    # -- MIDI ------------------------------------------------------------

    def add_midi_clip(self, project: Project, track_id: int,
                      clip: MidiClip) -> None:
        self.engine.add_midi_clip(
            track_id, clip.id,
            project.beats_to_frames(clip.start_beat),
            project.beats_to_frames(clip.length_beats))
        for note in clip.notes:
            self.add_note(project, track_id, clip.id, note)

    def remove_midi_clip(self, track_id: int, clip_id: int) -> None:
        self.engine.remove_midi_clip(track_id, clip_id)

    def move_midi_clip(self, project: Project, track_id: int,
                       clip: MidiClip) -> None:
        self.engine.move_midi_clip(
            track_id, clip.id, project.beats_to_frames(clip.start_beat))

    def resize_midi_clip(self, project: Project, track_id: int,
                         clip: MidiClip) -> None:
        self.engine.resize_midi_clip(
            track_id, clip.id, project.beats_to_frames(clip.length_beats))
        # Length affects the fade-out ramp of every note.
        if clip.fade_out_beats > 0:
            self.refresh_midi_clip_notes(project, track_id, clip)

    def trim_midi_clip(self, project: Project, track_id: int,
                       clip: MidiClip) -> None:
        """Re-sync after a left-edge trim: position, length and the note
        window (source_start_beat) may all have changed."""
        self.engine.move_midi_clip(
            track_id, clip.id, project.beats_to_frames(clip.start_beat))
        self.engine.resize_midi_clip(
            track_id, clip.id, project.beats_to_frames(clip.length_beats))
        self.refresh_midi_clip_notes(project, track_id, clip)

    def set_midi_clip_fades(self, project: Project, track_id: int,
                            clip: MidiClip) -> None:
        """Fades are velocity ramps: re-send every note of the clip."""
        self.refresh_midi_clip_notes(project, track_id, clip)

    def refresh_midi_clip_notes(self, project: Project, track_id: int,
                                clip: MidiClip) -> None:
        for note in clip.notes:
            self.update_note(project, track_id, clip.id, note)

    @staticmethod
    def _midi_fade_scale(clip: MidiClip | None, note: MidiNote) -> float:
        """Velocity multiplier from the clip's fade ramps (0..1)."""
        if clip is None:
            return 1.0
        pos = note.start_beat - clip.source_start_beat
        scale = 1.0
        if clip.fade_in_beats > 0 and pos < clip.fade_in_beats:
            scale = min(scale, max(0.0, pos / clip.fade_in_beats))
        tail = clip.length_beats - pos
        if clip.fade_out_beats > 0 and tail < clip.fade_out_beats:
            scale = min(scale, max(0.0, tail / clip.fade_out_beats))
        return scale

    @staticmethod
    def _midi_clip_by_id(project: Project, track_id: int,
                         clip_id: int) -> MidiClip | None:
        track = project.track_by_id(track_id)
        return track.midi_clip_by_id(clip_id) if track else None

    def _engine_note_span(self, project: Project, track_id: int,
                          clip_id: int, note: MidiNote) -> tuple[int, int]:
        """Note (start, length) in engine frames, end-consistent.

        Start and end are rounded independently on the beat grid, then
        the length is their difference. Rounding start and length
        separately could make a note end one frame PAST the next
        same-pitch note's start (round(s)+round(L) > round(s+L)): its
        note-off then lands after the next note-on and chokes it - after
        joining clips every seam played a "faded" first note.
        """
        clip = self._midi_clip_by_id(project, track_id, clip_id)
        source_start = clip.source_start_beat if clip else 0.0
        rel_start = max(0.0, note.start_beat - source_start)
        start = project.beats_to_frames(rel_start)
        end = project.beats_to_frames(rel_start + note.length_beats)
        return start, max(0, end - start)

    def add_note(self, project: Project, track_id: int, clip_id: int,
                 note: MidiNote) -> None:
        clip = self._midi_clip_by_id(project, track_id, clip_id)
        start, length = self._engine_note_span(project, track_id, clip_id,
                                               note)
        self.engine.add_note(
            track_id, clip_id, note.id, note.pitch,
            note.velocity * self._midi_fade_scale(clip, note),
            start, length)

    def remove_note(self, track_id: int, clip_id: int, note_id: int) -> None:
        self.engine.remove_note(track_id, clip_id, note_id)

    def update_note(self, project: Project, track_id: int, clip_id: int,
                    note: MidiNote) -> None:
        clip = self._midi_clip_by_id(project, track_id, clip_id)
        start, length = self._engine_note_span(project, track_id, clip_id,
                                               note)
        self.engine.update_note(
            track_id, clip_id, note.id, note.pitch,
            note.velocity * self._midi_fade_scale(clip, note),
            start, length)

    def set_instrument(self, track_id: int, inst: Instrument) -> None:
        self.engine.set_instrument(track_id, inst.wave, inst.attack,
                                   inst.decay, inst.sustain, inst.release,
                                   inst.gain)

    def preview_note_on(self, track_id: int, pitch: int,
                        velocity: float = 1.0) -> None:
        self.engine.preview_note_on(track_id, pitch, velocity)

    def preview_note_off(self, pitch: int) -> None:
        self.engine.preview_note_off(pitch)

    # -- effects -----------------------------------------------------------

    def add_effect(self, track_id: int, fx_type: str) -> bool:
        return bool(self.engine.add_effect(track_id, fx_type))

    def add_vst_effect(self, track_id: int, path: str, uid: str) -> bool:
        return bool(self.engine.add_vst_effect(track_id, path, uid))

    def append_effect(self, track_id: int, model_slot: int,
                      fx: Effect) -> bool:
        """Append one model effect to the engine without rebuilding the chain."""
        if fx.type == "vst":
            ok = bool(self.engine.add_vst_effect(track_id, fx.path, fx.uid))
            if not ok:
                self.last_sync_errors.append(fx.name or fx.path or "VST effect")
                return False
        else:
            ok = bool(self.engine.add_effect(track_id, fx.type))
            if not ok:
                return False

        self.note_fx_added(track_id, model_slot)
        if fx.type == "vst":
            if fx.state:
                self.engine.vst_set_state(
                    track_id, self._fx_slot_map[(track_id, model_slot)],
                    fx.state)
        else:
            eng_slot = self._fx_slot_map[(track_id, model_slot)]
            for i, value in enumerate(fx.params):
                self.engine.set_effect_param(track_id, eng_slot, i, value)
        return True

    def note_fx_added(self, track_id: int, model_slot: int) -> None:
        """Record mapping after the model effect list was appended."""
        eng_slot = self._fx_engine_count.get(track_id, 0)
        self._fx_slot_map[(track_id, model_slot)] = eng_slot
        self._fx_engine_count[track_id] = eng_slot + 1

    def resync_track_fx(self, track: Track) -> None:
        """Rebuild the engine effect chain from the model (after removal)."""
        self._resync_track_fx(track)

    def resync_master_fx(self, project: Project) -> None:
        """Rebuild the master insert chain from the model."""
        self._resync_fx(MASTER_FX_ID, project.master_effects)

    def remove_effect(self, track_id: int, slot_index: int) -> None:
        eng_slot = self.resolve_fx_slot(track_id, slot_index)
        if eng_slot is not None:
            self.engine.remove_effect(track_id, eng_slot)
        self.note_fx_removed(track_id, slot_index, eng_slot)

    def note_fx_removed(self, track_id: int, model_slot: int,
                        eng_slot: int | None) -> None:
        """Update model->engine slot maps after removing one model effect."""
        new_map: dict[tuple[int, int], int] = {}
        for (tid, mslot), eslot in self._fx_slot_map.items():
            if tid != track_id:
                new_map[(tid, mslot)] = eslot
                continue
            if mslot == model_slot:
                continue
            next_mslot = mslot - 1 if mslot > model_slot else mslot
            next_eslot = eslot
            if eng_slot is not None and eslot > eng_slot:
                next_eslot = eslot - 1
            new_map[(tid, next_mslot)] = next_eslot
        self._fx_slot_map = new_map
        if track_id in self._fx_engine_count and eng_slot is not None:
            self._fx_engine_count[track_id] = max(
                0, self._fx_engine_count[track_id] - 1)

    def set_effect_param(self, track_id: int, slot_index: int,
                         param_index: int, value: float) -> None:
        eng_slot = self.resolve_fx_slot(track_id, slot_index)
        if eng_slot is None:
            return
        self.engine.set_effect_param(track_id, eng_slot, param_index, value)

    # -- VST instruments & editors -----------------------------------------

    def set_vst_instrument(self, track_id: int, path: str, uid: str) -> bool:
        return bool(self.engine.set_vst_instrument(track_id, path, uid))

    def clear_vst_instrument(self, track_id: int) -> None:
        self.engine.clear_vst_instrument(track_id)

    def _resolve_vst_slot(self, track_id: int, slot: int) -> int:
        if slot < 0:
            return slot
        eng = self.resolve_fx_slot(track_id, slot)
        return eng if eng is not None else slot

    def vst_has_editor(self, track_id: int, slot: int) -> bool:
        return bool(self.engine.vst_has_editor(
            track_id, self._resolve_vst_slot(track_id, slot)))

    def vst_is_loading(self, track_id: int, slot: int) -> bool:
        try:
            return bool(self.engine.vst_is_loading(
                track_id, self._resolve_vst_slot(track_id, slot)))
        except AttributeError:
            return False

    def vst_open_editor(self, track_id: int, slot: int, hwnd: int) -> bool:
        return bool(self.engine.vst_open_editor(
            track_id, self._resolve_vst_slot(track_id, slot), hwnd))

    def vst_close_editor(self, track_id: int, slot: int) -> None:
        if self._shutting_down:
            return
        self.engine.vst_close_editor(
            track_id, self._resolve_vst_slot(track_id, slot))

    def vst_editor_size(self, track_id: int, slot: int) -> tuple[int, int]:
        w, h = self.engine.vst_editor_size(
            track_id, self._resolve_vst_slot(track_id, slot))
        return int(w), int(h)

    def vst_check_resize(self, track_id: int, slot: int) -> tuple[bool, int, int]:
        changed, w, h = self.engine.vst_check_resize(
            track_id, self._resolve_vst_slot(track_id, slot))
        return bool(changed), int(w), int(h)

    def vst_editor_can_resize(self, track_id: int, slot: int) -> bool:
        return bool(self.engine.vst_editor_can_resize(
            track_id, self._resolve_vst_slot(track_id, slot)))

    def vst_resize_editor(self, track_id: int, slot: int,
                          w: int, h: int) -> bool:
        return bool(self.engine.vst_resize_editor(
            track_id, self._resolve_vst_slot(track_id, slot), w, h))

    def vst_set_content_scale(self, track_id: int, slot: int,
                              scale: float) -> bool:
        return bool(self.engine.vst_set_content_scale(
            track_id, self._resolve_vst_slot(track_id, slot), scale))

    def vst_diagnostic(self, track_id: int, slot: int) -> str:
        try:
            return str(self.engine.vst_diagnostic(
                track_id, self._resolve_vst_slot(track_id, slot)))
        except AttributeError:
            return "VST diagnostic unavailable; rebuild pydaw_engine"

    def vst_param_count(self, track_id: int, slot: int) -> int:
        return int(self.engine.vst_param_count(
            track_id, self._resolve_vst_slot(track_id, slot)))

    def vst_params(self, track_id: int, slot: int) -> list:
        return list(self.engine.vst_params(
            track_id, self._resolve_vst_slot(track_id, slot)))

    def vst_set_param(self, track_id: int, slot: int, param_index: int,
                      normalized: float, mark_user_edit: bool = True) -> None:
        """`mark_user_edit=False` for programmatic writes (undo/redo):
        they must not register as TWEAK-mode touches."""
        try:
            self.engine.vst_set_param(
                track_id, self._resolve_vst_slot(track_id, slot),
                param_index, normalized, mark_user_edit)
        except TypeError:  # older compiled engine without the flag
            self.engine.vst_set_param(
                track_id, self._resolve_vst_slot(track_id, slot),
                param_index, normalized)

    def vst_get_param(self, track_id: int, slot: int,
                      param_index: int) -> float:
        return float(self.engine.vst_get_param(
            track_id, self._resolve_vst_slot(track_id, slot), param_index))

    def vst_get_param_default(self, track_id: int, slot: int,
                              param_index: int) -> float:
        """Plugin-declared default (normalized) for reset-to-default."""
        try:
            return float(self.engine.vst_get_param_default(
                track_id, self._resolve_vst_slot(track_id, slot),
                param_index))
        except AttributeError:  # older compiled engine without the binding
            return 0.0

    def vst_refresh_changed_params(self) -> bool:
        """Re-read parameter lists of plugins that reported a change
        (preset switch / dynamic GUI). True = UI should rebuild its
        parameter views (generic sliders, pickers use fresh data)."""
        try:
            return bool(self.engine.vst_refresh_changed_params())
        except AttributeError:  # older compiled engine
            return False

    def vst_sync_controllers(self) -> None:
        """Mirror audio-thread automation into VST edit controllers."""
        self.engine.vst_sync_controllers()

    def vst_consume_last_tweak(self) -> tuple[int, int, int] | None:
        """(track_id, model_slot, param_index) of the last user tweak in any
        VST (native editor or generic sliders), or None. slot -1 = instrument.
        Consumes the pending edit flags in the engine (TWEAK mode poll)."""
        result = self.engine.vst_consume_last_tweak()
        if not result:
            return None
        track_id, eng_slot, param = int(result[0]), int(result[1]), int(result[2])
        if param < 0:
            return None
        if eng_slot < 0:
            return track_id, -1, param
        for (tid, model_slot), es in self._fx_slot_map.items():
            if tid == track_id and es == eng_slot:
                return track_id, model_slot, param
        return None

    def vst_get_state(self, track_id: int, slot: int) -> bytes:
        data = self.engine.vst_get_state(
            track_id, self._resolve_vst_slot(track_id, slot))
        return data if isinstance(data, bytes) else b""

    def vst_set_state(self, track_id: int, slot: int, data: bytes) -> bool:
        return bool(self.engine.vst_set_state(
            track_id, self._resolve_vst_slot(track_id, slot), data))

    def capture_vst_states(self, project: Project) -> None:
        """Pull VST plugin state from the engine into the model."""
        for track in project.tracks:
            if track.is_midi and track.instrument.vst_path:
                state = self.vst_get_state(track.id, -1)
                if state:
                    track.instrument.vst_state = state
            for model_slot, fx in enumerate(track.effects):
                if fx.type != "vst":
                    continue
                if self.resolve_fx_slot(track.id, model_slot) is None:
                    continue
                state = self.vst_get_state(track.id, model_slot)
                if state:
                    fx.state = state
        for model_slot, fx in enumerate(project.master_effects):
            if fx.type != "vst":
                continue
            if self.resolve_fx_slot(MASTER_FX_ID, model_slot) is None:
                continue
            state = self.vst_get_state(MASTER_FX_ID, model_slot)
            if state:
                fx.state = state

    # -- mixer params ------------------------------------------------------

    def set_gain(self, track_id: int, gain: float) -> None:
        self.engine.set_gain(track_id, gain)

    def set_pan(self, track_id: int, pan: float) -> None:
        self.engine.set_pan(track_id, pan)

    def set_stereo_width(self, track_id: int, width: float) -> None:
        self.engine.set_stereo_width(track_id,
                                     max(0.0, min(2.0, float(width))))

    def set_mute(self, track_id: int, mute: bool) -> None:
        self.engine.set_mute(track_id, mute)

    def set_solo(self, track_id: int, solo: bool) -> None:
        self.engine.set_solo(track_id, solo)

    def set_track_group(self, track_id: int, group_id: int) -> None:
        self.engine.set_track_group(track_id, group_id)

    def set_master_gain(self, gain: float) -> None:
        self.engine.set_master_gain(gain)

    def set_tempo(self, bpm: float) -> None:
        self.engine.set_tempo(bpm)

    def set_metronome(self, on: bool) -> None:
        self.engine.set_metronome(on)

    def sync_loop(self, project: Project) -> None:
        """Push the loop region (stored in beats) as engine frames."""
        self.engine.set_loop(
            project.beats_to_frames(project.loop_start_beat),
            project.beats_to_frames(project.loop_end_beat),
            bool(project.loop_enabled
                 and project.loop_end_beat > project.loop_start_beat))

    def sync_project(self, project: Project) -> None:
        """Full resync: push the whole project into the engine (after load
        or after a BPM change, which moves all MIDI clips in frames)."""
        self.last_sync_errors.clear()
        self._clear_fx_maps()
        self.engine.clear_all()
        # Wait until ClearAll is applied and old VST instances are torn
        # down before createVstPlugin runs on the UI thread.
        if hasattr(self.engine, "drain_commands"):
            self.engine.drain_commands()
        for track in project.tracks:
            self.add_track(track, project)
            for clip in track.clips:
                self.add_clip(track.id, clip)
            for mclip in track.midi_clips:
                self.add_midi_clip(project, track.id, mclip)
        self._apply_effects(MASTER_FX_ID, project.master_effects)
        for lane in project.master_automation_lanes:
            self.sync_master_automation_lane(project, lane)
        self.engine.set_master_gain(project.master_gain)
        self.engine.set_tempo(project.bpm)
        self.engine.set_metronome(project.metronome)
        self.sync_loop(project)
        if hasattr(self.engine, "drain_commands"):
            self.engine.drain_commands()

    def sync_midi(self, project: Project) -> None:
        """Re-push MIDI clip/note positions after a BPM change."""
        self.engine.set_tempo(project.bpm)
        for track in project.tracks:
            for clip in track.midi_clips:
                self.engine.remove_midi_clip(track.id, clip.id)
                self.add_midi_clip(project, track.id, clip)
            for lane in track.automation_lanes:
                self.sync_automation_lane(project, track, lane)
        for lane in project.master_automation_lanes:
            self.sync_master_automation_lane(project, lane)
        self.sync_loop(project)  # loop is in beats: frames moved too

    # -- transport --------------------------------------------------------

    def play(self) -> None:
        self.engine.play()

    def pause(self) -> None:
        self.engine.pause()

    def seek(self, frame: int) -> None:
        self.engine.seek(max(0, int(frame)))

    def is_playing(self) -> bool:
        return bool(self.engine.is_playing())

    def playhead_frame(self) -> int:
        return int(self.engine.playhead_frame())

    def master_peaks(self) -> tuple[float, float]:
        return tuple(self.engine.master_peaks())

    def track_peaks(self, track_id: int) -> tuple[float, float]:
        return tuple(self.engine.track_peaks(track_id))

    def export_wav(self, path: str, start_frame: int = 0,
                   end_frame: int = -1) -> bool:
        return bool(self.engine.export_wav(path, start_frame, end_frame))

    def export_progress(self) -> float:
        """0..1 progress of the export currently running (1 = idle/done)."""
        try:
            return float(self.engine.export_progress())
        except AttributeError:  # older compiled engine
            return 1.0

    def cancel_export(self) -> None:
        try:
            self.engine.cancel_export()
        except AttributeError:
            pass

    def export_was_cancelled(self) -> bool:
        try:
            return bool(self.engine.export_was_cancelled())
        except AttributeError:
            return False

    # -- performance / latency compensation --------------------------------

    def set_pdc_enabled(self, on: bool) -> None:
        """Toggle plugin delay compensation (on by default)."""
        try:
            self.engine.set_pdc_enabled(bool(on))
        except AttributeError:
            pass

    def set_pdc_mode(self, mode: int) -> None:
        """PDC strategy: 0 = look-ahead (timeline, default),
        1 = classic delay lines (future live-input monitoring)."""
        try:
            self.engine.set_pdc_mode(int(mode))
        except AttributeError:
            pass

    def pdc_mode(self) -> int:
        try:
            return int(self.engine.pdc_mode())
        except AttributeError:
            return 0

    def track_latency(self, track_id: int) -> int:
        """Current PDC offset of a track in engine frames."""
        try:
            return int(self.engine.track_latency(track_id))
        except AttributeError:
            return 0

    def mix_threads(self) -> int:
        """Render-pool worker threads (excluding the audio thread)."""
        try:
            return int(self.engine.mix_threads())
        except AttributeError:
            return 0
