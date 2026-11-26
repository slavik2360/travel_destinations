"""
Собственный ИИ подбора: работает полностью на компьютере, без сети и внешних сервисов.

В отличие от классических правил (Database.find_relaxed), которые отбрасывают условия целиком и по
очереди, движок сравнивает с запросом ВСЕ направления сразу: по каждому критерию считает степень
соответствия от 0 до 1 (нечёткая логика), складывает её с весами и выбирает лучший компромисс.
Ещё он понимает уточнения обычным текстом («не больше 1000 $, без Турции») и умеет объяснить расчёт.

Веб-порт - docs/smart.js, правила и числа в обоих файлах должны совпадать.
"""

import random
import re

####################################
############ КРИТЕРИИ ##############
####################################

# (ключ, название, колонка в БД, вес). Порядок важности тот же, что у правил ослабления.
CRITERIA = [
    ("budget", "Бюджет", "budget_code", 6),
    ("continent", "Континент", "continent_code", 7),
    ("rest_type", "Тип отдыха", "type_code", 10),
    ("season", "Время года", "season_code", 4),
    ("duration", "Продолжительность", "duration_code", 2),
    ("visa", "Виза", "visa_code", 3),
    ("interest", "Интересы", "interest_code", 2),
]
WEIGHTS = {key: weight for key, _, _, weight in CRITERIA}
COLUMNS = {key: column for key, _, column, _ in CRITERIA}

BUDGET_ORDER = ["economy", "standard", "high", "luxury"]
SEASON_ORDER = ["spring", "summer", "autumn", "winter"]
DURATION_ORDER = ["short", "week", "long", "extended"]
VISA_ORDER = ["visa_free", "easy_visa", "advance_visa"]

# Насколько один вариант заменяет другой, если точного совпадения нет.
_NEAR_TYPES = {
    frozenset(("nature", "active")): 0.6, frozenset(("beach", "wellness")): 0.4,
    frozenset(("nature", "wellness")): 0.35, frozenset(("nature", "excursion")): 0.3,
    frozenset(("active", "excursion")): 0.2,
}
_NEAR_CONTINENTS = {
    frozenset(("europe", "asia")): 0.2, frozenset(("europe", "africa")): 0.2,
    frozenset(("asia", "africa")): 0.15, frozenset(("asia", "australia_oceania")): 0.2,
    frozenset(("north_america", "south_america")): 0.2,
}
# «any» у направления - в базе нет ограничения, но и нет подтверждения, что оно именно для этого.
_UNSPECIFIED = {"season": 0.95, "duration": 0.9, "visa": 0.85, "interest": 0.8}


def degree(key, wanted, actual):
    """Степень соответствия 0..1 или None, если критерий пользователю не важен."""
    if wanted == "any":
        return None
    if actual == wanted:
        return 1.0
    if actual == "any":
        return _UNSPECIFIED.get(key, 0.8)
    if key == "rest_type":
        return _NEAR_TYPES.get(frozenset((wanted, actual)), 0.0)
    if key == "continent":
        return _NEAR_CONTINENTS.get(frozenset((wanted, actual)), 0.0)
    if key == "budget" and wanted in BUDGET_ORDER and actual in BUDGET_ORDER:
        step = BUDGET_ORDER.index(actual) - BUDGET_ORDER.index(wanted)
        return max(0.0, 1 + step * 0.35) if step < 0 else max(0.0, 0.2 - (step - 1) * 0.2)
    if key == "season" and wanted in SEASON_ORDER and actual in SEASON_ORDER:
        return 0.4 if (SEASON_ORDER.index(wanted) - SEASON_ORDER.index(actual)) % 2 else 0.0
    if key == "duration" and wanted in DURATION_ORDER and actual in DURATION_ORDER:
        return max(0.0, 1 - 0.5 * abs(DURATION_ORDER.index(wanted) - DURATION_ORDER.index(actual)))
    if key == "visa" and wanted in VISA_ORDER and actual in VISA_ORDER:
        # Виза проще, чем готов оформлять человек, - почти то же самое; сложнее - уже препятствие.
        return 0.95 if VISA_ORDER.index(actual) < VISA_ORDER.index(wanted) else 0.2
    if key == "interest":
        return 0.2
    return 0.0


