"""
ИИ-консультант (OpenAI-совместимый Chat Completions API): настройки, сетевой клиент, промпт, а
также курс тенге. Подбор направлений внешний ИИ не использует - он в smart_engine.py.

Здесь нет виджетов - интерфейс ИИ лежит в ai_windows.py, а мастер вопросов - в main.py.
Сеть идёт через QNetworkAccessManager (как и загрузка фото), поэтому интерфейс не блокируется
и дополнительные библиотеки не нужны.
"""

import json
import math
import os
import re
import sys
import time
from datetime import date
from urllib.parse import urlsplit

from PyQt5.QtCore import QObject, QTimer, QUrl
from PyQt5.QtNetwork import QNetworkReply, QNetworkRequest

####################################
############ НАСТРОЙКИ #############
####################################

# Режимы ИИ. Оба сервиса понимают один и тот же формат запросов, отличаются адрес, модель и ключ.
# Ключ каждого режима хранится отдельно и уходит только на адрес своего режима.
PROVIDERS = {
    1: {"base_url": "https://api.openai.com/v1", "model": "gpt-6-luna",
        "fallbacks": [], "key": "api_key", "env": "OPENAI_API_KEY"},
    2: {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-3.5-flash-lite",
        "fallbacks": ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.1-flash-lite"],
        "key": "api_key_2", "env": "GEMINI_API_KEY"},
}

# Перегрузка сервиса (503 и похожее) обычно проходит за секунды, поэтому запрос повторяется сам:
# та же модель, затем запасные по очереди. У запасных свои лимиты, поэтому они выручают и при 429.
RETRY_STATUSES = {500, 502, 503, 504}
RETRY_DELAY_MS = 2000
FALLBACK_DELAY_MS = 1000
# Модель, ответившая последней, - с неё начинается следующий запрос (до перезапуска программы).
PREFERRED_MODELS = {}


def attempt_plan(models):
    """Список попыток (пауза в мс, модель)."""
    plan = [(0, models[0]), (RETRY_DELAY_MS, models[0])]
    return plan + [(FALLBACK_DELAY_MS, model) for model in models[1:]]


def next_plan(plan, status, body, main_model):
    """
    Оставшиеся попытки после ошибки (plan[0] - только что неудавшаяся) или пустой список, если
    повторять бессмысленно.
    """
    model, rest = plan[0][1], plan[1:]
    if is_key_error(status, body.decode("utf-8", "replace")) or status in (401, 402) or is_billing_error(status, body):
        return []
    if status in RETRY_STATUSES:
        return rest
    if status == 429 or model != main_model:
        # Лимит исчерпан или запасная модель недоступна: помочь может только другая модель.
        return [step for step in rest if step[1] != model]
    return []


# Режим, с которым приложение стартует, пока в настройках не выбран другой. В docs/ai.js - такой же.
DEFAULT_PROVIDER = 2

# Адрес своего прокси (см. proxy/), например "https://travel-ai.example.workers.dev/v1". Если указан,
# ключ пользователям не нужен, а режим выбирается на прокси. То же значение - в docs/ai.js.
PROXY_URL = ""


