"""Runtime UI localization (English / Russian).

Usage: ``from . import i18n`` and wrap user-facing strings in
``i18n.tr("...")``. Keys are the English strings; when the active
language is Russian, ``tr`` looks the key up in the RU dictionary and
falls back to the key itself when a translation is missing.

Language is read from ``ui/language`` in QSettings at startup (app.py).
Theme and UI scale still require a soft restart; language can be switched
live via ``MainWindow._retranslate_ui()`` after ``set_language()``.
"""

from __future__ import annotations

LANGUAGES = [("en", "English"), ("ru", "Русский")]

_lang = "en"


def system_default() -> str:
    """'ru' when the OS locale is Russian, otherwise 'en'."""
    try:
        from PySide6.QtCore import QLocale
        if QLocale.system().name().lower().startswith("ru"):
            return "ru"
    except Exception:  # noqa: BLE001 - locale detection is best-effort
        pass
    return "en"


def normalize(name: str | None) -> str:
    name = str(name or "").lower()
    return name if name in ("en", "ru") else system_default()


def set_language(name: str) -> None:
    global _lang
    _lang = "ru" if str(name).lower() == "ru" else "en"


def language() -> str:
    return _lang


def tr(text: str) -> str:
    """Translate an English UI string into the active language."""
    if _lang == "ru":
        return _RU.get(text, text)
    return text


def trf(text: str, **kwargs) -> str:
    """Translate ``text`` and apply ``str.format`` placeholders."""
    return tr(text).format(**kwargs)


