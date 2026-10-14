# Одиннадцатый шаг: 0.1.1 — C++ Realtime Warp и фикс VST-транспорта

Версия программы после этого шага: **0.1.1** (`pyproject.toml` и
`pydaw.__version__`).

Итог этапа: **warp (stretch / stretch HD / resample) перенесён из Python
offline bake в C++ realtime-обработку внутри микшера**. Смена BPM, ручной
Ctrl+edge warp и изменение warp-метода больше не требуют блокирующего
перерендеринга каждого клипа на UI thread. Также исправлены основные
краши/зависшие ноты VST/MIDI вокруг stop/seek/BPM/loop/shutdown и рассинхрон
`ProcessContext` у VST3-плагинов.

Текущий полный прогон после Mouse/Select tool icon fix:
`py -m pytest tests/ -q` → **64 passed, 1 skipped**. Skipped: real VST
integration test, который запускается только с `PYDAW_RUN_REAL_VST_TESTS=1`.
Ранее он дополнительно проверен отдельно через
`$env:PYDAW_RUN_REAL_VST_TESTS='1'; py -m pytest
tests/test_automation.py::test_vst_automation -q -s`: тест дошёл до scan,
не нашёл установленных VST3 effects и корректно завершился через guarded
skip-path (`1 passed`). Для предыдущего C++ fix выполнено
`cmake --build build\cp314-cp314-win_amd64 --target pydaw_engine pydaw_vst_host --config Release`;
свежие `pydaw_engine` и `pydaw_vst_host` собраны в `build/.../Release/`.
Текущие audio slice naming fix и Mouse/Select tool icon fix затрагивают
Python/UI только; дополнительная
проверка: `py -m pytest tests/test_gui_smoke.py -q` → **1 passed**.

---

## 1. Итог этапа

**Было:** при смене BPM Python на main thread синхронно рендерил каждый
warped audio clip (`numpy` + `soundfile` → `cache/stretch/*.wav` →
`load_file`). UI зависал на debounce/render path, а звук отставал от уже
обновлённой геометрии клипа.

**Стало:** движок играет **оригинальный** audio file. Параметры `warp` и
`stretch_ratio` задают realtime-маппинг timeline → source в
`Mixer::mixAudioTrack`.

```text
BPM / Ctrl+drag / clip properties
→ Python model
→ EngineBridge.set_clip_warp / add_clip(..., warp_method, stretch_ratio)
→ AudioEngine command queue
→ Clip.warpProc
→ Mixer::mixAudioTrack realtime path
```

Offline stretch остался не playback path, а вспомогательный путь для:

- `stretch_buffer` и DSP parity-тестов;
- `stretch_file_to_cache`;
- Ctrl+J join/cache bake.

---

## 2. Warp: фактическая архитектура

### Семантика координат

| Поле / режим | Пространство / поведение |
|--------------|---------------------------|
| `Clip.source_offset`, `Clip.length`, fades | timeline/output samples |
| Общий маппинг `rel → src` | `src = (source_offset + rel) / ratio` |
| `resample` | linear interpolation, pitch меняется |
| `stretch` | OLA, pitch-preserving основной режим |
| `stretch_hd` | phase vocoder, fallback на OLA для короткого source window |

Bounds заданы в `engine/src/warp/warp_constants.h`:

| Константа | Значение |
|-----------|----------|
| `kMinRatio` | `0.1f` |
| `kMaxRatio` | `10.0f` |

Методы кодируются как:

| Метод | Код | Реализация |
|-------|-----|------------|
| `stretch` | `0` | OLA |
| `stretch_hd` | `1` | phase vocoder |
| `resample` | `2` | linear/tape resample |

### Playback/export path

- Realtime playback вызывает `mixer_.mix(...)`, где warped clip рендерится в
  block scratch buffer и затем смешивается в track buffer.
- Export WAV также использует `mixer_.mix(...)`, поэтому export получает тот
  же realtime warp path, а не старый Python bake.
- Unity ratio использует быстрый direct-copy path в микшере; non-unity ratio
  идёт через `WarpProcessor::processBlock`.

---

## 3. C++ реализация warp

Основные файлы `engine/src/warp/`:

| Файл | Назначение |
|------|------------|
| `warp_constants.h` | `WarpMethod`, clamp ratio, method code mapping |
| `warp_resample.*` | offline + realtime block linear resample |
| `warp_ola.*` | offline OLA + `StreamingOla` |
| `warp_vocoder.*` | offline phase vocoder + `StreamingVocoder` |
| `warp_fft.*` | internal real FFT для vocoder |
| `warp_processor.*` | фасад над resample/OLA/vocoder |
| `warp_offline.*` | `stretchOffline`, `stretchFileToCache`, cache WAV |
| `warp_math.h` | `M_PI` fallback + общий `makeHanning` (OLA/vocoder) |
| `warp_windows_fix.h` | MSVC/Windows macro cleanup для `interface`, `In`, `Out`, `win` |

### `WarpProcessor`

`WarpProcessor` выбирает реализацию по `WarpMethod`:

- `Resample` или unity ratio → sample-level direct/resample path;
- `Stretch` → `StreamingOla`;
- `StretchHd` → `StreamingVocoder`, с OLA fallback для короткого материала.

Для OLA/vocoder source base считается как `source_offset / ratio`, а затем
streaming hop positions двигаются с `ana_hop = syn_hop / ratio`. Это
математически соответствует общему timeline-to-source mapping, но из-за
hop-alignment требует отдельного тестового покрытия на trimmed/non-hop-aligned
cases.

### `StreamingOla`

Реализовано:

- adaptive window для OLA (`4096`, минимум `256`);
- `syn_hop = win / 4`, `ana_hop = syn_hop / ratio`;
- stereo interleaved output buffer + normalization buffer;
- `resetToOutputPos(outPos)` / `resetToTimelineRel(rel)` без resynth от 0;
- bounds fix для stereo buffer: capacity `(outLimit + nWin) * 2`;
- `runUntil` ограничен `outLimit`, чтобы hop не писал за `outLimit + nWin`.

### `StreamingVocoder`

Реализовано:

- fixed window `2048`, hop `512`;
- phase accumulator/previous phase per channel;
- OLA accumulation как `outGrain * window`, normalization `window²`;
- fallback на `StreamingOla`, если source короче `nWin * 2`;
- reset на arbitrary output-relative position.

### Offline/cache API

`warp_offline.*` оставляет совместимый offline path:

- `stretchOffline(...)` для `stretch_buffer` и тестов;
- `stretchFileToCache(...)` для Python `stretched_wav_path(...)` и join;
- unity ratio возвращает исходный path;
- cache key учитывает source file signature, method, ratio и sample rate.

### Windows/MSVC исправления по ходу порта

- `warp_vocoder.cpp`: исправлен most vexing parse (`resize` вместо
  `std::vector<float> freqScale(size_t(nBins))`).
- `warp_offline.cpp`: убран второй `DR_WAV_IMPLEMENTATION`, так как реализация
  уже находится в `audio_file.cpp`.
