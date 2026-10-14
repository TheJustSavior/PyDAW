"""Project model: tracks, clips, MIDI, JSON serialization.

The model is the source of truth for the UI; every mutation is mirrored
into the C++ engine through EngineBridge.

Positions: audio clips live in engine frames (samples). MIDI clips and
notes live in musical beats, so changing the project BPM keeps them
musically in place; conversion to frames happens in EngineBridge.
"""

from __future__ import annotations

import base64
import itertools
import json
from dataclasses import dataclass, field
from pathlib import Path

FORMAT_VERSION = 8

# Legacy (v7 and older) channel modes mapped onto the continuous
# stereo width: stereo = 100%, merged = 50%, mono = 0%.
LEGACY_CHANNEL_MODE_WIDTH = {"stereo": 1.0, "merged": 0.5, "mono": 0.0}

# Automation curve shapes: interpolation from a point towards the next
# one. Engine codes: 0 linear, 1 step, 2 exponential, 3 bezier.
AUTOMATION_SHAPES = ("linear", "step", "exp", "bezier")
AUTOMATION_SHAPE_CODES = {"linear": 0, "step": 1, "exp": 2, "bezier": 3}
AUTOMATION_SHAPE_LABELS = {"linear": "Linear", "step": "Step",
                           "exp": "Exponential", "bezier": "Bezier"}

_id_counter = itertools.count(1)


def _random_track_color() -> str:
    from ..ui import theme
    return theme.random_track_color()


def next_id() -> int:
    return next(_id_counter)


def bump_id_counter(minimum: int) -> None:
    """Ensure future ids are greater than `minimum` (after project load)."""
    global _id_counter
    _id_counter = itertools.count(minimum + 1)


@dataclass
class Clip:
    id: int
    file_path: str
    file_id: int          # id inside the engine file cache
    start_frame: int      # position on the timeline (engine samples)
    length: int           # length in samples
    source_offset: int = 0
    fade_in: int = 0      # linear fade lengths in samples
    fade_out: int = 0
    name: str = ""        # display name; empty = file basename
    # Warp: time-stretching of the source audio.
    # "stretch" (OLA, default) | "stretch_hd" (phase vocoder) | "resample"
    warp: str = "stretch"
    stretch_ratio: float = 1.0  # 1.0 = original speed; 2.0 = twice as long
    # Follow tempo: auto re-warp on BPM changes (only while Snap is on).
    auto_warp: bool = True

    @property
    def end_frame(self) -> int:
        return self.start_frame + self.length

    @property
    def display_name(self) -> str:
        if self.name:
            return self.name
        import os
        return os.path.splitext(os.path.basename(self.file_path))[0]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "file_path": self.file_path,
            "start_frame": self.start_frame,
            "length": self.length,
            "source_offset": self.source_offset,
            "fade_in": self.fade_in,
            "fade_out": self.fade_out,
            "name": self.name,
            "warp": self.warp,
            "stretch_ratio": self.stretch_ratio,
            "auto_warp": self.auto_warp,
        }


@dataclass
class MidiNote:
    id: int
    pitch: int                 # MIDI note number 0..127
    start_beat: float = 0.0    # relative to the clip start
    length_beats: float = 1.0
    velocity: float = 1.0

    @property
    def end_beat(self) -> float:
        return self.start_beat + self.length_beats

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "pitch": self.pitch,
            "start_beat": self.start_beat,
            "length_beats": self.length_beats,
            "velocity": self.velocity,
        }


@dataclass
class MidiClip:
    id: int
    name: str = "MIDI"
    start_beat: float = 0.0    # position on the timeline
    length_beats: float = 4.0
    source_start_beat: float = 0.0  # visible window into the note timeline
    fade_in_beats: float = 0.0    # velocity ramps at the clip edges
    fade_out_beats: float = 0.0
    notes: list[MidiNote] = field(default_factory=list)

    @property
    def end_beat(self) -> float:
        return self.start_beat + self.length_beats

    def note_by_id(self, note_id: int) -> MidiNote | None:
        for n in self.notes:
            if n.id == note_id:
                return n
        return None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "start_beat": self.start_beat,
            "length_beats": self.length_beats,
            "source_start_beat": self.source_start_beat,
            "fade_in_beats": self.fade_in_beats,
            "fade_out_beats": self.fade_out_beats,
            "notes": [n.to_dict() for n in self.notes],
        }