_RU: dict[str, str] = {
    # -- menu bar --------------------------------------------------------
    "&File": "&Файл",
    "&Edit": "&Правка",
    "&Select": "&Выделение",
    "&Track": "&Дорожка",
    "&View": "Ви&д",
    "T&ransport": "&Транспорт",
    "&Help": "&Справка",
    # File
    "&New project": "&Новый проект",
    "&Open project...": "&Открыть проект...",
    "&Save project": "&Сохранить проект",
    "Save project &as...": "Сохранить проект &как...",
    "&Import audio file...": "&Импорт аудиофайла...",
    "&Export...": "&Экспорт...",
    "&Settings...": "Н&астройки...",
    "E&xit": "В&ыход",
    # Edit
    "&Undo": "&Отменить",
    "&Redo": "&Повторить",
    "Split at &playhead": "&Разрезать по курсору",
    "&Mouse tool": "Инструмент &мышь",
    "&Selection tool": "Инструмент &выделения",
    "&Slice tool": "Инструмент раз&резания",
    # Select
    "Select &all": "Выделить &всё",
    "&Deselect all": "&Снять выделение",
    "&Invert selection": "&Инвертировать выделение",
    "Select all on &track": "Выделить всё на &дорожке",
    # Track
    "&Add audio track": "Добавить &аудио-дорожку",
    "Add &MIDI track": "Добавить &MIDI-дорожку",
    "Add &group track": "Добавить &группу",
    "&Group selected tracks": "С&группировать выделенные дорожки",
    "&Rename track": "&Переименовать дорожку",
    # View
    "&Mixer": "&Микшер",
    "&FX panel": "Панель &эффектов",
    "Zoom &in": "При&близить",
    "Zoom &out": "От&далить",
    "Zoom to &fit project": "Показать весь прое&кт",
    # Transport
    "&Play/Pause": "&Играть/Пауза",
    "&Stop": "&Стоп",
    "&Metronome": "&Метроном",
    "&Loop": "&Луп",
    # Help
    "Keyboard &shortcuts": "Горячие &клавиши",

    # -- track panel context menu ----------------------------------------
    "Add audio track": "Добавить аудио-дорожку",
    "Add MIDI track": "Добавить MIDI-дорожку",
    "Add audio track (Ctrl+T)": "Добавить аудио-дорожку (Ctrl+T)",
    "Add MIDI track (Ctrl+Shift+T)": "Добавить MIDI-дорожку (Ctrl+Shift+T)",
    "Add group track (Ctrl+Shift+G)": "Добавить группу (Ctrl+Shift+G)",

    # -- settings dialog ---------------------------------------------------
    "Settings": "Настройки",
    "Appearance": "Внешний вид",
    "Audio": "Аудио",
    "Keyboard shortcuts": "Горячие клавиши",
    "Help": "Справка",
    "Theme": "Тема",
    "Dark": "Тёмная",
    "Light": "Светлая",
    "UI scale": "Масштаб интерфейса",
    "Language": "Язык",
    "Restart required": "Требуется перезапуск",
    "Show tooltips": "Показывать всплывающие подсказки",
    "Disables every tooltip in all program windows.\n"
    "Applies immediately.":
        "Отключает все всплывающие надписи (тултипы)\n"
        "во всех окнах программы. Применяется сразу.",
    "Audio API": "Audio API",
    "Output device": "Устройство вывода",
    "Sample rate": "Частота дискретизации",
    "Buffer size": "Размер буфера",
    "Applying restarts the audio engine;\n"
    "loaded files are resampled automatically.":
        "Применение перезапускает аудио-движок;\n"
        "загруженные файлы автоматически ресэмплируются.",
    "Action": "Действие",
    "Shortcut": "Сочетание",
    "New shortcut:": "Новое сочетание:",
    "Assign": "Назначить",
    "Reset all": "Сбросить всё",
    "Conflict": "Конфликт",
    "The shortcut \u00ab{seq}\u00bb is already assigned to \u00ab{other}\u00bb.":
        "Сочетание \u00ab{seq}\u00bb уже назначено действию \u00ab{other}\u00bb.",
    "There are duplicate shortcuts.\nFix them before saving.":
        "Есть дублирующиеся сочетания клавиш.\n"
        "Исправьте их перед сохранением.",

    # -- shortcuts reference dialog ---------------------------------------
    "Transport": "Транспорт",
    "Timeline": "Таймлайн",
    "Piano roll": "Piano roll",
    "Tracks": "Дорожки",
    "General": "Общие",
    "Selection": "Выделение",
    "Loop (with clips selected — region over them)":
        "Loop (клипы выделены — регион по ним)",
    "Select row of clips on a track": "Выделить ряд клипов на дорожке",
    "Add clip to selection": "Добавить клип к выделению",
    "Select all clips": "Выделить все клипы",
    "Deselect all": "Снять выделение",
    "Invert selection": "Инвертировать выделение",
    "Select all on track": "Выделить всё на дорожке",
    "Duplicate selection (as one block)":
        "Дублировать выделение (как один блок)",
    "Zoom to fit (selection or whole project)":
        "Zoom to fit (выделение или весь проект)",
    "Horizontal scroll": "Горизонтальная прокрутка",
    "Vertical scroll": "Вертикальная прокрутка",
    "Shift + wheel": "Shift + колесо",
    "Wheel / Alt + wheel": "Колесо / Alt + колесо",
    "Ctrl + wheel": "Ctrl + колесо",
    "MMB drag": "СКМ-перетаскивание",
    "Rubber-band select": "Выделение рамкой",
    "Mouse tool": "Инструмент мышь",
    "Selection tool": "Инструмент выделения",
    "Selection tool drag": "перетаскивание инструментом выделения",
    "Select tool / Ctrl + drag": "инструмент выделения / Ctrl + перетаскивание",
    "Ctrl + drag": "Ctrl + перетаскивание",
    "Resize without snap": "Резайз без привязки",
    "Ctrl + drag edge": "Ctrl + перетаскивание края",
    "Snap on/off": "Snap вкл/выкл",
    "Snap button in the piano roll window":
        "кнопка Snap в окне piano roll",
    "Full list": "Полный список",
    "Help ▾ in the piano roll window": "Help ▾ в окне piano roll",
    "Add audio track (under selected clip)":
        "Add audio track (под выделенным клипом)",
    "Add MIDI track (under selected clip)":
        "Add MIDI track (под выделенным клипом)",
    "Delete selected track": "Удалить выделенную дорожку",
    "Delete (when left header is selected)":
        "Delete (когда выделена шапка слева)",
    "Shift + click": "Shift + клик",
    "Ctrl + click / Shift + click": "Ctrl + клик / Shift + клик",
    "Hotkeys reference": "Справка по горячим клавишам",
    "Join clips on one lane": "Склеить клипы одной дорожки",
    "Rename selected clip": "Переименовать выделенный клип",
    "Rename track": "Переименовать дорожку",
    "Group selected tracks": "Сгруппировать выделенные дорожки",
    "Add group track": "Добавить группу",
    "Copy clips": "Копировать клипы",
    "Paste clips at playhead": "Вставить клипы на курсор",
    "Delete clip": "Удалить клип",
    "Delete notes": "Удалить ноты",
    "Exit slice tool": "Выйти из инструмента разрезания",
    "Jump to start": "В начало",
    "Jump to end": "В конец",
    "Slice tool": "Инструмент разрезания",
    "Split at playhead": "Разрезать по курсору",
    "New project": "Новый проект",
    "Open project": "Открыть проект",
    "Save project": "Сохранить проект",
    "Undo": "Отменить",
    "Redo": "Повторить",
    "Select all": "Выделить всё",
    "Copy": "Копировать",
    "Paste": "Вставить",
    "Duplicate": "Дублировать",
    "Play/Pause": "Играть/Пауза",
    "Stop": "Стоп",
    "Metronome": "Метроном",
    "Mixer": "Mixer",
    "Zoom in": "Приблизить",
    "Zoom out": "Отдалить",
    "Zoom": "Масштаб",
    "Pan": "Сдвиг вида",

    # -- tweak dialog -------------------------------------------------------
    "Add automation lane?": "Добавить полосу автоматизации?",
    "A lane for this parameter will appear under the track.":
        "Полоса этого параметра появится под дорожкой.",
    "Cancel": "Отмена",
    "Add lane": "Добавить полосу",

    # -- transport bar -----------------------------------------------------
    "Play/Pause (Space)": "Играть/Пауза (Space)",
    "Stop (Ctrl+.) — double-click rewinds to start":
        "Стоп (Ctrl+.) — двойной клик возвращает плейхед в начало",
    "Click to toggle time / bars display":
        "Клик — переключить время/такты",
    "Metronome (Ctrl+M)": "Метроном (Ctrl+M)",
    "Loop (Ctrl+L). Drag on the ruler to set the region":
        "Цикл (Ctrl+L). Регион задаётся перетаскиванием по линейке",
    "Auto-scroll the timeline with the playhead (Follow)":
        "Автопрокрутка таймлайна за курсором воспроизведения (Follow)",
    "Toggle snap while dragging clips and notes":
        "Включить/выключить привязку к сетке при перетаскивании",
    "Snap grid step": "Шаг привязки к сетке",
    "Mouse tool (1)": "Инструмент мышь (1)",
    "Selection tool (2)": "Инструмент выделения (2)",
    "Slice tool — click to cut a clip (C)":
        "Ножницы — разрез клипа по клику (C)",
    "TWEAK mode: adjust any knob or slider\n"
    "(built-in or VST3) — PyDAW offers to create\n"
    "an automation lane for that parameter":
        "Режим TWEAK: подёргайте любую крутилку или слайдер\n"
        "(встроенный или VST3) — PyDAW предложит создать\n"
        "automation lane этого параметра",
    "Mixer window (Ctrl+Shift+M)": "Окно микшера (Ctrl+Shift+M)",
    "Metro": "Metro",
    "Follow": "Follow",
    "\U0001f9f2 Snap": "\U0001f9f2 Snap",
    "Mouse": "Mouse",
    "Select": "Select",
    "\u2702 Slice": "\u2702 Slice",
    "Tweak": "Tweak",
    "1 bar": "1 такт",
    "1 beat": "1 доля",

    # -- mixer -------------------------------------------------------------
    "GROUP": "GROUP",
    "MASTER": "MASTER",
    "center": "центр",
    "Pan (RMB — reset)": "Панорама (ПКМ — reset)",
    "Volume (RMB — reset)": "Громкость (ПКМ — reset)",
    "Master volume (RMB — reset)": "Master volume (ПКМ — reset)",
    "Effect chain": "Цепочка эффектов",
    "Master effect chain": "Мастер-цепочка эффектов",
    "Mute": "Mute",
    "Solo": "Solo",

    # -- FX panel ----------------------------------------------------------
    "Select a track": "Выберите дорожку",
    "Instrument": "Instrument",
    "Remove instrument": "Убрать инструмент",
    "Select\u2026": "Select\u2026",
    "Choose a VST instrument": "Выбрать VST-инструмент",
    "Open VST instrument editor": "Открыть редактор VST-инструмента",
    "Drag to reorder effects": "Перетащите, чтобы изменить порядок эффектов",
    "Remove effect": "Убрать эффект",
    "Open plugin editor window": "Открыть окно редактора плагина",
    "Show plugin parameters": "Показать параметры плагина",
    "Plugin failed to load": "Не удалось загрузить плагин",
    "+ Add effect": "+ Add effect",
    "Add a built-in or VST effect": "Добавить встроенный или VST-эффект",
    "VST Plugin\u2026": "VST Plugin\u2026",
    "Params": "Params",
    "Editor": "Editor",
    "AUDIO": "AUDIO",
    "MIDI": "MIDI",

    # -- piano roll --------------------------------------------------------
    "Piano Roll — {track} / {clip}":
        "Piano Roll — {track} / {clip}",
    "Piano Roll shortcuts": "Горячие клавиши — Piano Roll",
    "Action": "Действие",
    "Snap to grid": "Привязка к сетке",
    "Slice notes (C — toggle, Esc — exit)":
        "Разрезать ноты (C — переключить, Esc — выйти)",
    "Help ▾": "Help ▾",
    "Hotkeys": "Горячие клавиши",
    "Create / paint notes": "Создать / рисовать ноты",
    "LMB": "ЛКМ",
    "Resize (right edge)": "Длительность (правый край)",
    "drag edge": "перетаскивание края",
    "Move/resize without snap": "Свободный сдвиг/ресайз без сетки",
    "Ctrl + drag note/edge": "Ctrl + перетаскивание ноты/края",
    "Delete note": "Удалить ноту",
    "RMB": "ПКМ",
    "Rubber-band select": "Резиновое выделение",
    "Ctrl + drag": "Ctrl + перетаскивание",
    "Add to selection": "Добавить к выделению",
    "Shift + click": "Shift + клик",
    "Nudge / transpose": "Сдвиг / транспон",
    "Arrows": "Стрелки",
    "Octave": "Октава",
    "Ctrl + Up/Down": "Ctrl + ↑↓",
    "Quantize": "Квантизация",
    "Select all / copy / paste / duplicate":
        "Выделить всё / копировать / вставить / дублировать",
    "Delete selected": "Удалить выделенные",
    "Slice tool": "Инструмент Slice",
    "Exit Slice": "Выйти из Slice",
    "Esc": "Esc",
    "Panorama": "Панорама",
    "MMB drag": "СКМ-перетаскивание",
    "drag in lane below": "перетаскивание в полосе внизу",
    "Velocity": "Velocity",
    "Snap": "Snap",
    "Zoom": "Зум",
    "C++ engine not built - running in silent UI mode":
        "C++-движок не собран — UI работает без звука",
    "No audio output device found": "Аудиоустройство вывода не найдено",
    "Saved audio device unavailable - using default output":
        "Сохранённое аудиоустройство недоступно — используется вывод "
        "по умолчанию",
    "Audio: {api}, {rate} Hz, buffer {buffer}":
        "Аудио: {api}, {rate} Гц, буфер {buffer}",
    "Join: select at least two clips on one lane":
        "Join: выделите минимум два клипа на одной дорожке",
    "Join: all selected clips must be on the same lane":
        "Join: все выделенные клипы должны быть на одной дорожке",
    "Join: overlapping clips cannot be joined":
        "Join: перекрывающиеся клипы нельзя склеить",
    "Loading {name}...": "Загрузка {name}...",
    "Ready": "Готово",
    "Warp failed: {name}": "Warp failed: {name}",
    "Warp ({method}): {ratio}x": "Warp ({method}): {ratio}x",
    "Snap: auto-warped {count} clip(s) to the grid":
        "Snap: auto-warp {count} клип(ов) на сетку",
    "Some VST plugins failed to load:\n{errors}":
        "Не удалось загрузить некоторые VST-плагины:\n{errors}",
    "Saved {path}": "Сохранено {path}",
    "Export cancelled ({count} file(s) done)":
        "Экспорт отменён (готово {count} файлов)",
    "Exported {count} file(s): {first}...":
        "Экспортировано {count} файл(ов): {first}...",
    "{label} (RMB — reset)": "{label} (ПКМ — reset)",
    "Exporting...": "Экспорт...",
    "Export": "Экспорт",

    # -- track headers -----------------------------------------------------
    "Vol": "Vol",
    "Fold/unfold child tracks": "Свернуть/развернуть дочерние дорожки",
    "Ctrl+Shift+R — rename": "Ctrl+Shift+R — переименовать",
    "Fold/unfold automation lanes":
        "Свернуть/развернуть automation lanes",
    "Delete track": "Удалить дорожку",
    "Volume (RMB — reset)": "Громкость (ПКМ — reset)",
    "center": "центр",
    "Remove automation": "Удалить автоматизацию",
    "Move to group \u25b8": "Move to group \u25b8",
    "Remove from group": "Remove from group",
    "Add automation \u25b8": "Add automation \u25b8",
    "Stereo width": "Stereo width",
    "{label}: parameters\u2026": "{label}: параметры\u2026",
    "{name}: parameters\u2026": "{name}: параметры\u2026",
    "Rename\tCtrl+Shift+R": "Rename\tCtrl+Shift+R",
    "Properties\u2026": "Properties\u2026",
    "Master": "Master",

    # -- automation lane item ----------------------------------------------
    "click to add a point": "клик — добавить точку",
    "Delete point": "Удалить точку",
    "Curve to next point": "Кривая к следующей точке",
    "Linear": "Линейная",
    "Step": "Ступенчатая",
    "Exponential": "Экспоненциальная",
    "Bezier": "Безье",

    # -- export dialog -----------------------------------------------------
    "Export audio": "Экспорт аудио",
    "Formats": "Форматы",
    "32-bit float": "32-bit float",
    "24-bit PCM": "24-bit PCM",
    "16-bit PCM": "16-bit PCM",
    "24-bit": "24-bit",
    "16-bit": "16-bit",
    "320 kbps": "320 kbps",
    "256 kbps": "256 kbps",
    "192 kbps": "192 kbps",
    "128 kbps": "128 kbps",
    "Range": "Диапазон",
    "Full arrangement (to end of last clip)":
        "Вся аранжировка (до конца последнего клипа)",
    "Loop region": "Loop region",
    "No loop region — set one on the timeline ruler":
        "Loop region не задан — выделите его на линейке таймлайна",
    "Tracks (each — separate file)":
        "Дорожки (каждая — отдельный файл)",
    "Master (full mix)": "Master (весь микс)",
    "Files: «name.ext» for Master, "
    "«name_track.ext» for other tracks.":
        "Файлы: «имя.расширение» для Master, "
        "«имя_дорожка.расширение» для остальных.",
    "Export...": "Экспорт...",
    "FLAC/MP3 export requires the soundfile package:\n"
    "pip install soundfile":
        "Для экспорта в FLAC/MP3 нужен пакет soundfile:\n"
        "pip install soundfile",
    "Installed libsndfile does not support MP3 — "
    "update soundfile (pip install -U soundfile).":
        "Установленная libsndfile не поддерживает MP3 —"
        " обновите пакет soundfile (pip install -U soundfile).",
    "Unknown export format: {fmt}": "Неизвестный формат экспорта: {fmt}",
    "Export: base file name": "Экспорт: базовое имя файлов",
    "Audio files (*.wav *.flac *.mp3)":
        "Аудиофайлы (*.wav *.flac *.mp3)",
    "Nothing to export yet.": "Пока нечего экспортировать.",
    "Done: {done}\nErrors:\n{errors}":
        "Готово: {done}\nОшибки:\n{errors}",

    # -- VST browser -------------------------------------------------------
    "VST Plugins": "VST Plugins",
    "VST Instruments": "VST Instruments",
    "Scan folders:": "Scan folders:",
    "Add folder\u2026": "Add folder\u2026",
    "Filter by name or vendor\u2026": "Filter by name or vendor\u2026",
    "All": "All",
    "Instruments": "Instruments",
    "Effects": "Effects",
    "Rescan": "Rescan",
    "Name": "Name",
    "Vendor": "Vendor",
    "Category": "Category",
    "Path": "Path",
    "cache": "cache",
    "scan": "scan",
    "{count} plugins ({source})": "{count} plugins ({source})",
    "Scanning VST3 plugins\u2026": "Scanning VST3 plugins\u2026",
    "Scan failed: {message}": "Scan failed: {message}",
    "VST3 folder": "VST3 folder",

    # -- file dialogs & message boxes --------------------------------------
    "Join clips": "Склеить клипы",
    "Could not render the joined audio:\n{err}":
        "Не удалось отрендерить склеенное аудио:\n{err}",
    "Rename clip": "Переименовать клип",
    "Name:": "Имя:",
    "Import audio": "Импорт аудио",
    "Import failed": "Ошибка импорта",
    "Empty audio file: {name}": "Пустой аудиофайл: {name}",
    "Cannot load {name}:\n{error}":
        "Не удалось загрузить {name}:\n{error}",
    "Warp failed": "Ошибка warp",
    "Cannot stretch {name}:\n{err}":
        "Не удалось растянуть {name}:\n{err}",
    "VST instrument": "VST-инструмент",
    "Could not load plugin:\n{name}":
        "Не удалось загрузить плагин:\n{name}",
    "VST effect": "VST-эффект",
    "Loading VST": "Загрузка VST",
    "Loading VST plugin:\n{name}":
        "Загрузка VST-плагина:\n{name}",
    "Could not open the audio device.":
        "Не удалось открыть аудиоустройство.",
    "Could not save the session for restart:\n{err}\n"
    "Theme/scale will apply after a manual restart.":
        "Не удалось сохранить сессию для перезапуска:\n{err}\n"
        "Тема/масштаб применятся после ручного перезапуска.",
    "Missing files": "Отсутствующие файлы",
    "Some audio files were not found:\n":
        "Некоторые аудиофайлы не найдены:\n",
    "VST plugins": "VST-плагины",
    "Save project": "Сохранить проект",
    "Save failed": "Ошибка сохранения",
    "Cannot create project folder:\n{err}":
        "Не удалось создать папку проекта:\n{err}",
    "Open project": "Открыть проект",
    "Open failed": "Ошибка открытия",
}