- `bindings.cpp`: `gil_scoped_release` используется только вокруг DSP, не при
  формировании Python return value.

---

## 4. Интеграция в микшер и engine API

### `Clip` state

В `engine/src/mixer.h` audio clip теперь хранит realtime warp state:

- `warp`;
- `stretchRatio`;
- `warpProc`;
- `lastMixRel`;
- `lastMixPlayhead`.

`Clip::syncWarpProcessor()` конфигурирует processor по текущим `warp`,
`stretchRatio` и timeline length, затем инвалидирует continuity.

### `Mixer::mixAudioTrack`

- При `ratio ≈ 1` используется быстрый 1:1 direct path.
- При warped playback блок рендерится через `WarpProcessor::processBlock` в
  scratch buffer.
- Fades применяются **после warp** в timeline space.
- При discontinuity/seek/loop/set-warp вызывается reset processor state на
  текущий timeline-relative `rel`.

### Commands/API/bindings

Реализованы:

- `AudioEngine::addClip(..., warpMethod, stretchRatio)`;
- `AudioEngine::setClipWarp(trackId, clipId, warpMethod, stretchRatio)`;
- command `SetClipWarp`;
- warp params в `AddClip`;
- `TrimClip` пересинхронизирует processor после изменения geometry;
- pybind11 bindings: `add_clip`, `set_clip_warp`, `stretch_buffer`,
  `stretch_file_to_cache`, `warp_method_code`.

Continuity invalidation вызывается на Stop, Seek, MoveClip и loop-wrap.

---

## 5. Python/UI интеграция

| Было | Стало |
|------|-------|
| `_pending_warp_renders` + QTimer/debounce | playback render path удалён |
| `_rerender_clip_audio` после BPM | `_sync_clip_warp` → engine `set_clip_warp` |
| `clip.pending_src` | удалено из модели/path |
| stretched WAV как playback source | engine играет original file |

### Bridge

`pydaw/engine_bridge.py`:

- `add_clip(...)` конвертирует `clip.warp` через `warp_method_code` и передаёт
  `warp_method`, `stretch_ratio` в C++;
- `set_clip_warp(...)` обновляет realtime engine state;
- `trim_clip(...)` после geometry update дополнительно синхронизирует warp.

### BPM rewarp

`pydaw/ui/main_window.py`:

- `_apply_bpm(...)` вызывает `_rewarp_audio_clips_for_bpm(...)`, только если
  BPM изменился и Snap включён;
- `clip.auto_warp=False` исключает clip из BPM rewarp;
- `start_frame`, `length`, `source_offset`, fades и `stretch_ratio`
  масштабируются через frames-per-beat scale;
- звук обновляется через realtime `set_clip_warp`, без Python render.

### Ctrl+edge stretch

`pydaw/ui/clip_item.py` и `main_window.py`:

- во время drag меняется preview geometry;
- на release создаётся undoable command с новым `(start, source_offset,
  length, fade_in, fade_out, stretch_ratio)`;
- `_sync_clip_warp(...)` отправляет новое состояние в engine.

### Waveform

Waveform остаётся lightweight preview по peaks оригинального file:

```text
src_off = int(clip.source_offset / ratio)
src_len = int(clip.length / ratio)
```

То есть waveform показывает source window, растянутый на timeline width, но не
является точной визуализацией результата OLA/vocoder.

### Warp default

- `audio_stretch.NEW_CLIP_WARP = "resample"`.
- File → Import audio и drag-and-drop на timeline создают новые audio clips с
  `warp="resample"`.
- Модельный fallback для старых `.pydaw` без поля `warp` остаётся
  `warp="stretch"`.
- Split/duplicate/paste наследуют состояние клипа; join создаёт baked/unity
  clip (`warp=NEW_CLIP_WARP`, `stretch_ratio=1.0`, `auto_warp=False`), чтобы
  результат Ctrl+J не re-warp-ился при последующих BPM changes.

### UI tools рядом со Slice

Добавлены явные инструменты редактирования в главный toolbar и piano roll:

- `Mouse` — обычный текущий режим редактирования, выбран по умолчанию при
  каждом запуске окна; кнопка квадратная icon-only с cursor arrow icon;
- `Select` — инструмент выделения рамкой; кнопка квадратная icon-only с
  тонким центрированным пунктирным квадратом;
- `Slice` остаётся существующим инструментом разрезания.

Горячие клавиши:

- `1` — Mouse;
- `2` — Select;
- `C` — Slice, `Esc` — выход из Slice.

Главное окно и каждое окно piano roll держат независимое состояние
инструментов. Переключение `1/2` в piano roll не меняет инструмент таймлайна,
а `1/2` в главном окне не трогает открытые piano roll окна.

Поведение Select:

- на таймлайне Select drag выделяет audio/MIDI clips пунктирной рамкой без
  seek/move;
- в piano roll Select использует уже существующее rubber-band выделение нот без
  необходимости держать `Ctrl`; рамка также рисуется пунктиром в accent-стиле;
- старый `Ctrl+drag` rubber-band в piano roll сохранён как совместимый путь.

Обновлены help/shortcut dialogs и RU i18n строки для новых кнопок и shortcuts.

---

## 6. Join/cache/offline stretch

`pydaw/audio_join.py` использует offline helper для warped clips:

```text
render_join_wav
→ _effective_source_path
→ audio_stretch.stretched_wav_path
→ pydaw_engine.stretch_file_to_cache
→ cache/stretch/*.wav
→ bake joined WAV in cache/join/*.wav
```

Join сохраняет gaps как silence, применяет fades, ресемплит source к sample
rate проекта и возвращает baked WAV. Playback этого joined clip дальше идёт
обычным engine path.

Joined clip явно создаётся как baked/unity artifact:
`warp=NEW_CLIP_WARP`, `stretch_ratio=1.0`, `auto_warp=False`. Поэтому результат
Ctrl+J не наследует model defaults и не re-warp-ится при последующих BPM changes
с включённым Snap.

---

## 7. Тестовое покрытие warp

`tests/test_warp.py` покрывает C++ offline DSP/bindings:

- `stretch_buffer` vs NumPy reference из `audio_stretch.reference_stretch_audio`;
- методы `stretch`, `stretch_hd`, `resample`;
- ratios `0.5`, `2.0`;
- ratio bounds;
- unity passthrough;
- `warp_method_code`.

`tests/test_gui_smoke.py` частично покрывает UI/model flow:

- BPM rewarp audio clips при Snap ON;
- Snap OFF и `auto_warp=False` не rewarp-ят audio clip;
- one-time grid fit для нового audio clip;
- join gaps as silence;
- reject overlapping join и allow adjacent/one-frame slop.
- toolbar tools: Mouse выбран по умолчанию, `1/2` переключают Mouse/Select,
  Mouse/Select кнопки квадратные icon-only, Select drag выделяет clips на
  таймлайне пунктирной рамкой, а piano-roll Select выделяет ноты локально и не
  меняет инструмент главного окна.

