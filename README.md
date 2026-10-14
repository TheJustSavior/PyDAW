# PyDAW

![Python](https://img.shields.io/badge/python-3.10+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey.svg)

Digital Audio Workstation с интерфейсом на Python (PySide6) и аудио-ядром на C++ (RtAudio + dr_libs, обёрнуто pybind11).

## 🎵 Возможности

- **Мультитрековый таймлайн**: drag&drop аудиофайлов (WAV/FLAC/MP3/AIFF) на дорожки, перемещение клипов, зум колесом мыши (Ctrl+колесо), клик по пустому месту — перемотка
- **Воспроизведение в реальном времени**: WASAPI, 48 кГц, микширование с gain/pan/mute/solo на дорожку
- **Мастер-секция**: мастер-громкость, лимитер, пик-метры
- **Волновые формы**: отображение waveform клипов (peak-кэш считается в C++)
- **Undo/Redo**: полная поддержка отмены операций с дорожками и клипами
- **Проекты**: сохранение/загрузка проектов (JSON, файлы `.pydaw`), экспорт микса в WAV
- **Расширяемость**: в ядре заложен интерфейс `EffectSlot` под будущий хостинг VST3

## 📦 Установка

### Требования

- Python 3.10+
- MSVC (Visual Studio Build Tools) на Windows
- CMake, Ninja

### Вариант 1: Установка из исходников

```powershell
# Клонируйте репозиторий
git clone https://github.com/your-username/PyDAW.git
cd PyDAW

# Установите зависимости для сборки
pip install cmake ninja pybind11 scikit-build-core

# Соберите и установите проект
pip install -e . --no-build-isolation
```

### Вариант 2: Standalone сборка (рекомендуется для пользователей)

Скачайте готовый исполняемый файл из раздела [Releases](https://github.com/your-username/PyDAW/releases):

1. Скачайте `PyDAW-vX.X.X-Windows-x64.zip`
2. Распакуйте архив в любую папку
3. Запустите `PyDAW.exe`

Установка Python не требуется.

### Вариант 3: Создание собственной standalone сборки

Для разработчиков, желающих создать автономную версию:

```powershell
pip install pyinstaller
py build_exe.py
```

Подробности в [BUILD_STANDALONE.md](BUILD_STANDALONE.md).

## 🚀 Запуск

Двойным кликом по `run.bat` в корне проекта, либо из консоли:

```powershell
.\run.bat        # или: py -m pydaw
```

## 🧪 Тесты

```powershell
python tests/test_engine.py
```

Тест без GUI: генерирует тоновые WAV, загружает их через ядро (с ресэмплингом), собирает проект из 2 дорожек, сохраняет/загружает его и экспортирует микс.

## 📁 Структура проекта

- `engine/src/` — C++ ядро: `audio_engine` (устройство, транспорт, очередь команд), `mixer` (дорожки, клипы, суммирование), `audio_file` (декодирование, ресэмплинг, пики)
- `pydaw/` — Python: `model/` (Project/Track/Clip, undo), `ui/` (таймлайн, дорожки, транспорт), `engine_bridge.py` (единственная точка доступа к ядру)
- `tests/` — тесты ядра и функциональности

Подробности архитектуры — в [PLAN.md](PLAN.md).

## 📝 Лицензия

MIT License. См. [LICENSE](LICENSE).

## 🤝 Вклад в проект

Pull requests приветствуются! Для больших изменений сначала откройте issue для обсуждения.

## 📸 Скриншоты

![Main Window](screens/1.png)
*Главное окно с мультитрековым таймлайном*

![Timeline and Mixer](screens/2.png)
*Таймлайн с клипами*

![Playback](screens/3.png)
*Таймлайн с примером автоматизации*
