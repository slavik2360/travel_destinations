"""
Интерфейс ИИ: меню ИИ, ИИ-консультант (чат), настройки подключения и карточка стоимости.

Текст от модели нигде не вставляется как HTML: в QLabel он показывается как PlainText либо как
Markdown с вырезанными картинками и экранированными тегами.
"""

import re
import sys

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtNetwork import QNetworkAccessManager
from PyQt5.QtWidgets import (
    QButtonGroup, QDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QRadioButton, QScrollArea, QSizePolicy, QSpinBox, QVBoxLayout, QWidget,
)

from ai_service import (
    AISettings, CONSULTANT_GREETING, CONSULTANT_SUGGESTIONS, ChatClient,
    CurrencyConverter, PROVIDERS, build_consultant_messages, format_usd,
    result_intro, result_suggestions, summarize_result, validate_base_url,
)
from smart_engine import BUDGET_STATUS_TEXT

INPUT_STYLE = (
    "QLineEdit, QSpinBox, QPlainTextEdit { background-color: white; "
    "border: 1px solid #cfe3ee; border-radius: 6px; padding: 7px; }"
)


# Неприметные кнопки: шестерёнка в углу и «Дополнительно».
QUIET_BUTTON_STYLE = (
    "QPushButton { background-color: transparent; border: none; color: #a9b7c0; padding: 2px; font-weight: 400; }"
    "QPushButton:hover { color: #2f9ed1; background-color: transparent; }"
)


class AIContext:
    """Общие для всех окон объекты ИИ."""

    def __init__(self, data_dir, parent=None):
        self.parent = parent
        self.settings = AISettings(data_dir)
        self.manager = QNetworkAccessManager(parent)
        self.converter = CurrencyConverter(data_dir, self.manager, parent)
        self.last_result = None  # последний подбор - контекст для консультанта

    def new_client(self, parent):
        return ChatClient(self.settings, self.manager, parent)


def chat_error_text(message):
    """Текст ошибки для чата: без технических подробностей о подключении."""
    if message.startswith("Нет связи"):
        return "Нет связи с интернетом. Проверьте подключение и повторите."
    return "Консультант сейчас занят. Попробуйте ещё раз через минуту."


def plain_label(text, style="", align=None):
    """QLabel, который никогда не трактует текст как разметку."""
    label = QLabel()
    label.setTextFormat(Qt.PlainText)
    label.setText(text)
    label.setWordWrap(True)
    if style:
        label.setStyleSheet(style)
    if align is not None:
        label.setAlignment(align)
    return label


def safe_markdown(text):
    """Markdown без картинок и HTML: модель не должна подгружать файлы или рисовать свою вёрстку."""
    text = re.sub(r"!\[([^\]]*)\]", r"[\1]", text)
    return text.replace("<", "&lt;")


####################################
############ СТОИМОСТЬ #############
####################################