Direct realtime tests существуют в `tests/test_realtime_warp.py`
(`debug_process`, seek, `set_clip_warp`, invalid geometry, loop-wrap,
export-vs-realtime equivalence, left-trimmed resample exact-match и
non-hop-aligned seek для `stretch`/`stretch_hd`). `debug_process` теперь явно
бросает исключение, если audio stream запущен, чтобы не гонять mixer из двух
потоков.

Помимо перечисленных, в `tests/` есть и более старые suites:
`test_step10.py`, `test_performance.py`, `test_group_solo.py`,
`test_gui_smoke.py`, `test_automation.py`; `tests/test_engine.py` — не
pytest, а standalone интеграционный скрипт.

---

## 8. VST/MIDI transport fixes

### Симптом

После BPM change во время playback или seek + stop VST-инструменты могли
оставлять зависшие ноты. В логах VST transport UI было видно резкие скачки
позиции, иногда затем `0xC0000005` (`-1073741819`) в native-коде VST/engine.

### Причины

1. `RemoveMidiClip` при `sync_midi` не отправлял note-off в VST.
2. `Seek` не сбрасывал `lastMidiBlockEnd` и не гасил VST notes.
3. VST3 `ProcessContext.projectTimeMusic` мог прыгать из-за рассинхрона
   playhead/sample position.
4. Render pool мог вызвать VST processing с worker threads параллельно с UI
   controller/editor calls.
5. `eventMutex.try_lock` мог редко отбросить pending note-off из `allNotesOff`.

### Исправлено

- `VstPlugin::setTransport(bpm, playing, projectSamplePos)` обновляет transport
  context каждый block.
- `Mixer::resetTrackMidiTransport(...)` используется на Stop, Seek,
  RemoveMidiClip и Play.
- `RemoveMidiClip` с VST instrument отправляет note-off/all-notes-off path.
- `Mixer::mixMidiTrack(...)` различает discontinuity и note chase.
- При transport jump вызывается `onTransportJump()` до нового `setTransport`.
- VST tracks/groups исключены из render pool и обрабатываются на audio thread.
- `VstPlugin::processMutex_` сериализует `process()` и UI/controller/editor
  операции.
- Pending MIDI events в `processBlock` берутся через blocking `lock_guard` на
  `eventMutex`.
- Silence при shutdown теперь защищён и внутри `VstPlugin` direct processing
  path: `shutdown()` выставляет `Impl::shuttingDown`, а `processWithEvents()` /
  `processBlock()` возвращают silence, если plugin уже tear-downится. Это
  покрывает future direct/plugin render paths, не только внешний
  `AudioEngine::process()` guard.

---

## 9. Loop/shutdown/project lifecycle fixes

### Loop-wrap

Исправлены краши на переходе `loopEnd → loopStart`:

- `continuousPos` для VST3 monotonic `continousTimeSamples` больше не прыгает
  назад вместе с project playhead;
- `projectTimeSamples` продолжает следовать timeline playhead;
- `Mixer::onLoopWrap(loopStart)` инвалидирует warp continuity, сбрасывает MIDI
  block state, отправляет VST transport jump/note-off и выставляет transport на
  `loopStart`;
- `Play` использует `resetTrackMidiTransport(..., killNotes=false)`, чтобы не
  ломать note chase.

### Warp crash fixes during playback

Исправлены AV/crash cases при смене warp-метода, loop-wrap и длинном
`stretch` clip:

- `StreamingOla::ensureCapacity` выделяет stereo interleaved buffer как
  `(outLimit + nWin) * 2`;
- `resetToOutputPos(outPos)` уважает аргумент и не пересинтезирует всё от 0;
- при `relStart != streamOutPos_` processor reset-ится на текущий `relStart`;
- `mixAudioTrack` reset-ит processor при invalidated continuity;
- OLA/vocoder `runUntil` ограничены `outLimit`, а sample index bound проверяет
  frame index, не raw float-buffer size.

### Save → New → Open project with VST

Исправлена гонка старых VST instances и повторного `createInstance`:

- `clear_all` переносит registry plugins в graveyard, а не уничтожает их
  немедленно;
- `drainCommands()` под `mixMutex_` применяет queue и flush-ит graveyard;
- `EngineBridge.sync_project(...)` вызывает `drain_commands()` после
  `clear_all` и после полного sync;
- `new_project` также дренирует команды после `clear_all`.

### Shutdown/exit with VST

Текущий shutdown path:

- `AudioEngine::shutdown()` выставляет `shuttingDown_`, останавливает playback,
  stop-ит stream, ждёт примерно один buffer, затем под `mixMutex_` закрывает
  VST editors, отправляет all-notes-off, применяет queued commands, очищает
  mixer/registry/graveyard и закрывает device;
- `AudioEngine::process()` отдаёт silence, если shutdown уже начался;
- `vstCloseEditor` становится no-op после shutdown;
- Python side: `EngineBridge.shutdown()` вызывает `engine.shutdown()` и ставит
  `_shutting_down`; `vst_close_editor` становится no-op;
- UI side: `VstEditorWindow.detach_editor()` и `_close_all_vst_editors()`
  detach/hide/delete native editor до `bridge.shutdown()`;
- `VstModule::unload()` release-ит factory, но намеренно не вызывает
  `ExitDll`/`FreeLibrary`, чтобы избежать падений в static destructors VST DLL;
- `app._hard_exit()` использует `ExitProcess(0)` на Windows, fallback —
  `os._exit(0)`.

---

## 10. Low-risk аудит после 0.1.1: исправлено и TODO

### Исправлено в hardening pass

- `RenderPool::shutdown()` больше не join-ит worker threads под внутренним
  `mutex_`, синхронизируется с `run()` через `runMutex_`, будит workers через
  `generation_` и очищает shared job state после join.
- `AudioEngine::shutdown()` явно останавливает `RenderPool` после stop stream и
  перед VST/mixer teardown.
- Producer-side доступ к SPSC `RingBuffer<Command>` сериализован через
  `commandMutex_`; `pushCommand()` также прекращает enqueue после начала
  shutdown.
- Public reads `trackPeaks()` и `trackLatency()` теперь защищают traversal
  `mixer_.tracks` через `mixMutex_`.
- `addClip()` clamp-ит `sourceOffset`/`length` по file frame count и не ставит
  zero-length clip commands; `TrimClip` в apply path игнорирует invalid/zero
  geometry.
- Follow-up fix: clamp для audio clip geometry теперь работает в timeline
  coordinates. Для warped clips максимальная длина считается как
  `round(fileFrames * stretchRatio) - sourceOffset`, а не как
  `fileFrames - sourceOffset`. Это исправляет silent tail, когда stretched clip
  визуально длится дольше source file, но engine раньше обрезал `Clip.length`
  до raw source frame count (практически проявлялось как потеря концовки на
  несколько тактов). `EngineBridge.trim_clip()` сначала синхронизирует текущий
  warp ratio, затем отправляет geometry, чтобы C++ `TrimClip` clamp-ил длину по
  актуальному `stretchRatio`.
