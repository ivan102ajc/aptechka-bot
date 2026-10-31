"""
Облачная версия бота для Yandex Cloud Functions (работает круглосуточно, без вашего компьютера).

Как это устроено:
  • Telegram сам присылает каждое сообщение на адрес функции (вебхук).
  • Таймер раз в минуту запускает функцию — она рассылает напоминания и
    предупреждения о сроках годности (то, что на компьютере делал фоновый цикл).
  • База medbot.db хранится в бакете Object Storage: функция скачивает её перед
    работой и загружает обратно, если что-то изменилось.
  • Адрес вебхука функция настраивает сама при первом срабатывании таймера.

Настройки функции:
  точка входа:            cloud.handler
  переменные окружения:   BOT_TOKEN — токен от @BotFather
                          BUCKET    — имя бакета для базы
  сервисный аккаунт:      с ролью storage.editor
  масштабирование:        не больше 1 экземпляра (чтобы база не перезаписывалась параллельно)
"""

import asyncio
import base64
import hashlib
import json
import logging
import os

from tg import (BaseStorage, Bot, DefaultBotProperties, Dispatcher, ParseMode, State, StorageKey,
                http_request)

import bot as core

log = logging.getLogger("medbot.cloud")
logging.getLogger().setLevel(logging.INFO)

DB_LOCAL = "/tmp/medbot.db"
DB_KEY = "medbot.db"
S3_URL = "https://storage.yandexcloud.net"
core.DB_PATH = DB_LOCAL

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
BUCKET = os.environ.get("BUCKET", "").strip()
# Секрет, по которому функция отличает настоящие запросы Telegram от чужих
SECRET = hashlib.sha256(("telegram-webhook:" + TOKEN).encode()).hexdigest()[:48]


# ------------------------------------------------------------ состояние диалогов в базе

