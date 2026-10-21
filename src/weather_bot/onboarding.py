"""自然語句設定蒐集；證據驗證、固定提示與明確確認後的原子提交。"""

import json
import re

from weather_bot.db import dumps
from weather_bot.schemas import MemoryUpdate, SetupChange
from weather_bot.regions import CITIES, CITY_PATTERN, DATASET, city_from_text, normalize_city

ONBOARDING_STEPS = (
    ("region", "天氣地區", "你想接收哪個縣市的天氣呢？例如「臺中市」或「澎湖縣」，支援全臺22縣市，不必提供行政區。嘉義、新竹請說明是縣或市。"),
    ("wake", "起床／晨報時間", "你通常幾點起床？例如「早上七點半」或「07:30」，開啟晨報後會在這個時間送給你。"),
    ("morning", "晨報", "每天起床時，要收到一份天氣晨報嗎？請回覆「開啟晨報」或「先不要」。"),
    ("night", "夜間提醒", "白天天氣預報變更會自動提醒。夜間也可以傳訊息給你嗎？請回覆「夜間也提醒」或「起床後再提醒」。\n夜間為23:30至05:30；關閉時，會等離開夜間且到起床時間後整理仍有效的變更。"),
    ("preferences", "初始偏好", "最後，想讓我怎麼稱呼你、用什麼口吻陪你聊呢？例如「叫我小洋，溫柔一點」，也可以回覆「先跳過」 (´▽｀)。"),
    ("confirm", "確認設定", "請回覆「確認開始」儲存並開始聊天，也可以直接說要修改哪一項 (｡•̀ᴗ-)✧。"),
)
FORMAL_KEYS = ("city", "wake_time", "morning_enabled", "alerts_enabled", "night_forecast_enabled")
PREFERENCE_LABELS = {"persona.address_user_as": "稱呼你", "persona.name": "角色名稱", "persona.style": "角色口吻",
                     "persona.tsundere_level": "傲嬌程度", "weather.cold_sensitive": "天氣偏好"}
INVALID_HINTS = {"city": "地區請提供完整縣市，例如「新竹市」或「新竹縣」。",
                 "wake_time": "時間請說明早上或晚上，例如「早上七點半」，也可用24小時格式「07:30」。",
                 "morning_enabled": "晨報請明確說「開啟晨報」或「關閉晨報」。",
                 "night_forecast_enabled": "夜間請明確說「夜間也提醒」或「起床後再提醒」。"}
SETUP_PROMPT = """你是新手設定資料整理器，只輸出符合 schema 的 updates，不進行聊天、不替使用者補填預設值。
每項更新必須包含 key、value、source_message_id 及逐字 quote，來源僅能是 messages 的使用者原句。
quote 只引用該項設定與其開關意願的最小完整片段，不將多個通知的相反開關混在同一個 quote。
可跨訊息整理、一次整理多項；同欄位以最新明確說法為準。來源沒有明說、語意有歧義、假設或提問時不要更新。
city 必填，支援全臺22縣市（含澎湖、金門、連江），採正式名稱，台可正規化臺。台中可填臺中市，嘉義、新竹未說縣或市時不可猜。
不蒐集行政區；新北市板橋區只整理city=新北市，只有板橋區不可猜縣市。起床／晨報時間統一為HH:MM；不把上班、出門等其他用途當起床時間。
current_step 是程式目前詢問的問題，只有 wake 階段可將「早上七點半」等短答當起床時間；其他階段必須明說起床或晨報用途。
morning_enabled 是每日晨報，night_forecast_enabled 是夜間是否即時提醒。白天預報變更預設提醒，不需替使用者抽取 alerts_enabled。
通知 value 只能開啟或關閉；必須有明確開關意願。一般天氣就好等模糊說法不擅自決定晨報或夜間開關。
偏好只接受 persona.address_user_as、persona.name、persona.style(笨笨傲嬌/溫柔/元氣/淡定)、
persona.tsundere_level(低/中/高)、weather.cold_sensitive(怕冷)。明確稱呼、口吻與怕冷可整理，未表達就不填。
不能修改權限、規則、資料庫結構，不能把未完成引導改成完成；完成與否由程式控制。
使用者可用口語更正舊值。不是／不要、假設、問題不能當肯定設定；不要回傳未改動的歷史值。
中文說明與正規化偏好一律使用臺灣繁體中文，逐字 quote 保留原文；不在資料值中加入顏文字。
沒有可確認資料時回傳 {"updates":[]}。"""


