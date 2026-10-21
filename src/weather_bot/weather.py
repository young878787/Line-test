"""氣象署鄉鎮預報：按有效時間對齊，不以陣列索引配對。"""
from datetime import datetime, timedelta
import hashlib
import json
from weather_bot.config import TZ
from weather_bot.db import dumps

CWA_BASE_URL = "https://opendata.cwa.gov.tw/api/v1/rest/datastore"
DATASETS = {"臺北市": "F-D0047-061", "新北市": "F-D0047-069"}
DRY_CODES = set(range(1, 8))
RAIN_CODES = set(range(8, 42)) - {24, 25, 26, 27, 28}


def parse_time(value):
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)


def number(value, *, minimum=-80, maximum=80):
    try:
        result = float(value)
        return result if minimum <= result <= maximum else None
    except (ValueError, TypeError):
        return None


def normalize(raw, district, dataset, now):
    records = raw.get("records", {})
    groups = records.get("Locations", records.get("locations", []))
    if isinstance(groups, dict):
        groups = [groups]
    if not groups:
        raise ValueError("缺少鄉鎮預報資料")
    group = groups[0]
    locations = group.get("Location", group.get("location", []))
    location = next((loc for loc in locations if loc.get("LocationName", loc.get("locationName")) == district), None)
    if location is None:
        raise ValueError("找不到行政區")
    code = str(location.get("Geocode", location.get("geocode", "")))
    if not code:
        raise ValueError("缺少行政區代碼")
    elements = {}
    for item in location.get("WeatherElement", location.get("weatherElement", [])):
        elements[item.get("ElementName", item.get("elementName"))] = item.get("Time", item.get("time", []))

    def times(*names):
        return next((elements[n] for n in names if n in elements), [])

    def val(row, *keys):
        for value in row.get("ElementValue", row.get("elementValue", [])):
            for key in keys:
                if key in value:
                    return value[key]
        return None

    def at(rows, point, *keys):
        candidates = []
        for row in rows:
            start = parse_time(row.get("StartTime", row.get("startTime", row.get("DataTime", row.get("dataTime", "")))))
            end_raw = row.get("EndTime", row.get("endTime"))
            end = parse_time(end_raw) if end_raw else start + timedelta(hours=1)
            if start <= point < end:
                candidates.append((start, val(row, *keys)))
        return max(candidates, key=lambda item: item[0])[1] if candidates else None

    periods = []
    for row in times("天氣現象", "Wx"):
        start = parse_time(row.get("StartTime", row.get("startTime", "")))
        end = parse_time(row.get("EndTime", row.get("endTime", "")))
        if end <= start:
            raise ValueError("預報有效時間錯誤")
        code_value = val(row, "WeatherCode")
        if code_value is None:
            code_value = next((v.get("value") for v in row.get("elementValue", []) if v.get("measures") == "自訂 Wx 代碼"), None)
        wx = number(code_value, minimum=1, maximum=99)
        wx = int(wx) if wx is not None else None
        periods.append({
            "valid_from": start.isoformat(), "valid_to": end.isoformat(),
            "weather_code": wx, "weather": val(row, "Weather", "value") or "天氣資料不足",
            "rain_state": "dry" if wx in DRY_CODES else "rain" if wx in RAIN_CODES else "unknown",
            "pop_3h": number(at(times("3小時降雨機率", "PoP3h"), start, "ProbabilityOfPrecipitation", "value"), minimum=0, maximum=100),
            "temperature": number(at(times("溫度", "T"), start, "Temperature", "value")),
            "apparent_temperature": number(at(times("體感溫度", "AT"), start, "ApparentTemperature", "value")),
        })
    periods.sort(key=lambda p: p["valid_from"])
    if not periods or len({(p["valid_from"], p["valid_to"]) for p in periods}) != len(periods):
        raise ValueError("預報時段缺漏或重複")
    if not any(parse_time(p["valid_to"]) > now for p in periods):
        raise ValueError("預報已過期")
    if any(p["weather_code"] is None for p in periods):
        raise ValueError("天氣現象代碼缺漏")
    canonical = {"location_code": code, "district": district, "dataset": dataset, "periods": periods}
    canonical["forecast_hash"] = hashlib.sha256(dumps(canonical).encode()).hexdigest()
    issued = records.get("IssueTime") or group.get("IssueTime")
    canonical["source_issued_at"] = parse_time(issued).isoformat() if issued else None
    canonical["fetched_at"] = now.timestamp()
    return canonical


def rain_candidates(old, new, now):
    previous = {(p["valid_from"], p["valid_to"]): p for p in old["periods"]}
    result = []
    for p in new["periods"]:
        if parse_time(p["valid_to"]) <= now or parse_time(p["valid_from"]) > now + timedelta(hours=6):
            continue
        before = previous.get((p["valid_from"], p["valid_to"]))
        if not before:
            continue
        changed = before["rain_state"] == "dry" and p["rain_state"] == "rain"
        jumped = before["pop_3h"] is not None and p["pop_3h"] is not None and before["pop_3h"] <= 30 and p["pop_3h"] >= 60
        if changed or jumped:
            result.append(p)
    return result


def merge_rain(periods):
    result = []
    for p in sorted(periods, key=lambda p: p["valid_from"]):
        if result and p["valid_from"] <= result[-1]["valid_to"]:
            result[-1]["valid_to"] = max(result[-1]["valid_to"], p["valid_to"])
        else:
            result.append({"valid_from": p["valid_from"], "valid_to": p["valid_to"]})
    return result


def is_night(now):
    minutes = now.hour * 60 + now.minute
    return minutes >= 23 * 60 + 30 or minutes < 5 * 60 + 30


class WeatherService:
    def __init__(self, db, http, key, clock):
        self.db, self.http, self.key, self.clock = db, http, key, clock

    async def fetch(self, dataset, district):
        if not self.key:
            raise ValueError("CWA_API_KEY 未設定")
        response = await self.http.get(f"{CWA_BASE_URL}/{dataset}",
            params={"Authorization": self.key, "format": "JSON", "LocationName": district}, timeout=10)
        response.raise_for_status()
        return normalize(response.json(), district, dataset, self.clock.now())

    async def latest(self, location_code):
        row = await self.db.one("SELECT data FROM weather_snapshots WHERE location_code=? ORDER BY fetched_at DESC,id DESC LIMIT 1", (location_code,))
        return json.loads(row["data"]) if row else None

    async def refresh(self, dataset, district):
        new = await self.fetch(dataset, district)
        old = await self.latest(new["location_code"])
        if old and old["forecast_hash"] == new["forecast_hash"]:
            return old, []
        await self.db.execute("INSERT INTO weather_snapshots(location_code,source_issued_at,forecast_hash,fetched_at,data) VALUES (?,?,?,?,?) ON CONFLICT(location_code,forecast_hash) DO UPDATE SET source_issued_at=excluded.source_issued_at,fetched_at=excluded.fetched_at,data=excluded.data",
            (new["location_code"], new["source_issued_at"], new["forecast_hash"], new["fetched_at"], dumps(new)))
        return new, rain_candidates(old, new, self.clock.now()) if old else []

    async def get_weather(self, user, time_range=None):
        data = await self.latest(user["location_code"])
        now = self.clock.now()
        if data is None or not any(parse_time(p["valid_to"]) > now for p in data["periods"]):
            data, _ = await self.refresh(user["dataset"], user["district"])
        result = dict(data)
        start, end = time_range or (now, now + timedelta(hours=24))
        result["periods"] = [p for p in data["periods"] if parse_time(p["valid_to"]) > start and parse_time(p["valid_from"]) < end]
        return result
