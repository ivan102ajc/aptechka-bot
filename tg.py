"""
Маленькая библиотека для Telegram Bot API без внешних зависимостей (только стандартный Python).

Зачем: популярная библиотека aiogram вместе с pydantic занимает ~100 МБ памяти,
а на самом дешёвом сервере доступно всего 128 МБ. Этот модуль повторяет ту часть
интерфейса aiogram 3, которой пользуется бот, и расходует в несколько раз меньше памяти.

Что есть: Bot, Dispatcher (long polling и feed_update для вебхука), Router с фильтрами
F.text == …, F.text.in_(…), F.data.startswith(…), F.location, Command, CommandStart,
состояния диалога (State, StatesGroup, FSMContext) и клавиатуры.
"""

import asyncio
import html
import http.client
import inspect
import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("tg")

API_URL = "https://api.telegram.org"
POLL_TIMEOUT_S = 25


class TelegramAPIError(Exception):
    def __init__(self, method: str, description: str, code: int = 0, retry_after: int | None = None):
        super().__init__(f"{method}: {description}")
        self.method = method
        self.description = description
        self.code = code
        self.retry_after = retry_after


class ParseMode:
    HTML = "HTML"


class DefaultBotProperties:
    def __init__(self, parse_mode: str | None = None):
        self.parse_mode = parse_mode


# ------------------------------------------------------------ HTTP на стандартной библиотеке

_ssl_ctx = ssl.create_default_context()


def _request_sync(method: str, url: str, data: bytes | None, headers: dict, timeout: float):
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return e.code, dict(e.headers or {}), body


async def http_request(method: str, url: str, *, params: dict | None = None, form: dict | None = None,
                       json_body=None, data: bytes | None = None, headers: dict | None = None,
                       timeout: float = 20) -> tuple[int, dict, bytes]:
    """Асинхронный HTTP-запрос: выполняется в отдельном потоке, чтобы не тормозить бота.
    Возвращает (код ответа, заголовки, тело)."""
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    hdrs = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    return await asyncio.wait_for(asyncio.to_thread(_request_sync, method, url, data, hdrs, timeout),
                                  timeout + 5)


async def fetch_json(method: str, url: str, **kw):
    """HTTP-запрос, ответ которого — JSON. Возвращает (код, разобранный JSON или None)."""
    status, _, body = await http_request(method, url, **kw)
    try:
        return status, json.loads(body.decode("utf-8")) if body else None
    except ValueError:
        return status, None


# ------------------------------------------------------------ объекты Telegram

class Obj:
    """Обёртка над словарём из ответа Telegram: obj.username вместо obj['username']."""

    def __init__(self, d: dict | None):
        self.__dict__["_d"] = d or {}

    def __getattr__(self, name):
        v = self._d.get(name)
        return Obj(v) if isinstance(v, dict) else v

    def __bool__(self):
        return bool(self._d)

    def __repr__(self):
        return f"Obj({self._d!r})"


_TAGS = {"bold": ("<b>", "</b>"), "italic": ("<i>", "</i>"), "underline": ("<u>", "</u>"),
         "strikethrough": ("<s>", "</s>"), "code": ("<code>", "</code>"), "pre": ("<pre>", "</pre>"),
         "spoiler": ("<tg-spoiler>", "</tg-spoiler>"), "blockquote": ("<blockquote>", "</blockquote>")}


def unparse_html(text: str | None, entities: list | None) -> str:
    """Текст сообщения с разметкой обратно в HTML (как message.html_text в aiogram).
    Смещения у Telegram считаются в единицах UTF-16 — поэтому работаем с utf-16-le."""
    if not text:
        return ""
    marks = []
    for e in entities or []:
        t = e.get("type")
        if t == "text_link":
            o, c = f'<a href="{html.escape(e.get("url", ""))}">', "</a>"
        elif t in _TAGS:
            o, c = _TAGS[t]
        else:
            continue
        start, length = e["offset"], e["length"]
        marks.append((start, 1, -length, o))           # открывающие: сначала внешние
        marks.append((start + length, 0, length, c))   # закрывающие — раньше открывающих, внутренние первыми
    if not marks:
        return html.escape(text, quote=False)
    marks.sort(key=lambda m: m[:3])
    raw = text.encode("utf-16-le")
    out, pos = [], 0
    for at, _, _, tag in marks:
        if at > pos:
            out.append(html.escape(raw[pos * 2:at * 2].decode("utf-16-le"), quote=False))
            pos = at
        out.append(tag)
    out.append(html.escape(raw[pos * 2:].decode("utf-16-le"), quote=False))
    return "".join(out)


