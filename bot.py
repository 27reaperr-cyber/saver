"""
TikTok Downloader Telegram Bot  —  финальная версия
====================================================
• Скачивает видео + аудио + изображения ОДНОВРЕМЕННО (asyncio.gather)
• Custom emoji через MessageEntity — видны всем пользователям
  (бот может отправлять их, если владелец бота подписан на Telegram Premium)
• Автоустановка ffmpeg на ограниченных хостингах (без root/apt)
• Все временные файлы удаляются через try/finally

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
from aiogram.types import FSInputFile, Message, MessageEntity
from dotenv import load_dotenv

# ──────────────────────────────────────────────────────────
# Загрузка .env
# ──────────────────────────────────────────────────────────
load_dotenv()

# ──────────────────────────────────────────────────────────
# Конфигурация
# ──────────────────────────────────────────────────────────
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
    FFMPEG_PATH = shutil.which("ffmpeg")          # type: ignore[assignment]
else:
    FFMPEG_PATH = str(FFMPEG_LOCAL)

if not BOT_TOKEN:
    sys.exit(
        "[FATAL] BOT_TOKEN не задан.\n"
        "Создай файл .env и добавь: BOT_TOKEN=ваш_токен_здесь"
    )

# ──────────────────────────────────────────────────────────
# Логирование
# ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────
# Директории
# ──────────────────────────────────────────────────────────
TEMP_DIR.mkdir(exist_ok=True)
FFMPEG_LOCAL.parent.mkdir(exist_ok=True)


# ══════════════════════════════════════════════════════════
# БЛОК 1 — Автоустановка FFmpeg
#
# Нужен для хостингов без root-доступа (Railway, Render,
# обычные VPS без apt). Скачивает статический GPL-бинарник
# в ./bin/ffmpeg — без системных зависимостей.
#
# Поддерживаемые платформы:
#   Linux  x86_64   — большинство VPS
#   Linux  aarch64  — ARM (Oracle Free Tier и др.)
#   Windows AMD64   — локальная разработка
# ══════════════════════════════════════════════════════════

_FFMPEG_URLS: dict[str, dict[str, str]] = {
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
    "Darwin": {},   # macOS: brew install ffmpeg
}


def _progress(blocks: int, bs: int, total: int) -> None:
    if total > 0:
        pct = min(blocks * bs * 100 // total, 100)
        print(f"\r  [{pct:3d}%] {blocks * bs / 1048576:.1f} MB", end="", flush=True)


def install_ffmpeg() -> bool:
    """Скачивает ffmpeg-бинарник в ./bin/ffmpeg. True = успех."""
    system, machine = platform.system(), platform.machine()
    url = _FFMPEG_URLS.get(system, {}).get(machine)
    if not url:
        logger.warning(f"Автоустановка ffmpeg не поддерживается для {system}/{machine}")
        return False

    ext    = ".zip" if system == "Windows" else ".tar.xz"
    tmp    = Path("bin") / f"ffmpeg_tmp{ext}"
    target = FFMPEG_LOCAL

    logger.info(f"Скачиваю ffmpeg ({system}/{machine})...")
    try:
        urllib.request.urlretrieve(url, tmp, reporthook=_progress)
        print()

        if system == "Windows":
            with zipfile.ZipFile(tmp, "r") as zf:
                for m in zf.namelist():
                    if m.endswith("/ffmpeg.exe") or m == "ffmpeg.exe":
                        target.write_bytes(zf.read(m)); break
                else:
                    raise FileNotFoundError("ffmpeg.exe не найден в архиве")
        else:
            import tarfile
            with tarfile.open(tmp, "r:xz") as tf:
                for m in tf.getmembers():
                    if Path(m.name).name == "ffmpeg" and m.isfile():
                        fobj = tf.extractfile(m)
                        if fobj:
                            target.write_bytes(fobj.read())
                        break
                else:
                    raise FileNotFoundError("ffmpeg не найден в архиве")
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        r = subprocess.run([str(target), "-version"], capture_output=True, timeout=15)
        if r.returncode != 0:
            raise RuntimeError("smoke-test провалился")

        logger.info(f"FFmpeg установлен: {target.resolve()}")
        return True

    except Exception as e:
        logger.error(f"Ошибка установки ffmpeg: {e}")
        return False
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def ensure_ffmpeg() -> None:
    """
    Гарантирует наличие ffmpeg.
    Порядок: .env → системный PATH → ./bin/ffmpeg (кэш) → авто-загрузка → exit
    """
    global FFMPEG_PATH

    resolved = shutil.which(FFMPEG_PATH) or (FFMPEG_PATH if Path(FFMPEG_PATH).is_file() else None)
    if resolved:
        FFMPEG_PATH = resolved
        logger.info(f"FFmpeg: {FFMPEG_PATH}")
        return

    if FFMPEG_LOCAL.is_file():
        FFMPEG_PATH = str(FFMPEG_LOCAL.resolve())
        logger.info(f"FFmpeg (локальный): {FFMPEG_PATH}")
        return

    logger.warning("FFmpeg не найден — запускаю автоустановку...")
    if install_ffmpeg():
        FFMPEG_PATH = str(FFMPEG_LOCAL.resolve())
        return

    sys.exit(
        "\n[FATAL] FFmpeg не найден и установить не удалось.\n"
        "  Ubuntu/Debian : sudo apt install ffmpeg\n"
        "  macOS         : brew install ffmpeg\n"
        "  Вручную       : укажи путь в .env через FFMPEG_PATH=\n"
    )


# ══════════════════════════════════════════════════════════
# БЛОК 2 — Построитель сообщений с MessageEntity
#
# Все пользователи видят custom emoji (animated/static),
# отправлять их от имени бота можно при наличии Telegram
# Premium у владельца бота (Bot API обновление 09.02.2026).
#
# Offset/length — в UTF-16 code units, как требует Bot API.
# BMP-символы (ASCII, кирилл., базовые emoji) = 1 unit.
# Supplementary plane (большинство цветных emoji) = 2 units.
# ══════════════════════════════════════════════════════════

def _u16len(s: str) -> int:
    """Длина строки в UTF-16 code units."""
    return len(s.encode("utf-16-le")) // 2


class Msg:
    """
    Построитель сообщений через MessageEntity.
    Не использует parse_mode вообще — только entities.

    Пример:
        m = Msg().ce(CE_DONE, "✅").bold(" Готово!").nl().text("Вот твой файл")
        await message.answer(**m.out())
        await message.answer_video(..., **m.cap())
    """

    def __init__(self) -> None:
        self._buf: str = ""
        self._ent: list[MessageEntity] = []

    def _off(self) -> int:
        return _u16len(self._buf)

    def _push(self, s: str, t: str, **kw) -> "Msg":
        self._ent.append(MessageEntity(type=t, offset=self._off(), length=_u16len(s), **kw))
        self._buf += s
        return self

    # ── Базовые методы ─────────────────────────
    def text(self, s: str)  -> "Msg": self._buf += s;          return self
    def bold(self, s: str)  -> "Msg": return self._push(s, "bold")
    def code(self, s: str)  -> "Msg": return self._push(s, "code")
    def nl(self, n: int = 1)-> "Msg": self._buf += "\n" * n;  return self

    def ce(self, emoji_id: str, placeholder: str) -> "Msg":
        """
        Custom emoji по ID.
        placeholder — стандартный emoji, ассоциированный с этим стикером
        (показывается в системных уведомлениях и там, где анимация недоступна).
        Должен быть ровно один Unicode-скаляр.
        """
        return self._push(placeholder, "custom_emoji", custom_emoji_id=emoji_id)

    # ── Сборка ─────────────────────────────────
    def out(self) -> dict:
        """Kwargs для message.answer()."""
        return {"text": self._buf, "entities": self._ent or None}

    def cap(self) -> dict:
        """Kwargs caption для answer_video / answer_audio / answer_document."""
        return {"caption": self._buf, "caption_entities": self._ent or None}


# ══════════════════════════════════════════════════════════
# БЛОК 3 — Custom Emoji IDs и тексты сообщений
#
# Все тексты строятся через Msg() — ни одного Unicode-emoji
# в строках нет, только custom emoji через entities.
# ══════════════════════════════════════════════════════════

# Идентификаторы custom emoji (animated Premium stickers)
CE_START    = "6028346797368283073"  # TikTok / музыка
CE_DOWNLOAD = "5850346984501680054"  # загрузка / ожидание
CE_DONE     = "5774022692642492953"  # успех / галочка
CE_ERROR    = "5774077015388852135"  # ошибка / крест
CE_HELP     = "5938195768832692153"  # подсказка / лампочка


def msg_start() -> dict:
    return (
        Msg()
        .ce(CE_START, "🎵").bold(" Saver").nl(2)
        .text("Скинь ссылку прямо сюда — получишь видос ")
        .bold("без водяного знака").text(".").nl(2)
        .bold("Что пришлю:").nl()
        .text("Видео ").bold(".mp4").text(" + аудио ").bold(".mp3").nl()
        .text("Карусель фото ").bold(".zip").text(" + аудио ").bold(".mp3").nl(2)
        .text("Всё скачивается сразу, без лишних кнопок.").nl(2)
        .text("Команда /help — подробная инструкция.")
        .text("Другие проекты - @dreinnh")
        .out()
    )


def msg_help(max_mb: int) -> dict:
    return (
        Msg()
        .ce(CE_HELP, "💡").bold(" Инструкция").nl(2)
        .bold("Форматы ссылок:").nl()
        .code("https://www.tiktok.com/@user/video/123").nl()
        .code("https://vm.tiktok.com/XXXXXXX/").nl()
        .code("https://vt.tiktok.com/XXXXXXX/").nl(2)
        .bold("Что скачиваю:").nl()
        .text("Видео без водяного знака").nl()
        .text("Аудио ").bold("MP3 192kbps").text(" из видео").nl()
        .text("Карусель изображений в ").bold("ZIP").nl(2)
        .bold("Ограничения:").nl()
        .text("Макс. размер файла: ").bold(f"{max_mb} МБ").nl()
        .text("Приватные видео недоступны").nl(2)
        .code("/start").text(" — приветствие").nl()
        .code("/help").text("  — инструкция")
        .out()
    )


def msg_downloading() -> dict:
    return (
        Msg()
        .ce(CE_DOWNLOAD, "⏳").bold(" Скачиваю...")
        .text(" Это займёт несколько секунд.")
        .out()
    )


def msg_done_video() -> dict:
    return Msg().ce(CE_DONE, "✅").bold(" Видео готово (by @saver_drbot)").cap()


def msg_done_audio() -> dict:
    return Msg().ce(CE_DONE, "✅").bold(" Аудио готово (by @saver_drbot)").cap()


def msg_done_images() -> dict:
    return Msg().ce(CE_DONE, "✅").bold(" Фотографии готовы (by @saver_drbot)").cap()


def msg_not_tiktok() -> dict:
    return (
        Msg()
        .ce(CE_ERROR, "❌").bold(" Неверная ссылка.").nl(2)
        .text("Принимаю только ссылки TikTok.").nl()
        .text("Пример: ").code("https://vm.tiktok.com/XXXXXXX/")
        .out()
    )


def msg_too_large(max_mb: int) -> dict:
    return (
        Msg()
        .ce(CE_ERROR, "❌").bold(" Файл слишком большой.").nl(2)
        .text("Максимальный размер: ").bold(f"{max_mb} МБ").text(".").nl()
        .text("Telegram не позволяет отправлять файлы большего размера.")
        .out()
    )


def msg_private() -> dict:
    return (
        Msg()
        .ce(CE_ERROR, "❌").bold(" Недоступный контент.").nl(2)
        .text("Видео приватное или удалено. Попробуй другую ссылку.")
        .out()
    )


def msg_error() -> dict:
    return (
        Msg()
        .ce(CE_ERROR, "❌").bold(" Ошибка при скачивании.").nl(2)
        .text("Возможные причины:").nl()
        .text("Неверная или устаревшая ссылка").nl()
        .text("TikTok временно недоступен").nl()
        .text("Приватное видео").nl(2)
        .text("Попробуй ещё раз или пришли другую ссылку.")
        .out()
    )


# ══════════════════════════════════════════════════════════
# БЛОК 4 — Валидация TikTok URL
# ══════════════════════════════════════════════════════════

_TIKTOK_RE = re.compile(
    r"https?://(www\.|vm\.|vt\.|m\.)?tiktok\.com/[\w@/\-?=&%.]+",
    re.IGNORECASE,
)


def is_tiktok(text: str) -> bool:
    return bool(_TIKTOK_RE.search(text.strip()))


def extract_url(text: str) -> str | None:
    m = _TIKTOK_RE.search(text.strip())
    return m.group(0) if m else None


# ══════════════════════════════════════════════════════════
# БЛОК 5 — Утилиты
# ══════════════════════════════════════════════════════════

def cleanup(path: Path) -> None:
    """Удаляет директорию со всем содержимым (игнорирует ошибки)."""
    try:
        if path.exists():
            shutil.rmtree(path)
    except Exception as e:
        logger.warning(f"Не удалось удалить {path}: {e}")


def make_zip(files: list[Path], out: Path) -> Path:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f.name)
    return out


def _ffmpeg_dir() -> str:
    return str(Path(FFMPEG_PATH).parent)


# ══════════════════════════════════════════════════════════
# БЛОК 6 — Скачивание через yt-dlp
#
# Две независимые функции: video/images и audio.
# Запускаются параллельно через asyncio.gather.
# Каждая работает в своей поддиректории, чтобы
# файлы не пересекались.
# ══════════════════════════════════════════════════════════

def _ydl_base_opts(out_dir: Path, outtmpl: str | None = None) -> dict:
    """Общие опции yt-dlp."""
    return {
        "outtmpl": outtmpl or str(out_dir / "%(title).50s.%(ext)s"),
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


async def fetch_video(url: str, work_dir: Path) -> dict:
    """
    Скачивает видео (MP4 без водяного знака) или карусель изображений (ZIP).

    Возвращает:
        { "type": "video"|"images", "files": [Path, ...], "title": str }
    """
    vid_dir = work_dir / "video"
    vid_dir.mkdir()
    loop = asyncio.get_event_loop()

    def _run() -> dict:
        opts = {
            **_ydl_base_opts(vid_dir),
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
            "writeinfojson": True,
        }
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
                # Карусель — скачиваем каждый слайд в отдельный файл
                files: list[Path] = []
                for i, entry in enumerate(entries):
                    eurl = entry.get("url") or entry.get("webpage_url")
                    if not eurl:
                        continue
                    sub = {**opts, "outtmpl": str(vid_dir / f"img_{i:03d}.%(ext)s")}
                    with yt_dlp.YoutubeDL(sub) as s:
                        s.download([eurl])
                    for f in sorted(vid_dir.glob(f"img_{i:03d}.*")):
                        if not f.name.endswith(".json"):
                            files.append(f); break
                return {"type": "images", "files": files, "title": title}

            # Обычное видео
            ydl.download([url])
            mp4s = [f for f in sorted(vid_dir.glob("*.mp4")) if not f.name.endswith(".json")]
            if not mp4s:
                mp4s = [f for f in sorted(vid_dir.iterdir())
                        if f.is_file() and f.suffix in (".mp4", ".webm", ".mkv", ".mov")]
            if not mp4s:
                raise FileNotFoundError("Видеофайл не найден")
            return {"type": "video", "files": [mp4s[0]], "title": title}

    return await loop.run_in_executor(None, _run)


async def fetch_audio(url: str, work_dir: Path) -> dict:
    """
    Извлекает аудио из TikTok поста → MP3 192 kbps.

    Возвращает:
        { "type": "audio", "files": [Path], "title": str }
    """
    aud_dir = work_dir / "audio"
    aud_dir.mkdir()
    loop = asyncio.get_event_loop()

    def _run() -> dict:
        opts = {
            **_ydl_base_opts(aud_dir),
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info  = ydl.extract_info(url, download=False)
            title = (info or {}).get("title", "tiktok_audio")
            ydl.download([url])

        mp3s = sorted(aud_dir.glob("*.mp3"))
        if not mp3s:
            mp3s = [f for f in sorted(aud_dir.iterdir())
                    if f.is_file() and f.suffix in (".mp3", ".m4a", ".aac", ".opus", ".ogg")]
        if not mp3s:
            raise FileNotFoundError("Аудиофайл не найден")
        return {"type": "audio", "files": [mp3s[0]], "title": title}

    return await loop.run_in_executor(None, _run)


# ══════════════════════════════════════════════════════════
# БЛОК 7 — Инициализация бота
# ══════════════════════════════════════════════════════════

# parse_mode НЕ задаём — всё форматирование через entities
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties())
dp  = Dispatcher()


# ══════════════════════════════════════════════════════════
# БЛОК 8 — Хендлеры команд
# ══════════════════════════════════════════════════════════

@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(**msg_start())
    logger.info(f"[{message.from_user.id}] /start")


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(**msg_help(MAX_FILE_SIZE_MB))


# ══════════════════════════════════════════════════════════
# БЛОК 9 — Основной хендлер: параллельное скачивание
#
# Флоу:
#   1. Получили ссылку → валидация
#   2. Отправили "Скачиваю..."
#   3. asyncio.gather(fetch_video, fetch_audio) — параллельно
#   4. Отправили все файлы (видео/ZIP + MP3)
#   5. Удалили сообщение "Скачиваю..."
#   6. finally: cleanup всей рабочей директории
# ══════════════════════════════════════════════════════════

@dp.message(F.text)
async def handle_link(message: Message) -> None:
    text = message.text.strip()

    if not is_tiktok(text):
        await message.answer(**msg_not_tiktok())
        return

    url     = extract_url(text)
    uid     = message.from_user.id
    work    = TEMP_DIR / f"u{uid}_{message.message_id}"
    work.mkdir(parents=True, exist_ok=True)

    logger.info(f"[{uid}] Запрос: {url}")
    status = await message.answer(**msg_downloading())

    try:
        # Параллельно скачиваем видео/изображения и аудио
        video_res, audio_res = await asyncio.gather(
            fetch_video(url, work),
            fetch_audio(url, work),
            return_exceptions=True,
        )

        sent_any = False  # флаг: удалось отправить хоть что-то

        # ── Видео или карусель ────────────────────────────
        if isinstance(video_res, Exception):
            logger.error(f"[{uid}] fetch_video: {video_res}")
        else:
            vtype  = video_res["type"]
            vfiles = video_res["files"]
            vtitle = video_res["title"]

            if vtype == "video":
                vpath = vfiles[0]
                if vpath.stat().st_size > MAX_FILE_SIZE_BYTES:
                    await message.answer(**msg_too_large(MAX_FILE_SIZE_MB))
                else:
                    await message.answer_video(
                        video=FSInputFile(vpath, filename=f"{vtitle[:50]}.mp4"),
                        supports_streaming=True,
                        **msg_done_video(),
                    )
                    sent_any = True
                    logger.info(f"[{uid}] Видео {vpath.stat().st_size / 1048576:.1f} МБ")

            elif vtype == "images" and vfiles:
                zip_path = work / f"{vtitle[:40]}_photos.zip"
                make_zip(vfiles, zip_path)
                if zip_path.stat().st_size > MAX_FILE_SIZE_BYTES:
                    await message.answer(**msg_too_large(MAX_FILE_SIZE_MB))
                else:
                    await message.answer_document(
                        document=FSInputFile(zip_path, filename=f"{vtitle[:40]}_photos.zip"),
                        **msg_done_images(),
                    )
                    sent_any = True
                    logger.info(f"[{uid}] ZIP {len(vfiles)} фото")

        # ── Аудио ─────────────────────────────────────────
        if isinstance(audio_res, Exception):
            logger.error(f"[{uid}] fetch_audio: {audio_res}")
        else:
            apath  = audio_res["files"][0]
            atitle = audio_res["title"]
            if apath.stat().st_size > MAX_FILE_SIZE_BYTES:
                await message.answer(**msg_too_large(MAX_FILE_SIZE_MB))
            else:
                await message.answer_audio(
                    audio=FSInputFile(apath, filename=f"{atitle[:50]}.mp3"),
                    title=atitle[:64],
                    **msg_done_audio(),
                )
                sent_any = True
                logger.info(f"[{uid}] Аудио {apath.stat().st_size / 1048576:.1f} МБ")

        # ── Если оба упали — показываем ошибку ────────────
        if not sent_any:
            err = video_res if isinstance(video_res, Exception) else audio_res
            err_s = str(err).lower()
            if any(kw in err_s for kw in ("private", "login", "age")):
                await message.answer(**msg_private())
            else:
                await message.answer(**msg_error())

    except Exception as e:
        logger.exception(f"[{uid}] Неожиданная ошибка: {e}")
        await message.answer(**msg_error())

    finally:
        # Удаляем статус-сообщение и все временные файлы
        try:
            await status.delete()
        except Exception:
            pass
        cleanup(work)
        logger.debug(f"[{uid}] Очищена {work}")


# ══════════════════════════════════════════════════════════
# БЛОК 10 — Точка входа
# ══════════════════════════════════════════════════════════

async def main() -> None:
    ensure_ffmpeg()

    logger.info("=" * 54)
    logger.info("  TikTok Downloader Bot")
    logger.info(f"  FFmpeg   : {FFMPEG_PATH}")
    logger.info(f"  Temp dir : {TEMP_DIR.resolve()}")
    logger.info(f"  Max size : {MAX_FILE_SIZE_MB} МБ")
    logger.info("=" * 54)

    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        logger.info("Бот остановлен")


if __name__ == "__main__":
    asyncio.run(main())