- Clip properties теперь всегда синхронизирует engine при смене warp method,
  даже если `stretch_ratio == 1.0`: method change больше не отсекается как
  «неслышимый», поэтому model и realtime engine не расходятся.
- Joined audio clip теперь явно помечается как baked/unity artifact:
  `warp=NEW_CLIP_WARP`, `stretch_ratio=1.0`, `auto_warp=False`. После Ctrl+J
  join output больше не наследует model defaults и не re-warp-ится на BPM
  change при Snap ON.
- Audio split сохраняет warp state правой части (`warp`, `stretch_ratio`,
  `auto_warp`) вместе с `source_offset`, чтобы split warped clip не терял
  realtime mapping.
- Audio split теперь задаёт правой части имя на основе исходного audio clip с
  номерным суффиксом `_2`, `_3`, `_4` и далее. Если режется уже пронумерованная
  часть (`Loop_2`), базовое имя нормализуется обратно к `Loop`, а новый clip
  получает следующий свободный номер по clips текущего audio track.
- Waveform preview для warped clips вынесен в `waveform_source_window(...)`:
  source window считается по той же timeline-to-source формуле
  `src = (source_offset + rel) / stretch_ratio`. Для `stretch`/`stretch_hd`
  это осознанно остаётся lightweight preview по peaks оригинального файла, не
  offline render фактического OLA/vocoder output.
- Transport BPM spinbox завершает ручное редактирование при клике в любом
  другом месте UI: введённый текст явно парсится/commit-ится, затем focus
  снимается с editor, поэтому поле не остаётся в режиме правки после клика вне
  поля.
- Исправлены icon-only glyphs инструментов Mouse/Select в transport bar и
  piano roll: Mouse теперь рисуется как cursor arrow вместо mouse body, а
  Select — как более тонкий, чаще пунктирный и точно центрированный dashed
  square.
- `Mixer::mixAudioTrack()` использует reusable `Track::warpScratch` вместо
  локального scratch-вектора на каждый audio block.
- `VstComponentHandler` умеет `detach()` и не вызывает owner callbacks после
  teardown; `VstPlugFrame` защищает resize state mutex-ом.
- Real VST automation test переведён в opt-in режим через
  `PYDAW_RUN_REAL_VST_TESTS=1`, чтобы обычный pytest не падал на native VST
  scanner/plugin bugs.
- VST scanner вынесен в short-lived subprocess (`pydaw.vst_scan_subprocess`),
  поэтому crash/hang plugin DLL во время scan не завершает PyDAW.
- Добавлен `pydaw_vst_host` subprocess executable с line protocol
  (`load`, `param`, `set_param`, `state`, legacy `process`/`process_events`,
  shared-memory `process_shm` / `process_shm2` (второй несёт transport и
  preview-note state), `has_editor`, `open_editor`, `close_editor`,
  `editor_size`, `check_resize`, `editor_can_resize`, `resize_editor`,
  `set_content_scale`, `shutdown`) для runtime host isolation.
- Исправлен regression после первого subprocess isolation pass: прежний
  `VstBridgePlugin` был fail-safe заглушкой, не запускал `pydaw_vst_host`, не
  выполнял `load` и поэтому plugins фактически не грузились (instruments давали
  silence, effects bypass, editor window показывал fallback).
- По умолчанию `AudioEngine::addVstEffect()` / `setVstInstrument()` теперь
  создают subprocess-backed `VstBridgePlugin`: основной процесс не грузит VST
  DLL, bridge стартует `pydaw_vst_host.exe`, выполняет `load`, подтягивает
  name/param list/defaults/latency, прокидывает `set_param`, state roundtrip,
  audio blocks и MIDI events (`process_events`) в child process. Editor API
  также прокинут в child process: `hasEditor/openEditor/closeEditor`, размер,
  resize и content scale вызывают уже существующий `VstPlugin` editor path в
  `pydaw_vst_host`, а plugin view attach-ится к HWND Qt host widget из main UI.
  Missing path всё ещё возвращает `false`; при crash/no response bridge уходит
  в offline fail-safe (effects bypass, instruments silence). Legacy in-process
  режим сохранён через `PYDAW_VST_IN_PROCESS=1`.
- Исправлен protocol regression runtime bridge: `VstPlugin` пишет diagnostic
  trace в `stderr`, а bridge раньше объединял child `stderr` с protocol
  `stdout`, из-за чего строки `[vst] ...` могли приходить вместо `ok\t...` и
  воспринимались как `load`/`open_editor` failure. Теперь stderr child process
  уводится отдельно, protocol pipe читает только stdout.
- Добавлен user-facing `vst_diagnostic`: editor fallback показывает конкретную
  причину (`host exe not found`, `load failed`, `no native editor`,
  `open_editor failed`, путь к host exe), а не только общий текст
  `Plugin has no editor / failed to open`.
- Добавлен watchdog для pipe IPC с `pydaw_vst_host`: чтение protocol response
  больше не блокируется бесконечно. `has_editor` timeout = 1s,
  `open_editor` timeout = 1.5s, audio `process` timeout = 250ms. При timeout
  bridge помечает plugin offline, завершает hung child process и возвращает
  fallback/diagnostic вместо зависания UI thread.
- Исправлена причина обрывистого звука в subprocess mode: первоначальный runtime
  audio path гонял каждый stereo float block через tab-separated text protocol
  как hex payload (`process` / `process_events`). Это создавало лишние копии,
  аллокации и pipe-трафик прямо из audio callback. Теперь parent создаёт
  inheritable shared-memory audio buffer на 4096 frames, передаёт handle в
  `pydaw_vst_host --audio-map ...`, пишет samples в shared memory и отправляет
  только короткую команду `process_shm\tframes\tevents...`; child process
  обрабатывает buffer in-place и отвечает `ok`. Legacy text commands оставлены
  для probes/tests и как совместимый fallback protocol surface.
- Исправлена причина неоткрывающегося/неживого editor в subprocess mode.
  Проблем было три:
  1. `pydaw_vst_host` сидел в blocking `std::getline(std::cin, ...)` и не
     обслуживал Windows message queue; первый fix (pump между командами)
     оказался недостаточным — тяжёлые editors (Kontakt) требуют живой
     message pump **во время** `createView`/`attached`, а не только между
     stdin-командами.
  2. Timeout `open_editor` 1.5s был слишком мал для Kontakt-класса плагинов.
  3. Обновлённые бинарники собирались в `build/`, но приложение запускало
     stale `pydaw_vst_host.exe`/`pydaw_engine.pyd` из site-packages
     (диагностика в editor fallback показывала путь host exe из
     site-packages) — фиксы фактически не доезжали до runtime.
  Итоговая архитектура `pydaw_vst_host`:
  - main thread процесса — постоянный GUI message pump
    (`MsgWaitForMultipleObjects` + `PeekMessage`/`DispatchMessage`);
  - stdin protocol (включая realtime audio `process_shm`) работает на
    отдельном reader thread с обычным blocking `std::getline` (без
    Sleep-polling);
  - GUI-операции (`load`, `has_editor`, `open_editor`, `close_editor`,
    `editor_size`, `check_resize`, `editor_can_resize`, `resize_editor`,
    `set_content_scale`) marshal-ятся через `GuiDispatcher` на main thread
    и выполняются под живым message pump.
  Timeouts в bridge подняты под тяжёлые плагины: `load` 60s,
  `open_editor` 20s, `has_editor` 5s, `get_state`/`set_state` 15s;
  audio `process_shm` остаётся 250ms.