def criterion_status(value):
    if value is None:
        return "any"
    if value >= 0.8:
        return "match"
    return "partial" if value > 0.2 else "mismatch"


def compare_with_query(dest, query, names):
    """Сверка направления с ответами по каждому критерию."""
    rows = []
    for key, title, column, weight in CRITERIA:
        wanted, actual = query[key], dest[column]
        value = degree(key, wanted, actual)
        rows.append({
            "key": key, "title": title, "weight": weight, "degree": value,
            "wanted": names[key].get(wanted, wanted),
            "actual": names[key].get(actual, actual),
            "status": criterion_status(value),
        })
    return rows


def score(dest, query):
    """Общее соответствие 0..1: взвешенное среднее по важным для пользователя критериям."""
    total = earned = 0.0
    for key, _, column, weight in CRITERIA:
        value = degree(key, query[key], dest[column])
        if value is not None:
            total += weight
            earned += weight * value
    return earned / total if total else 1.0


def count_matches(criteria):
    """(сколько критериев выполнено полностью или частично, сколько пользователь задал)."""
    asked = [c for c in criteria if c["status"] != "any"]
    return sum(c["status"] in ("match", "partial") for c in asked), len(asked)


####################################
######## СТОИМОСТЬ ПОЕЗДКИ #########
####################################

BUDGET_LIMITS = {"economy": (0, 500), "standard": (500, 1500), "high": (1500, 4000), "luxury": (4000, None)}
# Для «Люкс» в базе нет верхней границы, для «Экономного» - нижней; берём условные.
_COST_RANGE = {"economy": (250, 500), "standard": (500, 1500), "high": (1500, 4000), "luxury": (4000, 9000)}
_DURATION_SHARE = {"short": 0.15, "week": 0.45, "long": 0.75, "extended": 1.0}
_NIGHTS = {"short": 3, "week": 7, "long": 13, "extended": 20}
# Доля дороги в поездке: чем дальше континент, тем больше.
_ROAD_SHARE = {"asia": 0.30, "europe": 0.30, "africa": 0.36, "north_america": 0.45, "south_america": 0.45,
               "australia_oceania": 0.45, "antarctica": 0.55}
# Как обычно делятся остальные деньги (сумма долей = 1).
_SPLIT = {
    "default": (("Проживание", 0.42), ("Питание", 0.26), ("Транспорт на месте", 0.10),
                ("Экскурсии и развлечения", 0.14), ("Виза, страховка и прочее", 0.08)),
    "excursion": (("Проживание", 0.36), ("Питание", 0.24), ("Транспорт на месте", 0.12),
                  ("Экскурсии и развлечения", 0.20), ("Виза, страховка и прочее", 0.08)),
    "active": (("Проживание", 0.32), ("Питание", 0.22), ("Транспорт на месте", 0.12),
               ("Экскурсии и развлечения", 0.26), ("Виза, страховка и прочее", 0.08)),
    "wellness": (("Проживание", 0.46), ("Питание", 0.22), ("Транспорт на месте", 0.06),
                 ("Экскурсии и развлечения", 0.18), ("Виза, страховка и прочее", 0.08)),
}
_SPLIT["nature"] = _SPLIT["excursion"]
_SPLIT["beach"] = _SPLIT["default"]

BUDGET_STATUS_TEXT = {
    "within": "✅ Укладывается в выбранный бюджет",
    "over": "⚠️ Выше выбранного бюджета",
    "below": "💚 Дешевле выбранного бюджета",
}


def budget_status(budget_code, per_person):
    """Сравнение суммы на человека с выбранным бюджетом: None | 'within' | 'over' | 'below'."""
    if budget_code not in BUDGET_LIMITS:
        return None
    low, high = BUDGET_LIMITS[budget_code]
    if high is not None and per_person > high:
        return "over"
    return "below" if per_person < low else "within"


def per_person_cost(dest, query):
    low, high = _COST_RANGE.get(dest["budget_code"], (500, 1500))
    return int((low + (high - low) * _DURATION_SHARE[trip_duration(dest, query)]) / 10 + 0.5) * 10