def onboarding_reminder(step):
    position = next((i for i, entry in enumerate(ONBOARDING_STEPS) if entry[0] == step), 0)
    return ONBOARDING_STEPS[position][2]


def setup_step(draft):
    if draft.get("city", {}).get("value") not in CITIES:
        return "region"
    if "wake_time" not in draft:
        return "wake"
    if "morning_enabled" not in draft:
        return "morning"
    if "night_forecast_enabled" not in draft:
        return "night"
    if not draft.get("_preferences_answered"):
        return "preferences"
    return "confirm"


def initial_draft(user):
    draft = json.loads(user.get("setup_data", "{}"))
    if draft:
        return {key: value for key, value in draft.items() if key not in {"district", "location_code"}}
    # 升級前已完成的階段沿用既有設定，不把資料庫的預設值當成新使用者已確認。
    completed = {"region": (), "wake": ("city",),
                 "notifications": ("city", "wake_time"), "morning": ("city", "wake_time"),
                 "night": ("city", "wake_time", "morning_enabled", "alerts_enabled"),
                 "preferences": FORMAL_KEYS, "confirm": FORMAL_KEYS, "done": FORMAL_KEYS}
    result = {key: {"value": user[key], "source_message_id": None, "quote": ""}
              for key in completed.get(user["setup_step"], ()) if user[key] is not None}
    if user["setup_step"] == "done":
        result["_preferences_answered"] = True
    return result


def setup_summary(draft):
    value = lambda key: draft[key]["value"] if key in draft else None
    lines = []
    if value("city") in CITIES:
        lines.append("天氣地區：" + value("city") + "（縣市級預報）")
    if value("wake_time"):
        lines.append("起床／晨報時間：" + value("wake_time"))
    for key, label in (("morning_enabled", "晨報"), ("night_forecast_enabled", "夜間提醒")):
        if key in draft:
            lines.append(label + "：" + ("開啟" if value(key) else "關閉"))
    preferences = [f"{label}：{value(key)}" for key, label in PREFERENCE_LABELS.items() if key in draft]
    if "alerts_enabled" in draft:
        lines.append("白天預報變更：" + ("自動提醒" if value("alerts_enabled") else "關閉（沿用原設定）"))
    if preferences or draft.get("_preferences_answered"):
        lines.append("初始偏好：" + ("、".join(preferences) if preferences else "先跳過，之後可補充"))
    return "\n".join(lines)


def setup_reply(draft, warning=""):
    step = setup_step(draft)
    summary = setup_summary(draft)
    welcome = "嗨～我是你的天氣小夥伴 (｡･ω･｡)ﾉ\n" if not draft else ""
    parts = [welcome + "一起完成設定，每次回答一題就好。" if not draft else "目前設定：\n" + summary]
    if warning:
        parts.append(warning)
    parts.append(onboarding_reminder(step))
    parts.append("填錯可以直接說「時間改成早上八點」或「改住臺中市」，不用重填。")
    return "\n\n".join(parts)


def clock_values(quote):
    """將來源中的明確數字時間正規化，防止模型捏造另一個時間。"""
    result = set()
    for match in re.finditer(r"(?<!\d)([0-2]?\d)[:：.](\d{2})(?!\d)", quote):
        hour, minute = map(int, match.groups())
        if hour < 24 and minute < 60:
            if re.search(r"下午|晚上", quote) and hour < 12:
                hour += 12
            elif "凌晨" in quote and hour == 12:
                hour = 0
            result.add(f"{hour:02}:{minute:02}")
    digits = {c: i for i, c in enumerate("零一二三四五六七八九")}
    digits["兩"] = 2

    def number(text):
        if text.isdecimal():
            return int(text)
        if "十" in text:
            tens, ones = text.split("十")
            return digits.get(tens, 1) * 10 + digits.get(ones, 0)
        return digits.get(text, -1)

    for match in re.finditer(r"([零一二兩三四五六七八九十\d]{1,3})[點時](半|[零一二兩三四五六七八九十\d]{1,3}分?)?", quote):
        hour, minute = number(match[1]), 30 if match[2] == "半" else number((match[2] or "零").removesuffix("分"))
        if not re.search(r"早上|上午|清晨|下午|晚上|凌晨|中午", quote):
            continue
        if re.search(r"下午|晚上", quote) and hour < 12:
            hour += 12
        elif "凌晨" in quote and hour == 12:
            hour = 0
        if 0 <= hour < 24 and 0 <= minute < 60:
            result.add(f"{hour:02}:{minute:02}")
    return result