def validate_base_url(url):
    """
    Возвращает текст ошибки или None. На этот адрес уходит API-ключ, поэтому открытый http
    разрешён только для локального сервера на этом же компьютере.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return "Некорректный адрес сервера."
    if not host:
        return "Укажите полный адрес сервера, начиная с https://"
    if parts.scheme == "https":
        return None
    if parts.scheme == "http" and host in ("localhost", "127.0.0.1", "::1"):
        return None
    return "Адрес сервера должен начинаться с https:// - иначе ключ уйдёт по сети незашифрованным."


class AISettings:
    """Настройки ИИ. Хранятся только в пользовательской папке данных, в репозиторий не попадают."""

    DEFAULTS = {
        "provider": DEFAULT_PROVIDER,
        "api_key": "",    # ключ режима 1
        "api_key_2": "",  # ключ режима 2
        "base_url": "",  # свой сервер вместо адреса режима
        "departure_city": "Алматы",
        "citizenship": "Казахстан",
        "travelers": 1,
    }

    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "ai_settings.json")
        self.values = dict(self.DEFAULTS)
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                stored = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(stored, dict):
            return
        for key, default in self.DEFAULTS.items():
            if type(stored.get(key)) is type(default):
                self.values[key] = stored[key]
        if self.values["provider"] not in PROVIDERS:
            self.values["provider"] = DEFAULT_PROVIDER
        self.values["travelers"] = min(20, max(1, self.values["travelers"]))

    def save(self, **changes):
        self.values.update(changes)
        # 0o600 - файл с ключом читает только сам пользователь (на Windows папка %APPDATA%
        # и так закрыта от других учётных записей).
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.values, f, ensure_ascii=False, indent=2)

    def __getattr__(self, name):
        values = self.__dict__.get("values", {})
        if name in values:
            return values[name]
        raise AttributeError(name)

    @property
    def _provider(self):
        return PROVIDERS[self.values["provider"]]

    @property
    def effective_key(self):
        """Ключ текущего режима из настроек, а если он не задан - из переменной окружения."""
        return self.values[self._provider["key"]] or os.environ.get(self._provider["env"], "").strip()

    @property
    def effective_model(self):
        return self._provider["model"]

    @property
    def models(self):
        """Модели в порядке попыток. Прокси перебирает их сам, поэтому ему хватает одной."""
        if PROXY_URL and not self.values["base_url"]:
            return [self.effective_model]
        models = [self.effective_model] + self._provider["fallbacks"]
        preferred = PREFERRED_MODELS.get(self.values["provider"])
        if preferred in models:
            models.remove(preferred)
            models.insert(0, preferred)
        return models

    def remember_model(self, model):
        PREFERRED_MODELS[self.values["provider"]] = model

    @property
    def effective_base_url(self):
        return (self.values["base_url"] or PROXY_URL or self._provider["base_url"]).rstrip("/")

    @property
    def needs_key(self):
        """Прокси и свои серверы работают без ключа: он хранится на их стороне."""
        return not (self.values["base_url"] or PROXY_URL)

    @property
    def is_configured(self):
        return bool(self.effective_key) or not self.needs_key


####################################
######### КЛИЕНТ CHAT API ##########
####################################

# 429 бывает двух видов: временное "слишком часто" и "нет денег/исчерпан лимит расходов" - второе
# повтором не лечится. Точная причина в error.code, но error.type тоже может быть insufficient_quota.
BILLING_MARKERS = ("quota", "credit", "billing", "spend_limit", "usage_limit")


def parse_error(body):
    """(код, текст) ошибки из ответа API."""
    try:
        data = json.loads(body.decode("utf-8", "replace"))
        if isinstance(data, list) and data:  # режим 2 заворачивает ошибку в список
            data = data[0]
        err = data.get("error")
        if isinstance(err, dict):
            code = " ".join(str(err[k]) for k in ("code", "type", "status") if err.get(k))
            return code, str(err.get("message") or "")
        return "", str(err or "")
    except (ValueError, AttributeError):
        return "", ""


def is_billing_error(status, body):
    return status in (402, 429) and any(marker in parse_error(body)[0] for marker in BILLING_MARKERS)


def describe_http_error(status, body):
    """Переводит ответ API с ошибкой в понятный пользователю текст."""
    code, detail = parse_error(body)
    if status == 401 or is_key_error(status, detail):
        return "Ключ доступа не принят."
    if status == 429 and re.search(r"per.?day", body.decode("utf-8", "replace"), re.I):
        return "Дневной лимит ИИ исчерпан. Попробуйте завтра."
    if is_billing_error(status, body):
        return "На балансе сервиса ИИ нет средств или исчерпан лимит расходов. Пополните баланс ключа доступа."
    if status == 429:
        return "Слишком много запросов или исчерпан лимит. Подождите немного и повторите."
    if "model_not_found" in code or "NOT_FOUND" in code:
        base = "Модель ИИ недоступна для этого ключа доступа."
    elif status == 404:
        base = "Сервер ИИ не найден."
    elif status == 403:
        base = "Сервис ИИ запретил доступ."
    elif status >= 500:
        base = "Сервис ИИ сейчас перегружен. Попробуйте через пару минут."
    else:
        base = f"Сервис ИИ отклонил запрос (код {status})."
    return base


def is_key_error(status, detail):
    """Режим 2 сообщает о неверном ключе кодом 400, а об отозванном - 403."""
    return status in (400, 403) and bool(re.search(r"api key|authorization header", detail, re.I))


class ChatClient(QObject):
    """
    Асинхронный клиент. Одновременно выполняется один запрос: новый запрос или cancel()
    отменяют предыдущий, и его обработчики больше не вызываются.
    """

    JSON_TIMEOUT_MS = 120_000
    STREAM_IDLE_TIMEOUT_MS = 90_000

    def __init__(self, settings, manager, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.manager = manager
        self._reply = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_timeout)
        self._timed_out = False
        self._retry = None
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._run_retry)

    # ----- публичные методы -----

    def stream_chat(self, messages, on_delta, on_done, on_error):
        """Потоковый ответ. on_delta(кусок), on_done(весь текст, обрезан ли), on_error(str, часть текста)."""
        models = self.settings.models
        self._stream_chat(messages, on_delta, on_done, on_error, attempt_plan(models), self.settings.effective_model)

    def _stream_chat(self, messages, on_delta, on_done, on_error, plan, main_model, first_error=None):
        model = plan[0][1]
        state = {"buffer": b"", "raw": b"", "text": "", "truncated": False, "finished": False, "error": ""}

        def consume(chunk):
            state["raw"] += chunk
            state["buffer"] += chunk
            # Делим по переводам строк на уровне байтов: кириллица не рвётся посреди символа.
            *lines, state["buffer"] = state["buffer"].split(b"\n")
            for line in lines:
                self._consume_sse_line(line.strip(), state, on_delta)

        def finished(status, network_error):
            self._consume_sse_line(state["buffer"].strip(), state, on_delta)
            if status and status != 200:
                self._log_failure(model, status, state["raw"])
                error = first_error or describe_http_error(status, state["raw"])
                rest = next_plan(plan, status, state["raw"], main_model)
                if rest:
                    self._schedule(rest[0][0], lambda: self._stream_chat(
                        messages, on_delta, on_done, on_error, rest, main_model, error))
                else:
                    on_error(error, state["text"])
            elif state["error"]:
                print(f"[ChatClient] {state['error'][:300]}", file=sys.stderr)
                on_error("Сервис ИИ прервал ответ. Попробуйте ещё раз.", state["text"])
            elif network_error:
                on_error(network_error, state["text"])
            elif not state["text"]:
                # Некоторые совместимые сервисы игнорируют stream и отвечают обычным JSON.
                text, error = self._extract_message(state["raw"])
                if error:
                    on_error(error, "")
                else:
                    self.settings.remember_model(model)
                    on_delta(text)
                    on_done(text, False)
            elif not state["finished"]:
                on_error("Соединение оборвалось до конца ответа.", state["text"])
            else:
                self.settings.remember_model(model)
                on_done(state["text"], state["truncated"])

        self._post(
            {"model": model, "messages": messages, "stream": True},
            finished, on_error=lambda msg: on_error(msg, ""), on_chunk=consume,
        )

    def check_connection(self, on_success, on_error):
        """
        Проверка самым коротким настоящим запросом: список моделей отдаётся и при пустом балансе,
        поэтому по нему нельзя понять, заработает ли ИИ.
        """
        def finished(status, network_error, body=b""):
            if status == 200:
                on_success()
            else:
                if status:
                    self._log_failure(self.settings.effective_model, status, body)
                on_error(describe_http_error(status, body) if status else network_error)

        self._post({
            "model": self.settings.effective_model,
            "messages": [{"role": "user", "content": "Ответь одним словом: ок"}],
        }, finished, on_error)

    def cancel(self):
        self._retry_timer.stop()
        self._retry = None
        self._timer.stop()
        reply, self._reply = self._reply, None
        if reply is not None:
            reply.abort()
            reply.deleteLater()

    # ----- внутреннее -----

    @staticmethod
    def _log_failure(model, status, body):
        # Каждая неудачная попытка - в консоль, на экране только итог. Ключа в этих данных нет.
        code, detail = parse_error(body)
        print(f"[ChatClient] {model}: HTTP {status} {code}: {detail[:300]}", file=sys.stderr)

    def _schedule(self, delay_ms, action):
        """Откладывает повтор; cancel() его снимает."""
        self._retry = action
        self._retry_timer.start(delay_ms)

    def _run_retry(self):
        action, self._retry = self._retry, None
        if action:
            action()

    def _post(self, payload, finished, on_error, on_chunk=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(body, finished, on_error, on_chunk)

    def _send(self, body, finished, on_error, on_chunk=None):
        self.cancel()

        key = self.settings.effective_key
        if not key and self.settings.needs_key:
            on_error("ИИ не подключён.")
            return
        if not key.isascii() or any(c.isspace() for c in key):
            on_error("Ключ доступа содержит недопустимые символы.")
            return
        base_url = self.settings.effective_base_url
        url_error = validate_base_url(base_url)
        if url_error:
            on_error(url_error)
            return

        request = QNetworkRequest(QUrl(base_url + "/chat/completions"))
        # Перенаправления не выполняем: ключ должен уходить только на указанный адрес.
        request.setAttribute(QNetworkRequest.RedirectPolicyAttribute, QNetworkRequest.ManualRedirectPolicy)
        if key:
            request.setRawHeader(b"Authorization", b"Bearer " + key.encode("ascii"))
        request.setHeader(QNetworkRequest.ContentTypeHeader, "application/json; charset=utf-8")

        self._timed_out = False
        reply = self.manager.post(request, body)
        self._reply = reply
        idle_ms = self.STREAM_IDLE_TIMEOUT_MS if on_chunk else self.JSON_TIMEOUT_MS
        self._timer.start(idle_ms)

        def on_ready():
            if self._reply is reply:
                self._timer.start(idle_ms)
                on_chunk(bytes(reply.readAll()))

        def on_finished():
            if self._reply is not reply:
                return  # запрос отменён или вытеснен новым
            self._timer.stop()
            tail = bytes(reply.readAll())
            status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute) or 0
            network_error = None
            if self._timed_out:
                status = 0
                network_error = "ИИ слишком долго не отвечает. Проверьте интернет и повторите."
            elif reply.error() != QNetworkReply.NoError:
                print(f"[ChatClient] {reply.errorString()}", file=sys.stderr)
                network_error = "Нет связи с сервисом ИИ. Проверьте интернет и повторите."
            self._reply = None
            reply.deleteLater()

            if on_chunk:
                if tail:
                    on_chunk(tail)
                finished(status, network_error)
            else:
                finished(status, network_error, tail)

        if on_chunk:
            reply.readyRead.connect(on_ready)
        reply.finished.connect(on_finished)

    def _on_timeout(self):
        if self._reply is not None:
            self._timed_out = True
            self._reply.abort()  # вызовет finished, где ошибка и будет показана

    @staticmethod
    def _consume_sse_line(line, state, on_delta):
        if not line.startswith(b"data:"):
            return
        payload = line[5:].strip()
        if payload == b"[DONE]":
            state["finished"] = True
            return
        try:
            event = json.loads(payload.decode("utf-8", "replace"))
            if event.get("error"):
                err = event["error"]
                state["error"] = str(err.get("message") if isinstance(err, dict) else err)
                return
            choice = (event.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
        except (ValueError, AttributeError, IndexError, KeyError):
            return
        piece = delta.get("content") or delta.get("refusal") or ""
        if choice.get("finish_reason"):
            state["finished"] = True
            state["truncated"] = choice["finish_reason"] == "length"
        if piece:
            state["text"] += piece
            on_delta(piece)

    @staticmethod
    def _extract_message(body):
        """Возвращает (текст, None) или (None, ошибка) из обычного ответа Chat Completions."""
        try:
            choice = json.loads(body.decode("utf-8"))["choices"][0]
            message = choice["message"]
            if message.get("refusal"):
                return None, f"ИИ отказался отвечать: {message['refusal']}"
            text = message["content"]
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            return None, "Сервис ИИ прислал непонятный ответ. Попробуйте ещё раз."
        if choice.get("finish_reason") == "length":
            return None, "Ответ ИИ получился слишком длинным и обрезан. Попробуйте ещё раз."
        if not text:
            return None, "ИИ прислал пустой ответ. Попробуйте ещё раз."
        return text, None


####################################
########### КУРС ТЕНГЕ #############
####################################

class CurrencyConverter(QObject):
    """
    Курс USD -> KZT (open.er-api.com, без ключа, обновляется раз в сутки). Последний курс
    хранится на диске; если его ни разу не удалось получить, суммы показываются только в $ -
    выдуманный курс не подставляется.
    """

    URL = "https://open.er-api.com/v6/latest/USD"
    MAX_AGE = 12 * 3600

    def __init__(self, data_dir, manager, parent=None):
        super().__init__(parent)
        self.path = os.path.join(data_dir, "currency_cache.json")
        self.manager = manager
        self.rate = None
        self.fetched_at = 0.0
        self._reply = None
        self._waiting = []
        try:
            with open(self.path, encoding="utf-8") as f:
                cached = json.load(f)
            rate, fetched_at = float(cached["rate"]), float(cached["fetched_at"])
            if math.isfinite(rate) and rate > 0:
                self.rate, self.fetched_at = rate, fetched_at
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def ensure(self, callback=None):
        """Вызывает callback(), когда курс готов (или стало ясно, что обновить его нельзя)."""
        if self.rate and time.time() - self.fetched_at < self.MAX_AGE:
            if callback:
                callback()
            return
        if callback:
            self._waiting.append(callback)
        if self._reply is None:
            request = QNetworkRequest(QUrl(self.URL))
            self._reply = self.manager.get(request)
            self._reply.finished.connect(self._on_finished)
            # Таймер с контекстом reply: если ответ уже удалён, срабатывания не будет.
            timer = QTimer(self._reply)
            timer.setSingleShot(True)
            timer.timeout.connect(self._reply.abort)
            timer.start(8000)

    def _on_finished(self):
        reply, self._reply = self._reply, None
        try:
            if reply.error() == QNetworkReply.NoError:
                rate = float(json.loads(bytes(reply.readAll()).decode("utf-8"))["rates"]["KZT"])
                if math.isfinite(rate) and rate > 0:
                    self.rate, self.fetched_at = rate, time.time()
                    with open(self.path, "w", encoding="utf-8") as f:
                        json.dump({"rate": self.rate, "fetched_at": self.fetched_at}, f)
        except (OSError, ValueError, KeyError, TypeError):
            pass  # остаёмся на прежнем курсе (или без тенге)
        finally:
            reply.deleteLater()
        waiting, self._waiting = self._waiting, []
        for callback in waiting:
            callback()

    def format_kzt(self, usd):
        if not self.rate:
            return ""
        return f"{format_number(round(usd * self.rate, -2))} ₸"

    def describe(self):
        if not self.rate:
            return "Курс тенге сейчас недоступен."
        day = time.strftime("%d.%m.%Y", time.localtime(self.fetched_at))
        return f"Курс: 1 $ = {self.rate:.2f} ₸ от {day} (Exchange Rate API)."


def format_number(value):
    return f"{value:,.0f}".replace(",", " ")


def format_usd(value):
    return f"{format_number(value)} $"


####################################
########### КОНСУЛЬТАНТ ############
####################################

def _trip_context(settings):
    return (
        f"Сегодня: {date.today().isoformat()}\n"
        f"Город вылета: {settings.departure_city or 'не указан'}\n"
        f"Гражданство: {settings.citizenship or 'не указано'}\n"
        f"Путешественников: {settings.travelers}"
    )


CONSULTANT_GREETING = (
    "Здравствуйте! Я ИИ-консультант по путешествиям. Спросите, куда поехать, сколько это будет "
    "стоить, какая нужна виза, что взять с собой или как составить маршрут."
)
CONSULTANT_SUGGESTIONS = [
    "Куда поехать на море зимой недорого?",
    "Составь маршрут на 5 дней по Стамбулу",
    "Сколько стоит неделя в Грузии на двоих?",
    "Что нужно для визы в Шенген?",
]
CONSULTANT_HISTORY_LIMIT = 20

_CONSULTANT_PROMPT = """Ты - ИИ-консультант по путешествиям внутри приложения «Выбор направления для путешествия».