def trip_duration(dest, query):
    duration = query["duration"] if query["duration"] != "any" else dest["duration_code"]
    return duration if duration in _NIGHTS else "week"


def estimate_cost(dest, query, travelers):
    """
    Сумма поездки: уровень цен направления из базы, сдвинутый по длительности. Разбивка - типичные
    доли расходов для региона и типа отдыха; статьи всегда складываются ровно в итог.
    """
    per_person = per_person_cost(dest, query)
    total = per_person * travelers
    road = int(total * _ROAD_SHARE.get(dest["continent_code"], 0.35) / 10 + 0.5) * 10
    items = [{"category": "Перелёт и дорога", "amount_usd": road}]
    rest = total - road
    split = _SPLIT.get(dest["type_code"], _SPLIT["default"])
    for category, share in split[1:]:
        items.append({"category": category, "amount_usd": int(rest * share / 10 + 0.5) * 10})
    lodging = total - sum(i["amount_usd"] for i in items)  # остаток округления - в самую крупную статью
    items.insert(1, {"category": split[0][0], "amount_usd": lodging})
    return {
        "total_usd": total, "per_person_usd": per_person, "travelers": travelers,
        "nights": _NIGHTS[trip_duration(dest, query)], "items": items,
        "budget_status": budget_status(query["budget"], per_person),
    }


####################################
####### УТОЧНЕНИЯ ТЕКСТОМ ##########
####################################

def empty_filters():
    return {
        "max_total": None, "max_per_person": None, "only_countries": [], "no_countries": [],
        "no_continents": [], "no_types": [], "no_places": [], "quiet": False,
    }


# (что меняем, значение, шаблон). Порядок важен: более конкретные фразы - раньше.
_KEYWORDS = [
    ("visa", "visa_free", r"без\s+виз\w*|безвиз\w*"),
    ("visa", "easy_visa", r"виз\w*\s+по\s+прил[её]т\w*|электронн\w+\s+виз\w*"),
    ("rest_type", "beach", r"пляж\w*|мор(?:е|я|ю|ем)|купа(?:ть|ни)\w*|океан(?:а|е|у|ом)?"),
    ("rest_type", "excursion", r"экскурс\w*|музе\w*|архитектур\w*|достопримечательност\w*"),
    ("rest_type", "active", r"активн\w*|треккинг\w*|поход\w*|лыж\w*|с[её]рф\w*|дайв\w*|спорт\w*|экстрим\w*"),
    ("rest_type", "nature", r"природ\w*|гор(?:ы|ах|ам|ами)|лес(?:а|ах|у)?|оз[её]р\w*"),
    ("rest_type", "wellness", r"спа|оздоров\w*|массаж\w*|термальн\w*|релакс\w*|санатор\w*"),
    ("season", "spring", r"весн\w*|март\w*|апрел\w*|ма(?:й|е|я)"),
    ("season", "summer", r"лет(?:о|ом)|летн\w+|июн\w*|июл\w*|август\w*"),
    ("season", "autumn", r"осен\w*|сентябр\w*|октябр\w*|ноябр\w*"),
    ("season", "winter", r"зим\w*|декабр\w*|январ\w*|феврал\w*"),
    ("duration", "short", r"выходн\w*|уик-?энд\w*"),
    ("duration", "extended", r"месяц\w*"),
    ("interest", "nightlife", r"ночн\w+\s+жизн\w*|клуб\w*|вечерин\w*|тусов\w*"),
    ("interest", "gastronomy", r"ед(?:а|ы|ой)|кухн\w*|гастроном\w*|ресторан\w*"),
    ("interest", "family", r"дет(?:и|ей|ьми|ям)|детск\w+|реб[её]н\w*|семь(?:я|и|ю|ей)|семейн\w*"),
    ("interest", "romance", r"романти\w*|медов\w+|девушк\w*|жен(?:ой|а)|мужем|вдво[её]м"),
    ("interest", "shopping", r"шопп?инг\w*|покупк\w*|магазин\w*"),
    ("budget", "luxury", r"люкс\w*|роскош\w*"),
    ("continent", "asia", r"ази(?:я|и|ю|ей)"),
    ("continent", "africa", r"африк\w+"),
    ("continent", "europe", r"европ\w+"),
    ("continent", "north_america", r"северн\w+\s+америк\w+"),
    ("continent", "south_america", r"южн\w+\s+америк\w+|латинск\w+\s+америк\w+"),
    ("continent", "australia_oceania", r"океани(?:я|и|ю|ей)"),
    ("continent", "antarctica", r"антарктид\w+"),
]
_KEYWORDS = [(key, value, re.compile(rf"\b(?:{pattern})\b")) for key, value, pattern in _KEYWORDS]