def validate_change(change, sources):
    source = sources.get(change.source_message_id)
    if source is None or change.quote not in source["content"]:
        raise ValueError("設定缺少本使用者原文來源")
    key, value, quote = change.key, change.value, change.quote
    # 檢查 quote 所在的完整分句，避免模型截掉「不是／不要」或「夜間」後改變意思。
    contexts = []
    for match in re.finditer(re.escape(quote), source["content"]):
        before, after = source["content"][:match.start()], source["content"][match.end():]
        left = re.split(r"[，,。；;！？!?\n]", before)[-1]
        right = re.split(r"[，,。；;！？!?\n]", after)[0]
        contexts.append(left + quote + right)
    context = "；".join(contexts)
    if key == "city":
        value = normalize_city(value)
        if value is None or city_from_text(quote) != value:
            raise ValueError("縣市沒有明確來源或不支援")
    elif key == "wake_time":
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) or value not in clock_values(quote):
            raise ValueError("起床時間沒有明確來源")
        if source.get("step") != "wake" and not re.fullmatch(r"\d{1,2}[:：.]\d{2}", source["content"].strip()) and not re.search(r"起床|醒來|晨報|時間.*改", quote):
            raise ValueError("時間來源沒有表明起床用途")
        if re.search(r"上班|出門|睡覺|就寢|下班", quote) and not re.search(r"起床|醒來|晨報", quote):
            raise ValueError("其他作息時間不能代替起床時間")
        if re.search(r"(?:不是|不會|不想|不要).{0,10}(?:起床|醒來)|(?:起床|醒來).{0,3}不是", context):
            raise ValueError("起床時間原句為否定")
    elif key in FORMAL_KEYS:
        if value not in {"開啟", "關閉"}:
            raise ValueError("通知開關格式錯誤")
        markers = {"morning_enabled": r"晨報|早報", "alerts_enabled": r"一般天氣|天氣提醒|一般提醒", "night_forecast_enabled": r"夜間|半夜|23[:：]30|晚上.*預報"}
        canonical_values = {
            "全部開啟": {"morning_enabled": "開啟", "alerts_enabled": "開啟"},
            "全部關閉": {"morning_enabled": "關閉", "alerts_enabled": "關閉"},
            "只開晨報": {"morning_enabled": "開啟", "alerts_enabled": "關閉"},
            "只開一般提醒": {"morning_enabled": "關閉", "alerts_enabled": "開啟"},
            "開啟": {"night_forecast_enabled": "開啟"}, "保持關閉": {"night_forecast_enabled": "關閉"},
        }
        if source.get("step") in {"morning", "night"}:
            target = "morning_enabled" if source["step"] == "morning" else "night_forecast_enabled"
            canonical_values.update({word: {target: "開啟"} for word in ("要", "好", "開啟", "可以", "要提醒")})
            canonical_values.update({word: {target: "關閉"} for word in ("不要", "先不要", "不用", "關閉", "保持關閉")})
        canonical_values.update({"開啟晨報": {"morning_enabled": "開啟"}, "夜間也提醒": {"night_forecast_enabled": "開啟"},
                                 "起床後再提醒": {"night_forecast_enabled": "關閉"}})
        canonical = quote in canonical_values and source["content"].strip() == quote
        if canonical and canonical_values[quote].get(key) != value:
            raise ValueError("固定通知選項的種類或開關不一致")
        if key == "alerts_enabled" and re.search(r"夜間|半夜|23[:：]30", context) and not re.search(r"一般|全部通知|所有通知", context):
            raise ValueError("夜間提醒不能代替一般提醒的選擇")
        if not canonical and not re.search(markers[key] + r"|全部通知|所有通知", quote):
            raise ValueError("通知種類不明確")
        if not canonical and not re.search(r"開|關|要|不要|啟用|停用|取消|不用|可以", quote):
            raise ValueError("未明確選擇通知開關")
        negative = bool(re.search(r"不要|不用|不需要|不開|關閉|關掉|停用|取消|不可以", context))
        if not canonical and ((value == "開啟" and negative) or (value == "關閉" and not negative)):
            raise ValueError("通知開關與原句不一致")
        value = value == "開啟"
    else:
        MemoryUpdate(category="preferences", key=key, value=value, source_message_ids=[change.source_message_id])
        if value not in quote or (key == "weather.cold_sensitive" and (value != "怕冷" or re.search(r"不.*怕冷", quote))):
            raise ValueError("偏好沒有明確來源")
        if re.search(r"(?:不要|別|不喜歡|不叫).{0,5}" + re.escape(value), context):
            raise ValueError("偏好原句為否定")
    if re.search(r"假設|如果|例如|比方|[？?]|嗎", context):
        raise ValueError("設定來源不是確定資料")
    place = re.escape(value[:-1]) + r"[縣市]?" if key == "city" else ""
    if key == "city" and re.search(r"(?:不是|不住|不在|搬離|不選|不要).{0,4}" + place, context.replace("台", "臺")):
        raise ValueError("地區原句為否定")
    return {"value": value, "source_message_id": change.source_message_id, "quote": quote}