class Message:
    def __init__(self, d: dict, bot: "Bot"):
        self.bot = bot
        self.message_id = d.get("message_id")
        self.chat = Obj(d.get("chat"))
        self.from_user = Obj(d.get("from")) if d.get("from") else None
        self.text = d.get("text")
        self.entities = d.get("entities") or []
        self.location = Obj(d["location"]) if d.get("location") else None
        self.raw = d

    @property
    def html_text(self) -> str:
        return unparse_html(self.text, self.entities)

    async def answer(self, text: str, **kw) -> "Message":
        return await self.bot.send_message(self.chat.id, text, **kw)

    async def edit_text(self, text: str, **kw):
        return await self.bot.edit_message_text(text, chat_id=self.chat.id, message_id=self.message_id, **kw)

    async def delete(self):
        return await self.bot.delete_message(self.chat.id, self.message_id)


class CallbackQuery:
    def __init__(self, d: dict, bot: "Bot"):
        self.bot = bot
        self.id = d.get("id")
        self.from_user = Obj(d.get("from"))
        self.data = d.get("data")
        m = d.get("message")
        self.message = Message(m, bot) if m and m.get("date") else None   # date=0 — сообщение недоступно

    async def answer(self, text: str | None = None, show_alert: bool = False):
        return await self.bot.answer_callback_query(self.id, text=text, show_alert=show_alert)


# ------------------------------------------------------------ клавиатуры

def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None and v is not False}


class KeyboardButton:
    def __init__(self, text: str, request_location: bool | None = None):
        self.text, self.request_location = text, request_location

    def to_dict(self):
        return _clean({"text": self.text, "request_location": self.request_location})


class ReplyKeyboardMarkup:
    def __init__(self, keyboard: list, resize_keyboard: bool | None = None):
        self.keyboard, self.resize_keyboard = keyboard, resize_keyboard

    def to_dict(self):
        return _clean({"keyboard": [[b.to_dict() for b in row] for row in self.keyboard],
                       "resize_keyboard": self.resize_keyboard})


class InlineKeyboardButton:
    def __init__(self, text: str, callback_data: str | None = None, url: str | None = None):
        self.text, self.callback_data, self.url = text, callback_data, url

    def to_dict(self):
        return _clean({"text": self.text, "callback_data": self.callback_data, "url": self.url})


class InlineKeyboardMarkup:
    def __init__(self, inline_keyboard: list):
        self.inline_keyboard = inline_keyboard

    def to_dict(self):
        return {"inline_keyboard": [[b.to_dict() for b in row] for row in self.inline_keyboard]}


# ------------------------------------------------------------ бот

class _Session:
    async def close(self):
        pass