_CHEAPER = re.compile(r"\b(?:дешевл\w*|подешевл\w*|бюджетн\w+|эконом\w*|недорог\w*|дорого)\b")
_PRICIER = re.compile(r"\b(?:дороже|подороже|шикарн\w*)\b")
_OTHER_COUNTRY = re.compile(r"\bдруг\w+\s+стран\w+|\bв\s+другую\b")
_OTHER_PLACE = re.compile(r"\bдруг\w+\s+(?:вариант\w*|мест\w*|город\w*|направлен\w*)|\bещ[её]\s+вариант\w*|\bне\s+нравится\b")
_QUIET = re.compile(r"\b(?:спокойн\w*|тих(?:о|ое|ий|ую|ого)|потише|меньше\s+турист\w*|уедин\w*)\b")
_MONEY = re.compile(
    r"(?P<num>\d[\d\s]*(?:[.,]\d+)?)\s*(?P<k>тыс\w*\.?|к\b|млн|миллион\w*)?\s*"
    r"(?P<cur>\$|usd|доллар\w*|бакс\w*|₸|тенге|тг\b|kzt)")
# Сумма без валюты - только после явного «не больше / до / максимум»; считаем её долларами.
_BARE_MONEY = re.compile(r"\b(?:не\s+больше|не\s+дороже|не\s+более|максимум|дешевле|до)\s+(\d[\d\s]*\d)(?!\s*(?:дн|ноч|сут|чел))")
_DAYS = re.compile(r"(\d{1,2})\s*(?:-\s*\d{1,2}\s*)?(?:дн\w*|день|ноч\w*|сут\w*)")
_WEEKS = re.compile(r"(?:(\d|две|три)\s+)?недел\w+")
_NEGATION = re.compile(r"\b(не|без|кроме|ни)\b(?!\s+только)[^,.;!?]{0,18}$")


def _country_pattern(country):
    words = []
    for word in re.split(r"[\s-]+", country.lower().replace("ё", "е")):
        # Короткие и несклоняемые названия - целиком, остальные - основа с любым окончанием.
        cut = 2 if word.endswith(("ая", "яя", "ый", "ий", "ое", "ие", "ые")) else 1
        words.append(re.escape(word) if len(word) <= 4 else re.escape(word[:-cut]) + r"[а-я]{0,3}")
    return re.compile(r"\b" + r"[\s-]+".join(words) + r"\b")