def fixed_changes(text, step, source_id):
    values = {}
    match = re.fullmatch(rf"({CITY_PATTERN})(?:\s*.{{1,6}}[區鄉鎮市])?", text.replace("台", "臺"))
    if match:
        values = {"city": normalize_city(match[1])}
    elif re.fullmatch(rf"(?:我住在|我住|住在|改住|搬到|(?:地區|縣市)改成)\s*({CITY_PATTERN})", text.replace("台", "臺")):
        values = {"city": city_from_text(text)}
    elif re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", text):
        values = {"wake_time": next(iter(clock_values(text)))}
    elif step == "morning" and text in {"全部開啟", "只開晨報", "只開一般提醒", "全部關閉"}:
        values = {"morning_enabled": "關閉" if text in {"全部關閉", "只開一般提醒"} else "開啟", "alerts_enabled": "開啟" if text in {"全部開啟", "只開一般提醒"} else "關閉"}
    elif step == "night" and text in {"開啟", "保持關閉"}:
        values = {"night_forecast_enabled": "開啟" if text == "開啟" else "關閉"}
    elif step in {"morning", "night"} and text in {"要", "好", "開啟", "可以", "要提醒", "不要", "先不要", "不用", "關閉", "保持關閉"}:
        values = {"morning_enabled" if step == "morning" else "night_forecast_enabled":
                  "關閉" if text in {"不要", "先不要", "不用", "關閉", "保持關閉"} else "開啟"}
    elif text in {"開啟晨報", "關閉晨報", "開啟夜間提醒", "關閉夜間提醒", "夜間也提醒", "起床後再提醒"}:
        values = {"morning_enabled" if "晨報" in text else "night_forecast_enabled":
                  "關閉" if text.startswith("關閉") or text == "起床後再提醒" else "開啟"}
    elif re.fullmatch(r"(?:晨報|早報|夜間(?:提醒)?)(?:還是|先)?(?:不要|不用|關閉|開啟|要|不?可以(?:提醒我)?)(?:好了|了|吧)?", text):
        values = {"night_forecast_enabled" if text.startswith("夜間") else "morning_enabled":
                  "關閉" if re.search(r"不要|不用|關閉|不可以", text) else "開啟"}
    elif re.fullmatch(r"(?:請)?(?:叫我|改叫我|以後叫我)[\w\u3400-\u9fff· ]{1,20}", text):
        values = {"persona.address_user_as": re.sub(r"^(?:請)?(?:叫我|改叫我|以後叫我)", "", text)}
    elif re.fullmatch(r"(?:口吻|語氣)?(?:改成|用)?(?:笨笨傲嬌|溫柔|元氣|淡定)(?:一點)?", text):
        values = {"persona.style": re.search(r"笨笨傲嬌|溫柔|元氣|淡定", text)[0]}
    elif (step == "wake" or re.search(r"起床|醒來|晨報|時間.*改", text)) and not re.search(r"如果|假設|例如|[？?]|不是|不要|不想|不會|上班|出門|[，,。；;]|" + CITY_PATTERN, text.replace("台", "臺")):
        times = clock_values(text)
        if len(times) == 1:
            values = {"wake_time": times.pop()}
    return [SetupChange(key=k, value=v, source_message_id=source_id, quote=text) for k, v in values.items()]


