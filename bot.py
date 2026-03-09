"""
TikTok Downloader Telegram Bot
Скачивает видео и изображения из TikTok без водяного знака.
Использует: aiogram 3.x, yt-dlp, python-dotenv
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
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message
from dotenv import load_dotenv

# ──────────────────────────────────────────────
# Загрузка переменных окружения из .env
# ──────────────────────────────────────────────
load_dotenv()

# ──────────────────────────────────────────────
# Конфигурация (всё берётся из .env)
# ──────────────────────────────────────────────
BOT_TOKEN: str       = os.getenv("BOT_TOKEN", "")
MAX_FILE_SIZE_MB: int = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
MAX_FILE_SIZE_BYTES: int = MAX_FILE_SIZE_MB * 1024 * 1024
TEMP_DIR  = Path(os.getenv("TEMP_DIR", "temp"))
LOG_LEVEL: str        = os.getenv("LOG_LEVEL", "INFO").upper()

# Путь к ffmpeg: сначала .env, потом системный PATH, потом локальный ./bin/ffmpeg
_FFMPEG_ENV   = os.getenv("FFMPEG_PATH", "")
FFMPEG_LOCAL  = Path("bin") / ("ffmpeg.exe" if platform.system() == "Windows" else "ffmpeg")

if _FFMPEG_ENV:
    FFMPEG_PATH: str = _FFMPEG_ENV
elif shutil.which("ffmpeg"):
    FFMPEG_PATH = shutil.which("ffmpeg")          # type: ignore[assignment]
else:
    FFMPEG_PATH = str(FFMPEG_LOCAL)

# ──────────────────────────────────────────────
# Ранняя валидация токена
# ──────────────────────────────────────────────
if not BOT_TOKEN:
    sys.exit(
        "[FATAL] BOT_TOKEN не задан!\n"
        "Создай файл .env и добавь строку:\n"
        "  BOT_TOKEN=ваш_токен_здесь"
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
# Создание рабочих директорий
# ──────────────────────────────────────────────
TEMP_DIR.mkdir(exist_ok=True)
FFMPEG_LOCAL.parent.mkdir(exist_ok=True)


# ══════════════════════════════════════════════
# БЛОК: Автоустановка FFmpeg
#
# На хостингах без root-доступа (Railway, Render,
# VPS без apt и т.д.) ffmpeg часто отсутствует.
# Этот блок скачивает статический бинарник
# (без внешних зависимостей) прямо в ./bin/ffmpeg.
#
# Поддерживаемые платформы:
#   • Linux  x86_64  (большинство VPS / хостингов)
#   • Linux  aarch64 (ARM-серверы, Oracle Free Tier)
#   • Windows x64    (локальная разработка)
# ══════════════════════════════════════════════

# Прямые ссылки на статические GPL-сборки от команды yt-dlp
_FFMPEG_RELEASES: dict[str, dict[str, str]] = {
    "Linux": {
        "x86_64":  (
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
    # macOS: рекомендуется brew install ffmpeg
    "Darwin": {},
}


def _progress_hook(block_count: int, block_size: int, total_size: int) -> None:
    """Простой прогресс-бар для urllib в терминале."""
    downloaded = block_count * block_size
    if total_size > 0:
        pct = min(downloaded * 100 // total_size, 100)
        mb  = downloaded / 1024 / 1024
        print(f"\r  Загрузка: {pct:3d}%  ({mb:.1f} МБ)", end="", flush=True)


def install_ffmpeg() -> bool:
    """
    Скачивает статический бинарник ffmpeg в ./bin/ffmpeg.
    Возвращает True при успехе, False при неудаче.
    """
    system  = platform.system()    # "Linux" | "Windows" | "Darwin"
    machine = platform.machine()   # "x86_64" | "aarch64" | "AMD64"

    url = _FFMPEG_RELEASES.get(system, {}).get(machine)
    if not url:
        logger.warning(
            f"Автоустановка ffmpeg не поддерживается для {system}/{machine}. "
            "Установите ffmpeg вручную и укажите путь в .env (FFMPEG_PATH)."
        )
        return False

    is_windows       = system == "Windows"
    archive_suffix   = ".zip" if is_windows else ".tar.xz"
    archive_path     = Path("bin") / f"ffmpeg_tmp{archive_suffix}"
    ffmpeg_bin       = FFMPEG_LOCAL

    logger.info(f"Скачиваю ffmpeg для {system}/{machine}...")

    try:
        # ── 1. Скачиваем архив ────────────────
        urllib.request.urlretrieve(url, archive_path, reporthook=_progress_hook)
        print()  # новая строка после прогресс-бара

        # ── 2. Распаковываем нужный бинарник ─
        if is_windows:
            # ZIP → ищем ffmpeg.exe
            with zipfile.ZipFile(archive_path, "r") as zf:
                for member in zf.namelist():
                    if member.endswith("/ffmpeg.exe") or member == "ffmpeg.exe":
                        ffmpeg_bin.write_bytes(zf.read(member))
                        break
                else:
                    raise FileNotFoundError("ffmpeg.exe не найден в ZIP-архиве")

        else:
            # TAR.XZ → ищем бинарник ffmpeg (не ffprobe, не ffplay)
            import tarfile
            with tarfile.open(archive_path, "r:xz") as tf:
                for member in tf.getmembers():
                    # Имя вида "ffmpeg-xxx/bin/ffmpeg" — берём последний сегмент
                    basename = Path(member.name).name
                    if basename == "ffmpeg" and member.isfile():
                        f_obj = tf.extractfile(member)
                        if f_obj:
                            ffmpeg_bin.write_bytes(f_obj.read())
                        break
                else:
                    raise FileNotFoundError("ffmpeg не найден в TAR-архиве")

            # Устанавливаем флаг исполняемого файла (chmod +x)
            ffmpeg_bin.chmod(ffmpeg_bin.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        # ── 3. Smoke-test: убеждаемся, что бинарник запускается ──
        result = subprocess.run(
            [str(ffmpeg_bin), "-version"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg вернул код {result.returncode}")

        logger.info(f"FFmpeg успешно установлен → {ffmpeg_bin.resolve()}")
        return True

    except Exception as exc:
        logger.error(f"Ошибка установки ffmpeg: {exc}")
        return False

    finally:
        # Всегда удаляем временный архив
        if archive_path.exists():
            archive_path.unlink(missing_ok=True)


def ensure_ffmpeg() -> None:
    """
    Гарантирует наличие рабочего ffmpeg.

    Порядок проверки:
      1. FFMPEG_PATH из .env или системный PATH
      2. Ранее скачанный ./bin/ffmpeg
      3. Автоматическая загрузка → ./bin/ffmpeg
      4. Если ничего не сработало — завершение с понятной ошибкой
    """
    global FFMPEG_PATH

    # 1. Проверяем текущий FFMPEG_PATH
    resolved = shutil.which(FFMPEG_PATH) or (
        FFMPEG_PATH if Path(FFMPEG_PATH).is_file() else None
    )
    if resolved:
        logger.info(f"FFmpeg найден: {resolved}")
        FFMPEG_PATH = resolved
        return

    # 2. Проверяем локальный бинарник из прошлого запуска
    if FFMPEG_LOCAL.is_file():
        logger.info(f"FFmpeg найден (локальный): {FFMPEG_LOCAL.resolve()}")
        FFMPEG_PATH = str(FFMPEG_LOCAL.resolve())
        return

    # 3. Пробуем автоматическую установку
    logger.warning("FFmpeg не найден. Запускаю автоустановку...")
    if install_ffmpeg():
        FFMPEG_PATH = str(FFMPEG_LOCAL.resolve())
        return

    # 4. Полный провал — человекочитаемое сообщение
    sys.exit(
        "\n[FATAL] FFmpeg не найден и не удалось установить автоматически.\n"
        "Варианты решения:\n"
        "  • Ubuntu/Debian:  sudo apt install ffmpeg\n"
        "  • macOS:          brew install ffmpeg\n"
        "  • Вручную:        скачайте бинарник и укажите путь в .env:\n"
        "                    FFMPEG_PATH=/path/to/ffmpeg\n"
    )


# ──────────────────────────────────────────────
# Константы: Premium Emoji + тексты сообщений
# ──────────────────────────────────────────────
EMOJI_START    = '<tg-emoji emoji-id="6028346797368283073">🎵</tg-emoji>'
EMOJI_DOWNLOAD = '<tg-emoji emoji-id="5850346984501680054">⏳</tg-emoji>'
EMOJI_DONE     = '<tg-emoji emoji-id="5774022692642492953">✅</tg-emoji>'
EMOJI_ERROR    = '<tg-emoji emoji-id="5774077015388852135">❌</tg-emoji>'
EMOJI_HELP     = '<tg-emoji emoji-id="5938195768832692153">💡</tg-emoji>'

MSG_START = (
    f"{EMOJI_START} <b>TikTok Downloader</b>\n\n"
    "Привет! Я помогу тебе скачать видео и изображения из TikTok "
    "<b>без водяного знака</b>.\n\n"
    "📌 <b>Как использовать:</b>\n"
    "Просто отправь мне ссылку на TikTok — и я пришлю файл!\n\n"
    "🔹 Видео → отправлю <b>.mp4</b>\n"
    "🔹 Карусель с фото → отправлю <b>.zip</b> архив\n\n"
    "Отправь /help для подробной инструкции."
)

MSG_HELP = (
    f"{EMOJI_HELP} <b>Инструкция по использованию</b>\n\n"
    "<b>Поддерживаемые форматы ссылок:</b>\n"
    "• <code>https://www.tiktok.com/@user/video/123...</code>\n"
    "• <code>https://vm.tiktok.com/XXXXXXX/</code>\n"
    "• <code>https://vt.tiktok.com/XXXXXXX/</code>\n\n"
    "<b>Что я умею:</b>\n"
    "🎬 Скачивать видео <b>без водяного знака</b>\n"
    "🖼 Скачивать карусели изображений (в ZIP)\n\n"
    "<b>Ограничения:</b>\n"
    f"📦 Максимальный размер файла: <b>{MAX_FILE_SIZE_MB} МБ</b>\n"
    "🔒 Приватные видео недоступны\n\n"
    "<b>Команды:</b>\n"
    "/start — приветствие\n"
    "/help — эта инструкция\n\n"
    "💬 Просто отправь ссылку — и готово!"
)

MSG_DOWNLOADING = f"{EMOJI_DOWNLOAD} <b>Скачиваю...</b> Подожди немного."
MSG_DONE_VIDEO  = f"{EMOJI_DONE} <b>Готово!</b> Вот твоё видео 🎬"
MSG_DONE_IMAGES = f"{EMOJI_DONE} <b>Готово!</b> Вот архив с изображениями 🖼"

MSG_NOT_TIKTOK = (
    f"{EMOJI_ERROR} <b>Неверная ссылка.</b>\n\n"
    "Я принимаю только ссылки TikTok.\n"
    "Пример: <code>https://vm.tiktok.com/XXXXXXX/</code>"
)

MSG_TOO_LARGE = (
    f"{EMOJI_ERROR} <b>Файл слишком большой.</b>\n\n"
    f"Максимальный размер: <b>{MAX_FILE_SIZE_MB} МБ</b>.\n"
    "К сожалению, Telegram не позволяет отправлять файлы большего размера."
)

MSG_PRIVATE = (
    f"{EMOJI_ERROR} <b>Недоступный контент.</b>\n\n"
    "Видео приватное или было удалено. Попробуй другую ссылку."
)

MSG_ERROR = (
    f"{EMOJI_ERROR} <b>Ошибка при скачивании.</b>\n\n"
    "Не удалось скачать контент. Возможные причины:\n"
    "• Неверная или устаревшая ссылка\n"
    "• TikTok временно недоступен\n"
    "• Приватное видео\n\n"
    "Попробуй ещё раз или проверь ссылку."
)


# ──────────────────────────────────────────────
# Утилиты: валидация ссылок
# ──────────────────────────────────────────────
TIKTOK_URL_PATTERN = re.compile(
    r"https?://(www\.|vm\.|vt\.|m\.)?tiktok\.com/[\w@/\-?=&%.]+",
    re.IGNORECASE,
)


def is_tiktok_url(text: str) -> bool:
    """Проверяет, является ли текст ссылкой TikTok."""
    return bool(TIKTOK_URL_PATTERN.search(text.strip()))


def extract_tiktok_url(text: str) -> str | None:
    """Извлекает ссылку TikTok из текста."""
    match = TIKTOK_URL_PATTERN.search(text.strip())
    return match.group(0) if match else None


# ──────────────────────────────────────────────
# Утилиты: очистка временных файлов
# ──────────────────────────────────────────────
def cleanup_dir(directory: Path) -> None:
    """Рекурсивно удаляет директорию и все её содержимое."""
    try:
        if directory.exists():
            shutil.rmtree(directory)
            logger.debug(f"Удалена директория: {directory}")
    except Exception as e:
        logger.warning(f"Не удалось удалить {directory}: {e}")


# ──────────────────────────────────────────────
# Ядро: скачивание через yt-dlp
# ──────────────────────────────────────────────
def get_ydl_opts(output_dir: Path) -> dict:
    """Возвращает настройки yt-dlp для скачивания TikTok без водяного знака."""
    return {
        "outtmpl": str(output_dir / "%(title).50s.%(ext)s"),
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "postprocessors": [{
            "key": "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        }],
        # Указываем директорию ffmpeg явно — критично для хостингов
        "ffmpeg_location": str(Path(FFMPEG_PATH).parent),
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
        "write_all_thumbnails": False,
    }


async def download_tiktok(url: str, work_dir: Path) -> dict:
    """
    Скачивает контент TikTok в указанную директорию.

    Возвращает:
        {
            "type":  "video" | "images",
            "files": [Path, ...],
            "title": str,
        }
    """
    loop = asyncio.get_event_loop()

    def _download() -> dict:
        opts = get_ydl_opts(work_dir)

        with yt_dlp.YoutubeDL(opts) as ydl:
            # Получаем метаданные без скачивания
            info = ydl.extract_info(url, download=False)

            if info is None:
                raise ValueError("Не удалось получить информацию о видео")

            title    = info.get("title", "tiktok_media")
            ext      = info.get("ext", "")
            entries  = info.get("entries")  # карусели / плейлисты

            is_carousel = (
                entries is not None
                or info.get("_type") == "playlist"
                or ext in ("", "none") and info.get("images")
            )

            if is_carousel and entries:
                # ── Карусель: скачиваем каждый слайд отдельно ──
                downloaded: list[Path] = []
                for i, entry in enumerate(entries):
                    entry_url = entry.get("url") or entry.get("webpage_url")
                    if not entry_url:
                        continue
                    sub_opts = {
                        **opts,
                        "outtmpl": str(work_dir / f"image_{i:03d}.%(ext)s"),
                    }
                    with yt_dlp.YoutubeDL(sub_opts) as sub_ydl:
                        sub_ydl.download([entry_url])
                    for f in sorted(work_dir.glob(f"image_{i:03d}.*")):
                        if not f.name.endswith(".json"):
                            downloaded.append(f)
                            break

                return {"type": "images", "files": downloaded, "title": title}

            else:
                # ── Видео ──────────────────────────────────────
                ydl.download([url])

                video_files = [
                    f for f in sorted(work_dir.glob("*.mp4"))
                    if not f.name.endswith(".json")
                ]
                if not video_files:
                    video_files = [
                        f for f in sorted(work_dir.iterdir())
                        if f.is_file() and f.suffix in (".mp4", ".webm", ".mkv", ".mov")
                    ]
                if not video_files:
                    raise FileNotFoundError("Видеофайл не найден после скачивания")

                return {"type": "video", "files": [video_files[0]], "title": title}

    # Выполняем блокирующий yt-dlp в пуле потоков
    return await loop.run_in_executor(None, _download)


def create_zip_archive(files: list[Path], output_path: Path) -> Path:
    """Создаёт ZIP-архив из списка файлов."""
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in files:
            zf.write(file, file.name)
    return output_path


# ──────────────────────────────────────────────
# Инициализация бота и диспетчера
# ──────────────────────────────────────────────
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()


# ──────────────────────────────────────────────
# Хендлеры команд
# ──────────────────────────────────────────────
@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """Обработчик /start."""
    await message.answer(MSG_START)
    logger.info(f"Пользователь {message.from_user.id} запустил бота")


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Обработчик /help."""
    await message.answer(MSG_HELP)