def parse_wish(text, query, filters, current, countries, usd_to_kzt, names):
    """
    Разбирает уточнение обычным текстом. Возвращает (новый запрос, новые фильтры, что понято).
    current - показанный результат; пустой список «что понято» - уточнение не разобрано.
    """
    query = dict(query)
    filters = {k: list(v) if isinstance(v, list) else v for k, v in filters.items()}
    low = text.lower().replace("ё", "е")
    notes = []
    used = []  # участки текста, которые уже разобраны

    def negated(start):
        return bool(_NEGATION.search(low[:start]))

    def free(match):
        return all(match.end() <= a or match.start() >= b for a, b in used)

    def titled(key):
        return next(title for k, title, _, _ in CRITERIA if k == key)

    for match in _MONEY.finditer(low):
        used.append(match.span())
        value = float(re.sub(r"\s", "", match["num"]).replace(",", "."))
        factor = match["k"] or ""
        value *= 1_000_000 if factor.startswith(("млн", "миллион")) else 1000 if factor else 1
        if not match["cur"].startswith(("$", "usd", "доллар", "бакс")):
            if not usd_to_kzt:
                notes.append("сумму в тенге учесть не могу: курс недоступен")
                continue
            value /= usd_to_kzt
        if value > 0:
            filters["max_total"] = value
            notes.append(f"вся поездка не дороже {value:,.0f} $".replace(",", " "))

    money_given = bool(used)
    bare = None if money_given else _BARE_MONEY.search(low)
    if bare and float(re.sub(r"\s", "", bare[1])) >= 100:
        money_given = True
        used.append(bare.span())
        filters["max_total"] = float(re.sub(r"\s", "", bare[1]))
        notes.append(f"вся поездка не дороже {filters['max_total']:,.0f} $".replace(",", " "))

    weeks, days = _WEEKS.search(low), _DAYS.search(low)
    number = None
    if days:
        number = int(days[1])
        used.append(days.span())
    elif weeks:
        number = 7 * {"две": 2, "три": 3, None: 1}.get(weeks[1], int(weeks[1]) if (weeks[1] or "").isdigit() else 1)
        used.append(weeks.span())
    if number:
        query["duration"] = ("short" if number <= 4 else "week" if number <= 9 else
                             "long" if number <= 16 else "extended")
        notes.append(f"{titled('duration').lower()}: {names['duration'][query['duration']]}")

    for country in sorted(countries, key=len, reverse=True):
        match = _country_pattern(country).search(low)
        if not match or not free(match):
            continue
        used.append(match.span())
        if negated(match.start()):
            filters["no_countries"].append(country)
            filters["only_countries"] = [c for c in filters["only_countries"] if c != country]
            notes.append(f"кроме страны {country}")
        else:
            if not any(n.startswith("страна: ") for n in notes):
                filters["only_countries"] = []
            filters["only_countries"].append(country)
            filters["no_countries"] = [c for c in filters["no_countries"] if c != country]
            query["continent"] = "any"  # названная страна важнее континента из анкеты
            notes.append(f"страна: {country}")

    for key, value, pattern in _KEYWORDS:
        match = pattern.search(low)
        if not match or not free(match):
            continue
        used.append(match.span())
        label = names[key][value]
        # «без визы» само содержит «без» - это не отрицание.
        if key != "visa" and negated(match.start()):
            if key == "rest_type":
                filters["no_types"].append(value)
            elif key == "continent":
                filters["no_continents"].append(value)
            if query[key] == value:
                query[key] = "any"
            elif key not in ("rest_type", "continent"):
                continue
            notes.append(f"кроме «{label}»")
        else:
            query[key] = value
            if key == "rest_type":
                filters["no_types"] = [t for t in filters["no_types"] if t != value]
            elif key == "continent":
                filters["no_continents"] = [c for c in filters["no_continents"] if c != value]
                filters["only_countries"] = []
            notes.append(f"{titled(key).lower()}: {label}")

    cost = current["cost"]
    cheaper, pricier = _CHEAPER.search(low), _PRICIER.search(low)
    if pricier and negated(pricier.start()):
        pricier = None
    if cheaper and not money_given:  # «слишком дорого» и «недорого» - одна и та же просьба
        filters["max_per_person"] = cost["per_person_usd"] - 1
        notes.append(f"дешевле {cost['per_person_usd']:,.0f} $ на человека".replace(",", " "))
    elif pricier:
        level = BUDGET_ORDER.index(current["destination"]["budget_code"])
        query["budget"] = BUDGET_ORDER[min(level + 1, 3)]
        filters["max_per_person"] = filters["max_total"] = None
        notes.append(f"бюджет: {names['budget'][query['budget']]}")
    if cheaper or money_given:
        query["budget"] = "any"  # иначе критерий «Бюджет» тянул бы обратно к прежнему уровню

    if _OTHER_COUNTRY.search(low):
        filters["no_countries"].append(current["country"])
        filters["only_countries"] = []
        notes.append(f"кроме страны {current['country']}")
    elif _OTHER_PLACE.search(low):
        filters["no_places"].append((current["country"], current["city"]))
        notes.append(f"кроме места {current['city']}")
    if _QUIET.search(low):
        filters["quiet"] = True
        if query["rest_type"] in ("excursion", "active"):
            query["rest_type"] = "wellness"
        filters["no_places"].append((current["country"], current["city"]))
        notes.append("спокойное место, без ночной жизни и шопинга")
    return query, filters, notes


