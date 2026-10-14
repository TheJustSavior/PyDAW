# Инструкция по публикации PyDAW на GitHub

## Шаг 1: Подготовка репозитория

### 1.1. Создайте новый репозиторий на GitHub
1. Перейдите на https://github.com/new
2. Введите название репозитория: `PyDAW`
3. Добавьте описание: `Digital Audio Workstation with Python UI and C++ audio engine`
4. Выберите **Public** (публичный репозиторий)
5. **НЕ** ставьте галочки на "Add README", "Add .gitignore", "Add license" (они уже есть в проекте)
6. Нажмите **Create repository**

### 1.2. Инициализируйте Git в локальном проекте (если еще не сделано)
```powershell
cd F:\Dev\CursorProjects\PyDAW

# Инициализация репозитория
git init

# Добавьте все файлы
git add .

# Создайте первый коммит
git commit -m "Initial commit: PyDAW v0.1.1"
```

### 1.3. Привяжите локальный репозиторий к GitHub
```powershell
# Замените YOUR-USERNAME на ваше имя пользователя GitHub
git remote add origin https://github.com/YOUR-USERNAME/PyDAW.git

# Переименуйте ветку в main (если нужно)
git branch -M main

# Отправьте код на GitHub
git push -u origin main
```

## Шаг 2: Создание релиза со standalone сборкой

### 2.1. Создайте ZIP-архив с исполняемым файлом

**Важно:** Перед созданием архива убедитесь, что standalone сборка работает:
```powershell
# Запустите собранный исполняемый файл для проверки
.\dist_pyinstaller\PyDAW\PyDAW.exe
```

Если всё работает, создайте архив:
```powershell
# Перейдите в папку с собранным приложением
cd F:\Dev\CursorProjects\PyDAW\dist_pyinstaller

# Создайте ZIP-архив (через PowerShell)
Compress-Archive -Path "PyDAW" -DestinationPath "PyDAW-v0.1.1-Windows-x64.zip" -Force
```

### 2.2. Создайте релиз на GitHub
1. Перейдите в ваш репозиторий на GitHub
2. Нажмите на вкладку **Releases** (справа на странице репозитория)
3. Нажмите **Create a new release**
4. Заполните форму:
   - **Tag version**: `v0.1.1`
   - **Release title**: `PyDAW v0.1.1 - Initial Release`
   - **Description**: 
     ```
     ## Первый релиз PyDAW
     
     Digital Audio Workstation с интерфейсом на Python (PySide6) и аудио-ядром на C++.
     
     ### Возможности:
     - Мультитрековый таймлайн с drag&drop аудиофайлов
     - Воспроизведение и микширование в реальном времени (WASAPI, 48 кГц)
     - Волновые формы клипов
     - Undo/Redo операций
     - Сохранение/загрузка проектов (.pydaw)
     - Экспорт микса в WAV
     
     ### Установка:
     1. Скачайте `PyDAW-v0.1.1-Windows-x64.zip`
     2. Распакуйте архив
     3. Запустите `PyDAW.exe`
     
     Не требуется установка Python или других зависимостей.
     ```
5. Прикрепите файл: перетащите `PyDAW-v0.1.1-Windows-x64.zip` в область "Attach binaries"
6. Нажмите **Publish release**

## Шаг 3: Обновите README.md на GitHub

После публикации обновите ссылки в README:
1. Откройте `README.md` на GitHub (в веб-интерфейсе)
2. Нажмите кнопку редактирования (карандаш)
3. Замените `your-username` на ваше имя пользователя GitHub
4. Сохраните изменения

## Шаг 4: Добавьте скриншоты (опционально)

1. Создайте папку `screenshots` в корне проекта:
   ```powershell
   mkdir screenshots
   ```
2. Сделайте скриншоты интерфейса PyDAW и сохраните их в эту папку
3. Добавьте их в репозиторий:
   ```powershell
   git add screenshots/
   git commit -m "Add screenshots"
   git push
   ```
4. Обновите `README.md`, добавив в конце раздел:
   ```markdown
   ## Скриншоты
   
   ![Main Window](screenshots/main-window.png)
   ![Timeline](screenshots/timeline.png)
   ```

## Шаг 5: Настройте Topics и описание репозитория

1. На главной странице репозитория нажмите на шестеренку рядом с "About"
2. Добавьте описание: `Digital Audio Workstation with Python UI and C++ audio engine`
3. Добавьте topics (теги):
   - `daw`
   - `audio`
   - `music`
   - `python`
   - `cpp`
   - `pyside6`
   - `rtaudio`
   - `digital-audio-workstation`

## Шаг 6: Дальнейшие обновления

Когда будете вносить изменения:

```powershell
# Добавьте измененные файлы
git add .

# Создайте коммит с описанием изменений
git commit -m "Описание ваших изменений"

# Отправьте на GitHub
git push
```

Для создания новых релизов:
1. Обновите версию в `pyproject.toml`
2. Пересоберите standalone версию: `py build_exe.py`
3. Создайте новый ZIP-архив
4. Создайте новый release на GitHub с новым тегом (например, `v0.1.2`)

## Полезные команды Git

```powershell
# Проверить статус
git status

# Посмотреть историю коммитов
git log --oneline

# Отменить последние изменения (осторожно!)
git reset --hard HEAD

# Создать новую ветку для экспериментов
git checkout -b feature-name
```

## Готово!

Теперь ваш проект опубликован на GitHub и доступен другим разработчикам. Пользователи могут:
- Просматривать исходный код
- Скачивать готовый исполняемый файл из Releases
- Создавать Issues для сообщений об ошибках
- Отправлять Pull Requests с улучшениями
