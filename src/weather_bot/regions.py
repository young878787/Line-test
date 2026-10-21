"""縣市級預報的正式名稱、氣象署代碼與無歧義別名。"""

import re

DATASET = "F-D0047-089"
CITIES = {
    "臺北市": "63000000", "新北市": "65000000", "桃園市": "68000000",
    "臺中市": "66000000", "臺南市": "67000000", "高雄市": "64000000",
    "基隆市": "10017000", "新竹市": "10018000", "嘉義市": "10020000",
    "新竹縣": "10004000", "苗栗縣": "10005000", "彰化縣": "10007000",
    "南投縣": "10008000", "雲林縣": "10009000", "嘉義縣": "10010000",
    "屏東縣": "10013000", "宜蘭縣": "10002000", "花蓮縣": "10015000",
    "臺東縣": "10014000", "澎湖縣": "10016000", "金門縣": "09020000",
    "連江縣": "09007000",
}
ALIASES = {city: city for city in CITIES}
ALIASES.update({city[:-1]: city for city in CITIES if city[:-1] not in {"新竹", "嘉義"}})
CITY_PATTERN = "(?:" + "|".join(sorted(ALIASES, key=len, reverse=True)) + ")(?![縣市])"


def normalize_city(value):
    return ALIASES.get(value.strip().replace("台", "臺")) if isinstance(value, str) else None


def city_from_text(text):
    text = text.replace("台", "臺")
    if re.search(r"(?:新竹|嘉義)(?![縣市])", text):
        raise ValueError("請提供完整縣市名稱，例如新竹市／新竹縣、嘉義市／嘉義縣。")
    cities = {ALIASES[match[0]] for match in re.finditer(CITY_PATTERN, text)}
    if len(cities) > 1:
        raise ValueError("請提供一個明確的縣市名稱。")
    return next(iter(cities), None)