- Audio callback больше не блокируется на общем bridge state lock: parent-side
  `VstBridgePlugin` разделён на `stateMutex_` для cached state/params/diagnostic
  и `ipcMutex_` для `ChildProcess` pipe/shared-memory host access;
  `processWithEvents` берёт `try_to_lock` только на IPC path, а `loaded_` и
  latency cache переведены на atomics. Async load commit дополнительно
  перепроверяет generation под lock-ами перед `host_.swap(child)`, чтобы
  superseded load не подменял live host.
- Уменьшен jitter parent-side pipe read: вместо фиксированного `Sleep(2)`
  на каждый poll — короткий spin (`Sleep(0)`) в первые ~2ms, затем
  `Sleep(1)`; audio reply обычно приходит в пределах миллисекунды.
- Исправлен critical regression первого shm pass: `process_shm` отвечает
  голым `ok` (audio возвращается через shared memory, не через pipe), а
  bridge проверял `parts.size() < 2` как для старого hex-протокола и
  помечал plugin offline на **первом же audio block** → инструмент сразу
  уходил в silence, `hasEditor` возвращал false, editor window показывал
  прежний timeout-fallback.
- Исправлен cross-process editor attach deadlock: пока child делает
  `SetParent`/`CreateWindow` на HWND родительского Qt widget, Windows шлёт
  synchronous (sent) messages потоку-владельцу этого HWND. UI thread
  родителя в это время сидел в plain `Sleep` внутри `readLine` и не
  доставлял sent messages → child блокировался навсегда → timeout
  `open_editor`. Теперь poll loop в `readLine` делает
  `PeekMessageW(..., PM_NOREMOVE)`, который доставляет pending sent
  messages, не трогая posted queue.
- Deployment note: после rebuild необходимо обновлять
  `site-packages/pydaw/pydaw_engine*.pyd` и `pydaw_vst_host.exe`
  (`pip install .` или копирование из `build/.../Release/`), иначе
  приложение продолжает работать со старым host protocol.
- Исправлено некорректное отображение editor у части plugins (Diva и др.):
  GUI занимал только верхний левый угол host-окна на HiDPI. Причины:
  1. `pydaw_vst_host` не был DPI-aware, а Qt main process — aware → Windows
     виртуализировал cross-process child HWND;
  2. `IPlugView::getSize()` иногда расходился с фактическим размером child
     HWND после cross-process `attached()`;
  3. `VstEditorWindow` мог читать `devicePixelRatioF()==1.0` до полного
     map/show и завышал logical size host widget (физически 2× native view).
  Fix:
  - `SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)` в `pydaw_vst_host`
    (server + `--probe`);
  - после `attached()` размер editor берётся из `GetClientRect(child HWND)`
    когда child существует;
  - `_dpr()` fallback на `screen().devicePixelRatio()` + повторный layout
    после `QApplication.processEvents()` и re-read `vst_editor_size`.
- Zoom editor window вынесен в чистый модуль `pydaw/ui/vst_zoom.py`
  (`clamp_zoom`, `to_logical`/`to_physical`, `sizes_close`, `ZoomState`).
  `VstEditorWindow` использует `ZoomState` для step/base-size math; Qt hook
  и bridge calls остаются в UI. Добавлены unit tests `tests/test_vst_zoom.py`.

### 10.1 Audit/refactor pass (warp + VST + engine)

Отдельный проход по итогам аудита кода (warp DSP, VST bridge/host, audio
engine). Сборка + `py -m pytest tests/ -q` → 57 passed, 1 skipped.

**Warp:**

- Исправлен stereo-баг `StreamingVocoder`: флаг `havePhase_` был общим для
  каналов, из-за чего первый hop правого канала шёл по phase-vocoder ветке с
  неинициализированным `prevPhaseR` (лишний транзиент в R на старте/после
  reset). Теперь `havePhaseL_`/`havePhaseR_` per-channel.
- `makeHanning` дедуплицирован в `warp_math.h` (общий для OLA/vocoder,
  с guard `nWin >= 2`).
- `warpMethodFromName` перенесён в `warp_constants.h` — единый mapping для
  C++ и bindings.
- Удалён мёртвый API: `WarpProcessor::setMethod/setRatio/needsWarp`,
  `StreamingOla::setRatio`, `StreamingVocoder::setRatio`, write-only
  `StreamingOla::primed_` и `Clip::lastMixRel`.
- `int64_t(srcBase)` заменён на `llround` в OLA/vocoder hop (усечение
  сдвигало source window на сэмпл).
- В `stretchVocoderOffline` убран лишний зануляющий `out.assign` (результат
  полностью перезаписывается).
- Streaming OLA/vocoder больше не держат full-length accumulation buffers и
  не делают O(outLimit) clear на seek: `outBuf_`/`normBuf_` стали sliding window
  с `bufferOrigin_`, reset зануляет только рабочее окно вокруг текущего hop.
  Чтение результата идёт по `op - bufferOrigin_`, поэтому non-hop-aligned seek
  больше не читает из неправильных absolute индексов.

**VST subprocess:**

- Транспорт/preview MIDI доведены до subprocess: `setTransport`,
  `onTransportJump`, `allNotesOff`, `queuePreviewNoteOn/Off` в
  `VstBridgePlugin` больше не заглушки — состояние копится под лёгким
  `pendingMutex_` и уезжает в child вместе с audio-блоком новой командой
  `process_shm2\tframes\tbpm\tplaying\tpos\tjump\tallOff\tnEvents\t...`
  (без дополнительных pipe round-trips). Раньше stop/seek/BPM и piano-roll
  preview не доходили до subprocess-hosted инструментов вовсе.
- `GuiDispatcher` в `pydaw_vst_host`: exception в marshaled task больше не
  оставляет reader thread навсегда ждать `done_`.
- `ChildProcess::close()` после `TerminateProcess` делает
  `WaitForSingleObject` (нет зомби с живыми pipe/shm handles); удалён
  недостижимый `CloseHandle(childMapping)`.
- Все `std::stoi`/`std::stof` на pipe-ответах заменены на
  `parseIntField`/`parseFloatField` с fallback — malformed reply из child
  process больше не бросает исключение в audio/UI thread.
- Убран двойной `splitTabs(reply)` в `set_param` path.
- Исправлен kill тяжёлых плагинов (Kontakt) на audio timeout: раньше первый
  же `process_shm` без ответа за 250ms помечал plugin offline и terminate-ил
  `pydaw_vst_host` — Kontakt, стойлящий processing во время загрузки
  библиотеки или re-attach editor, убивался посреди загрузки («Plugin has no
  editor / failed to open», library не загружалась). Теперь timeout аудио
  блока — transient: bridge помнит, что один reply ещё «в долгу» на pipe
  (`audioAwaitingReply_`), пропускает блоки в fail-safe, дренирует stale
  reply перед следующей командой (FIFO) и убивает subprocess только после
  `kAudioStallKillMs = 10s` полного молчания.
