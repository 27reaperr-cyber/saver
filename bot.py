"""
TikTok Downloader Telegram Bot
• Скачивает видео, аудио и изображения из TikTok без водяного знака
• Custom emoji через MessageEntity (entities-подход, без HTML-тегов)
• Автоустановка ffmpeg на ограниченных хостингах
Зависимости: aiogram 3.x, yt-dlp, python-dotenv
"""

import asyncio
import logging
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
)
from dotenv import load_dotenv

# ──────────────────────────────────────────────
# Загрузка .env
# ──────────────────────────────────────────────
load_dotenv()

# ──────────────────────────────────────────────
# Конфигурация из .env
# ──────────────────────────────────────────────
BOT_TOKEN: str        = os.getenv("BOT_TOKEN", "")
MAX_FILE_SIZE_MB: int = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
MAX_FILE_SIZE_BYTES   = MAX_FILE_SIZE_MB * 1024 * 1024
TEMP_DIR              = Path(os.getenv("TEMP_DIR", "temp"))
LOG_LEVEL: str        = os.getenv("LOG_LEVEL", "INFO").upper()

_FFMPEG_ENV  = os.getenv("FFMPEG_PATH", "")
FFMPEG_LOCAL = Path("bin") / ("ffmpeg.exe" if platform.system() == "Windows" else "ffmpeg")

if _FFMPEG_ENV:
    FFMPEG_PATH: str = _FFMPEG_ENV
elif shutil.which("ffmpeg"):
    FFMPEG_PATH = shutil.which("ffmpeg")  # type: ignore[assignment]
else:
    FFMPEG_PATH = str(FFMPEG_LOCAL)

if not BOT_TOKEN:
    sys.exit(
        "[FATAL] BOT_TOKEN не задан!\n"
        "Создай файл .env и добавь: BOT_TOKEN=ваш_токен_здесь"
    )

# ──────────────────────────────────────────────
# Логирование
# ──────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Рабочие директории
# ──────────────────────────────────────────────
TEMP_DIR.mkdir(exist_ok=True)
FFMPEG_LOCAL.parent.mkdir(exist_ok=True)


# ══════════════════════════════════════════════
# БЛОК 1: Автоустановка FFmpeg
#
# Скачивает статический бинарник без зависимостей
# в ./bin/ffmpeg — работает без root и apt.
#
# Платформы:
#   Linux  x86_64  — большинство VPS / Railway / Render
#   Linux  aarch64 — ARM (Oracle Free Tier)
#   Windows x64    — локальная разработка
# ══════════════════════════════════════════════

_FFMPEG_RELEASES: dict[str, dict[str, str]] = {
    "Linux": {
        "x86_64": (
            "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/"
            "latest/ffmpeg-master-latest-linux64-gpl.tar.xz"
        ),
        "aarch64": (
            "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/"
            "latest/ffmpeg-master-latest-linuxarm64-gpl.tar.xz"
        ),
    },
    "Windows": {
        "AMD64": (
            "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/"
            "latest/ffmpeg-master-latest-win64-gpl.zip"
        ),
    },
    "Darwin": {},  # macOS: brew install ffmpeg
}