@dataclass
class Instrument:
    """Built-in synth settings (mirrors SynthParams in the engine).

    When ``vst_path`` is set the track uses a VST instrument instead of
    the built-in synth; preset fields are kept for switching back.
    """
    preset: str = "Saw Lead"
    wave: int = 2       # 0 sine, 1 square, 2 saw, 3 triangle
    attack: float = 0.005
    decay: float = 0.08
    sustain: float = 0.75
    release: float = 0.2
    gain: float = 0.5
    vst_path: str = ""
    vst_uid: str = ""
    vst_name: str = ""
    vst_state: bytes = b""

    @property
    def uses_vst(self) -> bool:
        return bool(self.vst_path)

    def to_dict(self) -> dict:
        d = {
            "preset": self.preset,
            "wave": self.wave,
            "attack": self.attack,
            "decay": self.decay,
            "sustain": self.sustain,
            "release": self.release,
            "gain": self.gain,
        }
        if self.vst_path:
            d["vst_path"] = self.vst_path
            d["vst_uid"] = self.vst_uid
            d["vst_name"] = self.vst_name
            if self.vst_state:
                d["vst_state"] = base64.b64encode(self.vst_state).decode("ascii")
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Instrument":
        inst = cls(**{k: d[k] for k in
                      ("preset", "wave", "attack", "decay", "sustain",
                       "release", "gain") if k in d})
        inst.vst_path = d.get("vst_path", "")
        inst.vst_uid = d.get("vst_uid", "")
        inst.vst_name = d.get("vst_name", "")
        if "vst_state" in d:
            inst.vst_state = base64.b64decode(d["vst_state"])
        return inst


# Ready-made instruments for MIDI tracks.
INSTRUMENT_PRESETS: dict[str, Instrument] = {
    "Saw Lead": Instrument("Saw Lead", wave=2, attack=0.005, decay=0.08,
                           sustain=0.75, release=0.2, gain=0.4),
    "Square Bass": Instrument("Square Bass", wave=1, attack=0.003, decay=0.12,
                              sustain=0.6, release=0.12, gain=0.45),
    "Soft Keys": Instrument("Soft Keys", wave=3, attack=0.002, decay=0.5,
                            sustain=0.25, release=0.35, gain=0.6),
    "Sine Pad": Instrument("Sine Pad", wave=0, attack=0.25, decay=0.3,
                           sustain=0.8, release=0.6, gain=0.55),
}


# Built-in effect descriptions: type -> (name, [(param label, min, max, default)]).
EFFECT_DEFS: dict[str, tuple[str, list[tuple[str, float, float, float]]]] = {
    "delay": ("Delay", [("Time (s)", 0.02, 1.9, 0.35),
                        ("Feedback", 0.0, 0.95, 0.35),
                        ("Mix", 0.0, 1.0, 0.3)]),
    "filter": ("Filter", [("Cutoff (Hz)", 40.0, 18000.0, 2000.0),
                          ("HP mode", 0.0, 1.0, 0.0)]),
}


@dataclass
class Effect:
    type: str
    params: list[float] = field(default_factory=list)
    path: str = ""
    uid: str = ""
    name: str = ""
    state: bytes = b""

    def __post_init__(self) -> None:
        if not self.params and self.type in EFFECT_DEFS:
            self.params = [p[3] for p in EFFECT_DEFS[self.type][1]]

    @property
    def display_name(self) -> str:
        if self.type == "vst":
            return self.name or self.path or "VST"
        if self.type in EFFECT_DEFS:
            return EFFECT_DEFS[self.type][0]
        return self.type

    def to_dict(self) -> dict:
        d: dict = {"type": self.type, "params": self.params}
        if self.type == "vst":
            d["path"] = self.path
            d["uid"] = self.uid
            d["name"] = self.name
            if self.state:
                d["state"] = base64.b64encode(self.state).decode("ascii")
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Effect":
        fx = cls(type=d["type"], params=list(d.get("params", [])))
        if fx.type == "vst":
            fx.path = d.get("path", "")
            fx.uid = d.get("uid", "")
            fx.name = d.get("name", "")
            if "state" in d:
                fx.state = base64.b64decode(d["state"])
        return fx