# ──────────────────────────────────────────────
# Хендлер: обработка TikTok ссылок
# ──────────────────────────────────────────────
@dp.message(F.text)
async def handle_url(message: Message) -> None:
    """
    Основной хендлер: валидирует ссылку, скачивает контент,
    отправляет файл, очищает временные файлы.
    """
    text = message.text.strip()

    # Проверяем, что это ссылка TikTok
    if not is_tiktok_url(text):
        await message.answer(MSG_NOT_TIKTOK)
        return

    url     = extract_tiktok_url(text)
    user_id = message.from_user.id
    logger.info(f"[{user_id}] Запрос: {url}")

    status_msg = await message.answer(MSG_DOWNLOADING)

    # Изолированная директория для каждого запроса
    work_dir = TEMP_DIR / f"user_{user_id}_{message.message_id}"
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        result       = await download_tiktok(url, work_dir)
        content_type = result["type"]
        files        = result["files"]
        title        = result["title"]

        if content_type == "video":
            video_path = files[0]
            file_size  = video_path.stat().st_size

            if file_size > MAX_FILE_SIZE_BYTES:
                await status_msg.delete()
                await message.answer(MSG_TOO_LARGE)
                return

            await status_msg.delete()
            await message.answer_video(
                video=FSInputFile(video_path, filename=f"{title[:50]}.mp4"),
                caption=MSG_DONE_VIDEO,
                supports_streaming=True,
            )
            logger.info(f"[{user_id}] Видео отправлено ({file_size / 1024 / 1024:.1f} МБ)")

        elif content_type == "images":
            if not files:
                raise ValueError("Список изображений пуст")

            zip_path  = work_dir / f"{title[:40]}_images.zip"
            create_zip_archive(files, zip_path)
            file_size = zip_path.stat().st_size

            if file_size > MAX_FILE_SIZE_BYTES:
                await status_msg.delete()
                await message.answer(MSG_TOO_LARGE)
                return

            await status_msg.delete()
            await message.answer_document(
                document=FSInputFile(zip_path, filename=f"{title[:40]}_images.zip"),
                caption=MSG_DONE_IMAGES,
            )
            logger.info(f"[{user_id}] ZIP отправлен ({len(files)} изображений)")

    except yt_dlp.utils.DownloadError as e:
        error_str = str(e).lower()
        logger.error(f"[{user_id}] DownloadError: {e}")
        await status_msg.delete()
        if any(kw in error_str for kw in ("private", "login", "age")):
            await message.answer(MSG_PRIVATE)
        else:
            await message.answer(MSG_ERROR)

    except FileNotFoundError as e:
        logger.error(f"[{user_id}] FileNotFoundError: {e}")
        await status_msg.delete()
        await message.answer(MSG_ERROR)

    except Exception as e:
        logger.exception(f"[{user_id}] Неожиданная ошибка: {e}")
        try:
            await status_msg.delete()
        except Exception:
            pass
        await message.answer(MSG_ERROR)

    finally:
        # Гарантированно удаляем все временные файлы
        cleanup_dir(work_dir)


# ──────────────────────────────────────────────
# Точка входа
# ──────────────────────────────────────────────
async def main() -> None:
    """Запускает бота в режиме long polling."""
    # Проверяем / устанавливаем ffmpeg ДО запуска бота
    ensure_ffmpeg()

    logger.info("=" * 50)
    logger.info("TikTok Downloader Bot запускается...")
    logger.info(f"  FFmpeg    : {FFMPEG_PATH}")
    logger.info(f"  Temp dir  : {TEMP_DIR.resolve()}")
    logger.info(f"  Max size  : {MAX_FILE_SIZE_MB} МБ")
    logger.info("=" * 50)

    await bot.delete_webhook(drop_pending_updates=True)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        logger.info("Бот остановлен")


if __name__ == "__main__":
    asyncio.run(main())