class CostCard(QFrame):
    """Сумма путешествия: итог в $ и ₸, разбивка и сверка с бюджетом."""

    def __init__(self, cost, converter, parent=None):
        super().__init__(parent)
        self.cost = cost
        self.converter = converter
        self.setObjectName("costCard")
        self.setStyleSheet(
            "QFrame#costCard { background-color: white; border: 1px solid #cfe3ee; border-radius: 10px; }"
            "QFrame#costCard QLabel { background-color: transparent; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)

        layout.addWidget(plain_label("💵 Сумма путешествия", "font-size: 13px; font-weight: 600; color: #4c6c82;",
                                     Qt.AlignCenter))
        self.total_label = plain_label("", "font-size: 22px; font-weight: 700;", Qt.AlignCenter)
        layout.addWidget(self.total_label)
        self.detail_label = plain_label("", "font-size: 13px; color: #4c6c82;", Qt.AlignCenter)
        layout.addWidget(self.detail_label)

        if cost["budget_status"]:
            layout.addWidget(plain_label(BUDGET_STATUS_TEXT[cost["budget_status"]],
                                         "font-size: 13px; font-weight: 600;", Qt.AlignCenter))

        for item in cost["items"]:
            row = QHBoxLayout()
            row.addWidget(plain_label(item["category"], "font-size: 13px;"), 1)
            amount = plain_label(format_usd(item["amount_usd"]), "font-size: 13px; font-weight: 600;", Qt.AlignRight)
            amount.setWordWrap(False)
            row.addWidget(amount)
            layout.addLayout(row)

        note = ("Ориентир: уровень цен направления, длительность и типичные доли расходов. "
                "Перед покупкой сверьтесь с реальными ценами.")
        self.note_label = plain_label(note, "font-size: 11px; color: #4c6c82;", Qt.AlignCenter)
        layout.addWidget(self.note_label)
        self.rate_label = plain_label("", "font-size: 11px; color: #4c6c82;", Qt.AlignCenter)
        layout.addWidget(self.rate_label)

        self._refresh()
        converter.ensure(self._refresh_if_alive)

    def _refresh_if_alive(self):
        try:
            self._refresh()
        except RuntimeError:
            pass  # карточку уже закрыли, пока загружался курс

    def _refresh(self):
        cost = self.cost
        kzt = self.converter.format_kzt(cost["total_usd"])
        self.total_label.setText(f"≈ {format_usd(cost['total_usd'])}" + (f"  ·  {kzt}" if kzt else ""))
        parts = [f"на {cost['travelers']} чел."]
        if cost["nights"]:
            parts.append(f"ночей: {cost['nights']}")
        if cost["travelers"] > 1:
            parts.append(f"≈ {format_usd(cost['per_person_usd'])} на человека")
        self.detail_label.setText(" · ".join(parts))
        self.rate_label.setText(self.converter.describe())


####################################
############ НАСТРОЙКИ #############
####################################

class AISettingsDialog(QDialog):
    """Параметры поездки; подключение ИИ свёрнуто в раздел «Дополнительно»."""

    def __init__(self, ai, parent=None):
        super().__init__(parent)
        self.ai = ai
        self.setWindowTitle("Параметры")
        self.setMinimumWidth(520)
        self.setStyleSheet(INPUT_STYLE)
        self._client = None

        settings = ai.settings
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)

        title = QLabel("Параметры поездки")
        title.setObjectName("title")
        layout.addWidget(title)
        layout.addWidget(plain_label(
            "Нужны для суммы поездки и советов консультанта. Подбор работает на этом компьютере, а "
            "сообщения консультанту вместе с этими данными и последним подбором обрабатываются на "
            "внешнем сервере.", "font-size: 13px; color: #4c6c82;"))

        form = QFormLayout()
        form.setSpacing(10)
        self.city_input = QLineEdit(settings.departure_city)
        form.addRow("Город вылета", self.city_input)
        self.citizenship_input = QLineEdit(settings.citizenship)
        form.addRow("Гражданство", self.citizenship_input)
        self.travelers_input = QSpinBox()
        self.travelers_input.setRange(1, 20)
        self.travelers_input.setValue(settings.travelers)
        form.addRow("Путешественников", self.travelers_input)
        layout.addLayout(form)

        self.more_btn = QPushButton("Дополнительно ▸")
        self.more_btn.setCheckable(True)
        self.more_btn.setAutoDefault(False)
        self.more_btn.setCursor(Qt.PointingHandCursor)
        self.more_btn.setStyleSheet(QUIET_BUTTON_STYLE + "QPushButton { text-align: left; font-size: 12px; }")
        self.more_btn.toggled.connect(self._toggle_more)
        layout.addWidget(self.more_btn, 0, Qt.AlignLeft)

        self.more_box = self._build_connection(settings)
        self.more_box.hide()
        layout.addWidget(self.more_box)

        self.status_label = plain_label("", "font-size: 13px;")
        self.status_label.hide()
        layout.addWidget(self.status_label)

        row = QHBoxLayout()
        cancel_btn = QPushButton("Отмена")
        cancel_btn.setObjectName("noButton")
        cancel_btn.setAutoDefault(False)
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton("Сохранить")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self.save)
        row.addWidget(cancel_btn)
        row.addWidget(save_btn)
        layout.addLayout(row)

    def _build_connection(self, settings):
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        form = QFormLayout()
        form.setSpacing(10)

        # У каждого режима свой ключ: при переключении поле показывает ключ выбранного режима.
        self._keys = {number: settings.values[p["key"]] for number, p in PROVIDERS.items()}
        self._provider = settings.provider
        self.provider_group = QButtonGroup(self)
        provider_row = QHBoxLayout()
        for number in PROVIDERS:
            radio = QRadioButton(str(number))
            radio.setChecked(number == self._provider)
            self.provider_group.addButton(radio, number)
            provider_row.addWidget(radio)
        provider_row.addStretch()
        self.provider_group.buttonToggled.connect(self._on_provider_toggled)
        form.addRow("Режим", provider_row)

        self.key_input = QLineEdit(self._keys[self._provider])
        self.key_input.setEchoMode(QLineEdit.Password)
        self.key_input.setPlaceholderText("Хранится только на этом компьютере")
        show_btn = QPushButton("👁")
        show_btn.setObjectName("secondary")
        show_btn.setCheckable(True)
        show_btn.setFixedWidth(56)
        show_btn.setAutoDefault(False)
        show_btn.setToolTip("Показать ключ")
        show_btn.toggled.connect(
            lambda on: self.key_input.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        key_row = QHBoxLayout()
        key_row.addWidget(self.key_input, 1)
        key_row.addWidget(show_btn)
        form.addRow("Ключ доступа", key_row)

        self.url_input = QLineEdit(settings.base_url)
        self.url_input.setPlaceholderText("По умолчанию")
        form.addRow("Сервер", self.url_input)
        layout.addLayout(form)

        self.check_btn = QPushButton("Проверить подключение")
        self.check_btn.setObjectName("secondary")
        self.check_btn.setAutoDefault(False)
        self.check_btn.clicked.connect(self.check)
        layout.addWidget(self.check_btn)
        return box

    def _toggle_more(self, opened):
        self.more_btn.setText("Дополнительно ▾" if opened else "Дополнительно ▸")
        self.more_box.setVisible(opened)
        self.adjustSize()

    def _on_provider_toggled(self, radio, checked):
        if not checked:
            return
        self._keys[self._provider] = self.key_input.text().strip()
        self._provider = self.provider_group.id(radio)
        self.key_input.setText(self._keys[self._provider])
        self.status_label.hide()

    def _collect(self):
        """Значения полей или None, если что-то заполнено неверно."""
        self._keys[self._provider] = self.key_input.text().strip()
        values = {
            "provider": self._provider,
            "base_url": self.url_input.text().strip().rstrip("/"),
            "departure_city": self.city_input.text().strip(),
            "citizenship": self.citizenship_input.text().strip(),
            "travelers": self.travelers_input.value(),
        }
        for number, provider in PROVIDERS.items():
            values[provider["key"]] = self._keys[number]
        error = validate_base_url(values["base_url"]) if values["base_url"] else None
        if error:
            self._status(error, ok=False)
            return None
        return values

    def _status(self, text, ok=True):
        self.status_label.setStyleSheet(f"font-size: 13px; color: {'#1c7c43' if ok else '#b3261e'};")
        self.status_label.setText(text)
        self.status_label.show()

    def check(self):
        values = self._collect()
        if values is None:
            return
        # Проверяем введённые значения, не сохраняя их.
        draft = AISettings.__new__(AISettings)
        draft.path = None
        draft.values = {**self.ai.settings.values, **values}
        self._client = ChatClient(draft, self.ai.manager, self)
        self.check_btn.setEnabled(False)
        self._status("Проверяю подключение...")

        def ok():
            self.check_btn.setEnabled(True)
            self._status("Подключение работает, ИИ отвечает.")

        def fail(message):
            self.check_btn.setEnabled(True)
            self._status(message, ok=False)

        self._client.check_connection(ok, fail)

    def save(self):
        values = self._collect()
        if values is None:
            return
        try:
            self.ai.settings.save(**values)
        except OSError as e:
            self._status(f"Не удалось сохранить настройки: {e}", ok=False)
            return
        self.accept()

    def done(self, code):
        if self._client:
            self._client.cancel()
        super().done(code)


####################################
############ МЕНЮ ИИ ###############
####################################

class AIMenuWindow(QDialog):
    """Отдельное меню возможностей ИИ."""

    def __init__(self, ai, db, start_wizard, parent=None):
        super().__init__(parent)
        self.ai = ai
        self.db = db
        self.start_wizard = start_wizard
        self.setWindowTitle("Меню ИИ")
        self.setMinimumSize(480, 460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 12, 40, 32)
        layout.setSpacing(14)

        gear = QPushButton("⚙")
        gear.setFixedSize(26, 26)
        gear.setAutoDefault(False)
        gear.setCursor(Qt.PointingHandCursor)
        gear.setToolTip("Параметры")
        gear.setAccessibleName("Параметры")
        gear.setStyleSheet(QUIET_BUTTON_STYLE + "QPushButton { font-size: 16px; }")
        gear.clicked.connect(lambda _=False: self.open_settings())
        corner = QHBoxLayout()
        corner.addStretch()
        corner.addWidget(gear)
        layout.addLayout(corner)

        icon = QLabel("🤖")
        icon.setObjectName("icon")
        icon.setAlignment(Qt.AlignCenter)
        layout.addWidget(icon)
        title = QLabel("ИИ для путешествий")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)
        self.status_label = plain_label("", "font-size: 13px; color: #4c6c82;", Qt.AlignCenter)
        layout.addWidget(self.status_label)

        for text, name, handler in (
            ("💬 ИИ-консультант", "", self.open_consultant),
            ("🧭 Подбор путешествия с ИИ", "", self.start_ai_wizard),
            ("Назад", "noButton", self.accept),
        ):
            btn = QPushButton(text)
            if name:
                btn.setObjectName(name)
            btn.setAutoDefault(False)
            btn.clicked.connect(lambda _=False, h=handler: h())
            layout.addWidget(btn)
        layout.addStretch()
        self.refresh_status()

    def refresh_status(self):
        settings = self.ai.settings
        if settings.is_configured:
            self.status_label.setText("Консультант подключён и готов к работе")
        else:
            self.status_label.setText("Консультант не подключён, подбор работает")

    def open_consultant(self):
        AIConsultantWindow(self.ai, self.db, parent=self).exec_()
        self.refresh_status()

    def open_settings(self):
        AISettingsDialog(self.ai, self).exec_()
        self.refresh_status()

    def start_ai_wizard(self):
        self.start_wizard(True)
        self.refresh_status()