def passes(dest, query, filters, travelers):
    per_person = per_person_cost(dest, query)
    return not (
        (filters["max_total"] is not None and per_person * travelers > filters["max_total"])
        or (filters["max_per_person"] is not None and per_person > filters["max_per_person"])
        or (filters["only_countries"] and dest["country"] not in filters["only_countries"])
        or dest["country"] in filters["no_countries"]
        or dest["continent_code"] in filters["no_continents"]
        or dest["type_code"] in filters["no_types"]
        or (dest["country"], dest["city"]) in filters["no_places"]
        or (filters["quiet"] and dest["interest_code"] in ("nightlife", "shopping"))
    )


####################################
############# ДВИЖОК ###############
####################################

# Имена критериев, какими их знает build_absurd_note() и правила ослабления.
RELAXED_NAMES = {
    "interest": "интересы и впечатления", "duration": "продолжительность поездки",
    "visa": "визовые предпочтения", "season": "время года", "budget": "бюджет",
    "continent": "континент", "rest_type": "тип отдыха",
}
CLOSE_GAP = 0.02  # варианты с такой разницей считаем равноценными

_INTROS_HIGH = [
    "Сравнил направления базы ({total}) с вашими ответами: лучше всего подходит {place}.",
    "Из {total} направлений ближе всего к вашему запросу {place}.",
    "Взвесил ваши ответы по каждому из {total} направлений, и впереди оказалось место {place}.",
]
_INTROS_LOW = [
    "Точного попадания среди {total} направлений нет. Ближе всего к запросу {place}.",
    "Запрос непростой: из {total} направлений лучший компромисс сейчас {place}.",
]