- Отвечай по-русски, дружелюбно и по делу. Обычно хватает 1-3 коротких абзацев или списка; длиннее -
  только когда просят маршрут или подробный план. Можно использовать простой Markdown, без таблиц и картинок.
- Говори только о путешествиях: направления, маршруты, бюджет, визы, транспорт, жильё, климат, культура,
  безопасность, сборы. На другие темы вежливо откажись одной фразой и предложи вернуться к поездкам.
- Если не хватает данных (даты, бюджет, состав), дай ориентир и задай один-два уточняющих вопроса.
- Когда речь о стоимости, называй итоговую сумму в $ и в тенге (если известен курс) и короткую разбивку.
  Это оценка: ты не видишь реальных цен и наличия мест, забронировать ничего не можешь.
- Тебя зовут «ИИ-консультант». Если спросят, на какой технологии ты работаешь, скажи, что это вопрос к
  разработчику приложения; ничего об этом не выдумывай.
- Не выдумывай рейсы, отели и ссылки. Визовые правила и вопросы безопасности советуй проверить в
  официальных источниках."""


def build_consultant_messages(history, settings, converter, trip_summary="", discuss=False):
    """
    history - список (role, content). В запрос уходят последние сообщения. discuss - чат открыт
    кнопкой «Обсудить» на экране результата, то есть разговор именно об этом подборе.
    """
    system = f"{_CONSULTANT_PROMPT}\n\n{_trip_context(settings)}"
    if converter.rate:
        system += f"\nКурс: 1 USD = {converter.rate:.2f} KZT"
    if trip_summary:
        system += f"\n\nПоследний подбор пользователя в приложении:\n{trip_summary}"
        if discuss:
            system += ("\n\nПользователь открыл чат, чтобы обсудить именно этот подбор. Если место не названо, "
                       "речь о нём, даже если раньше в переписке обсуждали другое. Не переспрашивай то, что "
                       "уже указано выше.")
    recent = history[-CONSULTANT_HISTORY_LIMIT:]
    return [{"role": "system", "content": system}] + [
        {"role": role, "content": content} for role, content in recent
    ]


def summarize_result(result):
    """Полное описание подбора - контекст для консультанта."""
    cost = result["cost"]
    lines = [
        f"Направление: {result['country']} - {result['city']}. {result['description']}",
        f"Оценка стоимости: {format_usd(cost['total_usd'])} на {cost['travelers']} чел. "
        f"({format_usd(cost['per_person_usd'])} на человека), ночей: {cost['nights']}",
    ]
    lines += [f"- {i['category']}: {format_usd(i['amount_usd'])}" for i in cost["items"]]
    criteria = result["criteria"]
    asked = [c for c in criteria if c["status"] != "any"]
    if asked:
        lines.append("Пожелания: " + "; ".join(f"{c['title'].lower()} - {c['wanted']}" for c in asked))
    lines.append("Место по базе приложения: " + "; ".join(f"{c['title'].lower()} - {c['actual']}" for c in criteria))
    compromises = [c["title"].lower() for c in asked if c["status"] in ("partial", "mismatch")]
    if compromises:
        lines.append("Не совпало полностью: " + ", ".join(compromises))
    if result.get("refinements"):
        lines.append("Уточнения пользователя: " + "; ".join(result["refinements"]))
    if "confidence" in result:
        lines.append(f"Соответствие запросу по расчёту приложения: {result['confidence']}%")
    if result.get("alternatives"):
        lines.append("Запасные варианты: " + "; ".join(
            f"{a['country']} - {a['city']} ({format_usd(a['total_usd'])})" for a in result["alternatives"]))
    return "\n".join(lines)


def result_intro(result, converter):
    """Первое сообщение чата, открытого с экрана результата (собирается на месте, без запроса к ИИ)."""
    cost = result["cost"]
    kzt = converter.format_kzt(cost["total_usd"])
    lines = [
        f"Вижу ваш подбор: **{result['country']} — {result['city']}**. {result['description']}",
        "",
        f"- Сумма: ≈ {format_usd(cost['total_usd'])}" + (f" ({kzt})" if kzt else "")
        + f" на {cost['travelers']} чел., ночей: {cost['nights']}",
    ]
    asked = [c["wanted"] for c in result["criteria"] if c["status"] != "any"]
    if asked:
        lines.append("- Ваш запрос: " + ", ".join(asked))
    if result.get("refinements"):
        lines.append("- Уточнения: " + "; ".join(result["refinements"]))
    if "confidence" in result:
        lines.append(f"- Соответствие запросу: {result['confidence']}%")
    if result.get("alternatives"):
        lines.append("- Запасные варианты: " + ", ".join(
            f"{a['country']} — {a['city']}" for a in result["alternatives"]))
    lines += ["", "Что обсудим?"]
    return "\n".join(lines)


def result_suggestions(result):
    cost = result["cost"]
    return [
        f"Составь маршрут: {result['country']}, {result['city']}, ночей: {cost['nights']}",
        f"Хватит ли {format_usd(cost['total_usd'])} на эту поездку?",
        "Нужна ли мне виза и какие документы?",
        "Когда лучше ехать и что взять с собой?",
    ]
