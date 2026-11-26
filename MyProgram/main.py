import sys
import os
import json
import re
import sqlite3
import random
from datetime import datetime

import PyQt5
_QT5_PLUGINS_DIR = os.path.join(os.path.dirname(PyQt5.__file__), "Qt5", "plugins")
os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", os.path.join(_QT5_PLUGINS_DIR, "platforms"))
# QT_QPA_PLATFORM_PLUGIN_PATH сам по себе указывает Qt только на папку с плагином окна
# (platforms/), поэтому без QT_PLUGIN_PATH Qt не находит plugins/imageformats и не может
# декодировать скачанные JPEG/GIF/... - в частности, из-за этого QPixmap.loadFromData()
# в DestinationImageLoader молча возвращал False на "чистом" pip-окружении (venv), хотя сама
# картинка по сети успешно скачивалась.
os.environ.setdefault("QT_PLUGIN_PATH", _QT5_PLUGINS_DIR)

from PyQt5.QtWidgets import *
from PyQt5.QtGui import *
from PyQt5.QtCore import *
from PyQt5.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply

from ai_service import format_usd
from smart_engine import (
    SmartEngine, compare_with_query, count_matches, empty_filters, estimate_cost, parse_wish,
)
from ai_windows import AIConsultantWindow, AIContext, AIMenuWindow, CostCard, plain_label