- Асинхронная загрузка плагина: конструктор `VstBridgePlugin` больше не
  блокирует UI thread на старт subprocess + `load` (у Kontakt/Omnisphere —
  секунды; «подвисание» при добавлении второго VST). Загрузка идёт в
  background thread на локальном `ChildProcess`, результат коммитится под
  `mutex_` через `ChildProcess::swap`; до готовности плагин fail-safe
  (тишина/passthrough), `diagnostic()` показывает «loading...».
  `refreshParamsIfChanged()` возвращает true после коммита — UI-тик
  (`vst_refresh_changed_params`) перестраивает панели параметров.
  `setState()` во время загрузки сохраняется и доотправляется при коммите;
  generation counter защищает от гонки с sync-reload в `prepare()`
  (смена sample rate), деструктор join-ит load thread.
- Follow-up после async load (регрессии editor/TWEAK): `vst_is_loading()`
  в API; `VstEditorWindow` retry-ит `open_editor` каждые 200ms, пока
  subprocess грузится (до 60s); `MainWindow._defer_vst_editor_open`
  откладывает авто-открытие editor после добавления VST. TWEAK:
  `consume_tweak` теперь re-read-ит param из child (`refreshParamLocked`),
  `set_param` после успеха тоже re-read-ит value/default в parent cache;
  bridge `syncControllerParams()` шлёт `sync_controllers` в child (раньше
  no-op — native editor не получал programmatic reset); UI `_apply_vst_param`
  вызывает `vst_sync_controllers()` после каждого programmatic write. Следующий
  follow-up убрал старый snap-back TWEAK к default: после `Add lane` и
  `Cancel` touched параметр остаётся на пользовательском значении, а новая
  automation lane стартует с текущего значения параметра.
- Editor/progress follow-up: `_defer_vst_editor_open` больше не требует
  `vst_has_editor()` (probe мог вернуть false / timeout и окно не
  открывалось вовсе); окно editor открывается всегда и само retry-ит
  attach. `VstEditorWindow` ждёт реализации native HWND (`QTimer` 50ms),
  показывает «Loading…» пока `vst_is_loading`, retry-ит failed attach до
  15 раз. `MainWindow._with_vst_load_progress()` — modal indeterminate
  progress при добавлении/смене VST (instrument + effect): диалог
  открывается до `apply_fn`, polling ждёт
  `vst_is_loading` с коротким grace period (старый
  `_wait_vst_load` с ранним `if not loading: return` progress не
  показывал). Смена плагина: destructor
  вызывает `cancelActiveLoad()` (terminate in-flight child), graveyard не
  уничтожает ещё грузящиеся instances (`flushVstGraveyard` skip
  `isLoading()`), UI не блокируется на join старого load.
- Follow-up по VST load progress UI: общий progress helper теперь используется
  не только при добавлении/смене одного VST, но и при загрузке проекта/restore
  session/resync проекта с VST. Диалог появляется сразу, закрывается сразу
  после завершения загрузки последнего активного VST slot и не содержит
  нижнюю левую cancel-кнопку (`QProgressDialog.setCancelButton(None)`). Пока
  этот progress активен, `toggle_play()` игнорируется: если playback был
  остановлен, его нельзя запустить до конца VST-load; если playback уже шёл,
  аудио не останавливается и продолжает играть. Follow-up fix: во время
  playback при смене VST instrument progress теперь принудительно
  `show()`/`raise_()`/`repaint()`-ится до запуска apply path и в poll-loop;
  состояние loading проверяется не только через `vst_is_loading()`, но и через
  `vst_diagnostic()` со строкой `loading`, чтобы окно не закрывалось в короткое
  registry/commit окно до фактического окончания загрузки тяжёлого plugin.
- Follow-up по automation IPC: `VstBridgePlugin::setParamFromAudioThread`
  больше не шлёт `set_param` через общий pipe и не берёт `ipcMutex_` из audio
  callback. Automation values коалессятся в `pending_.params` под лёгким
  `pendingMutex_` и уезжают в child process вместе со следующим audio block в
  `process_shm2` (`... nEvents events... nParams params...`). Child применяет
  их через `VstPlugin::setParamFromAudioThread()` перед
  `processWithEvents()`, поэтому parameter changes попадают в тот же realtime
  processing block без отдельного IPC round-trip.
- Follow-up по async load teardown: in-flight load теперь управляется через
  `LoadControl` lifetime guard. `cancelActiveLoad()` помечает load cancelled и
  terminate-ит активный child process; destructor под `commitMutex` запрещает
  поздний commit в уже уничтожаемый object и detach-ит worker thread вместо
  join. `prepare()` перед sample-rate reload сначала отменяет старую загрузку,
  join-ит уже terminated worker и создаёт новый `LoadControl` для следующего
  host process.
- Follow-up по VST scan: default `EngineBridge.scan_vst_plugins()` уже работает
  через `pydaw.vst_scan_subprocess`, а test теперь monkeypatch-ем проверяет,
  что вызывается именно subprocess helper. Native
  `pydaw_engine.AudioEngine.scan_vst_plugins()` остаётся low-level in-process
  entrypoint для child scanner и explicit debug fallback
  `PYDAW_VST_SCAN_IN_PROCESS=1`.
- Follow-up по VST protocol helpers: общие `hexEncode`/`hexDecode`, `splitTabs`,
  `sanitizeField`, numeric parsers и `kSharedAudioFrames` вынесены в
  `engine/src/vst_protocol.h`. Parent-side `vst_bridge.cpp` и child-side
  `vst_host_cli.cpp` теперь используют один mapping/limits; numeric parsing в
  child protocol path переведён с throwing `std::stoi`/`std::stof`/`std::stoul`
  на fallback parsers, поэтому malformed command fields возвращают protocol
  error/fallback вместо исключения через reader loop.

**Audio engine:**

- `ClearAll` теперь сбрасывает `loopStart_`/`loopEnd_`/`loopOn_` (раньше
  после new project loop оставался включённым со старым регионом).
- `RenderPool`: job-исключения ловятся с гарантированным
  `pending_.fetch_sub` — worker exception больше не приводит к бесконечному
  spin audio thread в `run()`.
- `debug_process` бросает исключение при запущенном audio stream (раньше
  комментарий «only safe when not started» ничем не подкреплялся).
- `addMidiClip`/`ResizeMidiClip` clamp-ят отрицательную length.
- `AudioFile`: результат `drwav_read_pcm_frames_f32` проверяется; усечённый
  WAV урезается по факту прочитанного вместо мусора в хвосте.
- `pydaw/app.py`: убран недостижимый рекурсивный `sys.exit(main())` внутри
  `main()`; добавлен нормальный `if __name__ == "__main__"` блок.

### Новые тесты