class Bot:
    def __init__(self, token: str, default: DefaultBotProperties | None = None):
        self.token = token
        self.id = int(token.split(":")[0])
        self.parse_mode = default.parse_mode if default else None
        self.session = _Session()          # для совместимости с aiogram: await bot.session.close()

    async def request(self, method: str, params: dict | None = None, timeout: float = 30):
        url = f"{API_URL}/bot{self.token}/{method}"
        body = {k: v for k, v in (params or {}).items() if v is not None}
        for attempt in range(3):
            try:
                status, js = await fetch_json("POST", url, json_body=body, timeout=timeout)
            except (OSError, asyncio.TimeoutError, http.client.HTTPException) as e:
                if attempt == 2:
                    raise TelegramAPIError(method, f"сеть недоступна: {e!r}") from None
                await asyncio.sleep(1 + attempt * 2)
                continue
            if isinstance(js, dict) and js.get("ok"):
                return js.get("result")
            desc = (js or {}).get("description") or f"HTTP {status}"
            retry = ((js or {}).get("parameters") or {}).get("retry_after")
            if retry and attempt < 2 and retry <= 30:       # Telegram просит подождать — ждём и повторяем
                await asyncio.sleep(retry)
                continue
            raise TelegramAPIError(method, desc, (js or {}).get("error_code") or status, retry)
        raise TelegramAPIError(method, "не удалось выполнить запрос")

    def _text_params(self, text, parse_mode, reply_markup, disable_web_page_preview):
        p = {"text": text,
             "parse_mode": self.parse_mode if parse_mode is None else (parse_mode or None),
             "reply_markup": reply_markup.to_dict() if reply_markup is not None else None}
        if disable_web_page_preview:
            p["link_preview_options"] = {"is_disabled": True}
        return p

    async def send_message(self, chat_id, text: str, reply_markup=None, parse_mode: str | None = None,
                           disable_web_page_preview: bool | None = None) -> Message:
        p = {"chat_id": chat_id, **self._text_params(text, parse_mode, reply_markup, disable_web_page_preview)}
        return Message(await self.request("sendMessage", p), self)

    async def edit_message_text(self, text: str, chat_id=None, message_id=None, reply_markup=None,
                                parse_mode: str | None = None, disable_web_page_preview: bool | None = None):
        p = {"chat_id": chat_id, "message_id": message_id,
             **self._text_params(text, parse_mode, reply_markup, disable_web_page_preview)}
        res = await self.request("editMessageText", p)
        return Message(res, self) if isinstance(res, dict) else res

    async def delete_message(self, chat_id, message_id) -> bool:
        return await self.request("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None,
                                    show_alert: bool = False) -> bool:
        return await self.request("answerCallbackQuery", {"callback_query_id": callback_query_id,
                                                          "text": text, "show_alert": show_alert or None})

    async def get_me(self) -> Obj:
        return Obj(await self.request("getMe"))

    async def get_webhook_info(self) -> Obj:
        return Obj(await self.request("getWebhookInfo"))

    async def delete_webhook(self, drop_pending_updates: bool | None = None) -> bool:
        return await self.request("deleteWebhook", {"drop_pending_updates": drop_pending_updates})

    async def set_webhook(self, url: str, secret_token: str | None = None,
                          allowed_updates: list | None = None) -> bool:
        return await self.request("setWebhook", {"url": url, "secret_token": secret_token,
                                                 "allowed_updates": allowed_updates})

    async def get_updates(self, offset: int | None = None, timeout: int = POLL_TIMEOUT_S) -> list:
        return await self.request("getUpdates", {"offset": offset, "timeout": timeout,
                                                 "allowed_updates": ["message", "callback_query"]},
                                  timeout=timeout + 10)


# ------------------------------------------------------------ фильтры

class Filter:
    def __init__(self, fn):
        self.fn = fn

    def check(self, event, state: str | None) -> bool:
        return bool(self.fn(event))

    def __and__(self, other):
        return Filter(lambda e: self.fn(e) and other.fn(e))

    def __invert__(self):
        return Filter(lambda e: not self.fn(e))


class _Attr(Filter):
    """F.text, F.data, F.location — значение поля события."""

    def __init__(self, name: str):
        self.name = name
        super().__init__(lambda e: bool(self.get(e)))

    def get(self, e):
        return getattr(e, self.name, None)

    def __eq__(self, value):
        return Filter(lambda e: self.get(e) == value)

    __hash__ = object.__hash__

    def in_(self, values):
        return Filter(lambda e: self.get(e) in values)

    def startswith(self, prefix: str):
        return Filter(lambda e: isinstance(self.get(e), str) and self.get(e).startswith(prefix))


class _MagicF:
    def __getattr__(self, name):
        return _Attr(name)


F = _MagicF()


class Command(Filter):
    """Команда вида /help, /tz +5, /help@имя_бота."""

    def __init__(self, *names: str):
        self.names = {n.lower().lstrip("/") for n in names}
        super().__init__(self._match)

    def _match(self, e) -> bool:
        text = getattr(e, "text", None)
        if not text or not text.startswith("/"):
            return False
        cmd = text.split(maxsplit=1)[0][1:].split("@", 1)[0].lower()
        return cmd in self.names


class CommandStart(Command):
    def __init__(self):
        super().__init__("start")


# ------------------------------------------------------------ состояния диалога

class State:
    def __init__(self):
        self.state: str | None = None

    def __set_name__(self, owner, name):
        self.state = f"{owner.__name__}:{name}"

    def check(self, event, state: str | None) -> bool:     # State тоже работает как фильтр
        return state == self.state

    def __repr__(self):
        return f"<State {self.state}>"


class StatesGroup:
    pass


@dataclass(frozen=True)
class StorageKey:
    bot_id: int
    chat_id: int
    user_id: int
    thread_id: int | None = None
    destiny: str = "default"


class BaseStorage:
    async def set_state(self, key: StorageKey, state=None) -> None: ...
    async def get_state(self, key: StorageKey): ...
    async def set_data(self, key: StorageKey, data) -> None: ...
    async def get_data(self, key: StorageKey) -> dict: ...
    async def close(self) -> None: ...