####################################
########### КОНСУЛЬТАНТ ############
####################################

class MessageInput(QPlainTextEdit):
    """Enter - отправить, Shift+Enter - новая строка."""

    submitted = pyqtSignal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not event.modifiers() & Qt.ShiftModifier:
            self.submitted.emit()
            return
        super().keyPressEvent(event)


class AIConsultantWindow(QDialog):
    """Чат о путешествиях. Переписка хранится в локальной базе (таблица chat_messages)."""

    def __init__(self, ai, db, result=None, parent=None):
        super().__init__(parent)
        self.ai = ai
        self.db = db
        # discuss - чат открыт с экрана результата: сразу показываем подбор и вопросы именно о нём.
        self.discuss = result is not None
        self.result = result or ai.last_result
        self.client = ai.new_client(self)
        self.history = db.get_chat_messages()
        self._answer_label = None
        self._answer_text = ""
        self._pending_question = None

        # Ответ приходит мелкими кусками - перерисовываем не чаще 20 раз в секунду.
        self._render_timer = QTimer(self)
        self._render_timer.setInterval(50)
        self._render_timer.timeout.connect(self._render_answer)

        self.setWindowTitle("ИИ-консультант")
        self.setMinimumSize(520, 560)
        self.resize(640, 720)
        self.setStyleSheet(INPUT_STYLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("💬 ИИ-консультант")
        title.setObjectName("title")
        header.addWidget(title, 1)
        new_btn = QPushButton("Новый диалог")
        new_btn.setObjectName("secondary")
        new_btn.setAutoDefault(False)
        new_btn.setStyleSheet("padding: 6px 12px; font-size: 13px;")
        new_btn.clicked.connect(lambda _=False: self.new_dialog())
        header.addWidget(new_btn)
        layout.addLayout(header)

        if self.result and not self.discuss:
            layout.addWidget(plain_label(
                f"Консультант знает о вашем подборе: {self.result['country']} — {self.result['city']}",
                "font-size: 12px; color: #4c6c82;"))

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        self.messages_layout = QVBoxLayout(content)
        self.messages_layout.setContentsMargins(0, 0, 8, 0)
        self.messages_layout.setSpacing(8)
        self.messages_layout.addStretch()
        self.scroll.setWidget(content)
        self._stick_to_bottom = True
        bar = self.scroll.verticalScrollBar()
        bar.rangeChanged.connect(lambda _min, top: bar.setValue(top) if self._stick_to_bottom else None)
        bar.valueChanged.connect(self._on_scrolled)
        layout.addWidget(self.scroll, 1)

        self.suggestions = QWidget()
        chips = QVBoxLayout(self.suggestions)
        chips.setContentsMargins(0, 0, 0, 0)
        chips.setSpacing(6)
        for text in result_suggestions(self.result) if self.discuss else CONSULTANT_SUGGESTIONS:
            chip = QPushButton(text)
            chip.setObjectName("secondary")
            chip.setAutoDefault(False)
            chip.setStyleSheet("padding: 6px; font-size: 13px; border-width: 1px;")
            chip.clicked.connect(lambda _=False, t=text: self.ask(t))
            chips.addWidget(chip)
        layout.addWidget(self.suggestions)

        self.error_label = plain_label("", "font-size: 13px; color: #b3261e;")
        self.error_label.hide()
        layout.addWidget(self.error_label)

        row = QHBoxLayout()
        self.input = MessageInput()
        self.input.setPlaceholderText("Спросите о поездке... (Enter - отправить)")
        self.input.setFixedHeight(64)
        self.input.submitted.connect(self.send)
        row.addWidget(self.input, 1)
        self.send_btn = QPushButton("Отправить")
        self.send_btn.setAutoDefault(False)
        self.send_btn.clicked.connect(self.on_send_clicked)
        row.addWidget(self.send_btn)
        layout.addLayout(row)

        layout.addWidget(plain_label(
            "Не указывайте личные данные: сообщения обрабатываются на внешнем сервере.",
            "font-size: 11px; color: #4c6c82;", Qt.AlignCenter))

        self.ai.converter.ensure()
        self._rebuild()
        self.input.setFocus()

    # ----- лента сообщений -----

    def _on_scrolled(self, value):
        # Если пользователь отмотал вверх, не дёргаем его вниз при каждом куске ответа.
        bar = self.scroll.verticalScrollBar()
        self._stick_to_bottom = value >= bar.maximum() - 40

    def _rebuild(self):
        while self.messages_layout.count() > 1:
            widget = self.messages_layout.takeAt(0).widget()
            if widget:
                widget.hide()
                widget.deleteLater()
        if not self.discuss:
            self._add_bubble("assistant", CONSULTANT_GREETING)
        for role, content in self.history:
            self._add_bubble(role, content)
        self._asked = False
        if self.discuss:
            # После прежней переписки: так видно, что дальше речь о новом подборе.
            self._intro_label = self._add_bubble("assistant", result_intro(self.result, self.ai.converter))
            self.ai.converter.ensure(self._refresh_intro)
        self._update_suggestions()

    def _refresh_intro(self):
        try:
            self._intro_label.setText(safe_markdown(result_intro(self.result, self.ai.converter)))
        except RuntimeError:
            pass  # окно закрыли, пока загружался курс

    def _update_suggestions(self):
        self.suggestions.setVisible(not self._asked and (self.discuss or not self.history))

    def _add_bubble(self, role, text):
        label = QLabel()
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        if role == "user":
            label.setTextFormat(Qt.PlainText)
            label.setText(text)
            label.setStyleSheet("background-color: #2f9ed1; color: white; border-radius: 10px; padding: 10px;")
            row_layout.addStretch(1)
            row_layout.addWidget(label, 4)
        else:
            label.setTextFormat(Qt.MarkdownText)
            label.setText(safe_markdown(text))
            label.setStyleSheet("background-color: white; border: 1px solid #cfe3ee; "
                                "border-radius: 10px; padding: 10px;")
            row_layout.addWidget(label, 6)
            row_layout.addStretch(1)
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, row)
        return label

    def _remove_last_bubble(self):
        widget = self.messages_layout.takeAt(self.messages_layout.count() - 2).widget()
        widget.hide()
        widget.deleteLater()

    # ----- отправка -----

    @property
    def busy(self):
        return self._answer_label is not None

    def on_send_clicked(self):
        if self.busy:
            self.stop()
        else:
            self.send()

    def send(self):
        text = self.input.toPlainText().strip()
        if text and not self.busy:
            self.ask(text)

    def ask(self, text):
        if self.busy:
            return
        if not self.ai.settings.is_configured:
            self._show_error("ИИ не подключён.")
            return
        self.error_label.hide()
        self.suggestions.hide()
        self.input.clear()
        self._stick_to_bottom = True
        self._pending_question = text
        self._add_bubble("user", text)
        self._answer_text = ""
        self._answer_label = self._add_bubble("assistant", "Печатает...")
        self.send_btn.setText("Стоп")

        summary = summarize_result(self.result) if self.result else ""
        messages = build_consultant_messages(
            self.history + [("user", text)], self.ai.settings, self.ai.converter, summary, self.discuss)
        self._render_timer.start()
        self.client.stream_chat(messages, self._on_delta, self._on_done, self._on_error)

    def _on_delta(self, piece):
        self._answer_text += piece

    def _render_answer(self):
        if self._answer_label is not None and self._answer_text:
            text = safe_markdown(self._answer_text)
            if self._answer_label.text() != text:
                self._answer_label.setText(text)

    def _on_done(self, _text, truncated):
        if truncated:
            self._show_error("Ответ получился длинным. Напишите «продолжи», и я допишу.")
        self._commit()

    def _on_error(self, message, _partial):
        # В чате - только нейтральный текст от имени консультанта. Подробная причина видна
        # в настройках («Проверить подключение») и в консоли.
        print("[консультант]", message, file=sys.stderr)
        self._show_error(chat_error_text(message))
        self._commit()

    def stop(self):
        self.client.cancel()
        self._commit()

    def _commit(self):
        """Завершает ответ: сохраняет пару вопрос-ответ либо возвращает вопрос в поле ввода."""
        if not self.busy:
            return
        self._render_timer.stop()
        self._render_answer()
        question, self._pending_question = self._pending_question, None
        if self._answer_text.strip():
            for message in (("user", question), ("assistant", self._answer_text)):
                self.history.append(message)
                self.db.add_chat_message(*message)
            self._asked = True
        else:
            self._remove_last_bubble()
            self._remove_last_bubble()
            # Пока ждали ответ, пользователь мог начать печатать новое - не затираем.
            if not self.input.toPlainText().strip():
                self.input.setPlainText(question)
            self._update_suggestions()
        self._answer_label = None
        self.send_btn.setText("Отправить")

    def _show_error(self, message):
        self.error_label.setText(message)
        self.error_label.show()

    # ----- прочее -----

    def new_dialog(self):
        if self.history:
            answer = QMessageBox.question(
                self, "Новый диалог", "Удалить текущую переписку с консультантом?",
                QMessageBox.Yes | QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
        if self.busy:
            self.stop()
        self.db.clear_chat()
        self.history = []
        self.error_label.hide()
        self._rebuild()

    def done(self, code):
        if self.busy:
            self.stop()
        super().done(code)