- `tests/test_realtime_warp.py`:
  - realtime `debug_process` для warped clips;
  - seek/discontinuous render для warped clip;
  - `set_clip_warp()` после уже обработанных blocks;
  - export-vs-realtime equivalence для warped clips по audio data
    (`export_wav` vs `debug_process` на fresh engines);
  - loop-wrap warped audio clip: render после wrap совпадает с fresh render от
    loop start, playhead возвращается внутрь loop region;
  - left-trimmed resample warped clip exact-match по audio data: проверяет
    `source_offset` в timeline coords с формулой
    `src = (source_offset + rel) / stretch_ratio`;
  - non-hop-aligned seek для `stretch`/`stretch_hd` exact-match к fresh render;
  - stretched/resampled clip tail after raw source frame count remains audible
    (regression на `addClip`/`TrimClip` timeline-length clamp);
  - invalid clip geometry no-crash;
  - command producer / mixer read concurrency smoke.
  Текущий точечный прогон: `py -m pytest tests/test_realtime_warp.py -q` →
  **12 passed**.
- `tests/test_project_serialization.py`:
  - v8 roundtrip для warp/autowarp, VST state, master effects,
    MIDI source windows/fades и automation shapes;
  - relative sample paths;
  - legacy `channel_mode` migration;
  - invalid project format rejection.
- `tests/test_audio_join.py`:
  - gap-as-silence;
  - cache reuse;
  - source offset + fades;
  - sample-rate conversion;
  - mono/stereo join behavior.
- `tests/test_vst_scan_subprocess.py`:
  - missing dir scan через child process;
  - child crash/timeout не валит parent;
  - `EngineBridge` default subprocess scanner и in-process fallback.
- `tests/test_vst_process_isolation.py`:
  - missing VST path сохраняет прежнее `false` behavior;
  - default bridge остаётся fail-safe для invalid/fake plugin path;
  - state/param tweak сохраняются даже когда subprocess load не удался;
  - default bridge instrument renders silence без crash в offline/fake-plugin
    scenario.
- `tests/test_vst_zoom.py`:
  - clamp/step limits zoom;
  - logical/physical DPR conversions;
  - `sizes_close` tolerance scaling;
  - `ZoomState.init_from_open` для content-scale vs plain resize;
  - `target_size` fallback path.
- `tests/test_gui_smoke.py`:
  - квадратные icon-only Mouse/Select buttons рядом со Slice в transport bar и
    piano roll;
  - Mouse icon является cursor arrow, а Select icon — центрированным тонким
    frequent-dash square;
  - дефолтный Mouse tool при старте;
  - независимость tool state главного окна и piano roll;
  - Select rubber-band для clips на таймлайне и нот в piano roll;
  - TWEAK не возвращает touched control к default после `Add lane`/`Cancel`,
    а lane получает стартовую точку из текущего значения параметра;
  - смена warp method через clip properties вызывает engine sync даже при
    `stretch_ratio == 1.0`;
  - split audio clip наследует warp state в правой части;
  - joined audio clip создаётся как baked/unity artifact (`auto_warp=False`);
  - audio split/slice именует правые части как `Name_2`, `Name_3`, ...;
  - waveform source window использует timeline-to-source mapping;
  - ручное редактирование BPM commit-ится и завершается при клике вне поля;
  - VST load progress не имеет cancel-кнопки и блокирует старт playback из
    stopped-состояния, не отправляя pause/play toggles во время активного
    progress;
  - VST load progress видим уже внутри apply path и продолжает polling при
    активном playback, пока `vst_is_loading()` не завершит несколько циклов.

### Остаётся TODO

- Runtime VST bridge уже разделяет state/lifecycle lock и IPC lock
  (`stateMutex_`/`ipcMutex_`), а audio callback берёт `ipcMutex_` через
  short try-lock/fail-safe path. Но audio, state и editor команды всё ещё
  идут через один stdin/stdout FIFO в `pydaw_vst_host`: пока выполняется долгий
  `open_editor`/`get_state`/`set_state`, `process_shm2` не может обогнать эту
  команду, а audio временно уходит в fail-safe и потом дренирует stale reply.
  Для полного решения нужен отдельный realtime audio channel и control/editor
  channel или async/length-prefixed state/editor protocol.
- User-facing `EngineBridge.scan_vst_plugins()` уже изолирован через
  `pydaw.vst_scan_subprocess`, а in-process scanner доступен только как
  low-level native API и explicit debug fallback
  (`PYDAW_VST_SCAN_IN_PROCESS=1`). Остаточный риск: прямые debug/probe вызовы
  `pydaw_engine.AudioEngine.scan_vst_plugins()` всё ещё запускают native scan
  в своём процессе и могут падать на проблемных VST DLL.
- Legacy hex-команды `process`/`process_events` в `pydaw_vst_host`
  используются только для probes/tests; кандидат на удаление или `#ifdef`.
- `AudioEngine::process()` берёт `mixMutex_` на каждый callback; долгие
  UI-операции (export/drain/shutdown) могут блокировать realtime thread.
  Долгосрочно — snapshot/RCU для mixer state.
- `files_` cache в `AudioEngine` растёт без ограничения (нет LRU/unload);
  detached peak-builder threads в `audio_file.cpp` не лимитированы.
- `exportWav` рендерит линейно, без loop/metronome — экспорт может
  расходиться с тем, что слышно при включённом loop.
- Native VST editor для subprocess-hosted plugins теперь имеет MVP: child
  process создаёт plugin view и attach-ит его к HWND host widget из main UI.
  Нужно ещё ручное покрытие на разные plugins/HiDPI/resize/shutdown и, если
  окажется нестабильно для отдельных VST, вынести editor в самостоятельное
  top-level окно child process вместо embedding в parent HWND другого process.

### Предложения дальнейших улучшений

- Довести sliding buffers `StreamingOla`/`StreamingVocoder` до строгого bounded
  ring размера (`nWin + maxBlock`): full-length accumulation уже убран, но
  текущий sliding-window path всё ещё можно ужать и упростить по границам
  рабочего окна.
- Audio IPC уже идёт через shared memory (`process_shm2`); осталось довести
  до production: hard realtime deadlines, crash counters, автоматический
  restart упавшего host process и UI-status для disabled/offline plugins.
- Перевести `get_state`/`set_state` с hex-over-pipe на shared memory или
  length-prefixed binary (sampler states бывают десятки MB).
- Добавить CI/job для MSVC Debug + AddressSanitizer, а при наличии Linux build —
  ThreadSanitizer для command queue/render pool/VST registry races.
- Завести отдельный concurrency stress suite: command producers, clear/sync,
  export vs playback, shutdown while render pool active.
- В будущем сделать точную waveform-визуализацию rendered warp output как
  offline preview cache, отдельно от realtime playback path.

---

## 11. Файлы с наибольшими изменениями

**Новые / ключевые warp files:**