def _user_data_dir():
    """
    Папка для хранения базы знаний/истории, устойчивая к способу запуска приложения.

    os.path.dirname(__file__) ломается в собранном PyInstaller-приложении (--onefile
    распаковывает main.py во временную папку, которая удаляется при каждом закрытии
    приложения - вся история и добавленные вручную направления терялись бы при перезапуске),
    а для --onedir/.app это либо путь внутри пакета приложения (не всегда доступен для
    записи, например при установке в Program Files), либо не тот путь, который ожидает
    увидеть пользователь при переустановке новой версии. Поэтому база хранится в стандартной
    пользовательской папке данных - как это принято для обычных десктопных приложений.
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")

    data_dir = os.path.join(base, "TravelExpertSystem")
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


DB_PATH = os.path.join(_user_data_dir(), "travel_expert.db")

# Разовый перенос базы для тех, у кого она уже существовала рядом со скриптом (более ранние
# версии приложения хранили её именно там) - чтобы обновление не выглядело как потеря истории.
_LEGACY_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "travel_expert.db")
if not os.path.exists(DB_PATH) and os.path.exists(_LEGACY_DB_PATH):
    try:
        import shutil
        shutil.copy2(_LEGACY_DB_PATH, DB_PATH)
    except OSError:
        pass  # не критично - приложение просто создаст новую базу знаний с нуля

####################################
######### БАЗА ЗНАНИЙ (БД) #########
####################################

BUDGETS = [
    ("economy", "Экономный", 0, 500, "💰"),
    ("standard", "Средний", 500, 1500, "💼"),
    ("high", "Высокий", 1500, 4000, "💎"),
    ("luxury", "Люкс", 4000, 999999, "👑"),
]

CONTINENTS = [
    ("asia", "Азия", "🌏"),
    ("africa", "Африка", "🦁"),
    ("europe", "Европа", "🏰"),
    ("north_america", "Северная Америка", "🗽"),
    ("south_america", "Южная Америка", "🌴"),
    ("australia_oceania", "Австралия / Океания", "🐨"),
    ("antarctica", "Антарктида", "🧊"),
]

REST_TYPES = [
    ("beach", "Пляжный", "🏖️"),
    ("excursion", "Экскурсионный", "🏛️"),
    ("nature", "Природный", "⛰️"),
    ("active", "Активный", "🚵"),
    ("wellness", "Оздоровительный", "🧘"),
]

SEASONS = [
    ("spring", "Весна", "🌸"),
    ("summer", "Лето", "☀️"),
    ("autumn", "Осень", "🍂"),
    ("winter", "Зима", "❄️"),
]

DURATIONS = [
    ("short", "Короткая (2–4 дня)", "🕑"),
    ("week", "Неделя (5–9 дней)", "📅"),
    ("long", "Долгая (10–16 дней)", "🗓️"),
    ("extended", "Очень долгая (17+ дней)", "🧳"),
]

VISAS = [
    ("visa_free", "Без визы / по загранпаспорту", "🛂"),
    ("easy_visa", "Виза по прибытии или электронная", "📝"),
    ("advance_visa", "Готов оформлять визу заранее", "🏛️"),
]

INTERESTS = [
    ("gastronomy", "Гастрономия и местная кухня", "🍜"),
    ("nightlife", "Ночная жизнь и развлечения", "🎉"),
    ("family", "Семейный отдых с детьми", "👨‍👩‍👧"),
    ("romance", "Романтика и медовый месяц", "💞"),
    ("shopping", "Шоппинг и городской стиль", "🛍️"),
]

BUDGET_NAMES = {code: name for code, name, *_ in BUDGETS}
CONTINENT_NAMES = {code: name for code, name, _ in CONTINENTS}
REST_TYPE_NAMES = {code: name for code, name, _ in REST_TYPES}
SEASON_NAMES = {code: name for code, name, _ in SEASONS}
DURATION_NAMES = {code: name for code, name, _ in DURATIONS}
VISA_NAMES = {code: name for code, name, _ in VISAS}
INTEREST_NAMES = {code: name for code, name, _ in INTERESTS}

# Пользователь может ответить "нет" на все варианты в категории - тогда её код остаётся "any"
# ("не важно"). Без этой записи такие ответы отображались бы как буквальное слово "any".
for _names, _label in (
    (BUDGET_NAMES, "любой"), (CONTINENT_NAMES, "любой"), (REST_TYPE_NAMES, "любой"),
    (SEASON_NAMES, "любой"), (DURATION_NAMES, "любая"), (VISA_NAMES, "любой"), (INTEREST_NAMES, "любой"),
):
    _names["any"] = _label

# Ключи совпадают с QuestionWizard.CATEGORIES и smart_engine.CRITERIA.
QUERY_NAMES = {
    "budget": BUDGET_NAMES, "continent": CONTINENT_NAMES, "rest_type": REST_TYPE_NAMES,
    "season": SEASON_NAMES, "duration": DURATION_NAMES, "visa": VISA_NAMES, "interest": INTEREST_NAMES,
}

# (budget, continent, type, season, страна, город, описание) - исходный набор без новых 3 измерений,
# они достраиваются функцией _build_destinations() ниже (см. TAG_OVERRIDES и _EXTRA_DESTINATIONS)
_BASE_DESTINATIONS = [
    # ---------------- ЭКОНОМНЫЙ ----------------
    ("economy", "asia", "beach", "summer", "Вьетнам", "Нячанг", "Тёплое море, доступные цены, свежие морепродукты."),
    ("economy", "asia", "beach", "any", "Индонезия", "Бали", "Пляжи, доступный сёрфинг и гостеприимство местных жителей."),
    ("economy", "asia", "excursion", "any", "Вьетнам", "Ханой", "Древняя архитектура и недорогая уличная кухня."),
    ("economy", "asia", "nature", "any", "Непал", "Покхара", "Горные озёра и предгорья Гималаев за небольшие деньги."),
    ("economy", "asia", "active", "any", "Вьетнам", "Далат", "Треккинг и активити в горах по низким ценам."),
    ("economy", "asia", "wellness", "any", "Таиланд", "Чиангмай", "Тайский массаж, медитация и спокойная атмосфера."),

    ("economy", "africa", "beach", "any", "Египет", "Хургада", "Тёплое море, дайвинг, всё включено по доступной цене."),
    ("economy", "africa", "excursion", "any", "Египет", "Каир", "Пирамиды Гизы и древняя история за небольшой бюджет."),
    ("economy", "africa", "nature", "any", "Марокко", "Мерзуга", "Пустыня Сахара и ночёвки в берберских лагерях."),
    ("economy", "africa", "active", "any", "Марокко", "Атласские горы", "Недорогой треккинг в горах Атлас."),
    ("economy", "africa", "wellness", "any", "Марокко", "Марракеш", "Традиционные хаммамы по низким ценам."),

    ("economy", "europe", "beach", "summer", "Болгария", "Солнечный берег", "Доступные пляжи Чёрного моря."),
    ("economy", "europe", "excursion", "any", "Грузия", "Тбилиси", "Культура, кухня и гостеприимство по низким ценам."),
    ("economy", "europe", "nature", "any", "Грузия", "Казбеги", "Горные пейзажи Кавказа без больших затрат."),
    ("economy", "europe", "active", "any", "Черногория", "Дурмитор", "Недорогие треккинг-маршруты и каньоны."),
    ("economy", "europe", "wellness", "any", "Венгрия", "Будапешт", "Термальные купальни по доступным ценам."),

    ("economy", "north_america", "beach", "any", "Мексика", "Канкун (эконом-район)", "Доступные пляжи Карибского моря."),
    ("economy", "north_america", "excursion", "any", "Мексика", "Мехико", "Богатая история ацтеков за небольшой бюджет."),
    ("economy", "north_america", "nature", "any", "Мексика", "Оахака", "Природа и колоритные деревни."),
    ("economy", "north_america", "active", "any", "Мексика", "Сан-Кристобаль", "Недорогие горные и пещерные туры."),
    ("economy", "north_america", "wellness", "any", "Мексика", "Тепостлан", "Бюджетные спа на минеральных источниках."),

    ("economy", "south_america", "beach", "any", "Эквадор", "Монтаньита", "Доступный сёрф-курорт на тихоокеанском побережье."),
    ("economy", "south_america", "excursion", "any", "Перу", "Куско", "Наследие инков и Мачу-Пикчу за скромный бюджет."),
    ("economy", "south_america", "nature", "any", "Боливия", "Салар-де-Уюни", "Соляные равнины — одно из чудес природы."),
    ("economy", "south_america", "active", "any", "Перу", "Долина Колка", "Недорогой треккинг к каньону Колка."),
    ("economy", "south_america", "wellness", "any", "Колумбия", "Санта-Марта", "Спокойный отдых у подножия Сьерра-Невады."),

    ("economy", "australia_oceania", "beach", "any", "Индонезия", "Ломбок", "Бюджетная альтернатива Бали с чистыми пляжами."),
    ("economy", "australia_oceania", "excursion", "any", "Фиджи", "Сува", "Знакомство с культурой островов Фиджи."),
    ("economy", "australia_oceania", "nature", "any", "Индонезия", "Флорес", "Вулканические озёра Кэлимуту."),
    ("economy", "australia_oceania", "active", "any", "Индонезия", "Комодо", "Треккинг и наблюдение за драконами Комодо."),
    ("economy", "australia_oceania", "wellness", "any", "Индонезия", "Убуд", "Йога-ретриты по доступным ценам."),

    ("economy", "antarctica", "nature", "any", "Аргентина", "Ушуая", "Ворота в Антарктиду — бюджетный вариант знакомства с регионом."),
    ("economy", "antarctica", "active", "any", "Аргентина", "Ушуая", "Треккинг на краю света без круиза в Антарктиду."),

    # ---------------- СРЕДНИЙ ----------------
    ("standard", "asia", "beach", "any", "Таиланд", "Пхукет", "Пляжи, развлечения и комфортный сервис."),
    ("standard", "asia", "excursion", "any", "Таиланд", "Бангкок", "Храмы, рынки и насыщенная городская жизнь."),
    ("standard", "asia", "nature", "any", "Шри-Ланка", "Элла", "Чайные плантации и живописные горы."),
    ("standard", "asia", "active", "any", "Индонезия", "Бали (Убуд)", "Сёрфинг, вулканы и активные маршруты."),
    ("standard", "asia", "wellness", "any", "Шри-Ланка", "Коломбо", "Аюрведические спа-программы."),

    ("standard", "africa", "beach", "any", "Танзания", "Занзибар", "Белоснежные пляжи Индийского океана."),
    ("standard", "africa", "excursion", "any", "ЮАР", "Кейптаун", "Смешение культур, история и виды на океан."),
    ("standard", "africa", "nature", "any", "Танзания", "Серенгети", "Сафари и миграция животных."),
    ("standard", "africa", "active", "any", "Танзания", "Килиманджаро (подножие)", "Треккинг у подножия высочайшей вершины Африки."),
    ("standard", "africa", "wellness", "any", "Марокко", "Эс-Сувейра", "Спокойный отдых на атлантическом побережье."),

    ("standard", "europe", "beach", "summer", "Испания", "Барселона", "Пляжи и культура Средиземноморья."),
    ("standard", "europe", "excursion", "any", "Италия", "Рим", "История, архитектура, гастрономия."),
    ("standard", "europe", "nature", "any", "Австрия", "Зальцкаммергут", "Альпийские озёра и горные пейзажи."),
    ("standard", "europe", "active", "winter", "Австрия", "Инсбрук", "Горнолыжные курорты Альп."),
    ("standard", "europe", "active", "any", "Словения", "Бледское озеро", "Активный отдых в горах и на воде."),
    ("standard", "europe", "wellness", "any", "Чехия", "Карловы Вары", "Знаменитые термальные курорты."),

    ("standard", "north_america", "beach", "any", "США", "Майами", "Пляжи и атмосфера Флориды."),
    ("standard", "north_america", "excursion", "any", "США", "Нью-Йорк", "Музеи, архитектура и городская энергия."),
    ("standard", "north_america", "nature", "any", "Канада", "Джаспер", "Национальные парки и дикая природа."),
    ("standard", "north_america", "active", "autumn", "Канада", "Банф", "Треккинг и горные маршруты золотой осенью."),
    ("standard", "north_america", "active", "any", "Канада", "Банф", "Активный отдых в Скалистых горах."),
    ("standard", "north_america", "wellness", "any", "США", "Себастьян (Флорида)", "Спокойный прибрежный отдых и спа."),

    ("standard", "south_america", "beach", "any", "Бразилия", "Флорианополис", "Пляжи и сёрфинг южной Бразилии."),
    ("standard", "south_america", "excursion", "any", "Аргентина", "Буэнос-Айрес", "Танго, архитектура и богемная атмосфера."),
    ("standard", "south_america", "nature", "any", "Аргентина", "Патагония", "Ледники и нетронутая природа юга."),
    ("standard", "south_america", "active", "any", "Чили", "Пукон", "Вулканы, треккинг и активити на природе."),
    ("standard", "south_america", "wellness", "any", "Уругвай", "Пунта-дель-Эсте", "Спокойный курортный отдых на побережье."),

    ("standard", "australia_oceania", "beach", "any", "Австралия", "Голд-Кост", "Сёрфинг и знаменитые пляжи Австралии."),
    ("standard", "australia_oceania", "excursion", "any", "Новая Зеландия", "Окленд", "Культура маори и городской колорит."),
    ("standard", "australia_oceania", "nature", "any", "Новая Зеландия", "Роторуа", "Гейзеры и вулканическая природа."),
    ("standard", "australia_oceania", "active", "any", "Новая Зеландия", "Квинстаун", "Столица экстремального активного отдыха."),
    ("standard", "australia_oceania", "wellness", "any", "Фиджи", "Дэнэрау", "Спа-курорты на островах Фиджи."),

    ("standard", "antarctica", "nature", "any", "Аргентина", "Ушуая + острова", "Короткие круизы к субантарктическим островам."),
    ("standard", "antarctica", "active", "any", "Аргентина", "Ушуая", "Активные туры в проливе Бигль."),

    # ---------------- ВЫСОКИЙ ----------------
    ("high", "asia", "beach", "any", "Мальдивы", "Мале (виллы)", "Виллы на воде и premium-сервис."),
    ("high", "asia", "excursion", "any", "Япония", "Токио", "Технологии и традиции в одном мегаполисе."),
    ("high", "asia", "nature", "any", "Япония", "Хаконэ", "Виды на Фудзи и горячие источники."),
    ("high", "asia", "active", "any", "Япония", "Ниседко", "Премиальные горнолыжные курорты."),
    ("high", "asia", "wellness", "any", "Япония", "Хаконэ (онсэн)", "Традиционные японские онсэны высокого уровня."),

    ("high", "africa", "beach", "any", "Сейшелы", "Маэ", "Экзотика, природа и премиальный сервис."),
    ("high", "africa", "excursion", "any", "ЮАР", "Кейптаун (премиум)", "Винодельни, история и виды на океан."),
    ("high", "africa", "nature", "any", "Ботсвана", "Дельта Окаванго", "Эксклюзивное сафари в дикой природе."),
    ("high", "africa", "active", "any", "Танзания", "Килиманджаро", "Восхождение на высочайшую вершину Африки."),
    ("high", "africa", "wellness", "any", "Марокко", "Марракеш (риад-спа)", "Люксовые риады со спа-программами."),

    ("high", "europe", "beach", "any", "Франция", "Ницца", "Лазурный берег и высокий уровень сервиса."),
    ("high", "europe", "excursion", "any", "Франция", "Париж", "Романтика, культура и высокий сервис."),
    ("high", "europe", "nature", "any", "Швейцария", "Церматт", "Горы, премиальный отдых и виды на Маттерхорн."),
    ("high", "europe", "active", "autumn", "Швейцария", "Церматт", "Треккинг в Альпах осенью, уникальное сочетание природы и комфорта."),
    ("high", "europe", "active", "any", "Швейцария", "Церматт", "Горнолыжные и треккинговые маршруты премиум-класса."),
    ("high", "europe", "wellness", "any", "Швейцария", "Баден", "Элитные термальные и спа-курорты."),

    ("high", "north_america", "beach", "any", "США", "Гавайи", "Премиальные пляжные курорты Тихого океана."),
    ("high", "north_america", "excursion", "any", "США", "Нью-Йорк (премиум)", "Современный мегаполис и высокий уровень сервиса."),
    ("high", "north_america", "nature", "any", "Канада", "Банф", "Горы, премиальные отели и озёра ледникового происхождения."),
    ("high", "north_america", "active", "autumn", "Канада", "Банф", "Активный отдых и треккинг золотой осенью в Скалистых горах."),
    ("high", "north_america", "active", "any", "Канада", "Уистлер", "Премиальные горнолыжные и активные маршруты."),
    ("high", "north_america", "wellness", "any", "США", "Седона", "Известные спа- и wellness-курорты Аризоны."),

    ("high", "south_america", "beach", "any", "Бразилия", "Бузиос", "Элитный пляжный курорт недалеко от Рио."),
    ("high", "south_america", "excursion", "any", "Перу", "Мачу-Пикчу (премиум-тур)", "Наследие инков с комфортным сервисом."),
    ("high", "south_america", "nature", "any", "Чили", "Патагония (Торрес-дель-Пайне)", "Премиальные лоджи среди нетронутой природы."),
    ("high", "south_america", "active", "any", "Чили", "Патагония", "Активные экспедиции высокого уровня."),
    ("high", "south_america", "wellness", "any", "Чили", "Долина Кольчагуа", "Винный wellness-туризм."),

    ("high", "australia_oceania", "beach", "any", "Австралия", "Большой Барьерный риф", "Премиальные курорты у знаменитого рифа."),
    ("high", "australia_oceania", "excursion", "any", "Австралия", "Сидней", "Культурная столица с высоким уровнем сервиса."),
    ("high", "australia_oceania", "nature", "any", "Новая Зеландия", "Фьордленд", "Премиальные туры по фьордам."),
    ("high", "australia_oceania", "active", "any", "Новая Зеландия", "Квинстаун (премиум)", "Экстремальный активный отдых высокого уровня."),
    ("high", "australia_oceania", "wellness", "any", "Фиджи", "Лайкай", "Уединённые спа-острова Фиджи."),

    ("high", "antarctica", "nature", "any", "Антарктида", "Полуостров (круиз)", "Экспедиционный круиз к берегам Антарктиды."),
    ("high", "antarctica", "active", "any", "Антарктида", "Полуостров (экспедиция)", "Активная экспедиция с высадками на льду."),

    # ---------------- ЛЮКС ----------------
    ("luxury", "asia", "beach", "any", "Мальдивы", "Мале", "Роскошные курорты и уединение над водой."),
    ("luxury", "asia", "excursion", "any", "ОАЭ", "Дубай", "Эксклюзивная архитектура и высочайший сервис."),
    ("luxury", "asia", "nature", "any", "Бутан", "Паро", "Уединённые монастыри и эксклюзивные гималайские туры."),
    ("luxury", "asia", "active", "any", "Япония", "Хоккайдо (VIP)", "Приватные горнолыжные туры высшего класса."),
    ("luxury", "asia", "wellness", "any", "Таиланд", "Ко Самуи (VIP-спа)", "Элитные wellness-ретриты на частных виллах."),

    ("luxury", "africa", "beach", "any", "Сейшельские острова", "Виктория", "Экзотика, природа и премиальный сервис высшего уровня."),
    ("luxury", "africa", "excursion", "any", "Марокко", "Марракеш (VIP)", "Приватные туры и эксклюзивные риады."),
    ("luxury", "africa", "nature", "any", "Ботсвана", "Дельта Окаванго (люкс-сафари)", "Частные лоджи в самом сердце дикой природы."),
    ("luxury", "africa", "active", "any", "Танзания", "Килиманджаро (VIP-восхождение)", "Восхождение с полным премиальным сопровождением."),
    ("luxury", "africa", "wellness", "any", "Маврикий", "Порт-Луи", "Элитные оздоровительные курорты на острове."),

    ("luxury", "europe", "beach", "any", "Франция", "Ницца (Лазурный берег)", "Роскошные курорты и элитный сервис."),
    ("luxury", "europe", "excursion", "any", "Италия", "Амальфи", "Премиальные курорты и живописные пейзажи побережья."),
    ("luxury", "europe", "nature", "any", "Швейцария", "Санкт-Мориц", "Элитные альпийские курорты высшего класса."),
    ("luxury", "europe", "active", "any", "Швейцария", "Санкт-Мориц", "Люксовые горнолыжные маршруты."),
    ("luxury", "europe", "wellness", "any", "Швейцария", "Санкт-Мориц (VIP-спа)", "Элитные термальные и spa-программы."),

    ("luxury", "north_america", "beach", "any", "США", "Малибу", "Эксклюзивные виллы на побережье Калифорнии."),
    ("luxury", "north_america", "excursion", "any", "США", "Нью-Йорк (VIP)", "Премиальные апартаменты и частные туры по мегаполису."),
    ("luxury", "north_america", "nature", "any", "Канада", "Банф (люкс-лоджи)", "Эксклюзивные горные лоджи и частные экскурсии."),
    ("luxury", "north_america", "active", "any", "Канада", "Уистлер (VIP)", "Приватные горнолыжные инструкторы и премиум-сервис."),
    ("luxury", "north_america", "wellness", "any", "США", "Напа-Вэлли", "Винный wellness-туризм высшего класса."),

    ("luxury", "south_america", "beach", "any", "Бразилия", "Рио-де-Жанейро (VIP)", "Элитные пляжные курорты и частные виллы."),
    ("luxury", "south_america", "excursion", "any", "Перу", "Мачу-Пикчу (люкс-поезд)", "Путешествие на люксовом поезде Belmond."),
    ("luxury", "south_america", "nature", "any", "Чили", "Патагония (эксклюзив-лоджи)", "Частные эко-лоджи среди дикой природы."),
    ("luxury", "south_america", "active", "any", "Чили", "Патагония (VIP-экспедиция)", "Эксклюзивные экспедиции с личным гидом."),
    ("luxury", "south_america", "wellness", "any", "Аргентина", "Мендоса", "Элитный винный и spa-туризм."),

    ("luxury", "australia_oceania", "beach", "any", "Французская Полинезия", "Бора-Бора", "Виллы над водой и эксклюзивный сервис."),
    ("luxury", "australia_oceania", "excursion", "any", "Австралия", "Сидней (VIP)", "Премиальные частные туры по знаковым местам."),
    ("luxury", "australia_oceania", "nature", "any", "Новая Зеландия", "Милфорд-Саунд (люкс)", "Эксклюзивные круизы по фьордам."),
    ("luxury", "australia_oceania", "active", "any", "Новая Зеландия", "Квинстаун (VIP)", "Приватные экстремальные туры высшего уровня."),
    ("luxury", "australia_oceania", "wellness", "any", "Французская Полинезия", "Муреа", "Элитные спа на частных островах."),

    ("luxury", "antarctica", "nature", "any", "Антарктида", "Люкс-экспедиционный круиз", "Приватные каюты и вертолётные экскурсии на льдах."),
    ("luxury", "antarctica", "active", "any", "Антарктида", "VIP-экспедиция", "Эксклюзивные погружения и высадки с личным гидом."),
]

# Точечные "утверждения" для части направлений из базового списка: не переписывая все строки,
# доопределяем для самых характерных направлений подходящую длительность / визовый режим / интерес.
TAG_OVERRIDES = {
    ("Аргентина", "Ушуая"): {"duration": "long", "visa": "advance_visa"},
    ("Мальдивы", "Мале (виллы)"): {"interest": "romance"},
    ("Мальдивы", "Мале"): {"interest": "romance"},
    ("Французская Полинезия", "Бора-Бора"): {"interest": "romance"},
    ("Французская Полинезия", "Муреа"): {"interest": "romance"},
    ("Италия", "Амальфи"): {"interest": "romance"},
    ("Франция", "Париж"): {"interest": "gastronomy"},
    ("Франция", "Ницца"): {"interest": "romance"},
    ("Франция", "Ницца (Лазурный берег)"): {"interest": "romance"},
    ("Италия", "Рим"): {"interest": "gastronomy"},
    ("Таиланд", "Бангкок"): {"interest": "nightlife"},
    ("ОАЭ", "Дубай"): {"interest": "shopping"},
    ("США", "Нью-Йорк"): {"interest": "shopping"},
    ("США", "Нью-Йорк (премиум)"): {"interest": "shopping"},
    ("США", "Нью-Йорк (VIP)"): {"interest": "shopping"},
    ("США", "Майами"): {"interest": "nightlife"},
    ("США", "Гавайи"): {"interest": "romance"},
    ("Египет", "Хургада"): {"duration": "week"},
    ("Танзания", "Занзибар"): {"interest": "romance"},
    ("Швейцария", "Церматт"): {"duration": "week"},
    ("Швейцария", "Санкт-Мориц"): {"duration": "week"},
    ("Антарктида", "Полуостров (круиз)"): {"duration": "extended", "visa": "advance_visa"},
    ("Антарктида", "Полуостров (экспедиция)"): {"duration": "extended", "visa": "advance_visa"},
    ("Антарктида", "Люкс-экспедиционный круиз"): {"duration": "extended", "visa": "advance_visa"},
    ("Антарктида", "VIP-экспедиция"): {"duration": "extended", "visa": "advance_visa"},
    ("Новая Зеландия", "Квинстаун"): {"duration": "week"},
    ("Новая Зеландия", "Квинстаун (премиум)"): {"duration": "week"},
    ("Новая Зеландия", "Квинстаун (VIP)"): {"duration": "week"},
    ("Индонезия", "Бали"): {"interest": "romance"},
}

# Новые направления, у которых сразу заданы продолжительность / визовый режим / интересы -
# именно на них новые вопросы мастера реально влияют на результат.
# (budget, continent, type, season, duration, visa, interest, страна, город, описание)
_EXTRA_DESTINATIONS = [
    # ---------------- ЭКОНОМНЫЙ ----------------
    ("economy", "asia", "beach", "summer", "week", "visa_free", "family",
     "Вьетнам", "Фантьет", "Спокойный семейный пляжный отдых без визовых хлопот."),
    ("economy", "asia", "excursion", "any", "short", "easy_visa", "gastronomy",
     "Таиланд", "Чиангмай", "Короткая гастрономическая поездка по улочкам северного Таиланда."),
    ("economy", "asia", "nature", "any", "long", "visa_free", "romance",
     "Индонезия", "Гили Траванган", "Уединённый остров без машин для долгого романтичного отдыха."),

    ("economy", "africa", "beach", "any", "week", "easy_visa", "family",
     "Египет", "Шарм-эль-Шейх", "Семейный пляжный отдых с электронной визой по прибытии."),
    ("economy", "africa", "excursion", "any", "short", "easy_visa", "gastronomy",
     "Марокко", "Фес", "Короткое гастрономическое путешествие по пряным блюдам марокканской кухни."),
    ("economy", "africa", "active", "any", "long", "easy_visa", "nightlife",
     "Марокко", "Эс-Сувейра", "Сёрфинг днём и оживлённые вечера у океана."),

    ("economy", "europe", "excursion", "summer", "short", "visa_free", "romance",
     "Черногория", "Котор", "Романтичный короткий уикенд в средневековом городе у бухты."),
    ("economy", "europe", "beach", "summer", "week", "visa_free", "family",
     "Болгария", "Несебр", "Доступный семейный пляжный отдых на Чёрном море."),
    ("economy", "europe", "excursion", "any", "short", "visa_free", "gastronomy",
     "Грузия", "Кахетия", "Дегустационный тур по винодельням Кахетии за несколько дней."),

    ("economy", "north_america", "excursion", "any", "short", "advance_visa", "gastronomy",
     "Мексика", "Мехико", "Короткая гастрономическая поездка по стрит-фуду мексиканской столицы."),
    ("economy", "north_america", "beach", "any", "week", "advance_visa", "family",
     "Мексика", "Плайя-дель-Кармен", "Семейный пляжный отдых на Карибском побережье Мексики."),
    ("economy", "north_america", "active", "any", "long", "advance_visa", "family",
     "Мексика", "Сан-Кристобаль", "Долгая поездка с детьми постарше по горным городкам Чьяпаса."),

    ("economy", "south_america", "excursion", "any", "long", "advance_visa", "gastronomy",
     "Перу", "Лима", "Долгая поездка с акцентом на перуанскую гастрономию, признанную одной из лучших в мире."),
    ("economy", "south_america", "beach", "any", "week", "advance_visa", "nightlife",
     "Бразилия", "Ресифи", "Пляжи и оживлённая вечерняя жизнь северо-востока Бразилии."),
    ("economy", "south_america", "nature", "any", "extended", "advance_visa", "family",
     "Эквадор", "Баньос", "Длительное семейное путешествие с водопадами и природными термами."),

    ("economy", "australia_oceania", "beach", "any", "week", "easy_visa", "family",
     "Индонезия", "Ломбок (Сенггиги)", "Спокойный семейный пляжный отдых без визовых сложностей."),
    ("economy", "australia_oceania", "excursion", "any", "short", "easy_visa", "shopping",
     "Индонезия", "Джакарта", "Короткая поездка по шопинг-моллам и рынкам столицы Индонезии."),
    ("economy", "australia_oceania", "active", "any", "long", "easy_visa", "nightlife",
     "Индонезия", "Бали (Чангу)", "Сёрф-сцена и оживлённые бары серф-городка Чангу."),

    ("economy", "antarctica", "nature", "any", "week", "advance_visa", "gastronomy",
     "Аргентина", "Ушуая", "Неделя у ворот Антарктиды с дегустацией патагонской кухни и морепродуктов."),
    ("economy", "antarctica", "active", "any", "short", "advance_visa", "family",
     "Аргентина", "Ушуая (короткий тур)", "Короткая семейная поездка на край света без выхода в Антарктиду."),

    # ---------------- СРЕДНИЙ ----------------
    ("standard", "asia", "beach", "any", "week", "easy_visa", "romance",
     "Таиланд", "Краби", "Романтичный недельный отдых среди известняковых скал и лагун."),
    ("standard", "asia", "excursion", "any", "short", "easy_visa", "shopping",
     "Таиланд", "Бангкок", "Короткая поездка по торговым районам и ночным рынкам Бангкока."),
    ("standard", "asia", "active", "any", "long", "easy_visa", "family",
     "Шри-Ланка", "Элла", "Долгое семейное путешествие по чайным плантациям и горным тропам."),

    ("standard", "africa", "beach", "any", "week", "easy_visa", "romance",
     "Танзания", "Занзибар (Нунгви)", "Романтичный недельный отдых на белоснежных пляжах Занзибара."),
    ("standard", "africa", "nature", "any", "long", "advance_visa", "family",
     "Танзания", "Аруша", "Долгое сафари-путешествие, подходящее для семей с детьми школьного возраста."),
    ("standard", "africa", "excursion", "any", "short", "easy_visa", "gastronomy",
     "ЮАР", "Кейптаун", "Короткая поездка по винодельням и ресторанам Западного Кейпа."),

    ("standard", "europe", "excursion", "any", "short", "visa_free", "gastronomy",
     "Италия", "Болонья", "Короткий гастрономический тур по столице итальянской кухни."),
    ("standard", "europe", "beach", "summer", "week", "visa_free", "family",
     "Испания", "Коста-Брава", "Семейный недельный отдых на испанском побережье."),
    ("standard", "europe", "excursion", "any", "short", "visa_free", "romance",
     "Австрия", "Зальцбург", "Романтичный уикенд в городе музыки и альпийских пейзажей."),

    ("standard", "north_america", "excursion", "any", "week", "advance_visa", "family",
     "США", "Орландо", "Недельный семейный отдых с тематическими парками для детей."),
    ("standard", "north_america", "active", "autumn", "long", "advance_visa", "romance",
     "Канада", "Джаспер", "Долгое романтичное путешествие среди озёр и гор золотой осенью."),
    ("standard", "north_america", "excursion", "any", "short", "advance_visa", "nightlife",
     "США", "Лас-Вегас", "Короткая поездка с насыщенной ночной жизнью и шоу."),

    ("standard", "south_america", "excursion", "any", "long", "advance_visa", "gastronomy",
     "Аргентина", "Буэнос-Айрес + Мендоса", "Долгая поездка по стейк-хаусам столицы и винодельням Мендосы."),
    ("standard", "south_america", "beach", "any", "week", "advance_visa", "family",
     "Бразилия", "Порту-де-Галиньяс", "Семейный недельный отдых на спокойных пляжах северо-востока Бразилии."),
    ("standard", "south_america", "nature", "any", "extended", "advance_visa", "romance",
     "Чили", "Патагония (Пукон)", "Длительное романтичное путешествие среди вулканов и озёр юга."),

    ("standard", "australia_oceania", "excursion", "any", "short", "easy_visa", "shopping",
     "Австралия", "Мельбурн", "Короткая городская поездка с акцентом на шоппинг и кофейную культуру."),
    ("standard", "australia_oceania", "beach", "any", "week", "easy_visa", "family",
     "Австралия", "Саншайн-Кост", "Семейный недельный отдых на спокойном пляжном побережье."),
    ("standard", "australia_oceania", "nature", "any", "long", "easy_visa", "romance",
     "Новая Зеландия", "Милфорд-Саунд (тур)", "Долгое романтичное путешествие по фьордам Новой Зеландии."),

    ("standard", "antarctica", "nature", "any", "week", "advance_visa", "romance",
     "Аргентина", "Ушуая + круиз к островам", "Романтичная неделя с коротким круизом к субантарктическим островам."),
    ("standard", "antarctica", "active", "any", "long", "advance_visa", "family",
     "Аргентина", "Огненная Земля (нацпарк)", "Долгое путешествие по национальному парку на краю света."),

    # ---------------- ВЫСОКИЙ ----------------
    ("high", "asia", "beach", "any", "week", "easy_visa", "romance",
     "Мальдивы", "Мале (романтическая вилла)", "Романтичная неделя на приватной вилле над водой."),
    ("high", "asia", "excursion", "any", "short", "easy_visa", "gastronomy",
     "Япония", "Осака", "Короткий гастрономический тур по уличной еде и мишленовским ресторанам Осаки."),
    ("high", "asia", "active", "winter", "long", "easy_visa", "family",
     "Япония", "Хоккайдо", "Долгий семейный горнолыжный отдых со знаменитым японским порошковым снегом."),

    ("high", "africa", "nature", "any", "week", "advance_visa", "romance",
     "Ботсвана", "Дельта Окаванго (романтик-лодж)", "Романтичная неделя в уединённом лодже среди дикой природы."),
    ("high", "africa", "beach", "any", "week", "easy_visa", "family",
     "Сейшелы", "Праслин", "Семейный премиальный пляжный отдых на тропическом острове."),
    ("high", "africa", "active", "any", "long", "advance_visa", "family",
     "Танзания", "Килиманджаро (комфорт-треккинг)", "Долгий семейный треккинг с комфортными лоджами по пути."),

    ("high", "europe", "excursion", "any", "short", "visa_free", "gastronomy",
     "Франция", "Лион", "Короткий гастрономический тур по гастрономической столице Франции."),
    ("high", "europe", "beach", "summer", "week", "visa_free", "romance",
     "Италия", "Позитано", "Романтичная неделя на побережье Амальфи."),
    ("high", "europe", "active", "winter", "long", "visa_free", "family",
     "Швейцария", "Санкт-Мориц", "Долгий семейный горнолыжный отдых в престижном альпийском курорте."),

    ("high", "north_america", "excursion", "any", "short", "advance_visa", "shopping",
     "США", "Нью-Йорк (Пятая авеню)", "Короткая поездка с акцентом на шоппинг в центре Манхэттена."),
    ("high", "north_america", "beach", "any", "week", "advance_visa", "romance",
     "США", "Мауи (Гавайи)", "Романтичная неделя на одном из самых красивых островов Гавайев."),
    ("high", "north_america", "nature", "autumn", "long", "advance_visa", "family",
     "Канада", "Банф (премиум-лоджи)", "Долгий семейный отдых в премиальных лоджах Скалистых гор осенью."),

    ("high", "south_america", "excursion", "any", "long", "advance_visa", "gastronomy",
     "Перу", "Лима + Куско (гастро-тур)", "Долгий гастрономический и культурный тур по Перу высокого уровня."),
    ("high", "south_america", "beach", "any", "week", "advance_visa", "family",
     "Бразилия", "Бузиос (премиум)", "Семейный премиальный пляжный отдых недалеко от Рио."),
    ("high", "south_america", "nature", "any", "extended", "advance_visa", "romance",
     "Чили", "Патагония (Торрес-дель-Пайне, романтик)", "Длительное романтичное путешествие в премиальном эко-лодже."),

    ("high", "australia_oceania", "beach", "any", "week", "easy_visa", "romance",
     "Австралия", "Хайден-Айленд (Большой Барьерный риф)", "Романтичная неделя у знаменитого рифа."),
    ("high", "australia_oceania", "excursion", "any", "short", "easy_visa", "shopping",
     "Австралия", "Сидней (QVB и гавань)", "Короткая городская поездка с акцентом на шоппинг и виды на гавань."),
    ("high", "australia_oceania", "active", "any", "long", "easy_visa", "family",
     "Новая Зеландия", "Квинстаун (семейный формат)", "Долгий семейный активный отдых в столице экстрима Новой Зеландии."),

    ("high", "antarctica", "nature", "any", "extended", "advance_visa", "romance",
     "Антарктида", "Полуостров (романтик-круиз)", "Длительный романтичный экспедиционный круиз к берегам Антарктиды."),
    ("high", "antarctica", "active", "any", "long", "advance_visa", "family",
     "Антарктида", "Полуостров (семейная экспедиция)", "Долгая экспедиция, адаптированная для путешествий с детьми подросткового возраста."),

    # ---------------- ЛЮКС ----------------
    ("luxury", "asia", "beach", "any", "week", "easy_visa", "romance",
     "Мальдивы", "Мале (президентская вилла)", "Романтичная неделя в самой эксклюзивной вилле курорта."),
    ("luxury", "asia", "excursion", "any", "short", "easy_visa", "shopping",
     "ОАЭ", "Дубай (Дубай Молл, VIP)", "Короткий шоппинг-тур высшего уровня по Дубаю."),
    ("luxury", "asia", "active", "winter", "long", "easy_visa", "family",
     "Япония", "Ниседко (VIP-шале)", "Долгий семейный отдых в приватном шале на горнолыжном курорте."),

    ("luxury", "africa", "nature", "any", "week", "advance_visa", "romance",
     "Ботсвана", "Дельта Окаванго (VIP-лодж)", "Романтичная неделя в эксклюзивном лодже с личным гидом."),
    ("luxury", "africa", "beach", "any", "week", "easy_visa", "family",
     "Сейшельские острова", "Норт-Айленд", "Семейный отдых на приватном острове высшего класса."),
    ("luxury", "africa", "excursion", "any", "short", "easy_visa", "gastronomy",
     "Марокко", "Марракеш (гастро-VIP)", "Короткий гастрономический тур с шеф-ужинами в риадах Марракеша."),

    ("luxury", "europe", "excursion", "any", "short", "visa_free", "gastronomy",
     "Франция", "Париж (мишленовский тур)", "Короткий тур по мишленовским ресторанам Парижа."),
    ("luxury", "europe", "beach", "summer", "week", "visa_free", "romance",
     "Италия", "Капри", "Романтичная неделя на легендарном острове Капри."),
    ("luxury", "europe", "active", "winter", "long", "visa_free", "family",
     "Швейцария", "Санкт-Мориц (VIP-шале)", "Долгий семейный отдых в приватном шале премиум-класса."),

    ("luxury", "north_america", "excursion", "any", "short", "advance_visa", "shopping",
     "США", "Беверли-Хиллз", "Короткий эксклюзивный шоппинг-тур в Лос-Анджелесе."),
    ("luxury", "north_america", "beach", "any", "week", "advance_visa", "romance",
     "США", "Малибу (приватная вилла)", "Романтичная неделя в приватной вилле на побережье Калифорнии."),
    ("luxury", "north_america", "nature", "autumn", "long", "advance_visa", "family",
     "Канада", "Банф (VIP-лоджи)", "Долгий семейный отдых в эксклюзивных горных лоджах."),

    ("luxury", "south_america", "excursion", "any", "long", "advance_visa", "gastronomy",
     "Перу", "Мачу-Пикчу (люкс-поезд + гастро)", "Долгий гастрономический тур на люксовом поезде Belmond."),
    ("luxury", "south_america", "beach", "any", "week", "advance_visa", "family",
     "Бразилия", "Рио-де-Жанейро (VIP-вилла)", "Семейный премиальный отдых в приватной вилле у пляжа."),
    ("luxury", "south_america", "nature", "any", "extended", "advance_visa", "romance",
     "Чили", "Патагония (эксклюзив-лодж, романтика)", "Длительный романтичный отдых в частном эко-лодже премиум-класса."),

    ("luxury", "australia_oceania", "beach", "any", "week", "easy_visa", "romance",
     "Французская Полинезия", "Бора-Бора (президентская вилла)", "Романтичная неделя в самой эксклюзивной вилле над лагуной."),
    ("luxury", "australia_oceania", "excursion", "any", "short", "easy_visa", "shopping",
     "Австралия", "Сидней (VIP-шоппинг)", "Короткий эксклюзивный шоппинг-тур по бутикам Сиднея."),
    ("luxury", "australia_oceania", "active", "any", "long", "easy_visa", "family",
     "Новая Зеландия", "Квинстаун (VIP + семья)", "Долгий семейный отдых с приватными экстремальными турами."),

    ("luxury", "antarctica", "nature", "any", "extended", "advance_visa", "romance",
     "Антарктида", "Люкс-экспедиционный круиз (романтика)", "Длительный романтичный люкс-круиз с приватными каютами."),
    ("luxury", "antarctica", "active", "any", "long", "advance_visa", "family",
     "Антарктида", "VIP-экспедиция (семейная)", "Долгая семейная VIP-экспедиция с вертолётными экскурсиями."),
]


def _build_destinations():
    """Достраивает базовый список направлений тремя новыми измерениями (any по умолчанию,
    точечные переопределения из TAG_OVERRIDES) и добавляет новые направления из _EXTRA_DESTINATIONS."""
    rows = []
    for budget, continent, rtype, season, country, city, description in _BASE_DESTINATIONS:
        tags = TAG_OVERRIDES.get((country, city), {})
        rows.append((
            budget, continent, rtype, season,
            tags.get("duration", "any"),
            tags.get("visa", "any"),
            tags.get("interest", "any"),
            country, city, description,
        ))
    rows.extend(_EXTRA_DESTINATIONS)
    return rows


DESTINATIONS = _build_destinations()

# Версия набора направлений. Поднимаем при добавлении новых пачек данных (см. seed_data) -
# позволяет дозаполнить уже существующую у пользователя базу без дублирования и без потери
# направлений, добавленных вручную через окно "База знаний".
DESTINATIONS_SEED_VERSION = "2"


####################################
###### АБСУРДНЫЕ КОМБИНАЦИИ #######
####################################
# Для части сочетаний "континент + тип отдыха" в базе знаний физически нет ни одного
# направления (например, пляжный отдых в Антарктиде), поэтому find_relaxed() всегда
# вынужден отбросить континент, сохранив тип отдыха. Здесь честно и с юмором объясняем
# пользователю, почему именно так, вместо того чтобы молча подменить направление.
ABSURD_COMBOS = {
    ("antarctica", "beach"): [
        "Пляжный отдых в Антарктиде? Вода там тоже есть — просто она круглый год в виде "
        "многометрового льда, а не матраса для загара. Ни один туроператор не поставит вам "
        "шезлонг между пингвинами, так что вот честный пляжный вариант, который существует "
        "на самом деле: {country} — {city}.",
        "На Земле пока не изобрели загар при –30°C, а единственные, кто там «отдыхает на "
        "пляже», — это пингвины, и то колонией, а не на шезлонгах. Возвращаем вас на "
        "нормальный пляж: {country} — {city}.",
        "«Antarctica Beach Club» звучит как название несуществующего аккаунта в соцсетях. "
        "В реальности там только лёд, ветер под 100 км/ч и ни одного бара с коктейлями. "
        "Ловите настоящий пляж: {country} — {city}.",
        "Спойлер: пляжного сезона в Антарктиде не бывает ни в один месяц года. Поэтому вместо "
        "купания среди айсбергов предлагаем вариант, где реально можно намочить ноги в тёплой "
        "воде: {country} — {city}.",
    ],
    ("antarctica", "wellness"): [
        "СПА среди айсбергов пока не придумали — ближайшее, что есть в Антарктиде, это "
        "ледяная вода Южного океана, а это скорее шоковая терапия, чем массаж. Настоящий "
        "оздоровительный вариант — здесь: {country} — {city}.",
        "Ни одного хаммама и ни одной чаши для медитации на весь ледяной континент — только "
        "исследовательские станции и очень серьёзный дресс-код (термобельё). Настоящий "
        "wellness ищем тут: {country} — {city}.",
        "Йога-коврик на льду продержится минут пять, а потом либо коврик, либо йог примёрзнут "
        "к месту. Вместо этого — рабочий вариант спокойного отдыха: {country} — {city}.",
    ],
    ("antarctica", "excursion"): [
        "Экскурсии в Антарктиде — это не музеи и не автобусные туры, а высадки с ледокола к "
        "колониям пингвинов, и таких маршрутов в год буквально единицы. Классический "
        "экскурсионный формат, который вы, вероятно, имели в виду, — вот здесь: "
        "{country} — {city}.",
        "Гид с зонтиком и группа туристов у памятника посреди Антарктиды — это не реальность, "
        "а сюрреализм. Там либо многонедельная экспедиция, либо ничего. Предлагаем настоящий "
        "экскурсионный вариант: {country} — {city}.",
        "На ледяном континенте нет ни одной достопримечательности с табличкой и кассой — "
        "только природа и наука. Поэтому классические экскурсии смотрим тут: "
        "{country} — {city}.",
    ],
}

ANTARCTICA_DURATION_JOKES = [
    "За 2–4 дня до Антарктиды не то что не отдохнёшь — вы не успеете даже доплыть: один "
    "только пролив Дрейка занимает около двух суток в одну сторону. Поэтому нашли вариант, "
    "который реально укладывается в короткую поездку: {country} — {city}.",
    "Короткая поездка в Антарктиду существует только в рекламных буклетах. На деле — минимум "
    "полторы-две недели, иначе весь отпуск пройдёт в открытом океане, а не на льду. Вот что "
    "действительно помещается в несколько дней: {country} — {city}.",
]


def _clean_place_name(text):
    """Убирает уточнения в скобках и части после '+' - нужно только для текста шутки,
    на подбор направления и поиск изображения (см. DestinationImageLoader) не влияет."""
    return text.split("(")[0].split("+")[0].strip()


def build_absurd_note(continent, rest_type, duration, relaxed, result):
    """
    Если пользователь выбрал сочетание, для которого в базе знаний физически нет ни одного
    направления (пляж/спа/экскурсии в Антарктиде, слишком короткая поездка туда же), возвращает
    отдельную саркастичную, но по существу объясняющую реплику. Иначе - None, и текст ответа
    остаётся обычным.
    """
    combo = ABSURD_COMBOS.get((continent, rest_type))
    if combo and "континент" in relaxed:
        template = random.choice(combo)
        return template.format(country=result["country"], city=_clean_place_name(result["city"]))

    if (continent == "antarctica" and rest_type in ("nature", "active")
            and duration == "short" and "продолжительность поездки" in relaxed):
        template = random.choice(ANTARCTICA_DURATION_JOKES)
        return template.format(country=result["country"], city=_clean_place_name(result["city"]))

    return None


class Database:
    """Единая точка доступа к базе знаний и истории (SQLite)."""

    def __new__(cls, *args, **kwargs):
        if not hasattr(cls, "instance"):
            cls.instance = super(Database, cls).__new__(cls)
        return cls.instance

    def __init__(self, path=DB_PATH):
        if getattr(self, "_initialized", False):
            return
        self.path = path
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._initialized = True
        self.create_tables()
        self.migrate_schema()
        self.seed_data()

    def create_tables(self):
        cur = self.conn.cursor()
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS budgets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                min_price INTEGER NOT NULL,
                max_price INTEGER NOT NULL,
                icon TEXT
            );

            CREATE TABLE IF NOT EXISTS continents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                icon TEXT
            );

            CREATE TABLE IF NOT EXISTS rest_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                icon TEXT
            );

            CREATE TABLE IF NOT EXISTS seasons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                icon TEXT
            );

            CREATE TABLE IF NOT EXISTS durations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                icon TEXT
            );

            CREATE TABLE IF NOT EXISTS visas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                icon TEXT
            );

            CREATE TABLE IF NOT EXISTS interests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                icon TEXT
            );

            CREATE TABLE IF NOT EXISTS destinations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                budget_code TEXT NOT NULL,
                continent_code TEXT NOT NULL,
                type_code TEXT NOT NULL,
                season_code TEXT NOT NULL,
                country TEXT NOT NULL,
                city TEXT NOT NULL,
                description TEXT
            );

            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dt TEXT NOT NULL,
                budget TEXT,
                continent TEXT,
                rest_type TEXT,
                season TEXT,
                use_ai INTEGER,
                country TEXT,
                city TEXT
            );

            CREATE TABLE IF NOT EXISTS seed_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dt TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL
            );
        """)
        self.conn.commit()

    def migrate_schema(self):
        """
        Добавляет новые колонки в уже существующие у пользователя таблицы (destinations, history),
        не трогая ранее сохранённые данные - в т.ч. направления, добавленные вручную через UI.
        """
        cur = self.conn.cursor()

        cur.execute("PRAGMA table_info(destinations)")
        dest_cols = {row[1] for row in cur.fetchall()}
        for col in ("duration_code", "visa_code", "interest_code"):
            if col not in dest_cols:
                cur.execute(f"ALTER TABLE destinations ADD COLUMN {col} TEXT NOT NULL DEFAULT 'any'")

        cur.execute("PRAGMA table_info(history)")
        hist_cols = {row[1] for row in cur.fetchall()}
        for col in ("duration", "visa", "interest"):
            if col not in hist_cols:
                cur.execute(f"ALTER TABLE history ADD COLUMN {col} TEXT NOT NULL DEFAULT 'any'")
        if "cost_usd" not in hist_cols:
            cur.execute("ALTER TABLE history ADD COLUMN cost_usd REAL")  # NULL - сумма не считалась

        self.conn.commit()

    def _get_seed_version(self):
        cur = self.conn.cursor()
        cur.execute("SELECT value FROM seed_meta WHERE key = 'destinations_seed_version'")
        row = cur.fetchone()
        return row[0] if row else None

    def _set_seed_version(self, version):
        cur = self.conn.cursor()
        cur.execute("DELETE FROM seed_meta WHERE key = 'destinations_seed_version'")
        cur.execute("INSERT INTO seed_meta (key, value) VALUES ('destinations_seed_version', ?)", (version,))
        self.conn.commit()

    def seed_data(self):
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM budgets")
        if cur.fetchone()[0] == 0:
            cur.executemany(
                "INSERT INTO budgets (code, name, min_price, max_price, icon) VALUES (?, ?, ?, ?, ?)",
                BUDGETS,
            )
        cur.execute("SELECT COUNT(*) FROM continents")
        if cur.fetchone()[0] == 0:
            cur.executemany(
                "INSERT INTO continents (code, name, icon) VALUES (?, ?, ?)", CONTINENTS
            )
        cur.execute("SELECT COUNT(*) FROM rest_types")
        if cur.fetchone()[0] == 0:
            cur.executemany(
                "INSERT INTO rest_types (code, name, icon) VALUES (?, ?, ?)", REST_TYPES
            )
        cur.execute("SELECT COUNT(*) FROM seasons")
        if cur.fetchone()[0] == 0:
            cur.executemany(
                "INSERT INTO seasons (code, name, icon) VALUES (?, ?, ?)", SEASONS
            )
        cur.execute("SELECT COUNT(*) FROM durations")
        if cur.fetchone()[0] == 0:
            cur.executemany(
                "INSERT INTO durations (code, name, icon) VALUES (?, ?, ?)", DURATIONS
            )
        cur.execute("SELECT COUNT(*) FROM visas")
        if cur.fetchone()[0] == 0:
            cur.executemany(
                "INSERT INTO visas (code, name, icon) VALUES (?, ?, ?)", VISAS
            )
        cur.execute("SELECT COUNT(*) FROM interests")
        if cur.fetchone()[0] == 0:
            cur.executemany(
                "INSERT INTO interests (code, name, icon) VALUES (?, ?, ?)", INTERESTS
            )

        insert_sql = (
            "INSERT INTO destinations (budget_code, continent_code, type_code, season_code, "
            "duration_code, visa_code, interest_code, country, city, description) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )

        cur.execute("SELECT COUNT(*) FROM destinations")
        dest_count = cur.fetchone()[0]
        seed_version = self._get_seed_version()

        if dest_count == 0:
            # Свежая база - заливаем всё целиком (базовые + новые направления с тегами)
            cur.executemany(insert_sql, DESTINATIONS)
            self._set_seed_version(DESTINATIONS_SEED_VERSION)
        elif seed_version != DESTINATIONS_SEED_VERSION:
            # У пользователя уже была база (например, с прошлой версии приложения) -
            # добавляем только новую пачку направлений, не трогая уже существующие/добавленные вручную
            cur.executemany(insert_sql, _EXTRA_DESTINATIONS)
            self._set_seed_version(DESTINATIONS_SEED_VERSION)

        self.conn.commit()

    def get_lookup(self, table):
        cur = self.conn.cursor()
        if table == "budgets":
            cur.execute("SELECT code, name, min_price, max_price, icon FROM budgets")
        else:
            cur.execute(f"SELECT code, name, icon FROM {table}")
        return cur.fetchall()

    # Критерии в порядке значимости - от наименее важного к самому важному. "Тип отдыха"
    # стоит последним (отпускается в самую последнюю очередь): это то, что пользователь выбрал
    # явным образом и что определяет саму суть поездки (пляжный/активный/...). Иначе получаются
    # абсурдные результаты вроде "пляжный отдых в Антарктиде" -> "полярная экспедиция": система
    # формально держится за континент и бюджет, но подсовывает совершенно не тот тип поездки,
    # который просили. Гораздо честнее в такой ситуации предложить пляжный отдых в другом
    # регионе/бюджете (см. также build_absurd_note()).
    _CRITERIA_ORDER = [
        ("интересы и впечатления", "interest_code"),
        ("продолжительность поездки", "duration_code"),
        ("визовые предпочтения", "visa_code"),
        ("время года", "season_code"),
        ("бюджет", "budget_code"),
        ("континент", "continent_code"),
        ("тип отдыха", "type_code"),
    ]

    # season/duration/visa/interest умеют в БД буквально означать "любой" у самого направления
    # (например, направление одинаково хорошо в любой сезон). budget/continent/type у
    # направления в БД всегда конкретны - там строк со значением 'any' не бывает.
    _WILDCARD_COLUMNS = {"season_code", "duration_code", "visa_code", "interest_code"}

    DESTINATION_COLUMNS = (
        "id", "budget_code", "continent_code", "type_code", "season_code",
        "duration_code", "visa_code", "interest_code", "country", "city", "description",
    )

    def find_relaxed(self, budget, continent, rest_type, season, duration="any", visa="any", interest="any"):
        """
        Механизм ослабления условий экспертной системы: последовательно убираем самые
        незначимые критерии, пока не найдётся совпадение.

        Критерий, на который пользователь ответил "нет" на все варианты (значение "any" -
        ему не важно, каким будет этот параметр), в фильтрации не участвует вообще и не
        считается "ослабленным": ослаблять просто нечего, ведь пользователь изначально не
        задавал по нему требования. Ослабляются только критерии, которые пользователь
        действительно указал, но точного совпадения по ним не нашлось - поэтому трасса вывода
        отражает именно те компромиссы, на которые пришлось пойти для конкретного запроса,
        а не одну и ту же техническую деталь при любом ответе.
        """
        cur = self.conn.cursor()
        values = {
            "interest_code": interest, "duration_code": duration, "visa_code": visa,
            "season_code": season, "budget_code": budget, "continent_code": continent,
            "type_code": rest_type,
        }

        # Только реально заданные пользователем критерии участвуют в каскаде ослабления.
        active = [(name, col) for name, col in self._CRITERIA_ORDER if values[col] != "any"]

        trace = []
        for cut in range(len(active) + 1):
            remaining = active[cut:]
            dropped = [name for name, _ in active[:cut]]

            where_parts, params = [], []
            for _, col in remaining:
                if col in self._WILDCARD_COLUMNS:
                    where_parts.append(f"({col} = ? OR {col} = 'any')")
                else:
                    where_parts.append(f"{col} = ?")
                params.append(values[col])
            where_sql = " AND ".join(where_parts) if where_parts else "1 = 1"

            if cut == 0:
                stage_desc = "Точное совпадение по всем указанным вами критериям"
            elif cut == len(active):
                stage_desc = "Любое направление из базы знаний (остальные критерии тоже не учитываются)"
            else:
                stage_desc = "Без учёта: " + ", ".join(dropped)

            cur.execute(
                f"SELECT {', '.join(self.DESTINATION_COLUMNS)} FROM destinations WHERE {where_sql}", params
            )
            res = [dict(zip(self.DESTINATION_COLUMNS, row)) for row in cur.fetchall()]
            trace.append({"stage": stage_desc, "count": len(res), "success": bool(res)})
            if res:
                return res, dropped, trace

        return res, dropped, trace

    def save_history(self, budget, continent, rest_type, season, duration, visa, interest,
                      use_ai, country, city, cost_usd=None):
        """Возвращает id записи: после уточнения у ИИ она обновляется (см. update_history_result)."""
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO history (dt, budget, continent, rest_type, season, duration, visa, interest, "
            "use_ai, country, city, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                budget, continent, rest_type, season, duration, visa, interest,
                int(use_ai), country, city, cost_usd,
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def update_history_result(self, history_id, country, city, cost_usd):
        self.conn.execute(
            "UPDATE history SET country = ?, city = ?, cost_usd = ? WHERE id = ?",
            (country, city, cost_usd, history_id),
        )
        self.conn.commit()

    def get_history(self):
        cur = self.conn.cursor()
        cur.execute("SELECT dt, budget, continent, rest_type, season, duration, visa, interest, "
                     "use_ai, country, city, cost_usd FROM history ORDER BY id DESC")
        return cur.fetchall()

    def clear_history(self):
        cur = self.conn.cursor()
        cur.execute("DELETE FROM history")
        self.conn.commit()

    # ----- переписка с ИИ-консультантом -----

    def get_chat_messages(self):
        cur = self.conn.cursor()
        cur.execute("SELECT role, content FROM chat_messages ORDER BY id")
        return cur.fetchall()

    def add_chat_message(self, role, content):
        self.conn.execute(
            "INSERT INTO chat_messages (dt, role, content) VALUES (?, ?, ?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), role, content),
        )
        self.conn.commit()

    def clear_chat(self):
        self.conn.execute("DELETE FROM chat_messages")
        self.conn.commit()

    def add_destination(self, budget, continent, rest_type, season, duration, visa, interest,
                         country, city, description):
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO destinations (budget_code, continent_code, type_code, season_code, "
            "duration_code, visa_code, interest_code, country, city, description) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (budget, continent, rest_type, season, duration, visa, interest, country, city, description),
        )
        self.conn.commit()

    def delete_destination(self, dest_id):
        cur = self.conn.cursor()
        cur.execute("DELETE FROM destinations WHERE id = ?", (dest_id,))
        self.conn.commit()

    def list_all_destinations(self):
        cur = self.conn.cursor()
        cur.execute(f"SELECT {', '.join(self.DESTINATION_COLUMNS)} FROM destinations ORDER BY id")
        return cur.fetchall()

    def list_destination_dicts(self):
        return [dict(zip(self.DESTINATION_COLUMNS, row)) for row in self.list_all_destinations()]


####################################
######## ЛОГИКА ЭКСПЕРТ.СИСТЕМЫ ####
####################################

class RecommendationEngine:
    """Классический вывод по правилам (без ИИ): точный подбор с ослаблением условий."""

    NOTES_PERFECT = [
        "Точное совпадение по всем критериям базы знаний — ничего ослаблять не пришлось.",
        "Все заданные критерии совпали один в один, направление подобрано без компромиссов.",
        "Повезло: нашлось направление, которое отвечает сразу всем вашим ответам.",
    ]
    NOTES_MINOR = [
        "Точного совпадения по всем критериям не нашлось, система не учла только: {relaxed}.",
        "Почти идеально: пришлось отпустить лишь {relaxed}, остальное совпало полностью.",
        "Пришлось поступиться одним пунктом ({relaxed}), в остальном направление точно ваше.",
    ]
    NOTES_MODERATE = [
        "Точного совпадения нет, поэтому система ослабила условия и не учла: {relaxed}.",
        "Не всё совпало идеально — без учёта {relaxed}, но это лучшее, что нашлось в базе.",
        "Пришлось скорректировать несколько пунктов ({relaxed}), направление всё ещё близко к запросу.",
    ]
    NOTES_MAJOR = [
        "Такого сочетания критериев в базе не нашлось вовсе, пришлось ослабить почти всё "
        "({relaxed}) — воспринимайте результат как общий ориентир, а не точное попадание.",
        "Ваша комбинация ответов оказалась редкой: чтобы вообще что-то предложить, пришлось "
        "отступить от {relaxed}.",
    ]

    def __init__(self, db: Database):
        self.db = db

    def _reasoning(self, relaxed):
        relaxed_str = ", ".join(relaxed)
        n = len(relaxed)
        if n == 0:
            variants = self.NOTES_PERFECT
        elif n <= 2:
            variants = self.NOTES_MINOR
        elif n <= 4:
            variants = self.NOTES_MODERATE
        else:
            variants = self.NOTES_MAJOR
        return random.choice(variants).format(relaxed=relaxed_str)

    def recommend(self, budget, continent, rest_type, season, duration="any", visa="any", interest="any"):
        rows, relaxed, trace = self.db.find_relaxed(budget, continent, rest_type, season, duration, visa, interest)
        dest = random.choice(rows)

        return {
            "country": dest["country"],
            "city": dest["city"],
            "description": dest["description"],
            "destination": dest,
            "source": "rules",
            "reasoning": self._reasoning(relaxed),
            "mode": "🧭 Экспертная система (правила)",
            "trace": trace,
            "relaxed": relaxed,
        }


####################################
########### СТИЛИ / БАЗА ###########
####################################

APP_STYLE = """
    QWidget {
        background-color: #eef6fb;
        font-family: 'Segoe UI', Arial, sans-serif;
        font-size: 14px;
        color: #14364f;
    }

    QMainWindow, QDialog {
        background-color: #eef6fb;
    }

    QPushButton {
        background-color: #2f9ed1;
        border: none;
        padding: 12px;
        border-radius: 8px;
        font-size: 15px;
        font-weight: 600;
        color: white;
    }

    QPushButton:hover {
        background-color: #2688b8;
    }

    QPushButton:pressed {
        background-color: #1e6f99;
    }

    QPushButton#noButton {
        background-color: #e0e0e0;
        color: #14364f;
    }
    QPushButton#noButton:hover {
        background-color: #cfcfcf;
    }

    QPushButton#secondary {
        background-color: #ffffff;
        color: #2f9ed1;
        border: 2px solid #2f9ed1;
    }
    QPushButton#secondary:hover {
        background-color: #dff2fb;
    }

    QPushButton:disabled {
        background-color: #eef2f5;
        color: #a9b7c0;
        border: 2px solid #d8e2e8;
    }

    QLabel {
        color: #14364f;
    }

    QLabel#title {
        font-size: 24px;
        font-weight: 700;
        color: #12293c;
    }

    QLabel#subtitle {
        font-size: 15px;
        color: #4c6c82;
    }

    QLabel#icon {
        font-size: 60px;
    }

    QProgressBar {
        border: none;
        border-radius: 6px;
        background-color: #d7e9f2;
        height: 12px;
        text-align: center;
    }

    QProgressBar::chunk {
        background-color: #2f9ed1;
        border-radius: 6px;
    }

    QTableWidget {
        background-color: white;
        border: 1px solid #cfe3ee;
        border-radius: 6px;
        gridline-color: #e3edf4;
    }

    QHeaderView::section {
        background-color: #d7e9f2;
        padding: 6px;
        border: none;
        font-weight: 600;
    }
"""


class ToggleSwitch(QCheckBox):
    """
    Тумблер в стиле мобильных приложений с анимированным скольжением ручки
    (с лёгким "пружинным" эффектом) и свечением вокруг тумблера при включении.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(58, 30)

        self._position = 0.0  # 0.0 = выключено, 1.0 = включено
        self._off_color = QColor("#c6d4dd")
        self._on_color = QColor("#2f9ed1")

        self._anim = QPropertyAnimation(self, b"position", self)
        self._anim.setDuration(260)
        self._anim.setEasingCurve(QEasingCurve.OutBack)

        # Свечение вокруг тумблера, которое "загорается" при включении ИИ-режима
        self._glow = QGraphicsDropShadowEffect(self)
        self._glow.setColor(QColor(47, 158, 209, 0))
        self._glow.setBlurRadius(0)
        self._glow.setOffset(0, 0)
        self.setGraphicsEffect(self._glow)

        self._glow_anim = QPropertyAnimation(self._glow, b"blurRadius", self)
        self._glow_anim.setDuration(300)

        self.toggled.connect(self._animate_to_state)

    def hitButton(self, pos):
        # По умолчанию QCheckBox реагирует на клик только по маленькому
        # квадрату-индикатору стиля. Делаем кликабельным весь тумблер.
        return self.contentsRect().contains(pos)

    def _animate_to_state(self, checked):
        self._anim.stop()
        self._anim.setStartValue(self._position)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

        self._glow.setColor(QColor(47, 158, 209, 220 if checked else 0))
        self._glow_anim.stop()
        self._glow_anim.setStartValue(self._glow.blurRadius())
        self._glow_anim.setEndValue(20 if checked else 0)
        self._glow_anim.start()

    def getPosition(self):
        return self._position

    def setPosition(self, value):
        self._position = value
        self.update()

    position = pyqtProperty(float, getPosition, setPosition)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        bg_color = self._blend(self._off_color, self._on_color, self._position)
        painter.setPen(Qt.NoPen)
        painter.setBrush(bg_color)
        painter.drawRoundedRect(0, 0, self.width(), self.height(), 15, 15)

        knob_diameter = self.height() - 6
        min_x, max_x = 3, self.width() - knob_diameter - 3
        x = min_x + (max_x - min_x) * self._position
        painter.setBrush(QColor("white"))
        painter.drawEllipse(QRectF(x, 3, knob_diameter, knob_diameter))

    @staticmethod
    def _blend(c1, c2, t):
        r = c1.red() + (c2.red() - c1.red()) * t
        g = c1.green() + (c2.green() - c1.green()) * t
        b = c1.blue() + (c2.blue() - c1.blue()) * t
        return QColor(int(r), int(g), int(b))


####################################
##### ИЗОБРАЖЕНИЯ НАПРАВЛЕНИЙ ######
####################################

class DestinationImageLoader(QObject):
    """
    Подтягивает фотографию направления по названию города/страны через публичный
    (без ключей и авторизации) API Wikipedia - для карточки результата подбора.

    Это лучшая эффективность/усилия: вместо того чтобы вручную прописывать и поддерживать
    ссылку на картинку для каждого из более чем 150 направлений в базе знаний (и рисковать
    битыми ссылками), запрос картинки идёт "на лету" по названию места. Если картинки
    получить не удалось - нет сети, нет статьи, нет фото - вызывающий код просто продолжает
    показывать эмодзи-иконку, как и раньше: наличие фото никак не влияет на логику подбора.
    """

    REQUEST_TIMEOUT_MS = 6000

    # Wikimedia API отдаёт 403 Forbidden на запросы без описательного User-Agent (их политика
    # для анонимных запросов) - Qt по умолчанию такой заголовок не выставляет, поэтому без
    # него картинки молча не загружались бы вообще ни для одного направления.
    USER_AGENT = b"TravelExpertQtApp/1.0 (educational PyQt5 desktop project)"

    # Заглавная картинка статьи иногда оказывается флагом/гербом/картой, а не фотографией
    # (например, у статьи "Сидней" это флаг города) - такие файлы на Commons практически
    # всегда лежат в SVG и имеют предсказуемые имена.
    _NON_PHOTO_HINTS = (
        "flag_of", "flag-", "coat_of_arms", "coatofarms", "blank_map", "locator_map",
        "location_map", "_locator", "emblem_of", "seal_of", "_map_of", "map_of_", ".svg",
    )

    # Полнотекстовый поиск по "город страна" иногда попадает не в статью о месте, а в статью
    # о человеке, у которого оба слова просто встречаются в тексте (актёр родом из этого
    # города, футболист местного клуба и т.п.) - и тогда пользователю показывают чей-то
    # портрет вместо фотографии направления. У Wikipedia почти для каждой персоны есть
    # короткое автоматическое описание вида "<прилагательное> <профессия>" - по нему и
    # отсеиваем людей. Сравниваем по ЦЕЛЫМ словам (с учётом русских окончаний - как префикс),
    # а не подстрокой где попало: иначе, например, "министр" ложно находится внутри
    # совершенно не относящегося к делу слова "административный".
    _PERSON_WORD_PREFIXES = (
        "актёр", "актрис", "певец", "певиц", "политик", "предпринимат", "писатель", "поэт",
        "режиссёр", "журналист", "спортсмен", "футболист", "хоккеист", "баскетболист",
        "теннисист", "боксёр", "боец", "музыкант", "художни", "учён", "инженер", "блогер",
        "телеведущ", "фотограф", "дизайнер", "продюсер", "композитор", "дирижёр", "историк",
        "архитектор", "философ", "экономист", "юрист", "врач", "профессор", "академик",
        "чемпион", "депутат", "министр", "президент", "монах", "епископ", "генерал",
        "адмирал", "офицер",
    )
    # Эти маркеры, наоборот, безопасно ловить подстрокой где угодно - за счёт непривычной
    # пунктуации/пробелов они практически не встречаются внутри случайных слов.
    _PERSON_SUBSTRING_HINTS = ("модель (", "(р. ", "(род. ", "родился", "родилась")

    _WORD_RE = re.compile(r"[a-zа-яё]+", re.IGNORECASE)

    SEARCH_RESULT_LIMIT = 5

    @classmethod
    def _looks_like_photo(cls, thumb_url):
        lowered = thumb_url.lower()
        return not any(hint in lowered for hint in cls._NON_PHOTO_HINTS)

    @staticmethod
    def _looks_like_person_page(page):
        # Биографические статьи в русской Википедии почти без исключений называются
        # "Фамилия, Имя" (например, "Туиваса, Тай") - у статей о городах, странах, островах
        # такой пунктуации в заголовке не бывает (там для уточнения используются скобки:
        # "Париж (Техас)"). Это ловит и те персональные статьи, у которых почему-то не
        # заполнено короткое Wikidata-описание, - на них одних ключевых слов недостаточно.
        if "," in page.get("title", ""):
            return True
        return DestinationImageLoader._looks_like_person(page.get("description"))

    @classmethod
    def _looks_like_person(cls, description):
        if not description:
            return False
        lowered = description.lower()
        if any(hint in lowered for hint in cls._PERSON_SUBSTRING_HINTS):
            return True
        words = cls._WORD_RE.findall(lowered)
        return any(word.startswith(prefix) for word in words for prefix in cls._PERSON_WORD_PREFIXES)

    def __init__(self, manager: QNetworkAccessManager, parent=None):
        super().__init__(parent)
        self.manager = manager

    def _build_request(self, url):
        request = QNetworkRequest(url)
        request.setRawHeader(b"User-Agent", self.USER_AGENT)
        return request

    @staticmethod
    def _base_query():
        query = QUrlQuery()
        query.addQueryItem("action", "query")
        query.addQueryItem("format", "json")
        query.addQueryItem("formatversion", "2")
        query.addQueryItem("prop", "pageimages|description")
        query.addQueryItem("piprop", "thumbnail")
        query.addQueryItem("pithumbsize", "600")
        return query

    def fetch(self, country, city, on_success, on_failure):
        place = _clean_place_name(city)
        # Сначала пробуем статью, точно озаглавленную названием места - это надёжнее
        # полнотекстового поиска и не путает место с однофамильцем-человеком.
        self._fetch_exact_title(place, place, country, on_success, on_failure)

    def _fetch_exact_title(self, title, place, country, on_success, on_failure):
        url = QUrl("https://ru.wikipedia.org/w/api.php")
        query = self._base_query()
        query.addQueryItem("redirects", "1")
        query.addQueryItem("titles", title)
        url.setQuery(query)

        reply = self.manager.get(self._build_request(url))
        # Без явного родителя и без одновременных конкурирующих запросов Python иногда успевал
        # собрать Python-обёртку reply как мусор до завершения запроса (C++ объект ещё "летит"),
        # что приводило к segfault при нескольких параллельных fetch() (см. экран "Посмотрите,
        # куда уже поехали другие" с тремя одновременными загрузками фото). setParent привязывает
        # время жизни reply к загрузчику и снимает проблему независимо от Python-рефкаунта.
        reply.setParent(self)
        self._arm_timeout(reply)
        reply.finished.connect(
            lambda: self._on_exact_title_reply(reply, title, place, country, on_success, on_failure)
        )

    def _arm_timeout(self, reply):
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(reply.abort)
        timer.start(self.REQUEST_TIMEOUT_MS)
        reply.finished.connect(timer.stop)

    @staticmethod
    def _log_failure(stage, reply):
        # Не показываем пользователю - для него всё равно ничего не меняется, картинка
        # просто не появится и останется эмодзи-иконка. Но в консоли видно, что реально
        # произошло (таймаут, DNS, SSL, отказ сервера и т.п.) - без этого диагностировать
        # проблему с сетью на конкретной машине пользователя было бы невозможно.
        print(f"[DestinationImageLoader] {stage}: {reply.errorString()} (код {int(reply.error())})",
              file=sys.stderr)

    def _on_exact_title_reply(self, reply, title, place, country, on_success, on_failure):
        try:
            if reply.error() == QNetworkReply.NoError:
                payload = json.loads(bytes(reply.readAll()).decode("utf-8"))
                pages = payload.get("query", {}).get("pages", [])
                page = pages[0] if pages else None
                # "missing"/"pageid" отсутствует - статьи с таким названием нет; описание вида
                # "страница значений..." - это неоднозначность (например, "Роторуа" без уточнения)
                if page and not page.get("missing"):
                    thumb = page.get("thumbnail", {}).get("source")
                    if thumb and self._looks_like_photo(thumb) and not self._looks_like_person_page(page):
                        self._fetch_image_bytes(thumb, on_success, on_failure)
                        return
        except (ValueError, KeyError):
            pass
        finally:
            reply.deleteLater()

        if title == place:
            # Голое название не подошло (неоднозначность/нет статьи/нет фото) - у части городов
            # статья на Wikipedia уточняется явной припиской "(город)" (например, "Роторуа" -
            # страница значений, а сам город - "Роторуа (город)"). Пробуем этот вариант, прежде
            # чем переходить к более ненадёжному полнотекстовому поиску.
            self._fetch_exact_title(f"{place} (город)", place, country, on_success, on_failure)
        else:
            self._fetch_via_search(f"{place} {country}".strip(), on_success, on_failure)

    def _fetch_via_search(self, search_query, on_success, on_failure):
        url = QUrl("https://ru.wikipedia.org/w/api.php")
        query = self._base_query()
        query.addQueryItem("generator", "search")
        query.addQueryItem("gsrsearch", search_query)
        query.addQueryItem("gsrlimit", str(self.SEARCH_RESULT_LIMIT))
        url.setQuery(query)

        reply = self.manager.get(self._build_request(url))
        # Без явного родителя и без одновременных конкурирующих запросов Python иногда успевал
        # собрать Python-обёртку reply как мусор до завершения запроса (C++ объект ещё "летит"),
        # что приводило к segfault при нескольких параллельных fetch() (см. экран "Посмотрите,
        # куда уже поехали другие" с тремя одновременными загрузками фото). setParent привязывает
        # время жизни reply к загрузчику и снимает проблему независимо от Python-рефкаунта.
        reply.setParent(self)
        self._arm_timeout(reply)
        reply.finished.connect(lambda: self._on_search_reply(reply, on_success, on_failure))

    def _on_search_reply(self, reply, on_success, on_failure):
        try:
            if reply.error() != QNetworkReply.NoError:
                self._log_failure("поиск статьи", reply)
                on_failure()
                return
            payload = json.loads(bytes(reply.readAll()).decode("utf-8"))
            pages = sorted(payload.get("query", {}).get("pages", []), key=lambda p: p.get("index", 999))

            # Если среди кандидатов нет ни одной настоящей фотографии (только флаги/гербы/карты
            # или люди), лучше вообще не показывать картинку и оставить эмодзи-иконку, чем
            # подсовывать флаг или чей-то портрет вместо фото места - см. также _looks_like_photo.
            thumb_url = None
            for page in pages:
                thumb = page.get("thumbnail", {}).get("source")
                if not thumb or self._looks_like_person_page(page):
                    continue
                if self._looks_like_photo(thumb):
                    thumb_url = thumb
                    break

            if not thumb_url:
                on_failure()  # статья не нашлась или у неё нет подходящей фотографии - не ошибка сети
                return

            self._fetch_image_bytes(thumb_url, on_success, on_failure)
        except (ValueError, KeyError) as exc:
            print(f"[DestinationImageLoader] не удалось разобрать ответ Wikipedia: {exc}", file=sys.stderr)
            on_failure()
        finally:
            reply.deleteLater()

    def _fetch_image_bytes(self, thumb_url, on_success, on_failure):
        img_reply = self.manager.get(self._build_request(QUrl(thumb_url)))
        img_reply.setParent(self)
        self._arm_timeout(img_reply)
        img_reply.finished.connect(lambda: self._on_image_reply(img_reply, on_success, on_failure))

    def _on_image_reply(self, reply, on_success, on_failure):
        try:
            if reply.error() != QNetworkReply.NoError:
                self._log_failure("загрузка изображения", reply)
                on_failure()
                return
            pixmap = QPixmap()
            if pixmap.loadFromData(bytes(reply.readAll())) and not pixmap.isNull():
                on_success(pixmap)
            else:
                on_failure()
        finally:
            reply.deleteLater()


####################################
############ ГЛ. МЕНЮ ##############
####################################

class MainWindow(QMainWindow):
    AI_HINT_OFF = "ИИ сравнит с вашими ответами все направления сразу, объяснит выбор и учтёт ваши уточнения"

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.ai = AIContext(_user_data_dir(), self)
        self.ai.image_loader = DestinationImageLoader(self.ai.manager, self)
        self.ai.converter.ensure()
        self.setWindowTitle("Экспертная система: Выбор направления для путешествия")
        self.setGeometry(200, 100, 560, 560)
        self.init_ui()

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout()
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(18)
        central.setLayout(layout)

        self.icon_label = QLabel("✈️")
        self.icon_label.setObjectName("icon")
        self.icon_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.icon_label)

        title = QLabel("Выбор направления для путешествия")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignCenter)
        title.setWordWrap(True)
        layout.addWidget(title)

        subtitle = QLabel("Ответьте на несколько вопросов Да / Нет — и получите готовую рекомендацию")
        subtitle.setObjectName("subtitle")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        layout.addSpacing(10)

        ai_row = QHBoxLayout()
        ai_row.addStretch()
        ai_label = QLabel("🤖 Использовать ИИ")
        ai_label.setStyleSheet("font-size: 16px; font-weight: 600;")
        self.ai_toggle = ToggleSwitch()
        self.ai_toggle.toggled.connect(self.on_ai_toggle_changed)
        ai_row.addWidget(ai_label)
        ai_row.addWidget(self.ai_toggle)
        ai_row.addStretch()
        layout.addLayout(ai_row)

        self.ai_hint = QLabel(self.AI_HINT_OFF)
        self.ai_hint.setObjectName("subtitle")
        self.ai_hint.setAlignment(Qt.AlignCenter)
        self.ai_hint.setWordWrap(True)
        layout.addWidget(self.ai_hint)

        layout.addSpacing(10)

        start_btn = QPushButton("Начать подбор путешествия")
        start_btn.clicked.connect(self.start_wizard)
        layout.addWidget(start_btn)

        ai_menu_btn = QPushButton("🤖 Меню ИИ и консультант")
        ai_menu_btn.setObjectName("secondary")
        ai_menu_btn.clicked.connect(self.open_ai_menu)
        layout.addWidget(ai_menu_btn)

        history_btn = QPushButton("История подборов")
        history_btn.setObjectName("secondary")
        history_btn.clicked.connect(self.open_history)
        layout.addWidget(history_btn)

        kb_btn = QPushButton("База знаний (направления)")
        kb_btn.setObjectName("secondary")
        kb_btn.clicked.connect(self.open_kb)
        layout.addWidget(kb_btn)

        exit_btn = QPushButton("Выход")
        exit_btn.setObjectName("noButton")
        exit_btn.clicked.connect(self.close)
        layout.addWidget(exit_btn)

        layout.addStretch()

    def on_ai_toggle_changed(self, checked):
        self.icon_label.setText("🤖" if checked else "✈️")
        self._update_ai_hint()
        self._flash(self.icon_label)

    def _update_ai_hint(self):
        if not self.ai_toggle.isChecked():
            self.ai_hint.setText(self.AI_HINT_OFF)
            self.ai_hint.setStyleSheet("")
        else:
            self.ai_hint.setText("🤖 Режим ИИ включён: сравню все направления и найду лучший компромисс")
            self.ai_hint.setStyleSheet("color: #2f9ed1; font-weight: 600; font-size: 14px;")

    def _flash(self, widget):
        """Короткая вспышка прозрачности - визуальный отклик на переключение тумблера."""
        effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", widget)
        anim.setDuration(350)
        anim.setStartValue(0.15)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QPropertyAnimation.DeleteWhenStopped)
        self._flash_anim = anim  # храним ссылку, чтобы анимация не была удалена раньше времени

    def start_wizard(self):
        self.run_wizard(self.ai_toggle.isChecked())

    def run_wizard(self, use_ai):
        self.wizard = QuestionWizard(self.db, use_ai, self.ai)
        self.wizard.exec_()

    def open_ai_menu(self):
        AIMenuWindow(self.ai, self.db, self.run_wizard, self).exec_()
        self._update_ai_hint()

    def open_history(self):
        self.history_window = HistoryWindow(self.db)
        self.history_window.exec_()

    def open_kb(self):
        self.kb_window = KnowledgeBaseWindow(self.db)
        self.kb_window.exec_()


####################################
######### МАСТЕР ВОПРОСОВ ##########
####################################

class QuestionWizard(QDialog):
    """
    Последовательно задаёт вопросы Да/Нет по каждой из категорий:
    Бюджет -> Континент -> Тип отдыха -> Время года -> Продолжительность -> Виза -> Интересы,
    затем выводит рекомендацию.
    """

    CATEGORIES = [
        ("budget", "Бюджет", "budgets"),
        ("continent", "Континент", "continents"),
        ("rest_type", "Тип отдыха", "rest_types"),
        ("season", "Время года", "seasons"),
        ("duration", "Продолжительность поездки", "durations"),
        ("visa", "Визовые предпочтения", "visas"),
        ("interest", "Интересы и впечатления", "interests"),
    ]

    def __init__(self, db: Database, use_ai: bool, ai: AIContext):
        super().__init__()
        self.db = db
        self.use_ai = use_ai
        self.ai = ai
        self.rule_engine = RecommendationEngine(db)
        self.ai_engine = SmartEngine(QUERY_NAMES)

        self.answers = {}
        self.cat_index = 0
        self.opt_index = 0
        self.options_cache = {}
        self.history_stack = []  # для кнопки "Назад": снимки (cat_index, opt_index, answers)

        # Подгрузка фото направления по сети (см. show_result/_on_photo_loaded); при неудаче -
        # или пока картинка ещё не пришла - виден обычный эмодзи-значок direction.
        # Загрузчик общий на всё приложение: если он принадлежит окну, то закрытие окна во время
        # загрузки уничтожает сетевой менеджер вместе с летящими запросами и роняет процесс.
        self.image_loader = ai.image_loader
        self._result_generation = 0
        self.result_icon_label = None
        self._travelers_generation = 0

        # Состояние подбора: запрос (меняется уточнениями), фильтры уточнений и показанный результат.
        self._query = None
        self._filters = empty_filters()
        self._result = None
        self._history_id = None
        self._thinking_timer = None

        self._dot_timer = None
        self._dot_count = 0
        self._tw_timer = None
        self._tw_text = ""
        self._tw_index = 0
        self._tw_label = None

        self.setWindowTitle("Подбор путешествия")
        self.setMinimumSize(560, 480)
        self.resize(560, 640)

        # Контент кладём в прокручиваемую область: экран результата (особенно в ИИ-режиме
        # с полосой уверенности) заметно выше экрана вопроса, и без скролла на небольшом
        # окне виджеты просто сжимались бы друг в друга.
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer_layout.addWidget(scroll)

        content_widget = QWidget()
        scroll.setWidget(content_widget)

        self.layout = QVBoxLayout()
        self.layout.setContentsMargins(30, 30, 30, 30)
        self.layout.setSpacing(16)
        content_widget.setLayout(self.layout)

        self.load_options_cache()
        self.show_intro_gate()

    def load_options_cache(self):
        for code, _, table in self.CATEGORIES:
            self.options_cache[code] = self.db.get_lookup(table)

    # -------- Входная развилка (см. MyProgram/shema.html): нужен ли вообще подбор --------

    def show_intro_gate(self):
        """Первый экран мастера: пользователь вообще хочет, чтобы систему подбирала направление?"""
        self.clear_layout(self.layout)
        self.layout.addStretch()

        icon = QLabel("✈️")
        icon.setObjectName("icon")
        icon.setAlignment(Qt.AlignCenter)
        self.layout.addWidget(icon)

        question = QLabel("Хотели выбрать направление для путешествия?")
        question.setAlignment(Qt.AlignCenter)
        question.setWordWrap(True)
        question.setStyleSheet("font-size: 19px; font-weight: 600;")
        self.layout.addWidget(question)

        self.layout.addSpacing(14)

        row = QHBoxLayout()
        no_btn = QPushButton("Нет")
        no_btn.setObjectName("noButton")
        no_btn.clicked.connect(self.show_intro_known)
        yes_btn = QPushButton("Да")
        yes_btn.clicked.connect(self.begin_main_flow)
        row.addWidget(no_btn)
        row.addWidget(yes_btn)
        self.layout.addLayout(row)

        self.layout.addStretch()

    def show_intro_known(self):
        """Ветка 'нет': уточняем, не нужна ли помощь по другой причине - человек мог уже определиться сам."""
        self.clear_layout(self.layout)
        self.layout.addStretch()

        icon = QLabel("🤔")
        icon.setObjectName("icon")
        icon.setAlignment(Qt.AlignCenter)
        self.layout.addWidget(icon)

        question = QLabel("Уже знаете, куда поедете?")
        question.setAlignment(Qt.AlignCenter)
        question.setWordWrap(True)
        question.setStyleSheet("font-size: 19px; font-weight: 600;")
        self.layout.addWidget(question)

        self.layout.addSpacing(14)

        row = QHBoxLayout()
        no_btn = QPushButton("Нет")
        no_btn.setObjectName("noButton")
        no_btn.clicked.connect(self.show_other_travelers)
        yes_btn = QPushButton("Да")
        yes_btn.clicked.connect(self.show_farewell)
        row.addWidget(no_btn)
        row.addWidget(yes_btn)
        self.layout.addLayout(row)

        self.layout.addSpacing(8)

        back_btn = QPushButton("← Назад")
        back_btn.setObjectName("secondary")
        back_btn.clicked.connect(self.show_intro_gate)
        self.layout.addWidget(back_btn)

        self.layout.addStretch()

    def show_farewell(self):
        """Ветка 'да/да': человек уже знает направление - просто прощаемся."""
        self.clear_layout(self.layout)
        self.layout.addStretch()

        icon = QLabel("👋")
        icon.setObjectName("icon")
        icon.setAlignment(Qt.AlignCenter)
        self.layout.addWidget(icon)

        title = QLabel("Хорошего пути!")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignCenter)
        self.layout.addWidget(title)

        subtitle = QLabel("Возвращайтесь, если понадобится помощь с выбором следующего направления.")
        subtitle.setObjectName("subtitle")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setWordWrap(True)
        self.layout.addWidget(subtitle)

        self.layout.addSpacing(14)

        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(self.accept)
        self.layout.addWidget(close_btn)

        back_btn = QPushButton("← Назад")
        back_btn.setObjectName("secondary")
        back_btn.clicked.connect(self.show_intro_known)
        self.layout.addWidget(back_btn)

        self.layout.addStretch()

    def show_other_travelers(self):
        """
        Ветка 'да/нет': человек не знает направление, но и подбор не хочет - явное противоречие.
        Вместо того чтобы просто попрощаться, показываем реальные направления из базы знаний
        (как "примеры результатов" на схеме) и предлагаем всё-таки попробовать подбор.
        """
        self.clear_layout(self.layout)

        title = QLabel("Посмотрите, куда уже поехали другие")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignCenter)
        title.setWordWrap(True)
        self.layout.addWidget(title)

        subtitle = QLabel("Реальные направления из базы знаний — возможно, одно из них приглянется и вам.")
        subtitle.setObjectName("subtitle")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setWordWrap(True)
        self.layout.addWidget(subtitle)

        self.layout.addSpacing(10)

        self._travelers_generation += 1
        gen = self._travelers_generation

        rows = self.db.list_all_destinations()
        sample = random.sample(rows, min(3, len(rows))) if rows else []
        for row in sample:
            country, city, description = row[8], row[9], row[10]

            card = QWidget()
            card.setStyleSheet("background-color: #dff2fb; border-radius: 10px;")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(0, 10, 0, 10)
            card_layout.setSpacing(6)

            photo = QLabel("🧳")
            photo.setAlignment(Qt.AlignCenter)
            photo.setStyleSheet("font-size: 40px;")
            card_layout.addWidget(photo, 0, Qt.AlignHCenter)

            text = QLabel(f"<b>{country} — {city}</b><br>{description}")
            text.setWordWrap(True)
            text.setAlignment(Qt.AlignCenter)
            text.setStyleSheet("font-size: 13px; padding: 0 12px;")
            card_layout.addWidget(text)

            self.layout.addWidget(card)
            self.layout.addSpacing(6)

            self.image_loader.fetch(
                country, city,
                on_success=lambda pixmap, g=gen, lbl=photo: self._on_traveler_photo_loaded(g, lbl, pixmap),
                on_failure=lambda: None,  # остаётся эмодзи-иконка
            )

        self.layout.addSpacing(8)

        try_btn = QPushButton("Всё-таки попробовать подбор")
        try_btn.clicked.connect(self.begin_main_flow)
        self.layout.addWidget(try_btn)

        close_btn = QPushButton("Закрыть")
        close_btn.setObjectName("noButton")
        close_btn.clicked.connect(self.accept)
        self.layout.addWidget(close_btn)

        back_btn = QPushButton("← Назад")
        back_btn.setObjectName("secondary")
        back_btn.clicked.connect(self.show_intro_known)
        self.layout.addWidget(back_btn)

        self.layout.addStretch()

    def begin_main_flow(self):
        """Ветка 'да' входной развилки: переходим в уже существующую цепочку вопросов."""
        self.clear_layout(self.layout)
        self.build_question_ui()
        self.show_question()

    def build_question_ui(self):
        """Наполняет self.layout виджетами экрана вопроса. Layout создаётся только один раз в __init__."""
        self.progress_label = QLabel("")
        self.progress_label.setObjectName("subtitle")
        self.progress_label.setAlignment(Qt.AlignCenter)
        self.layout.addWidget(self.progress_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, len(self.CATEGORIES))
        self.progress_bar.setTextVisible(False)
        self.layout.addWidget(self.progress_bar)

        self.layout.addSpacing(10)

        self.icon_label = QLabel("")
        self.icon_label.setObjectName("icon")
        self.icon_label.setAlignment(Qt.AlignCenter)
        self.layout.addWidget(self.icon_label)

        self.question_label = QLabel("")
        self.question_label.setAlignment(Qt.AlignCenter)
        self.question_label.setWordWrap(True)
        self.question_label.setStyleSheet("font-size: 19px; font-weight: 600;")
        self.layout.addWidget(self.question_label)

        self.layout.addSpacing(10)

        answer_row = QHBoxLayout()
        self.yes_btn = QPushButton("Да")
        self.yes_btn.clicked.connect(self.answer_yes)
        self.no_btn = QPushButton("Нет")
        self.no_btn.setObjectName("noButton")
        self.no_btn.clicked.connect(self.answer_no)
        answer_row.addWidget(self.no_btn)
        answer_row.addWidget(self.yes_btn)
        self.layout.addLayout(answer_row)

        self.layout.addSpacing(8)

        self.back_btn = QPushButton("← Назад")
        self.back_btn.setObjectName("secondary")
        self.back_btn.clicked.connect(self.go_back)
        self.layout.addWidget(self.back_btn)

        self.layout.addStretch()

    def current_category(self):
        return self.CATEGORIES[self.cat_index]

    def current_options(self):
        code, _, _ = self.current_category()
        return self.options_cache[code]

    def push_history(self):
        self.history_stack.append((self.cat_index, self.opt_index, dict(self.answers)))

    def go_back(self):
        if not self.history_stack:
            return
        self.cat_index, self.opt_index, self.answers = self.history_stack.pop()
        self.show_question()

    def show_question(self):
        code, name, _ = self.current_category()
        options = self.current_options()

        self.back_btn.setEnabled(bool(self.history_stack))

        if self.opt_index >= len(options):
            # Пользователь ответил "Нет" на все варианты — считаем критерий не важным
            self.push_history()
            self.answers[code] = "any"
            self.next_category()
            return

        opt_code, opt_name, *rest = options[self.opt_index]
        icon = rest[-1] if rest else ""

        total = len(self.CATEGORIES)
        self.progress_label.setText(f"Шаг {self.cat_index + 1} из {total}: {name}")
        self.progress_bar.setValue(self.cat_index)

        self.icon_label.setText(icon)

        if code == "budget":
            min_p, max_p = options[self.opt_index][2], options[self.opt_index][3]
            price_txt = f"до {max_p}$" if min_p == 0 else (f"более {min_p}$" if max_p >= 999999 else f"{min_p}$ – {max_p}$")
            self.question_label.setText(f"Подходит бюджет «{opt_name}» ({price_txt})?")
        else:
            self.question_label.setText(f"Вам подходит вариант «{opt_name}»?")

    def answer_yes(self):
        self.push_history()
        code, _, _ = self.current_category()
        options = self.current_options()
        opt_code = options[self.opt_index][0]
        self.answers[code] = opt_code
        self.next_category()

    def answer_no(self):
        self.push_history()
        self.opt_index += 1
        self.show_question()

    def next_category(self):
        self.cat_index += 1
        self.opt_index = 0
        if self.cat_index >= len(self.CATEGORIES):
            self.progress_bar.setValue(len(self.CATEGORIES))
            self.finish()
        else:
            self.show_question()

    def finish(self):
        # "any" передаётся в поиск как есть: это означает "пользователю не важно значение
        # этого критерия" (см. Database.find_relaxed - там такой критерий просто не участвует
        # в фильтрации). Раньше здесь "any" подменялось на первый вариант списка, из-за чего
        # почти любой запрос с "не важно" по интересам искусственно проваливал точное
        # совпадение и трасса вывода выглядела одинаково для всех направлений.
        self._query = {code: self.answers.get(code, "any") for code, _, _ in self.CATEGORIES}
        self._filters = empty_filters()
        self._history_id = None

        if self.use_ai:
            self.show_thinking("ИИ сравнивает направления", self._finish_ai)
        else:
            self._finish_rules()

    def _query_args(self):
        return [self._query[code] for code, _, _ in self.CATEGORIES]

    def _finish_rules(self):
        result = self.rule_engine.recommend(*self._query_args())
        dest = result["destination"]
        result["criteria"] = compare_with_query(dest, self._query, QUERY_NAMES)
        result["cost"] = estimate_cost(dest, self._query, self.ai.settings.travelers)
        self._accept_result(result)

    def _finish_ai(self):
        self._accept_result(self._run_ai(self._query, self._filters))

    def _run_ai(self, query, filters):
        # Последние подборы из истории: среди равноценных вариантов движок предложит новые.
        recent = {(row[9], row[10]) for row in self.db.get_history()[:15]}
        return self.ai_engine.recommend(
            self.db.list_destination_dicts(), query, self.ai.settings.travelers, recent, filters)

    def _accept_result(self, result):
        """Общий финал: шутка про невозможное сочетание, история, экран результата."""
        self._apply_absurd_note(result)
        cost_usd = result["cost"]["total_usd"]
        if self._history_id is None:
            self._history_id = self.db.save_history(
                *self._query_args(), self.use_ai, result["country"], result["city"], cost_usd)
        else:
            self.db.update_history_result(self._history_id, result["country"], result["city"], cost_usd)
        self._result = result
        self.ai.last_result = result
        self.show_result(result)

    def refine(self, wish):
        """Уточнение обычным текстом: ИИ разбирает его и подбирает заново."""
        wish = wish.strip()
        if not wish or self._result is None or self._result["source"] != "ai":
            return
        destinations = self.db.list_destination_dicts()
        query, filters, notes = parse_wish(
            wish, self._query, self._filters, self._result, {d["country"] for d in destinations},
            self.ai.converter.rate, QUERY_NAMES)
        if not notes:
            self._refine_message("Не понял уточнение. Попробуйте, например: «не больше 1000 $», «без Испании», "
                                 "«с детьми на море», «на 5 дней зимой».")
            return
        if query == self._query and filters == self._filters:
            self._refine_message("Это уже учтено: " + "; ".join(notes) + ".")
            return
        result = self._run_ai(query, filters)
        if result is None:
            self._refine_message("Понял: " + "; ".join(notes) + ". Но таких направлений в базе нет, поэтому "
                                 "оставил прежний вариант.")
            return
        result["refinements"] = self._result["refinements"] + notes

        def apply():
            self._query, self._filters = query, filters
            self._accept_result(result)

        self.show_thinking("ИИ учитывает уточнение", apply)

    def _refine_message(self, text):
        self.refine_label.setText(text)
        self.refine_label.show()

    def show_thinking(self, text, then):
        """Короткий экран «ИИ думает», после которого выполняется then()."""
        self._stop_timers()
        self._result_generation += 1  # фото для прежнего экрана больше не нужно
        self.clear_layout(self.layout)

        self.layout.addStretch()

        icon = QLabel("🤖")
        icon.setObjectName("icon")
        icon.setAlignment(Qt.AlignCenter)
        self.layout.addWidget(icon)

        self._thinking_text = text
        self.thinking_label = QLabel(text)
        self.thinking_label.setAlignment(Qt.AlignCenter)
        self.thinking_label.setStyleSheet("font-size: 17px; font-weight: 600;")
        self.layout.addWidget(self.thinking_label)

        busy_bar = QProgressBar()
        busy_bar.setRange(0, 0)  # индикатор без конкретного значения - "бегущая" полоса
        busy_bar.setTextVisible(False)
        busy_bar.setFixedWidth(260)
        bar_row = QHBoxLayout()
        bar_row.addStretch()
        bar_row.addWidget(busy_bar)
        bar_row.addStretch()
        self.layout.addLayout(bar_row)
        self.layout.addStretch()

        self._dot_count = 0
        self._dot_timer = QTimer(self)
        self._dot_timer.timeout.connect(self._tick_dots)
        self._dot_timer.start(350)

        self._thinking_timer = QTimer(self)
        self._thinking_timer.setSingleShot(True)
        self._thinking_timer.timeout.connect(lambda: (self._stop_timers(), then()))
        self._thinking_timer.start(1000)

    def _tick_dots(self):
        try:
            self._dot_count = (self._dot_count + 1) % 4
            self.thinking_label.setText(self._thinking_text + "." * self._dot_count)
        except RuntimeError:
            # Виджет уже уничтожен (окно закрыли/перезапустили во время анимации)
            self._stop_dots()

    def _stop_dots(self):
        if self._dot_timer:
            self._dot_timer.stop()
            self._dot_timer.deleteLater()
            self._dot_timer = None

    def _apply_absurd_note(self, result):
        """Если пользователь запросил физически невозможное сочетание (например, пляжный
        отдых в Антарктиде), вставляет в начало объяснения честную саркастичную реплику
        об этом - см. build_absurd_note(). Реплика говорит "вот вариант в другом месте", поэтому
        нужна только для направления, которое действительно на другом континенте."""
        dest = result["destination"]
        continent = self._query["continent"]
        if dest["continent_code"] != continent:
            note = build_absurd_note(
                continent, self._query["rest_type"], self._query["duration"], result["relaxed"], result)
            if note:
                result["reasoning"] = note + " " + result["reasoning"]
        return result

    def show_result(self, result):
        # Очищаем текущий layout и строим экран результата (сам self.layout не пересоздаём)
        self.clear_layout(self.layout)
        is_ai = result["source"] == "ai"

        # Новый "поколение" результата: если пользователь уйдёт с этого экрана (перезапуск,
        # закрытие) раньше, чем придёт фото по сети, устаревший ответ просто игнорируется.
        self._result_generation += 1
        result_generation = self._result_generation

        mode_label = QLabel(result["mode"])
        mode_label.setAlignment(Qt.AlignCenter)
        mode_label.setStyleSheet("font-size: 13px; color: #4c6c82; font-weight: 600;")
        self.layout.addWidget(mode_label)

        icon = QLabel("🤖" if self.use_ai else "🧭")
        icon.setObjectName("icon")
        icon.setAlignment(Qt.AlignCenter)
        # Явное центрирование самого виджета в layout - без него QVBoxLayout прижимает
        # виджет с фиксированным размером (после загрузки фото, см. _on_photo_loaded) влево.
        self.layout.addWidget(icon, 0, Qt.AlignHCenter)
        self.result_icon_label = icon

        self.image_loader.fetch(
            result["country"], result["city"],
            on_success=lambda pixmap: self._on_photo_loaded(result_generation, icon, pixmap),
            on_failure=lambda: None,  # ничего не делаем - эмодзи-иконка уже показана
        )

        title = plain_label(f"{result['country']} — {result['city']}", align=Qt.AlignCenter)
        title.setObjectName("title")
        self.layout.addWidget(title)

        self.layout.addWidget(plain_label(result["description"], "font-size: 15px;", Qt.AlignCenter))

        self.layout.addSpacing(6)

        if result.get("refinements"):
            self.layout.addWidget(plain_label("Учтено: " + "; ".join(result["refinements"]),
                                              "font-size: 12px; color: #4c6c82;", Qt.AlignCenter))

        self.reasoning_label = plain_label(
            "", "font-size: 13px; color: #4c6c82; padding: 8px; "
                "background-color: #dff2fb; border-radius: 8px;", Qt.AlignCenter)
        self.layout.addWidget(self.reasoning_label)

        self.layout.addSpacing(4)
        self.layout.addWidget(CostCard(result["cost"], self.ai.converter))

        if is_ai:
            self._add_ai_details(result)

        if "confidence" in result:
            self.layout.addSpacing(4)

            matched, asked = count_matches(result["criteria"])
            conf_label = QLabel(
                f"Соответствие запросу: {result['confidence']}%"
                + (f" · совпало критериев: {matched} из {asked}" if asked else ""))
            conf_label.setAlignment(Qt.AlignCenter)
            conf_label.setStyleSheet("font-size: 12px; color: #4c6c82;")
            self.layout.addWidget(conf_label)

            conf_bar = QProgressBar()
            conf_bar.setRange(0, 100)
            conf_bar.setValue(0)  # заполняется анимацией ниже
            conf_bar.setTextVisible(False)
            conf_bar.setFixedWidth(260)
            conf_row = QHBoxLayout()
            conf_row.addStretch()
            conf_row.addWidget(conf_bar)
            conf_row.addStretch()
            self.layout.addLayout(conf_row)
            self._animate_confidence(conf_bar, result["confidence"])

        self.layout.addSpacing(6)

        if is_ai:
            self._add_refine_box()

        explain_btn = QPushButton("Как получен этот результат?")
        explain_btn.setObjectName("secondary")
        explain_btn.clicked.connect(lambda: self.show_explanation(result))
        self.layout.addWidget(explain_btn)

        self.layout.addSpacing(6)

        chat_btn = QPushButton("💬 Обсудить с ИИ-консультантом")
        chat_btn.setObjectName("secondary")
        chat_btn.clicked.connect(lambda: AIConsultantWindow(self.ai, self.db, result, self).exec_())
        self.layout.addWidget(chat_btn)

        self.layout.addSpacing(6)

        again_btn = QPushButton("Пройти ещё раз")
        again_btn.clicked.connect(self.restart)
        self.layout.addWidget(again_btn)

        close_btn = QPushButton("Закрыть")
        close_btn.setObjectName("noButton")
        close_btn.clicked.connect(self.accept)
        self.layout.addWidget(close_btn)

        self.layout.addStretch()

        if self.use_ai:
            self._start_typewriter(self.reasoning_label, result["reasoning"])
        else:
            self.reasoning_label.setText(result["reasoning"])

    def _add_ai_details(self, result):
        """Советы и запасные варианты."""
        blocks = []
        if result["tips"]:
            blocks.append(("💡 Советы", ["• " + t for t in result["tips"]]))
        alternatives = [
            f"• {a['country']} — {a['city']} ({a['percent']}%, ≈ {format_usd(a['total_usd'])}): {a['reason']}"
            for a in result["alternatives"]
        ]
        if alternatives:
            blocks.append(("🔁 Запасные варианты", alternatives))
        for title, lines in blocks:
            self.layout.addWidget(plain_label(title, "font-size: 14px; font-weight: 600;"))
            self.layout.addWidget(plain_label("\n".join(lines), "font-size: 13px;"))

    def _add_refine_box(self):
        """Поле «уточнить ответ»: ИИ разбирает пожелание и подбирает заново."""
        self.layout.addWidget(plain_label("✏️ Уточнить ответ ИИ", "font-size: 14px; font-weight: 600;"))

        row = QHBoxLayout()
        refine_input = QLineEdit()
        refine_input.setPlaceholderText("Например: с детьми на море, не больше 1000 $")
        refine_input.setMaxLength(500)
        refine_input.setStyleSheet("background-color: white; border: 1px solid #cfe3ee; "
                                   "border-radius: 6px; padding: 9px;")
        refine_btn = QPushButton("Уточнить")
        refine_btn.clicked.connect(lambda: self.refine(refine_input.text()))
        # Enter в поле нажимает кнопку по умолчанию - назначаем ею именно «Уточнить».
        refine_btn.setDefault(True)
        row.addWidget(refine_input, 1)
        row.addWidget(refine_btn)
        self.layout.addLayout(row)

        chips = QHBoxLayout()
        for text, wish in (
            ("Дешевле", "Найди вариант дешевле"),
            ("Другая страна", "Предложи другую страну"),
            ("Спокойнее", "Хочу спокойное место"),
            ("Активнее", "Хочу активный отдых"),
        ):
            chip = QPushButton(text)
            chip.setObjectName("secondary")
            chip.setAutoDefault(False)
            chip.setStyleSheet("padding: 6px; font-size: 12px; border-width: 1px;")
            chip.clicked.connect(lambda _=False, w=wish: self.refine(w))
            chips.addWidget(chip)
        self.layout.addLayout(chips)

        self.refine_label = plain_label("", "font-size: 13px; color: #b3261e;")
        self.refine_label.hide()
        self.layout.addWidget(self.refine_label)

    def _on_photo_loaded(self, result_generation, label, pixmap):
        if result_generation != self._result_generation:
            return  # пользователь уже ушёл с этого экрана результата - ответ устарел
        try:
            scaled = pixmap.scaled(320, 190, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            label.setPixmap(self._rounded_pixmap(scaled, radius=16))
            label.setFixedSize(scaled.size())
            label.setStyleSheet("")  # снимаем стиль #icon (font-size 60px) - теперь тут картинка
        except RuntimeError:
            pass  # виджет результата успели уничтожить (перезапуск/закрытие окна)

    def _on_traveler_photo_loaded(self, generation, label, pixmap):
        if generation != self._travelers_generation:
            return  # пользователь уже ушёл с этого экрана - карточка больше не актуальна
        try:
            scaled = pixmap.scaled(480, 150, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            label.setPixmap(self._rounded_pixmap(scaled, radius=10))
            label.setFixedSize(scaled.size())
            label.setStyleSheet("")
        except RuntimeError:
            pass  # карточка успела быть уничтожена (переход на другой экран)

    @staticmethod
    def _rounded_pixmap(pixmap, radius=16):
        if pixmap.isNull():
            return pixmap
        rounded = QPixmap(pixmap.size())
        rounded.fill(Qt.transparent)
        painter = QPainter(rounded)
        painter.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(rounded.rect()), radius, radius)
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, pixmap)
        painter.end()
        return rounded

    def show_explanation(self, result):
        self.explanation_window = ExplanationWindow(result, self.ai.converter)
        self.explanation_window.exec_()

    def _animate_confidence(self, bar, target_value):
        """Плавное заполнение полосы уверенности ИИ от 0 до целевого значения."""
        state = {"value": 0}

        def tick():
            try:
                state["value"] += 3
                bar.setValue(min(state["value"], target_value))
                if state["value"] >= target_value:
                    timer.stop()
            except RuntimeError:
                timer.stop()

        timer = QTimer(self)
        timer.timeout.connect(tick)
        timer.start(20)

    def _start_typewriter(self, label, text, chars_per_tick=2, interval=14):
        """Печатает текст по несколько символов за тик - эффект 'ИИ печатает ответ'."""
        if self._tw_timer:
            self._tw_timer.stop()

        self._tw_text = text
        self._tw_index = 0
        self._tw_label = label

        self._tw_timer = QTimer(self)
        self._tw_timer.timeout.connect(lambda: self._tw_tick(chars_per_tick))
        self._tw_timer.start(interval)

    def _tw_tick(self, chars_per_tick):
        try:
            self._tw_index += chars_per_tick
            self._tw_label.setText(self._tw_text[:self._tw_index])
            if self._tw_index >= len(self._tw_text):
                self._tw_timer.stop()
        except RuntimeError:
            if self._tw_timer:
                self._tw_timer.stop()

    def _stop_timers(self):
        """Останавливает анимации и отменяет отложенный показ результата."""
        self._stop_dots()
        if self._thinking_timer:
            self._thinking_timer.stop()
            self._thinking_timer = None
        if self._tw_timer:
            self._tw_timer.stop()
            self._tw_timer = None

    def clear_layout(self, layout):
        """Полностью выгружает виджеты и вложенные layout'ы, не трогая сам layout-контейнер."""
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
                continue
            child_layout = item.layout()
            if child_layout is not None:
                self.clear_layout(child_layout)

    def restart(self):
        self._stop_timers()
        self._result = None
        self.answers = {}
        self.cat_index = 0
        self.opt_index = 0
        self.history_stack = []
        self.begin_main_flow()

    def done(self, code):
        # Сюда сходятся все способы закрыть окно: кнопка, крестик и Esc.
        self._stop_timers()
        self._result_generation += 1
        super().done(code)


####################################
######### ОБЪЯСНЕНИЕ ВЫВОДА ########
####################################

class ExplanationWindow(QDialog):
    """
    Показывает, как получена рекомендация: ответы пользователя, ход отбора (трасса правил или рейтинг
    ИИ), сверка по каждому критерию с баллами, расчёт суммы и итоговой оценки.
    """

    STATUS = {
        "match": ("✅", "#dff2fb"), "partial": ("🟡", "#fff6db"), "mismatch": ("❌", "#fbe4e2"),
    }
    MUTED = "font-size: 12px; color: #4c6c82;"

    def __init__(self, result, converter):
        super().__init__()
        self.setWindowTitle("Как получена рекомендация")
        self.setMinimumSize(560, 620)
        self.result = result
        self.is_ai = result["source"] == "ai"

        layout = QVBoxLayout()
        self.setLayout(layout)

        title = QLabel("Ход рассуждения")
        title.setObjectName("title")
        layout.addWidget(title)

        subtitle = plain_label(f"Итоговый выбор: {result['country']} — {result['city']}")
        subtitle.setObjectName("subtitle")
        layout.addWidget(subtitle)
        layout.addWidget(plain_label(result["mode"], self.MUTED))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        steps_widget = QWidget()
        self.steps = QVBoxLayout()
        self.steps.setSpacing(6)
        steps_widget.setLayout(self.steps)

        self._step = 0
        self._add_answers()
        if self.is_ai:
            self._add_ranking()
        else:
            self._add_trace()
            self._add_rule_choice()
        self._add_criteria()
        self._add_cost(converter)

        self.steps.addStretch()
        scroll.setWidget(steps_widget)
        layout.addWidget(scroll)

        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)

    def _heading(self, text):
        self._step += 1
        self.steps.addSpacing(8)
        self.steps.addWidget(plain_label(f"{self._step}. {text}", "font-size: 15px; font-weight: 700;"))

    def _line(self, text, color="#dff2fb", extra=""):
        self.steps.addWidget(plain_label(
            text, f"font-size: 13px; padding: 8px; border-radius: 6px; background-color: {color}; {extra}"))

    def _note(self, text):
        self.steps.addWidget(plain_label(text, self.MUTED))

    def _add_answers(self):
        self._heading("Ваши ответы")
        asked = [f"{c['title']}: {c['wanted']}" for c in self.result["criteria"] if c["status"] != "any"]
        free = [c["title"].lower() for c in self.result["criteria"] if c["status"] == "any"]
        if asked:
            self._line("\n".join(asked))
        if free:
            self._note("Не важно (в подборе не участвует): " + ", ".join(free))
        for wish in self.result.get("refinements", []):
            self._line(f"✏️ Уточнение: {wish}", "#fff6db")

    def _add_trace(self):
        self._heading("Отбор по базе знаний")
        self._note("Условия ослабляются от наименее важного к самому важному, пока не найдётся совпадение.")
        for step in self.result["trace"]:
            if step["success"]:
                self._line(f"✅ {step['stage']} — совпадений: {step['count']}", extra="font-weight: 600;")
            else:
                self._line(f"❌ {step['stage']} — совпадений: {step['count']}", "#f0f0f0", "color: #8a8a8a;")

    def _add_rule_choice(self):
        self._heading("Выбор варианта")
        count = self.result["trace"][-1]["count"]
        if count > 1:
            self._line(f"Подошло направлений: {count}. Для правил они равноценны, поэтому одно выбрано случайно.")
        else:
            self._line("Подошло единственное направление, оно и предложено.")

    def _add_ranking(self):
        result = self.result
        self._heading("Сравнение направлений")
        self._note("ИИ не отбрасывает условия по очереди, а оценивает каждое направление по всем важным для "
                   "вас критериям сразу: чем важнее критерий и точнее совпадение, тем больше баллов.")
        skipped = result["total_destinations"] - result["considered"]
        text = f"Сравнил направлений: {result['considered']}"
        if skipped:
            text += f" (ещё {skipped} исключено вашими уточнениями)"
        self._line(f"{text}. Соответствие 80% и выше: {result['strong']}.")
        for place, item in enumerate(result["ranking"], start=1):
            if item["chosen"]:
                self._line(f"✅ {place}. {item['place']}: {item['percent']}%", extra="font-weight: 600;")
            else:
                self._line(f"{place}. {item['place']}: {item['percent']}%", "#f0f0f0")
        if result["equal"] > 1:
            text = f"Почти равных лидеров: {result['equal']}, из них выбран один случайно"
            if result["skipped_recent"]:
                text += f"; недавно показанные вам ({result['skipped_recent']}) пропущены"
            self._note(text + ".")

    def _add_criteria(self):
        self._heading("Сверка по критериям")
        asked = [c for c in self.result["criteria"] if c["status"] != "any"]
        if not asked:
            self._note("Вы не задали ни одного критерия, поэтому подходит любое направление.")
            return
        for c in asked:
            mark, color = self.STATUS[c["status"]]
            text = f"{mark} {c['title']}: вы хотели «{c['wanted']}», у направления «{c['actual']}»"
            if self.is_ai:
                text += f"\n{c['weight'] * c['degree']:.1f} из {c['weight']} баллов"
            self._line(text, color)
        if self.is_ai:
            earned = sum(c["weight"] * c["degree"] for c in asked)
            total = sum(c["weight"] for c in asked)
            self._line(f"Итого {earned:.1f} из {total} баллов = соответствие {self.result['confidence']}%",
                       extra="font-weight: 600;")
            self._note("«Любой» у направления даёт чуть меньше полного балла: в базе нет ограничения, но нет и "
                       "подтверждения, что место именно для этого.")
        else:
            matched, total = count_matches(self.result["criteria"])
            self._note(f"Совпало полностью или частично: {matched} из {total}")

    def _add_cost(self, converter):
        cost = self.result["cost"]
        self._heading("Как посчитана сумма")
        for item in cost["items"]:
            self._line(f"{item['category']}: {format_usd(item['amount_usd'])}", "#f0f0f0")
        kzt = converter.format_kzt(cost["total_usd"])
        self._line(f"Итого на {cost['travelers']} чел.: ≈ {format_usd(cost['total_usd'])}"
                   + (f" ({kzt})" if kzt else ""), extra="font-weight: 600;")
        self._note("Основа - уровень цен направления в базе, сдвинутый по длительности поездки и умноженный на "
                   "число путешественников. По статьям сумма разделена типичными долями: дорога зависит от "
                   "дальности континента, остальное - от типа отдыха. Это ориентир, а не цены на билеты.")
        self._note(converter.describe())


####################################
############ ИСТОРИЯ ###############
####################################

class HistoryWindow(QDialog):
    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.setWindowTitle("История подборов")
        self.setMinimumSize(700, 450)
        self.init_ui()
        self.load_history()

    def init_ui(self):
        layout = QVBoxLayout()
        self.setLayout(layout)

        title = QLabel("История подборов направлений")
        title.setObjectName("title")
        layout.addWidget(title)

        self.table = QTableWidget()
        self.table.setColumnCount(12)
        self.table.setHorizontalHeaderLabels(
            ["Дата", "Бюджет", "Континент", "Тип отдыха", "Сезон", "Длительность",
             "Виза", "Интересы", "Режим", "Страна", "Город", "Сумма"]
        )
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.table)

        btn_row = QHBoxLayout()
        refresh_btn = QPushButton("Обновить")
        refresh_btn.setObjectName("secondary")
        refresh_btn.clicked.connect(self.load_history)
        btn_row.addWidget(refresh_btn)

        clear_btn = QPushButton("Очистить историю")
        clear_btn.setObjectName("noButton")
        clear_btn.clicked.connect(self.clear_history)
        btn_row.addWidget(clear_btn)

        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)

        layout.addLayout(btn_row)

    def load_history(self):
        rows = self.db.get_history()
        self.table.setRowCount(len(rows))
        for r, (dt, budget, continent, rest_type, season, duration, visa, interest,
                use_ai, country, city, cost_usd) in enumerate(rows):
            values = [
                dt, budget, continent, rest_type, season, duration, visa, interest,
                "ИИ" if use_ai else "Правила", country, city,
                "" if cost_usd is None else format_usd(cost_usd),
            ]
            for c, val in enumerate(values):
                self.table.setItem(r, c, QTableWidgetItem(str(val)))

    def clear_history(self):
        confirm = QMessageBox.question(
            self, "Подтверждение",
            "Очистить всю историю подборов?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm == QMessageBox.Yes:
            self.db.clear_history()
            self.load_history()


####################################
######### БАЗА ЗНАНИЙ (UI) #########
####################################

class KnowledgeBaseWindow(QDialog):
    """
    Позволяет добавлять и удалять направления (правила подбора) без правки кода -
    ровно те факты, которыми оперирует RecommendationEngine / SmartEngine.
    """

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.setWindowTitle("Управление базой знаний")
        self.setMinimumSize(820, 580)
        self.init_ui()
        self.load_table()

    def init_ui(self):
        layout = QVBoxLayout()
        self.setLayout(layout)

        title = QLabel("База знаний: направления путешествий")
        title.setObjectName("title")
        layout.addWidget(title)

        hint = QLabel("Каждая строка — это правило: при таком сочетании бюджета, континента, "
                       "типа отдыха, сезона, длительности, визы и интересов система предложит "
                       "именно это направление. Значение \"Любой/Любая\" означает, что критерий "
                       "не важен и подходит любой ответ пользователя.")
        hint.setObjectName("subtitle")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addSpacing(6)

        form = QGridLayout()
        form.setSpacing(8)

        self.budget_combo = QComboBox()
        for code, name, *_ in self.db.get_lookup("budgets"):
            self.budget_combo.addItem(name, code)

        self.continent_combo = QComboBox()
        for code, name, _ in self.db.get_lookup("continents"):
            self.continent_combo.addItem(name, code)

        self.type_combo = QComboBox()
        for code, name, _ in self.db.get_lookup("rest_types"):
            self.type_combo.addItem(name, code)

        self.season_combo = QComboBox()
        self.season_combo.addItem("Любой сезон", "any")
        for code, name, _ in self.db.get_lookup("seasons"):
            self.season_combo.addItem(name, code)

        self.duration_combo = QComboBox()
        self.duration_combo.addItem("Любая длительность", "any")
        for code, name, _ in self.db.get_lookup("durations"):
            self.duration_combo.addItem(name, code)

        self.visa_combo = QComboBox()
        self.visa_combo.addItem("Любой визовый режим", "any")
        for code, name, _ in self.db.get_lookup("visas"):
            self.visa_combo.addItem(name, code)

        self.interest_combo = QComboBox()
        self.interest_combo.addItem("Любой интерес", "any")
        for code, name, _ in self.db.get_lookup("interests"):
            self.interest_combo.addItem(name, code)

        self.country_input = QLineEdit()
        self.country_input.setPlaceholderText("Страна")
        self.city_input = QLineEdit()
        self.city_input.setPlaceholderText("Город")
        self.description_input = QLineEdit()
        self.description_input.setPlaceholderText("Краткое описание направления")

        form.addWidget(QLabel("Бюджет:"), 0, 0)
        form.addWidget(self.budget_combo, 0, 1)
        form.addWidget(QLabel("Континент:"), 0, 2)
        form.addWidget(self.continent_combo, 0, 3)

        form.addWidget(QLabel("Тип отдыха:"), 1, 0)
        form.addWidget(self.type_combo, 1, 1)
        form.addWidget(QLabel("Сезон:"), 1, 2)
        form.addWidget(self.season_combo, 1, 3)

        form.addWidget(QLabel("Длительность:"), 2, 0)
        form.addWidget(self.duration_combo, 2, 1)
        form.addWidget(QLabel("Виза:"), 2, 2)
        form.addWidget(self.visa_combo, 2, 3)

        form.addWidget(QLabel("Интересы:"), 3, 0)
        form.addWidget(self.interest_combo, 3, 1)

        form.addWidget(QLabel("Страна:"), 4, 0)
        form.addWidget(self.country_input, 4, 1)
        form.addWidget(QLabel("Город:"), 4, 2)
        form.addWidget(self.city_input, 4, 3)

        form.addWidget(QLabel("Описание:"), 5, 0)
        form.addWidget(self.description_input, 5, 1, 1, 3)

        layout.addLayout(form)

        add_btn = QPushButton("Добавить направление в базу знаний")
        add_btn.clicked.connect(self.add_destination)
        layout.addWidget(add_btn)

        layout.addSpacing(8)

        self.table = QTableWidget()
        self.table.setColumnCount(10)
        self.table.setHorizontalHeaderLabels(
            ["Бюджет", "Континент", "Тип отдыха", "Сезон", "Длительность", "Виза",
             "Интересы", "Страна", "Город", "Описание"]
        )
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.table)

        btn_row = QHBoxLayout()
        delete_btn = QPushButton("Удалить выбранное")
        delete_btn.setObjectName("noButton")
        delete_btn.clicked.connect(self.delete_selected)
        btn_row.addWidget(delete_btn)

        close_btn = QPushButton("Закрыть")
        close_btn.setObjectName("secondary")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def add_destination(self):
        country = self.country_input.text().strip()
        city = self.city_input.text().strip()
        description = self.description_input.text().strip() or "Описание не указано."

        if not country or not city:
            QMessageBox.warning(self, "Ошибка", "Укажите страну и город!")
            return

        self.db.add_destination(
            self.budget_combo.currentData(),
            self.continent_combo.currentData(),
            self.type_combo.currentData(),
            self.season_combo.currentData(),
            self.duration_combo.currentData(),
            self.visa_combo.currentData(),
            self.interest_combo.currentData(),
            country, city, description,
        )

        self.country_input.clear()
        self.city_input.clear()
        self.description_input.clear()
        self.load_table()

    def delete_selected(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Внимание", "Сначала выберите строку в таблице.")
            return

        dest_id = self.table.item(row, 0).data(Qt.UserRole)
        confirm = QMessageBox.question(
            self, "Подтверждение",
            "Удалить это направление из базы знаний?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm == QMessageBox.Yes:
            self.db.delete_destination(dest_id)
            self.load_table()

    def load_table(self):
        rows = self.db.list_all_destinations()
        self.table.setRowCount(len(rows))
        for r, (dest_id, budget, continent, rtype, season, duration, visa, interest,
                country, city, description) in enumerate(rows):
            values = [
                BUDGET_NAMES.get(budget, budget),
                CONTINENT_NAMES.get(continent, continent),
                REST_TYPE_NAMES.get(rtype, rtype),
                "Любой" if season == "any" else SEASON_NAMES.get(season, season),
                "Любая" if duration == "any" else DURATION_NAMES.get(duration, duration),
                "Любой" if visa == "any" else VISA_NAMES.get(visa, visa),
                "Любой" if interest == "any" else INTEREST_NAMES.get(interest, interest),
                country, city, description,
            ]
            for c, val in enumerate(values):
                item = QTableWidgetItem(str(val))
                if c == 0:
                    item.setData(Qt.UserRole, dest_id)
                self.table.setItem(r, c, item)


####################################
############# ЗАПУСК ###############
####################################

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet(APP_STYLE)

    db = Database()

    window = MainWindow(db)
    window.show()

    sys.exit(app.exec_())