@dataclass
class AutomationPoint:
    beat: float
    value: float  # normalized 0..1
    shape: str = "linear"  # curve towards the next point
    curve_amount: float = 1.0  # signed intensity towards the next point

    def to_dict(self) -> dict:
        d = {"beat": self.beat, "value": self.value}
        if self.shape != "linear":
            d["shape"] = self.shape
        if abs(self.curve_amount - 1.0) > 1e-9:
            d["curve_amount"] = self.curve_amount
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "AutomationPoint":
        shape = d.get("shape", "linear")
        if shape not in AUTOMATION_SHAPES:
            shape = "linear"
        curve_amount = max(-1.0, min(1.0, float(d.get("curve_amount", 1.0))))
        return cls(beat=d.get("beat", 0.0), value=d.get("value", 0.0),
                   shape=shape, curve_amount=curve_amount)


@dataclass
class AutomationLane:
    id: int
    target: str  # "volume" | "pan" | "width" | "fx" | "inst"
    fx_slot: int = -1   # model effect index for "fx"
    param: int = 0      # param index for "fx" / "inst"
    name: str = ""      # display name, resolved at creation
    points: list[AutomationPoint] = field(default_factory=list)

    def value_at(self, beat: float) -> float | None:
        """Normalized lane value at `beat` (same interpolation as engine).

        Returns None when the lane has no points.
        """
        pts = sorted(self.points, key=lambda p: p.beat)
        if not pts:
            return None
        if beat <= pts[0].beat:
            return pts[0].value
        if beat >= pts[-1].beat:
            return pts[-1].value
        for a, b in zip(pts, pts[1:]):
            if a.beat <= beat < b.beat:
                if b.beat <= a.beat:
                    return a.value
                t = (beat - a.beat) / (b.beat - a.beat)
                amount = max(-1.0, min(1.0, a.curve_amount))
                if a.shape == "step":
                    shaped = 0.0 if amount >= 0.0 else 1.0
                    t += abs(amount) * (shaped - t)
                elif a.shape == "exp":
                    shaped = (t * t * t if amount >= 0.0
                              else 1.0 - (1.0 - t) ** 3)
                    t += abs(amount) * (shaped - t)
                elif a.shape == "bezier":
                    smooth = t * t * (3.0 - 2.0 * t)
                    shaped = smooth if amount >= 0.0 else 2.0 * t - smooth
                    t += abs(amount) * (shaped - t)
                return a.value + (b.value - a.value) * t
        return pts[-1].value

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "target": self.target,
            "fx_slot": self.fx_slot,
            "param": self.param,
            "name": self.name,
            "points": [p.to_dict() for p in self.points],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AutomationLane":
        lane = cls(
            id=d["id"],
            target=d.get("target", "volume"),
            fx_slot=d.get("fx_slot", -1),
            param=d.get("param", 0),
            name=d.get("name", ""),
        )
        lane.points = [AutomationPoint.from_dict(p)
                       for p in d.get("points", [])]
        return lane


