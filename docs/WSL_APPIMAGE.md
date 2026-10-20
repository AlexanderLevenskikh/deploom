# Запуск DepLoom в WSL через AppImage

Если проект требует Linux-окружения, запускайте Linux-версию DepLoom в той же Ubuntu WSL. WSLg покажет окно на рабочем столе Windows, а Git, Python, Node и агент будут работать внутри Linux. Для выполнения Windows-инструментами используйте [Windows-версию с WSL-папками](WINDOWS_WSL.md).

## 1. Проверка WSL

В PowerShell:

```powershell
wsl --list --verbose
```

У Ubuntu должна быть версия WSL `2`. Если указана `1`, выполните (имя дистрибутива должно совпадать с выводом команды выше):

```powershell
wsl --set-version Ubuntu 2
```

Для графических приложений требуется Windows 11 или Windows 10 build 19044+ и WSLg. При необходимости обновите WSL из PowerShell администратора:

```powershell
wsl --update
```

Сохраните работу во всех дистрибутивах WSL и перезапустите WSL:

```powershell
wsl --shutdown
```

Снова откройте Ubuntu. Подробнее: [графические приложения в WSL — Microsoft](https://learn.microsoft.com/en-us/windows/wsl/tutorials/gui-apps).

## 2. Установка пакетов

Далее все команды выполняются **в терминале Ubuntu**. Посмотрите версию Ubuntu:

```bash
cat /etc/os-release
```

Для **Ubuntu 24.04**:

```bash
sudo apt update
sudo apt install -y git python3 libnspr4 libnss3 libasound2t64
```

Для **Ubuntu 22.04**:

```bash
sudo apt update
sudo apt install -y git python3 libnspr4 libnss3 libasound2
```

`libnspr4` устраняет ошибку отсутствия `libnspr4.so`, а `libasound2t64` / `libasound2` предоставляет `libasound.so.2`. Имена ALSA-пакетов различаются между версиями Ubuntu: [24.04](https://packages.ubuntu.com/noble/libasound2t64), [22.04](https://packages.ubuntu.com/jammy/libasound2).

## 3. Скачивание AppImage

На [странице релизов DepLoom](https://github.com/AlexanderLevenskikh/deploom/releases) скачайте Linux x64 файл:

```text
DepLoom-<версия>-x64.AppImage
```

В Ubuntu создайте папку и откройте её в Проводнике:

```bash
mkdir -p ~/Applications/deploom
cd ~/Applications/deploom
explorer.exe .
```

Перенесите скачанный файл в эту папку и переименуйте его в `DepLoom.AppImage`.

## 4. Запуск

В терминале Ubuntu:

```bash
cd ~/Applications/deploom
chmod +x DepLoom.AppImage
./DepLoom.AppImage --appimage-extract-and-run
```

Параметр `--appimage-extract-and-run` распаковывает приложение перед запуском и позволяет обойтись без настройки FUSE. Подробнее: [документация AppImage](https://docs.appimage.org/user-guide/troubleshooting/fuse.html#extract-and-run-type-2-appimages).

Для следующих запусков достаточно:

```bash
cd ~/Applications/deploom
./DepLoom.AppImage --appimage-extract-and-run
```

## 5. Подключение проекта

В открывшемся DepLoom создайте workspace внутри Ubuntu и добавьте папку проекта с `package.json`. Укажите **Linux-путь**, например:

```text
/home/your-user/dev/your-project
```

Путь вида `\\wsl.localhost\Ubuntu\home\...` используется для доступа из Windows. В Linux-приложении выбирайте путь `/home/...`.

Проверьте инструменты в терминале Ubuntu:

```bash
command -v git python3 node npm
node --version
cd /home/your-user/dev/your-project
git rev-parse --show-toplevel
```

Замените пример пути своим. Git должен вывести корень репозитория. Node, пакетный менеджер проекта и выбранный агент CLI должны быть установлены внутри той же Ubuntu; пути к ним не должны вести к Windows-программам в `/mnt/c/...`. Используйте версию Node, требуемую проектом, и авторизуйте агент в Ubuntu.

После добавления проекта запустите **проверку окружения** в DepLoom. Дальнейшая работа описана в [руководстве пользователя](../USERGUIDE.md).
