# PyDAW — Digital Audio Workstation (Python UI + C++ ядро)

## Цель

DAW с интерфейсом на PySide6 (Qt) и производительным аудио-ядром на C++:
мультитрековый таймлайн, добавление аудиофайлов на дорожки,
воспроизведение/микширование в реальном времени. Хостинг VST3-плагинов
закладывается в архитектуру (слоты эффектов на дорожках), но реализуется
на позднем этапе.

## Архитектура

```
┌───────────────────────────── Python ─────────────────────────────┐
│  PySide6 GUI: таймлайн, дорожки, микшер, транспорт                │
│  Модель проекта: Project / Track / Clip (+ undo/redo, JSON)       │
│  Отрисовка волновых форм (peak-кэш, QPainter)                     │
└───────────────┬───────────────────────────────▲──────────────────┘
                │ команды (play/stop/add clip…) │ позиция, уровни
                ▼                               │ (lock-free, QTimer)
┌───────────────────────── C++ (pydaw_engine) ─────────────────────┐
│  Аудиопоток реального времени — RtAudio (WASAPI)                  │
│  Граф микширования: дорожки → gain/pan → мастер-шина → лимитер    │
│  Декодирование WAV/FLAC/MP3 (dr_libs) + ресэмплинг                │
│  Слоты эффектов — интерфейс под будущий VST3-хостинг              │
└───────────────────────────────────────────────────────────────────┘
```

Ключевой принцип: аудиопоток C++ никогда не вызывает Python. Обмен через
очередь команд (Python → ядро) и lock-free состояние (ядро → Python,
опрос таймером Qt ~60 Гц).

## Структура проекта

```
PyDAW/
├── PLAN.md
├── pyproject.toml           # scikit-build-core + pybind11
├── CMakeLists.txt
├── engine/                  # C++ ядро
│   ├── src/
│   │   ├── audio_engine.cpp/.h    # девайс, коллбек, транспорт
│   │   ├── mixer.cpp/.h           # граф дорожек, суммирование
│   │   ├── audio_file.cpp/.h      # загрузка/декодирование, ресэмплинг
│   │   ├── ring_buffer.h          # lock-free обмен
│   │   └── bindings.cpp           # pybind11
│   └── third_party/               # RtAudio, dr_libs
├── pydaw/                   # Python-пакет
│   ├── app.py               # входная точка (python -m pydaw.app)
│   ├── engine_bridge.py     # обёртка над pydaw_engine
│   ├── model/               # Project, Track, Clip + undo/redo
│   └── ui/                  # main_window, timeline, track_header,
│                            # clip_item, mixer, transport_bar, waveform
└── tests/
```

## Этапы

### Этап 1 — Каркас и сборка
- `pyproject.toml` (scikit-build-core), `CMakeLists.txt`, pybind11-модуль `pydaw_engine`, сборка на Windows (MSVC).
- Окно PySide6 с меню и панелями.

### Этап 2 — Аудио-ядро (C++)
- Инициализация устройства через RtAudio (WASAPI), аудио-коллбек.
- `AudioFile`: декодирование WAV/FLAC/MP3 (dr_wav/dr_flac/dr_mp3), ресэмплинг к частоте устройства, float32 в памяти.
- Транспорт: play/stop/seek, позиция в сэмплах, lock-free передача позиции в Python.

### Этап 3 — Дорожки и клипы
- `Mixer`: N дорожек, клипы (файл + смещение), gain, pan, mute/solo; суммирование на мастер-шину, мягкий лимитер.
- Команды из Python: add_track, add_clip, move_clip, set_gain и т.д. — через очередь команд без блокировок в аудиопотоке.

### Этап 4 — UI таймлайна
- `TimelineView` на QGraphicsScene: клипы с волновыми формами, drag&drop файлов, перемещение клипов, зум/скролл, курсор воспроизведения.
- Peak-кэш волновых форм из C++ (min/max по блокам), отрисовка QPainter.
- Заголовки дорожек: имя, mute/solo, фейдер, панорама. Транспорт-бар: play/stop, позиция.

### Этап 5 — Проект и полировка
- Сериализация проекта в JSON, undo/redo на командах.
- Экспорт микса в WAV (офлайн-рендер через то же ядро).

### Этап 6 (позже) — VST3
- VST3-хостинг (JUCE или VST3 SDK) поверх интерфейса `EffectSlot`, заложенного в ядре.

## Запуск

```powershell
pip install -e .          # соберёт C++ модуль (нужны MSVC; cmake/ninja ставятся из pip)
python -m pydaw.app
```