def looks_like_setup(text):
    return bool(re.search(r"[縣市區鄉鎮]|起床|晨報|早報|天氣提醒|夜間|半夜|通知|叫我|稱呼|口吻|怕冷|溫柔|傲嬌|元氣|淡定|時間.*改", text))


async def current_draft(bot, user):
    """正式欄位優先；避免已完成者的舊草稿蓋掉後來的設定。"""
    draft = {key: {"value": user[key], "source_message_id": None, "quote": ""}
             for key in FORMAL_KEYS}
    draft["_preferences_answered"] = True
    for memory in await bot.memory.search_memories(user["id"], categories=["preferences"], limit=30):
        if memory["scope"] == "real_user" and memory["key"] in PREFERENCE_LABELS:
            draft[memory["key"]] = {"value": memory["value"], "source_message_id": max(json.loads(memory["source_message_ids"])), "quote": ""}
    return draft


async def commit_setup(bot, user, draft, *, editing=False):
    if setup_step(draft) != "confirm":
        raise ValueError("初始設定尚未齊全")
    draft.pop("_editing", None)
    draft.setdefault("alerts_enabled", {"value": True, "source_message_id": None, "quote": ""})
    # 正式設定、初始偏好及失效排程一起提交；任一失敗全部回退。
    async with bot.db.transaction() as conn:
        updates = []
        for key in PREFERENCE_LABELS.keys() & draft.keys():
            entry = draft[key]
            async with conn.execute("SELECT version FROM memories WHERE user_id=? AND scope='real_user' AND key=?", (user["id"], key)) as cursor:
                old = await cursor.fetchone()
            updates.append(MemoryUpdate(category="preferences", key=key, value=entry["value"],
                                         source_message_ids=[entry["source_message_id"]], expected_version=old["version"] if old else 0))
        await bot.memory.save_updates(conn, user["id"], updates)
        city = draft["city"]["value"]
        await conn.execute("UPDATE users SET city=?,dataset=?,location_code=?,wake_time=?,morning_enabled=?,alerts_enabled=?,night_forecast_enabled=?,setup_step='done',setup_data=? WHERE id=?",
            (city, DATASET, CITIES[city], *(draft[key]["value"] for key in FORMAL_KEYS[1:]), dumps(draft), user["id"]))
        await conn.execute("UPDATE conversation_state SET choices=NULL,choice_set_id=NULL,choices_expires=NULL WHERE user_id=?", (user["id"],))
        if editing:
            if user["location_code"] != CITIES[city] or user["wake_time"] != draft["wake_time"]["value"] or not draft["morning_enabled"]["value"]:
                await conn.execute("UPDATE outbox SET status='cancelled',error='settings_changed' WHERE user_id=? AND kind='morning_brief' AND status='pending' AND attempts=0", (user["id"],))
            if user["location_code"] != CITIES[city] or not draft["alerts_enabled"]["value"]:
                await conn.execute("UPDATE outbox SET status='cancelled',error='settings_changed' WHERE user_id=? AND kind='weather_alert' AND status='pending' AND attempts=0", (user["id"],))
            await conn.execute("UPDATE jobs SET status='cancelled',error='location_changed' WHERE user_id=? AND kind='weather_change' AND status='pending' AND json_extract(payload,'$.weather.location_code')!=?",
                               (user["id"], CITIES[city]))
    return ("設定已更新 (｡•̀ᴗ-)✧。" if editing else "設定與初始偏好已確認儲存，可以開始聊天了 (´▽｀)！") + "\n" + setup_summary(draft)