- `engine/src/warp/warp_constants.h`
- `engine/src/warp/warp_resample.*`
- `engine/src/warp/warp_ola.*`
- `engine/src/warp/warp_vocoder.*`
- `engine/src/warp/warp_fft.*`
- `engine/src/warp/warp_processor.*`
- `engine/src/warp/warp_offline.*`
- `engine/src/warp/warp_math.h`
- `engine/src/warp/warp_windows_fix.h`
- `tests/test_warp.py`
- `tests/test_realtime_warp.py`
- `tests/test_project_serialization.py`
- `tests/test_audio_join.py`

**C++ core:**

- `engine/src/mixer.*`
- `engine/src/audio_engine.*`
- `engine/src/vst_host.*`
- `engine/src/vst_bridge.*`
- `engine/src/vst_protocol.h`
- `engine/src/vst_host_cli.cpp`
- `engine/src/render_pool.*`
- `engine/src/bindings.cpp`
- `CMakeLists.txt`

**Python/UI:**

- `pydaw/audio_stretch.py`
- `pydaw/engine_bridge.py`
- `pydaw/vst_scan_subprocess.py`
- `pydaw/audio_join.py`
- `pydaw/model/project.py`
- `pydaw/ui/main_window.py`
- `pydaw/ui/clip_item.py`
- `pydaw/ui/vst_editor_window.py`
- `pydaw/app.py`
- `pydaw/ui/vst_zoom.py`
- `tests/test_automation.py`
- `tests/test_vst_scan_subprocess.py`
- `tests/test_vst_process_isolation.py`
- `tests/test_vst_zoom.py`

**Roadmap:**

- `roadmap/eleventh_step.md`

---

## 12. Ручной чеклист

### Уже покрыто автотестами или smoke-тестами

- [x] C++ offline `stretch_buffer` соответствует NumPy reference для
  `stretch` / `stretch_hd` / `resample` в основных ratios.
- [x] Ratio bounds и unity passthrough.
- [x] `warp_method_code` для трёх методов.
- [x] BPM change rewarp-ит audio clips при Snap ON.
- [x] Snap OFF и `auto_warp=False` не rewarp-ят audio clips.
- [x] Join сохраняет gaps как silence и reject-ит overlapping clips.
- [x] Realtime `debug_process` рендерит warped clips без NaN/Inf и с
  audible output.
- [x] Seek/discontinuous render для warped clip reset-ит streaming continuity.
- [x] `set_clip_warp()` после предыдущих realtime blocks остаётся bounded.
- [x] Export-vs-realtime equivalence для warped clips покрыт сравнением audio
  data (`export_wav` vs `debug_process` на fresh engines).
- [x] Loop-wrap with warped audio clip покрыт pytest: после wrap audio
  совпадает с fresh render от loop start, playhead возвращается внутрь loop.
- [x] Left-trimmed resample warped clip exact-match по audio data: проверяет
  единый timeline-to-source mapper для `source_offset`.
- [x] Joined baked audio clip получает unity-safe state (`stretch_ratio=1.0`,
  `auto_warp=False`) и split audio clip наследует warp state правой части.
- [x] Audio split/slice сохраняет базовое имя и назначает правым частям
  последовательные суффиксы `_2`, `_3`, ... .
- [x] Invalid audio clip geometry clamp-ится/игнорируется без crash.
- [x] Command producer / public mixer read concurrency smoke (встроен в
  хвост `test_invalid_clip_geometry_is_ignored_or_clamped`, не отдельная
  test-функция).
- [x] Project v8 serialization roundtrip для warp/autowarp/VST state/MIDI
  windows/master automation.
- [x] `audio_join.render_join_wav` покрывает cache reuse, gaps, fades,
  resample и mono/stereo behavior.
- [x] VST scan выполняется в subprocess; child crash/timeout не валит parent,
  а `EngineBridge.scan_vst_plugins()` unit-test проверяет именно subprocess
  helper path.
- [x] Default VST runtime path не грузит plugin DLL в main process и fail-safe
  bypass/silence покрыт тестами.
- [x] Subprocess-hosted VST automation не шлёт `set_param` IPC из audio
  callback: parameter updates батчатся в `process_shm2` вместе с audio block.
- [x] Async VST load teardown не ждёт heavy load: in-flight child terminate-ится,
  поздний commit блокируется `LoadControl`, worker detach-ится при destructor.
- [x] Mouse/Select tools рядом со Slice: дефолтный Mouse, shortcuts `1/2`,
  квадратные icon-only кнопки, локальное состояние главного окна и piano roll,
  Mouse cursor arrow icon, центрированный тонкий frequent-dash Select icon,
  пунктирное rubber-band выделение clips и notes.

### Требует ручной проверки перед релизом

- [ ] Snap ON: BPM 120→140 — clips сохраняют musical length, звук меняется
  мгновенно, UI не блокируется.
- [ ] Ctrl+edge stretch во время playback.
- [ ] Seek в середину warped clip — без click/pop и без CPU spike.
- [ ] `stretch_hd` на длинном tonal sample.
- [ ] Export WAV слышимо совпадает с realtime playback.
- [ ] Join (Ctrl+J) с warped clips.
- [ ] Stop после BPM change / seek — VST и built-in synth замолкают.
- [ ] VST с transport UI — нет краша при stop/seek/tempo spin.
- [ ] Смена warp-метода во время playback — без `0xC0000005`.
- [ ] Save → New → Open проекта с VST — без краша.
- [ ] Loop-wrap во время playback — без краша и зависших нот.
- [ ] Warp `stretch` на длинном audio clip — loop / смена метода без краша.
- [ ] Выход из программы с загруженным VST и открытым native editor — без
  error exit.
- [ ] Import / drag-and-drop — в свойствах нового clip `warp = Resample`
  (код задаёт `NEW_CLIP_WARP="resample"`, но прямого автотеста-assert нет).
- [ ] Piano-roll preview и stop/seek/BPM с subprocess-hosted VST instrument
  (новый `process_shm2` transport/notes path).

### Кандидаты на новые автотесты

- [ ] Shutdown while render pool is active with deterministic non-flaky harness.
- [ ] Hard deadlines / crash recovery UI для VST audio IPC через
  `pydaw_vst_host` (shared memory уже используется).
- [ ] Out-of-process native VST editor windows.
- [ ] Non-hop-aligned seek warped clip: exact-match против offline reference.
- [ ] Import/drag-and-drop assert `clip.warp == "resample"`.

Полный прогон:

```powershell
pip install -e . --no-build-isolation
py -m pytest tests/ -q          # 64 passed, 1 skipped
```

После rebuild движка бинарники нужно доводить до runtime-копии пакета
(`pip install .` или копирование `pydaw_engine*.pyd` и `pydaw_vst_host.exe`
из `build/.../Release/` в `site-packages/pydaw/`), иначе приложение и тесты
работают со старым protocol/DSP.

---

## 13. Что сознательно не делали в v1

- Rubber Band / SoundTouch — другой звук, новая зависимость и лицензия.
- SIMD/AVX оптимизации — после расширения realtime test coverage.
- Realtime warp для MIDI — только audio clips.
- Formant preservation и transient detection — out of scope.
- Точная waveform-визуализация результата OLA/vocoder — пока только scaled
  original peaks.