def _dl_progress(blocks: int, block_size: int, total: int) -> None:
    if total > 0:
        pct = min(blocks * block_size * 100 // total, 100)
        mb = blocks * block_size / 1024 / 1024
        print(f"\r  [{pct:3d}%] {mb:.1f} МБ", end="", flush=True)


def install_ffmpeg() -> bool:
    """Скачивает ffmpeg в ./bin/ffmpeg. Возвращает True при успехе."""
    system  = platform.system()
    machine = platform.machine()
    url     = _FFMPEG_RELEASES.get(system, {}).get(machine)

    if not url:
        logger.warning(f"Автоустановка ffmpeg не поддерживается для {system}/{machine}")
        return False

    suffix       = ".zip" if system == "Windows" else ".tar.xz"
    archive_path = Path("bin") / f"ffmpeg_tmp{suffix}"

    logger.info(f"Скачиваю ffmpeg ({system}/{machine})...")
    try:
        urllib.request.urlretrieve(url, archive_path, reporthook=_dl_progress)
        print()

        if system == "Windows":
            with zipfile.ZipFile(archive_path, "r") as zf:
                for m in zf.namelist():
                    if m.endswith("/ffmpeg.exe") or m == "ffmpeg.exe":
                        FFMPEG_LOCAL.write_bytes(zf.read(m))
                        break
                else:
                    raise FileNotFoundError("ffmpeg.exe не найден в ZIP")
        else:
            import tarfile
            with tarfile.open(archive_path, "r:xz") as tf:
                for m in tf.getmembers():
                    if Path(m.name).name == "ffmpeg" and m.isfile():
                        f = tf.extractfile(m)
                        if f:
                            FFMPEG_LOCAL.write_bytes(f.read())
                        break
                else:
                    raise FileNotFoundError("ffmpeg не найден в TAR")
            FFMPEG_LOCAL.chmod(
                FFMPEG_LOCAL.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )

        r = subprocess.run([str(FFMPEG_LOCAL), "-version"], capture_output=True, timeout=15)
        if r.returncode != 0:
            raise RuntimeError("ffmpeg smoke-test не прошёл")

        logger.info(f"FFmpeg установлен: {FFMPEG_LOCAL.resolve()}")
        return True

    except Exception as e:
        logger.error(f"Ошибка установки ffmpeg: {e}")
        return False
    finally:
        if archive_path.exists():
            archive_path.unlink(missing_ok=True)


def ensure_ffmpeg() -> None:
    """
    Гарантирует наличие ffmpeg.
    Порядок: .env → PATH → ./bin/ffmpeg (кэш) → авто-загрузка → exit
    """
    global FFMPEG_PATH

    resolved = shutil.which(FFMPEG_PATH) or (
        FFMPEG_PATH if Path(FFMPEG_PATH).is_file() else None
    )
    if resolved:
        logger.info(f"FFmpeg: {resolved}")
        FFMPEG_PATH = resolved
        return

    if FFMPEG_LOCAL.is_file():
        logger.info(f"FFmpeg (локальный): {FFMPEG_LOCAL.resolve()}")
        FFMPEG_PATH = str(FFMPEG_LOCAL.resolve())
        return

    logger.warning("FFmpeg не найден — запускаю автоустановку...")
    if install_ffmpeg():
        FFMPEG_PATH = str(FFMPEG_LOCAL.resolve())
        return

    sys.exit(
        "\n[FATAL] FFmpeg не найден и не удалось установить.\n"
        "  Ubuntu/Debian : sudo apt install ffmpeg\n"
        "  macOS         : brew install ffmpeg\n"
        "  Вручную       : FFMPEG_PATH=/path/to/ffmpeg в .env\n"
    )


# ══════════════════════════════════════════════
# БЛОК 2: Построитель текста с entities
#
# Вместо HTML-тегов (<b>, <tg-emoji>) используем
# MessageEntity напрямую. Это самый надёжный способ
# по Bot API — не зависит от парсера разметки.
#
# Offset/length считаются в UTF-16 code units:
#   BMP-символы (ASCII, кирилл., базовые emoji) → 1 unit
#   Supplementary plane (большинство цветных emoji) → 2 units
# ══════════════════════════════════════════════

def _utf16(s: str) -> int:
    """Длина строки в UTF-16 code units (как считает Telegram)."""
    return len(s.encode("utf-16-le")) // 2


class Msg:
    """
    Построитель сообщений с поддержкой форматирования и custom emoji.

    Использование:
        m = Msg().emoji(CE_DONE).bold(" Готово!").nl().text("Вот файл")
        await message.answer(**m.build())
    """
    __slots__ = ("_text", "_entities")

    def __init__(self) -> None:
        self._text     = ""
        self._entities: list[MessageEntity] = []

    # ── Утилиты ────────────────────────────────
    def _offset(self) -> int:
        return _utf16(self._text)

    def _add(self, s: str, entity_type: str, **kwargs) -> "Msg":
        self._entities.append(
            MessageEntity(type=entity_type, offset=self._offset(), length=_utf16(s), **kwargs)
        )
        self._text += s
        return self

    # ── Форматирование ──────────────────────────
    def text(self, s: str) -> "Msg":
        """Обычный текст (без форматирования)."""
        self._text += s
        return self

    def bold(self, s: str) -> "Msg":
        return self._add(s, "bold")

    def italic(self, s: str) -> "Msg":
        return self._add(s, "italic")

    def code(self, s: str) -> "Msg":
        return self._add(s, "code")

    def nl(self, n: int = 1) -> "Msg":
        self._text += "\n" * n
        return self

    # ── Custom emoji ────────────────────────────
    def emoji(self, emoji_id: str, placeholder: str = "●") -> "Msg":
        """
        Вставляет custom emoji по ID.
        placeholder — любой одиночный символ/emoji:
          • виден пользователям без Premium как fallback
          • должен быть ровно один "character" (Unicode scalar)
        """
        return self._add(placeholder, "custom_emoji", custom_emoji_id=emoji_id)

    # ── Сборка ──────────────────────────────────
    def build(self) -> dict:
        """Возвращает kwargs для message.answer() / bot.send_message()."""
        return {
            "text":     self._text,
            "entities": self._entities or None,
        }

    def build_caption(self) -> dict:
        """Возвращает kwargs для caption в answer_video / answer_document."""
        return {
            "caption":          self._text,
            "caption_entities": self._entities or None,
        }


# ══════════════════════════════════════════════
# БЛОК 3: Custom Emoji IDs + сообщения бота
# ══════════════════════════════════════════════

# ID кастомных emoji (Premium animated stickers)
CE_START    = "6028346797368283073"  # TikTok / музыка
CE_DOWNLOAD = "5850346984501680054"  # загрузка / ожидание
CE_DONE     = "5774022692642492953"  # успех / галочка
CE_ERROR    = "5774077015388852135"  # ошибка / крест
CE_HELP     = "5938195768832692153"  # помощь / лампочка


def msg_start() -> dict:
    return (
        Msg()
        .emoji(CE_START, "🎵").bold(" Saver").nl(2)
        .text("Привет! Скинь ссылку прямо сюда, и получи видос ").bold("без водяного знака").text(".").nl(2)
        .bold("Как использовать:").nl()
        .text("Просто отправь ссылку на TikTok — выбери формат и получи файл!").nl(2)
        .text("Видео").text(" → отправлю ").bold(".mp4").nl()
        .text("Аудио").text(" → отправлю ").bold(".mp3").nl()
        .text("Карусель").text(" → отправлю ").bold(".zip").text(" архив с фото").nl(2)
        .text("Отправь /help для подробной инструкции.")
        .build()
    )


def msg_help(max_mb: int) -> dict:
    return (
        Msg()
        .emoji(CE_HELP, "💡").bold(" Инструкция по использованию").nl(2)
        .bold("Поддерживаемые форматы ссылок:").nl()
        .code("https://www.tiktok.com/@user/video/123...").nl()
        .code("https://vm.tiktok.com/XXXXXXX/").nl()
        .code("https://vt.tiktok.com/XXXXXXX/").nl(2)
        .bold("Что я умею:").nl()
        .text("Скачивать видео ").bold("без водяного знака").nl()
        .text("Извлекать аудио из видео (").bold("MP3").text(")").nl()
        .text("Скачивать карусели изображений (").bold("ZIP").text(")").nl(2)
        .bold("Ограничения:").nl()
        .text(f"Максимальный размер файла: ").bold(f"{max_mb} МБ").nl()
        .text("Приватные видео недоступны").nl(2)
        .bold("Команды:").nl()
        .code("/start").text(" — приветствие").nl()
        .code("/help").text(" — эта инструкция").nl(2)
        .text("Просто отправь ссылку — и готово!")
        .build()
    )


def msg_choose_format() -> dict:
    return (
        Msg()
        .emoji(CE_DOWNLOAD, "⏳").bold(" Ссылка получена!").nl(2)
        .text("Выбери что скачать:")
        .build()
    )


def msg_downloading(action: str) -> dict:
    label = "аудио" if action == "audio" else "контент"
    return (
        Msg()
        .emoji(CE_DOWNLOAD, "⏳").bold(f" Скачиваю {label}...").text(" Подожди немного.")
        .build()
    )


def msg_done_video() -> dict:
    return (
        Msg()
        .emoji(CE_DONE, "✅").bold(" Готово!").text(" Вот твоё видео")
        .build()
    )


def msg_done_audio() -> dict:
    return (
        Msg()
        .emoji(CE_DONE, "✅").bold(" Готово!").text(" Вот твоё аудио")
        .build()
    )


def msg_done_images() -> dict:
    return (
        Msg()
        .emoji(CE_DONE, "✅").bold(" Готово!").text(" Вот архив с изображениями")
        .build()
    )


def msg_not_tiktok() -> dict:
    return (
        Msg()
        .emoji(CE_ERROR, "❌").bold(" Неверная ссылка.").nl(2)
        .text("Я принимаю только ссылки TikTok.").nl()
        .text("Пример: ").code("https://vm.tiktok.com/XXXXXXX/")
        .build()
    )


def msg_too_large(max_mb: int) -> dict:
    return (
        Msg()
        .emoji(CE_ERROR, "❌").bold(" Файл слишком большой.").nl(2)
        .text(f"Максимальный размер: ").bold(f"{max_mb} МБ").text(".").nl()
        .text("Telegram не позволяет отправлять файлы большего размера.")
        .build()
    )


def msg_private() -> dict:
    return (
        Msg()
        .emoji(CE_ERROR, "❌").bold(" Недоступный контент.").nl(2)
        .text("Видео приватное или было удалено. Попробуй другую ссылку.")
        .build()
    )


def msg_error() -> dict:
    return (
        Msg()
        .emoji(CE_ERROR, "❌").bold(" Ошибка при скачивании.").nl(2)
        .text("Не удалось скачать контент. Возможные причины:").nl()
        .text("Неверная или устаревшая ссылка").nl()
        .text("TikTok временно недоступен").nl()
        .text("Приватное видео").nl(2)
        .text("Попробуй ещё раз или проверь ссылку.")
        .build()
    )


# ══════════════════════════════════════════════
# БЛОК 4: Inline-клавиатура и хранение ссылок
# ══════════════════════════════════════════════

# Временное хранилище: ключ → URL
# key = f"{user_id}:{message_id}" — уникален для каждого запроса
_url_cache: dict[str, str] = {}


def make_keyboard(key: str) -> InlineKeyboardMarkup:
    """Кнопки выбора формата для скачивания."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Video  .mp4",  callback_data=f"video:{key}"),
        InlineKeyboardButton(text="Audio  .mp3",  callback_data=f"audio:{key}"),
        InlineKeyboardButton(text="Cancel",       callback_data=f"cancel:{key}"),
    ]])


# ══════════════════════════════════════════════
# БЛОК 5: Валидация TikTok URL
# ══════════════════════════════════════════════

TIKTOK_RE = re.compile(
    r"https?://(www\.|vm\.|vt\.|m\.)?tiktok\.com/[\w@/\-?=&%.]+",
    re.IGNORECASE,
)


def is_tiktok(text: str) -> bool:
    return bool(TIKTOK_RE.search(text.strip()))


def extract_tiktok_url(text: str) -> str | None:
    m = TIKTOK_RE.search(text.strip())
    return m.group(0) if m else None


# ══════════════════════════════════════════════
# БЛОК 6: Очистка временных файлов
# ══════════════════════════════════════════════

def cleanup_dir(directory: Path) -> None:
    try:
        if directory.exists():
            shutil.rmtree(directory)
    except Exception as e:
        logger.warning(f"Не удалось удалить {directory}: {e}")


# ══════════════════════════════════════════════
# БЛОК 7: Скачивание через yt-dlp
# ══════════════════════════════════════════════

def _ffmpeg_dir() -> str:
    """Директория ffmpeg для yt-dlp."""
    return str(Path(FFMPEG_PATH).parent)


def get_video_opts(output_dir: Path) -> dict:
    """Настройки yt-dlp для видео без водяного знака."""
    return {
        "outtmpl": str(output_dir / "%(title).50s.%(ext)s"),
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
        "ffmpeg_location": _ffmpeg_dir(),
        "extractor_args": {
            "tiktok": {
                "api_hostname": ["api22-normal-c-alisg.tiktokv.com"],
                "app_version": ["26.1.3"],
            }
        },
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "writeinfojson": True,
    }


def get_audio_opts(output_dir: Path) -> dict:
    """Настройки yt-dlp для извлечения аудио → MP3 192kbps."""
    return {
        "outtmpl": str(output_dir / "%(title).50s.%(ext)s"),
        "format": "bestaudio/best",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "ffmpeg_location": _ffmpeg_dir(),
        "extractor_args": {
            "tiktok": {
                "api_hostname": ["api22-normal-c-alisg.tiktokv.com"],
                "app_version": ["26.1.3"],
            }
        },
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }


async def download_video(url: str, work_dir: Path) -> dict:
    """
    Скачивает видео или карусель изображений.

    Возвращает:
        { "type": "video"|"images", "files": [Path, ...], "title": str }
    """
    loop = asyncio.get_event_loop()

    def _run() -> dict:
        opts = get_video_opts(work_dir)

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise ValueError("Не удалось получить метаданные")

            title   = info.get("title", "tiktok")
            ext     = info.get("ext", "")
            entries = info.get("entries")

            is_carousel = (
                entries is not None
                or info.get("_type") == "playlist"
                or ext in ("", "none") and info.get("images")
            )

            if is_carousel and entries:
                # Скачиваем каждый слайд карусели отдельно
                files: list[Path] = []
                for i, entry in enumerate(entries):
                    entry_url = entry.get("url") or entry.get("webpage_url")
                    if not entry_url:
                        continue
                    sub_opts = {**opts, "outtmpl": str(work_dir / f"img_{i:03d}.%(ext)s")}
                    with yt_dlp.YoutubeDL(sub_opts) as sub:
                        sub.download([entry_url])
                    for f in sorted(work_dir.glob(f"img_{i:03d}.*")):
                        if not f.name.endswith(".json"):
                            files.append(f)
                            break
                return {"type": "images", "files": files, "title": title}

            # Обычное видео
            ydl.download([url])
            video_files = [
                f for f in sorted(work_dir.glob("*.mp4"))
                if not f.name.endswith(".json")
            ] or [
                f for f in sorted(work_dir.iterdir())
                if f.is_file() and f.suffix in (".mp4", ".webm", ".mkv", ".mov")
            ]
            if not video_files:
                raise FileNotFoundError("Видеофайл не найден")
            return {"type": "video", "files": [video_files[0]], "title": title}

    return await loop.run_in_executor(None, _run)


async def download_audio(url: str, work_dir: Path) -> dict:
    """
    Извлекает аудио из TikTok поста → MP3.

    Возвращает:
        { "type": "audio", "files": [Path], "title": str }
    """
    loop = asyncio.get_event_loop()

    def _run() -> dict:
        opts = get_audio_opts(work_dir)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = (info or {}).get("title", "tiktok_audio")
            ydl.download([url])

        mp3_files = sorted(work_dir.glob("*.mp3"))
        if not mp3_files:
            # Fallback: любой аудиофайл
            mp3_files = [
                f for f in sorted(work_dir.iterdir())
                if f.is_file() and f.suffix in (".mp3", ".m4a", ".aac", ".ogg", ".opus")
            ]
        if not mp3_files:
            raise FileNotFoundError("Аудиофайл не найден после скачивания")
        return {"type": "audio", "files": [mp3_files[0]], "title": title}

    return await loop.run_in_executor(None, _run)


def make_zip(files: list[Path], output: Path) -> Path:
    """Создаёт ZIP-архив из списка файлов."""
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f.name)
    return output


# ══════════════════════════════════════════════
# БЛОК 8: Инициализация бота
# ══════════════════════════════════════════════

# parse_mode НЕ задаём: форматирование идёт через entities
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties())
dp  = Dispatcher()


# ══════════════════════════════════════════════
# БЛОК 9: Хендлеры команд
# ══════════════════════════════════════════════

@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(**msg_start())
    logger.info(f"[{message.from_user.id}] /start")


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(**msg_help(MAX_FILE_SIZE_MB))


# ══════════════════════════════════════════════
# БЛОК 10: Хендлер входящих ссылок
# ══════════════════════════════════════════════

@dp.message(F.text)
async def handle_url(message: Message) -> None:
    """
    Получает TikTok-ссылку, сохраняет в кэш и
    показывает inline-клавиатуру выбора формата.
    """
    text = message.text.strip()

    if not is_tiktok(text):
        await message.answer(**msg_not_tiktok())
        return

    url = extract_tiktok_url(text)
    key = f"{message.from_user.id}:{message.message_id}"
    _url_cache[key] = url

    logger.info(f"[{message.from_user.id}] URL сохранён: {url}")
    await message.answer(**msg_choose_format(), reply_markup=make_keyboard(key))


# ══════════════════════════════════════════════
# БЛОК 11: Callback — обработка выбора формата
# ══════════════════════════════════════════════

async def _send_result(
    callback: CallbackQuery,
    action: str,
    url: str,
    work_dir: Path,
) -> None:
    """
    Скачивает и отправляет файл пользователю.
    Вся логика вынесена сюда, чтобы хендлер был чистым.
    """
    user_id = callback.from_user.id

    try:
        if action == "audio":
            result = await download_audio(url, work_dir)
        else:
            result = await download_video(url, work_dir)

        content_type = result["type"]
        files        = result["files"]
        title        = result["title"]

        # ── Видео ────────────────────────────────────
        if content_type == "video":
            path = files[0]
            if path.stat().st_size > MAX_FILE_SIZE_BYTES:
                await callback.message.answer(**msg_too_large(MAX_FILE_SIZE_MB))
                return
            done = msg_done_video()
            await callback.message.answer_video(
                video=FSInputFile(path, filename=f"{title[:50]}.mp4"),
                caption=done["text"],
                caption_entities=done["entities"],
                supports_streaming=True,
            )
            logger.info(f"[{user_id}] Видео отправлено ({path.stat().st_size / 1024 / 1024:.1f} МБ)")

        # ── Аудио ────────────────────────────────────
        elif content_type == "audio":
            path = files[0]
            if path.stat().st_size > MAX_FILE_SIZE_BYTES:
                await callback.message.answer(**msg_too_large(MAX_FILE_SIZE_MB))
                return
            done = msg_done_audio()
            await callback.message.answer_audio(
                audio=FSInputFile(path, filename=f"{title[:50]}.mp3"),
                caption=done["text"],
                caption_entities=done["entities"],
                title=title[:64],
            )
            logger.info(f"[{user_id}] Аудио отправлено ({path.stat().st_size / 1024 / 1024:.1f} МБ)")

        # ── Карусель изображений ─────────────────────
        elif content_type == "images":
            if not files:
                raise ValueError("Пустой список изображений")
            zip_path = work_dir / f"{title[:40]}_photos.zip"
            make_zip(files, zip_path)
            if zip_path.stat().st_size > MAX_FILE_SIZE_BYTES:
                await callback.message.answer(**msg_too_large(MAX_FILE_SIZE_MB))
                return
            done = msg_done_images()
            await callback.message.answer_document(
                document=FSInputFile(zip_path, filename=f"{title[:40]}_photos.zip"),
                caption=done["text"],
                caption_entities=done["entities"],
            )
            logger.info(f"[{user_id}] ZIP отправлен ({len(files)} фото)")

    except yt_dlp.utils.DownloadError as e:
        err = str(e).lower()
        logger.error(f"[{user_id}] DownloadError: {e}")
        if any(kw in err for kw in ("private", "login", "age")):
            await callback.message.answer(**msg_private())
        else:
            await callback.message.answer(**msg_error())

    except FileNotFoundError as e:
        logger.error(f"[{user_id}] FileNotFoundError: {e}")
        await callback.message.answer(**msg_error())

    except Exception as e:
        logger.exception(f"[{user_id}] Ошибка: {e}")
        await callback.message.answer(**msg_error())

    finally:
        cleanup_dir(work_dir)


@dp.callback_query(F.data.startswith("cancel:"))
async def cb_cancel(callback: CallbackQuery) -> None:
    """Пользователь нажал Cancel — удаляем сообщение с кнопками."""
    key = callback.data.split(":", 1)[1]
    _url_cache.pop(key, None)
    await callback.message.delete()
    await callback.answer()


@dp.callback_query(F.data.regexp(r"^(video|audio):"))
async def cb_download(callback: CallbackQuery) -> None:
    """
    Пользователь выбрал формат → скачиваем и отправляем.
    """
    action, key = callback.data.split(":", 1)
    url = _url_cache.pop(key, None)

    if not url:
        await callback.answer("Ссылка устарела. Отправь снова.", show_alert=True)
        await callback.message.delete()
        return

    user_id  = callback.from_user.id
    msg_id   = callback.message.message_id
    work_dir = TEMP_DIR / f"user_{user_id}_{msg_id}"
    work_dir.mkdir(parents=True, exist_ok=True)

    # Редактируем сообщение с кнопками → "Скачиваю..."
    dl = msg_downloading(action)
    await callback.message.edit_text(text=dl["text"], entities=dl["entities"])
    await callback.answer()

    await _send_result(callback, action, url, work_dir)

    # Удаляем сообщение "Скачиваю..." (оно уже не нужно)
    try:
        await callback.message.delete()
    except Exception:
        pass


# ══════════════════════════════════════════════
# БЛОК 12: Точка входа
# ══════════════════════════════════════════════

async def main() -> None:
    ensure_ffmpeg()

    logger.info("=" * 52)
    logger.info("  TikTok Downloader Bot запускается")
    logger.info(f"  FFmpeg   : {FFMPEG_PATH}")
    logger.info(f"  Temp dir : {TEMP_DIR.resolve()}")
    logger.info(f"  Max size : {MAX_FILE_SIZE_MB} МБ")
    logger.info("=" * 52)

    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        logger.info("Бот остановлен")


if __name__ == "__main__":
    asyncio.run(main())