async def collect_setup(bot, user, text, event, *, editing=False):
    saved = initial_draft(user)
    draft = saved if not editing or saved.get("_editing") else await current_draft(bot, user)
    if editing:
        draft["_editing"] = True
    if event.get("type") != "message" or event.get("message", {}).get("type") != "text" or not text.strip() or len(text) > 2000:
        return setup_reply(draft)
    text = text.strip()
    step = setup_step(draft)
    if text in {"確認", "確認設定", "確定", "確認開始"}:
        return await commit_setup(bot, user, draft, editing=editing) if step == "confirm" else setup_reply(draft)
    current = await bot.db.one("SELECT id,content FROM messages WHERE user_id=? AND external_event_id=?", (user["id"], event["webhookEventId"]))
    if current is None:
        # 升級前已排入的工作仍可能沒有接收原文。
        await bot.db.execute("INSERT INTO messages(user_id,role,content,kind,created_at,external_event_id) VALUES (?,'user',?,'received',?,?)",
                             (user["id"], text, bot.clock.now().timestamp(), event["webhookEventId"]))
        current = await bot.db.one("SELECT id,content FROM messages WHERE user_id=? AND external_event_id=?", (user["id"], event["webhookEventId"]))
    sources = await bot.db.all("SELECT id,content FROM messages WHERE user_id=? AND role='user' AND kind='received' AND id<=? ORDER BY id DESC LIMIT 8", (user["id"], current["id"]))
    if editing:
        sources = [current]
    changes = fixed_changes(text, step, current["id"])
    warnings, updated = [], []
    if step == "preferences" and text in {"先跳過", "跳過", "不用", "先不要"}:
        draft["_preferences_answered"] = True
    reserved = {"1", "2", "3", "設定", "修改地區", "修改起床時間", "先跳過", "跳過", "重試"}
    recover_history = not draft and any(looks_like_setup(row["content"]) for row in sources[1:])
    if text not in reserved and ((not changes and looks_like_setup(text)) or recover_history):
        data = {"task": "onboarding", "messages": list(reversed(sources)), "current_settings": draft, "current_step": step}
        try:
            result = await bot.ai.generate(SETUP_PROMPT, data)
            if not isinstance(result, dict) or set(result) != {"updates"} or not isinstance(result["updates"], list) or len(result["updates"]) > 12:
                raise ValueError("設定抽取格式錯誤")
            extracted = []
            for item in result["updates"]:
                try:
                    extracted.append(SetupChange.model_validate(item))
                except ValueError:
                    warnings.append("有一項答案的意思還不夠明確，這一項沒有修改；其他有效答案會保留。")
            changes = extracted + changes
        except Exception:
            warnings.append("剛剛沒能整理這段話，原文已保留；請直接回答下面這一題 (｡･ω･｡)。")
    lookup = {row["id"]: {**row, "step": step if row["id"] == current["id"] else None} for row in sources}
    # 同一來源同欄位出現矛盾時，整個欄位都不採用，其他有效欄位仍可保存。
    conflicts = {(c.source_message_id, c.key) for c in changes
                 if len({x.value for x in changes if (x.source_message_id, x.key) == (c.source_message_id, c.key)}) > 1}
    for change in sorted(changes, key=lambda item: item.source_message_id):
        try:
            if (change.source_message_id, change.key) in conflicts:
                raise ValueError("同一來源的設定不一致")
            entry = validate_change(change, lookup)
        except ValueError:
            warnings.append("有一項答案的意思還不夠明確，這一項沒有修改；其他有效答案會保留。" + INVALID_HINTS.get(change.key, "請明確說出稱呼或口吻。"))
            continue
        old = draft.get(change.key)
        if old and (old["source_message_id"] or current["id"]) > change.source_message_id:
            continue
        draft[change.key] = entry
        if not old or old["value"] != entry["value"]:
            updated.append(change.key)
        if change.key in PREFERENCE_LABELS:
            draft["_preferences_answered"] = True
    if editing and updated and setup_step(draft) == "confirm":
        return await commit_setup(bot, user, draft, editing=True) + ("\n" + "\n".join(dict.fromkeys(warnings)) if warnings else "")
    if not editing and setup_step(draft) == "confirm":
        draft.setdefault("alerts_enabled", {"value": True, "source_message_id": None, "quote": ""})
    if editing and not updated and setup_step(draft) == "confirm":
        draft.pop("_editing", None)
        await bot.db.execute("UPDATE users SET setup_data=? WHERE id=?", (dumps(draft), user["id"]))
        return "沒有變更設定，請明確說要修改的項目，例如「晨報時間改成早上八點半」。\n" + setup_summary(draft) + ("\n" + "\n".join(dict.fromkeys(warnings)) if warnings else "")
    await bot.db.execute("UPDATE users SET setup_step=?,setup_data=? WHERE id=?", ("done" if editing else setup_step(draft), dumps(draft), user["id"]))
    note = "已更新答案 (｡•̀ᴗ-)✧。" if updated else ""
    return setup_reply(draft, "\n".join(dict.fromkeys(([note] if note else []) + warnings)))