class MemoryStorage(BaseStorage):
    def __init__(self):
        self._states: dict = {}
        self._data: dict = {}

    async def set_state(self, key, state=None):
        value = state.state if isinstance(state, State) else state
        if value is None:
            self._states.pop(key, None)
        else:
            self._states[key] = value

    async def get_state(self, key):
        return self._states.get(key)

    async def set_data(self, key, data):
        if data:
            self._data[key] = dict(data)
        else:
            self._data.pop(key, None)

    async def get_data(self, key):
        return dict(self._data.get(key, {}))


class FSMContext:
    def __init__(self, storage: BaseStorage, key: StorageKey):
        self.storage, self.key = storage, key

    async def set_state(self, state=None):
        await self.storage.set_state(self.key, state.state if isinstance(state, State) else state)

    async def get_state(self):
        return await self.storage.get_state(self.key)

    async def get_data(self) -> dict:
        return await self.storage.get_data(self.key)

    async def set_data(self, data: dict):
        await self.storage.set_data(self.key, data)

    async def update_data(self, data: dict | None = None, **kw) -> dict:
        cur = await self.get_data()
        cur.update(data or {}, **kw)
        await self.set_data(cur)
        return cur

    async def clear(self):
        await self.set_state(None)
        await self.set_data({})


# ------------------------------------------------------------ маршрутизация

class _Handler:
    def __init__(self, fn, filters):
        self.fn = fn
        self.filters = filters
        self.wants_state = "state" in inspect.signature(fn).parameters

    def matches(self, event, state) -> bool:
        return all(f.check(event, state) for f in self.filters)


class Router:
    """Обработчики проверяются в порядке объявления; срабатывает первый подходящий.
    Как и в aiogram 3, обработчик без фильтра состояния срабатывает в любом состоянии."""

    def __init__(self):
        self.message_handlers: list[_Handler] = []
        self.callback_handlers: list[_Handler] = []

    def message(self, *filters):
        def deco(fn):
            self.message_handlers.append(_Handler(fn, filters))
            return fn
        return deco

    def callback_query(self, *filters):
        def deco(fn):
            self.callback_handlers.append(_Handler(fn, filters))
            return fn
        return deco


class Dispatcher:
    def __init__(self, storage: BaseStorage | None = None):
        self.storage = storage or MemoryStorage()
        self.routers: list[Router] = []
        self._tasks: set = set()

    def include_router(self, router: Router):
        self.routers.append(router)

    async def feed_update(self, bot: Bot, update: dict):
        if "message" in update:
            event, kind = Message(update["message"], bot), "message_handlers"
            chat_id, user = event.chat.id, event.from_user
        elif "callback_query" in update:
            event, kind = CallbackQuery(update["callback_query"], bot), "callback_handlers"
            user = event.from_user
            chat_id = event.message.chat.id if event.message else (user.id if user else None)
        else:
            return
        if not user or chat_id is None:
            return
        ctx = FSMContext(self.storage, StorageKey(bot.id, chat_id, user.id))
        current = await ctx.get_state()
        for router in self.routers:
            for h in getattr(router, kind):
                if h.matches(event, current):
                    if h.wants_state:
                        await h.fn(event, state=ctx)
                    else:
                        await h.fn(event)
                    return

    async def _process(self, bot: Bot, update: dict):
        try:
            await self.feed_update(bot, update)
        except Exception:
            log.exception("ошибка при обработке сообщения")

    async def start_polling(self, bot: Bot):
        offset = None
        pause = 1
        log.info("Начинаю получать сообщения (long polling)")
        while True:
            try:
                updates = await bot.get_updates(offset)
                pause = 1
            except TelegramAPIError as e:
                if e.code == 401:
                    raise SystemExit("Telegram не принимает токен бота — проверьте BOT_TOKEN.")
                if e.code == 409:
                    log.warning("Telegram сообщает, что бот запущен где-то ещё (или включён вебхук): %s",
                                e.description)
                else:
                    log.warning("Не получилось получить сообщения: %s", e)
                await asyncio.sleep(pause)
                pause = min(pause * 2, 30)
                continue
            except Exception:
                log.exception("Непредвиденная ошибка при получении сообщений")
                await asyncio.sleep(pause)
                pause = min(pause * 2, 30)
                continue
            for u in updates or []:
                offset = u["update_id"] + 1
                t = asyncio.create_task(self._process(bot, u))
                self._tasks.add(t)                      # держим ссылку, чтобы задачу не удалил сборщик мусора
                t.add_done_callback(self._tasks.discard)