@dataclass
class Track:
    """One timeline lane (audio, MIDI, or group bus).

    Invariant for group layout: child tracks of a group appear in
    ``project.tracks`` immediately after their group track, as one
    contiguous block (UI relies on this ordering).
    """
    id: int
    name: str = "Track"
    type: str = "audio"   # "audio" | "midi" | "group"
    group_id: int = 0     # 0 = not grouped; else id of the group track
    gain: float = 1.0
    pan: float = 0.0
    stereo_width: float = 1.0  # 0 = mono, 1 = stereo, 2 = extra wide
    mute: bool = False
    solo: bool = False
    color: str = field(default_factory=_random_track_color)
    clips: list[Clip] = field(default_factory=list)
    midi_clips: list[MidiClip] = field(default_factory=list)
    instrument: Instrument = field(default_factory=Instrument)
    effects: list[Effect] = field(default_factory=list)
    automation_lanes: list[AutomationLane] = field(default_factory=list)

    @property
    def is_midi(self) -> bool:
        return self.type == "midi"

    @property
    def is_group(self) -> bool:
        return self.type == "group"

    def clip_by_id(self, clip_id: int) -> Clip | None:
        for c in self.clips:
            if c.id == clip_id:
                return c
        return None

    def midi_clip_by_id(self, clip_id: int) -> MidiClip | None:
        for c in self.midi_clips:
            if c.id == clip_id:
                return c
        return None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "group_id": self.group_id,
            "gain": self.gain,
            "pan": self.pan,
            "stereo_width": self.stereo_width,
            "mute": self.mute,
            "solo": self.solo,
            "color": self.color,
            "clips": [c.to_dict() for c in self.clips],
            "midi_clips": [c.to_dict() for c in self.midi_clips],
            "instrument": self.instrument.to_dict(),
            "effects": [e.to_dict() for e in self.effects],
            "automation_lanes": [a.to_dict() for a in self.automation_lanes],
        }