class SmartEngine:
    """Подбор с оценкой всех направлений. names - словари «код -> название» по ключам CRITERIA."""

    def __init__(self, names):
        self.names = names

    def recommend(self, destinations, query, travelers, recent=(), filters=None):
        """Лучшее направление или None, если фильтрам уточнений не отвечает ни одно."""
        filters = filters or empty_filters()
        ranked, seen = [], set()
        for dest in destinations:
            if passes(dest, query, filters, travelers):
                ranked.append((score(dest, query), dest))
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        # Одно место может встречаться в базе несколько раз - оставляем лучшую запись.
        unique = []
        for value, dest in ranked:
            place = (dest["country"], dest["city"])
            if place not in seen:
                seen.add(place)
                unique.append((value, dest))
        if not unique:
            return None

        # Равноценные варианты не предлагаем по кругу: сначала те, которых не было в истории.
        best = unique[0][0]
        equal = [pair for pair in unique if best - pair[0] <= CLOSE_GAP]
        fresh = [pair for pair in equal if (pair[1]["country"], pair[1]["city"]) not in recent] or equal
        value, dest = random.choice(fresh)
        others = [pair for pair in unique if pair[1] is not dest]

        criteria = compare_with_query(dest, query, self.names)
        cost = estimate_cost(dest, query, travelers)
        return {
            "country": dest["country"],
            "city": dest["city"],
            "description": dest["description"],
            "destination": dest,
            "source": "ai",
            "mode": "🤖 Наш ИИ-подбор",
            "criteria": criteria,
            "relaxed": [RELAXED_NAMES[c["key"]] for c in criteria if c["status"] in ("partial", "mismatch")],
            "cost": cost,
            "confidence": int(value * 100 + 0.5),
            "reasoning": self._reasoning(dest, criteria, cost, value, len(unique)),
            "tips": self._tips(dest, query),
            "alternatives": [self._alternative(v, d, dest, query, travelers) for v, d in self._diverse(others, dest)],
            "considered": len(unique),
            "total_destinations": len({(d["country"], d["city"]) for d in destinations}),
            "strong": sum(v >= 0.8 for v, _ in unique),
            "equal": len(equal),
            "skipped_recent": len(equal) - len(fresh),
            "ranking": [{"place": f"{d['country']} — {d['city']}", "percent": int(v * 100 + 0.5), "chosen": d is dest}
                        for v, d in [(value, dest)] + others[:4]],
            "refinements": [],
        }

    def _reasoning(self, dest, criteria, cost, value, total):
        place = f"«{dest['country']} — {dest['city']}»"
        intro = random.choice(_INTROS_HIGH if value >= 0.75 else _INTROS_LOW).format(total=total, place=place)
        parts = [intro]
        matched = [c for c in criteria if c["status"] == "match"]
        matched.sort(key=lambda c: c["weight"], reverse=True)
        if matched:
            parts.append("Совпадает: " + ", ".join(f"{c['title'].lower()} («{c['wanted']}»)" for c in matched) + ".")
        for c in criteria:
            if c["status"] == "partial":
                parts.append(f"Отчасти подходит {c['title'].lower()}: вы хотели «{c['wanted']}», здесь «{c['actual']}».")
            elif c["status"] == "mismatch":
                parts.append(f"Пришлось уступить: {c['title'].lower()} здесь «{c['actual']}», а не «{c['wanted']}».")
        money = f"{cost['per_person_usd']:,.0f} $".replace(",", " ")
        tail = {"within": ", это в рамках бюджета", "over": ", это выше бюджета", "below": ", это дешевле бюджета"}
        parts.append(f"Примерная сумма {money} на человека{tail.get(cost['budget_status'], '')}.")
        return " ".join(parts)

    @staticmethod
    def _diverse(others, dest, limit=2):
        """Запасные варианты из разных стран, чтобы выбор был настоящим."""
        picked, countries = [], {dest["country"]}
        for value, other in others:
            if other["country"] not in countries:
                countries.add(other["country"])
                picked.append((value, other))
                if len(picked) == limit:
                    break
        return picked

    def _alternative(self, value, other, dest, query, travelers):
        mine = {c["key"]: c for c in compare_with_query(other, query, self.names)}
        base = {c["key"]: c for c in compare_with_query(dest, query, self.names)}
        cost = estimate_cost(other, query, travelers)["total_usd"]
        dest_cost = estimate_cost(dest, query, travelers)["total_usd"]
        pros = [c["title"].lower() for k, c in mine.items() if c["degree"] is not None and c["degree"] > base[k]["degree"]]
        cons = [c["title"].lower() for k, c in mine.items() if c["degree"] is not None and c["degree"] < base[k]["degree"]]
        reason = []
        if pros:
            reason.append("лучше: " + ", ".join(pros))
        if cons:
            reason.append("хуже: " + ", ".join(cons))
        if cost != dest_cost:
            reason.append("дешевле" if cost < dest_cost else "дороже")
        return {
            "country": other["country"], "city": other["city"], "total_usd": cost,
            "percent": int(value * 100 + 0.5), "reason": "; ".join(reason) or "почти так же хорошо",
        }

    def _tips(self, dest, query):
        """Советы по признакам направления из базы знаний."""
        tips = []
        season = dest["season_code"]
        if season != "any":
            tips.append(f"Лучшее время для поездки: {self.names['season'][season].lower()}.")
        tips.append({
            "visa_free": "По нашей базе виза не нужна, но перед покупкой билетов проверьте правила для вашего гражданства.",
            "easy_visa": "Виза оформляется по прибытии или онлайн: проверьте правила и срок действия загранпаспорта.",
            "advance_visa": "Визу нужно оформить заранее: начните за 1–2 месяца до поездки.",
        }.get(dest["visa_code"], "Проверьте визовые правила для вашего гражданства на официальном сайте."))
        tips.append({
            "beach": "Возьмите защиту от солнца и уточните сезон дождей и волн.",
            "excursion": "Билеты в популярные места лучше купить заранее онлайн.",
            "nature": "Пригодятся удобная обувь, дождевик и карта, доступная без интернета.",
            "active": "Оформите страховку, которая покрывает активный отдых.",
            "wellness": "Процедуры и места в спа бронируйте заранее.",
        }[dest["type_code"]] if dest["type_code"] in ("beach", "excursion", "nature", "active", "wellness") else
            "Заранее составьте список того, что хотите увидеть.")
        if dest["interest_code"] == "family":
            tips.append("Место подходит для поездки с детьми: уточните в отеле детские условия.")
        elif dest["budget_code"] in ("economy", "standard"):
            tips.append("Билеты за 2–3 месяца до поездки обычно дешевле.")
        return tips