class SQLiteStorage(BaseStorage):
    """Хранит «на каком шаге диалога пользователь» в той же базе — переживает перезапуски функции."""

    @staticmethod
    def _key(key: StorageKey) -> str:
        return f"{key.bot_id}:{key.chat_id}:{key.user_id}:{getattr(key, 'thread_id', None) or 0}:{key.destiny}"

    @staticmethod
    def _ensure(c) -> None:
        c.execute("CREATE TABLE IF NOT EXISTS fsm(key TEXT PRIMARY KEY, state TEXT, data TEXT)")

    async def set_state(self, key: StorageKey, state=None) -> None:
        value = state.state if isinstance(state, State) else state
        with core.db() as c:
            self._ensure(c)
            c.execute("INSERT INTO fsm(key, state, data) VALUES(?, ?, '{}') "
                      "ON CONFLICT(key) DO UPDATE SET state=excluded.state", (self._key(key), value))

    async def get_state(self, key: StorageKey):
        with core.db() as c:
            self._ensure(c)
            row = c.execute("SELECT state FROM fsm WHERE key=?", (self._key(key),)).fetchone()
        return row["state"] if row else None

    async def set_data(self, key: StorageKey, data) -> None:
        with core.db() as c:
            self._ensure(c)
            c.execute("INSERT INTO fsm(key, state, data) VALUES(?, NULL, ?) "
                      "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                      (self._key(key), json.dumps(dict(data), ensure_ascii=False)))

    async def get_data(self, key: StorageKey) -> dict:
        with core.db() as c:
            self._ensure(c)
            row = c.execute("SELECT data FROM fsm WHERE key=?", (self._key(key),)).fetchone()
        return json.loads(row["data"]) if row and row["data"] else {}

    async def close(self) -> None:
        pass


# ------------------------------------------------------------ база в Object Storage

_etag: str | None = None          # версия базы, которая сейчас лежит в /tmp
_db_ready = False


def _sig():
    try:
        st = os.stat(DB_LOCAL)
        return st.st_mtime_ns, st.st_size
    except FileNotFoundError:
        return None


async def _s3(method: str, iam: str, data: bytes | None = None):
    url = f"{S3_URL}/{BUCKET}/{DB_KEY}"
    status, headers, body = await http_request(method, url, data=data, timeout=20,
                                               headers={"Authorization": f"Bearer {iam}"})
    etag = next((v for k, v in headers.items() if k.lower() == "etag"), None)
    return status, etag, body if method == "GET" else b""


async def pull_db(iam: str) -> None:
    """Скачивает базу, если в бакете новее, чем у нас в /tmp."""
    global _etag, _db_ready
    status, etag, _ = await _s3("HEAD", iam)
    if status == 404:
        log.info("В бакете ещё нет базы — начинаю с чистой (или с той, что уже есть в /tmp)")
    elif status != 200:
        raise RuntimeError(f"Object Storage HEAD: HTTP {status} — проверьте BUCKET и роль storage.editor")
    elif etag != _etag or not os.path.exists(DB_LOCAL):
        status, etag, body = await _s3("GET", iam)
        if status != 200:
            raise RuntimeError(f"Object Storage GET: HTTP {status}")
        with open(DB_LOCAL, "wb") as f:
            f.write(body)
        _etag = etag
        _db_ready = False
        log.info("База скачана из бакета (%d байт)", len(body))
    if not _db_ready:
        core.init_db()                # создаёт таблицы и обновляет старые базы
        _db_ready = True


async def push_db(iam: str) -> None:
    global _etag
    with open(DB_LOCAL, "rb") as f:
        body = f.read()
    status, etag, _ = await _s3("PUT", iam, body)
    if status != 200:
        raise RuntimeError(f"Object Storage PUT: HTTP {status}")
    _etag = etag
    log.info("База сохранена в бакет (%d байт)", len(body))


# ------------------------------------------------------------ бот

_bot: Bot | None = None
_dp: Dispatcher | None = None
_webhook_ok = False
_loop: asyncio.AbstractEventLoop | None = None


def get_bot() -> tuple[Bot, Dispatcher]:
    global _bot, _dp
    if _bot is None:
        if not TOKEN:
            raise RuntimeError("Не задана переменная окружения BOT_TOKEN")
        _bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        _dp = Dispatcher(storage=SQLiteStorage())
        _dp.include_router(core.router)
    return _bot, _dp


async def ensure_webhook(bot: Bot, context) -> None:
    """Настраивает адрес, на который Telegram присылает сообщения (один раз на экземпляр)."""
    global _webhook_ok
    if _webhook_ok:
        return
    url = os.environ.get("WEBHOOK_URL") or f"https://functions.yandexcloud.net/{context.function_name}"
    info = await bot.get_webhook_info()
    if info.url != url:
        await bot.set_webhook(url, secret_token=SECRET, allowed_updates=["message", "callback_query"])
        log.info("Вебхук установлен: %s", url)
    _webhook_ok = True


def _iam(context) -> str:
    tok = getattr(context, "token", None)
    if isinstance(tok, dict):
        tok = tok.get("access_token")
    elif tok is not None and not isinstance(tok, str):
        tok = getattr(tok, "access_token", None)
    if not tok:
        raise RuntimeError("Нет IAM-токена: укажите у функции сервисный аккаунт")
    return tok


def _is_timer(event) -> bool:
    try:
        return "TimerMessage" in event["messages"][0]["event_metadata"]["event_type"]
    except (KeyError, IndexError, TypeError):
        return False


def _header(event, name: str) -> str:
    for k, v in (event.get("headers") or {}).items():
        if k.lower() == name.lower():
            return v
    return ""


async def handle(event, context) -> dict:
    if not BUCKET:
        raise RuntimeError("Не задана переменная окружения BUCKET")
    iam = _iam(context)
    await pull_db(iam)
    before = _sig()
    bot, dp = get_bot()
    try:
        if _is_timer(event):
            await ensure_webhook(bot, context)
            await core.announce_update(bot)
            await core.check_reminders(bot)
            await core.check_expiry(bot)
        elif event.get("httpMethod") == "POST":
            if _header(event, "X-Telegram-Bot-Api-Secret-Token") != SECRET:
                return {"statusCode": 403, "body": "forbidden"}
            body = event.get("body") or "{}"
            if event.get("isBase64Encoded"):
                body = base64.b64decode(body).decode("utf-8")
            update = json.loads(body)
            try:
                await dp.feed_update(bot, update)
            except Exception:
                # отвечаем Telegram «ок», иначе он будет присылать то же сообщение снова и снова
                log.exception("ошибка при обработке сообщения")
        else:
            return {"statusCode": 200, "body": "Бот «Аптечка» работает ✅"}
    finally:
        if _sig() != before:
            await push_db(iam)
    return {"statusCode": 200, "body": "ok"}


def handler(event, context):
    """Точка входа Yandex Cloud Functions. Один цикл asyncio живёт, пока жив экземпляр функции."""
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop.run_until_complete(handle(event, context))