@dataclass
class Project:
    name: str = "Untitled"
    sample_rate: int = 48000
    bpm: float = 120.0
    metronome: bool = False
    master_gain: float = 1.0
    # Loop region on the ruler, in beats (end <= start disables it).
    loop_start_beat: float = 0.0
    loop_end_beat: float = 0.0
    loop_enabled: bool = False
    tracks: list[Track] = field(default_factory=list)
    master_effects: list[Effect] = field(default_factory=list)
    master_automation_lanes: list[AutomationLane] = field(default_factory=list)
    file_path: str | None = None  # where the .pydaw project is saved

    def track_by_id(self, track_id: int) -> Track | None:
        for t in self.tracks:
            if t.id == track_id:
                return t
        return None

    def beats_to_frames(self, beats: float) -> int:
        return int(round(beats * 60.0 / self.bpm * self.sample_rate))

    def frames_to_beats(self, frames: int) -> float:
        return frames * self.bpm / (60.0 * self.sample_rate)

    @property
    def frames_per_beat(self) -> float:
        return 60.0 / self.bpm * self.sample_rate

    @property
    def total_frames(self) -> int:
        ends = [c.end_frame for t in self.tracks for c in t.clips]
        ends += [self.beats_to_frames(c.end_beat)
                 for t in self.tracks for c in t.midi_clips]
        return max(ends) if ends else 0

    # -- serialization -----------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "format": "pydaw-project",
            "version": FORMAT_VERSION,
            "name": self.name,
            "sample_rate": self.sample_rate,
            "bpm": self.bpm,
            "metronome": self.metronome,
            "master_gain": self.master_gain,
            "master_effects": [e.to_dict() for e in self.master_effects],
            "master_automation_lanes": [a.to_dict()
                                        for a in self.master_automation_lanes],
            "loop_start_beat": self.loop_start_beat,
            "loop_end_beat": self.loop_end_beat,
            "loop_enabled": self.loop_enabled,
            "tracks": [t.to_dict() for t in self.tracks],
        }

    def save(self, path: str | Path) -> None:
        path = Path(path)
        data = self.to_dict()
        # Store audio paths relative to the project folder when possible,
        # so the project folder (with its samples/) stays portable.
        base = path.parent.resolve()
        for td in data["tracks"]:
            for cd in td["clips"]:
                try:
                    rel = Path(cd["file_path"]).resolve().relative_to(base)
                    cd["file_path"] = rel.as_posix()
                except (ValueError, OSError):
                    pass
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        self.file_path = str(path)

    @classmethod
    def load(cls, path: str | Path) -> "Project":
        """Load project structure from JSON (v1..v6 formats).

        file_id fields are set to 0; the caller must re-register files
        with the engine and fill them in. Older formats omit group_id
        (defaults to 0).
        """
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("format") != "pydaw-project":
            raise ValueError("Not a PyDAW project file")

        project = cls(
            name=data.get("name", "Untitled"),
            sample_rate=data.get("sample_rate", 48000),
            bpm=data.get("bpm", 120.0),
            metronome=data.get("metronome", False),
            master_gain=data.get("master_gain", 1.0),
            loop_start_beat=data.get("loop_start_beat", 0.0),
            loop_end_beat=data.get("loop_end_beat", 0.0),
            loop_enabled=data.get("loop_enabled", False),
            file_path=str(path),
        )
        project.master_effects = [Effect.from_dict(e)
                                  for e in data.get("master_effects", [])]
        project.master_automation_lanes = [
            AutomationLane.from_dict(a)
            for a in data.get("master_automation_lanes", [])]
        base_dir = path.parent
        max_id = 0
        for lane in project.master_automation_lanes:
            max_id = max(max_id, lane.id)
        for td in data.get("tracks", []):
            track = Track(
                id=td["id"],
                name=td.get("name", "Track"),
                type=td.get("type", "audio"),
                group_id=td.get("group_id", 0),
                gain=td.get("gain", 1.0),
                pan=td.get("pan", 0.0),
                stereo_width=float(td.get(
                    "stereo_width",
                    LEGACY_CHANNEL_MODE_WIDTH.get(
                        td.get("channel_mode", "stereo"), 1.0))),
                mute=td.get("mute", False),
                solo=td.get("solo", False),
                color=td.get("color") or _random_track_color(),
                instrument=Instrument.from_dict(td.get("instrument", {})),
                effects=[Effect.from_dict(e) for e in td.get("effects", [])],
                automation_lanes=[AutomationLane.from_dict(a)
                                  for a in td.get("automation_lanes", [])],
            )
            max_id = max(max_id, track.id)
            for lane in track.automation_lanes:
                max_id = max(max_id, lane.id)
            for cd in td.get("clips", []):
                file_path = cd["file_path"]
                # Relative paths are resolved against the project folder.
                if file_path and not Path(file_path).is_absolute():
                    file_path = str(base_dir / file_path)
                clip = Clip(
                    id=cd["id"],
                    file_path=file_path,
                    file_id=0,
                    start_frame=cd["start_frame"],
                    length=cd["length"],
                    source_offset=cd.get("source_offset", 0),
                    fade_in=cd.get("fade_in", 0),
                    fade_out=cd.get("fade_out", 0),
                    name=cd.get("name", ""),
                    warp=cd.get("warp", "stretch"),
                    stretch_ratio=cd.get("stretch_ratio", 1.0),
                    auto_warp=cd.get("auto_warp", True),
                )
                max_id = max(max_id, clip.id)
                track.clips.append(clip)
            for cd in td.get("midi_clips", []):
                mclip = MidiClip(
                    id=cd["id"],
                    name=cd.get("name", "MIDI"),
                    start_beat=cd.get("start_beat", 0.0),
                    length_beats=cd.get("length_beats", 4.0),
                    source_start_beat=cd.get("source_start_beat", 0.0),
                    fade_in_beats=cd.get("fade_in_beats", 0.0),
                    fade_out_beats=cd.get("fade_out_beats", 0.0),
                )
                max_id = max(max_id, mclip.id)
                for nd in cd.get("notes", []):
                    note = MidiNote(
                        id=nd["id"],
                        pitch=nd["pitch"],
                        start_beat=nd.get("start_beat", 0.0),
                        length_beats=nd.get("length_beats", 1.0),
                        velocity=nd.get("velocity", 1.0),
                    )
                    max_id = max(max_id, note.id)
                    mclip.notes.append(note)
                track.midi_clips.append(mclip)
            project.tracks.append(track)
        bump_id_counter(max_id)
        return project
