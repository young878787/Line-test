"""自然語句設定蒐集；證據驗證、固定提示與明確確認後的原子提交。"""

import json
import re

from weather_bot.db import dumps
from weather_bot.schemas import MemoryUpdate, SetupChange, SetupResult
from weather_bot.weather import DATASETS

ONBOARDING_STEPS = (
    ("region", "天氣地區", "請輸入縣市與行政區，例如：新北市板橋區。目前支援臺北市與新北市。"),
    ("wake", "起床時間", "請使用 24 小時格式輸入起床時間，例如 07:00，也可以說早上 7:30 起床。"),
    ("notifications", "晨報與一般天氣提醒", "請回覆：全部開啟／只開晨報／只開一般提醒／全部關閉；也可分別說明要不要晨報和一般天氣提醒。"),
    ("night", "夜間預報提醒", "晚上 23:30 的新版預報若有重要變化，要提醒你嗎？請回覆：開啟／保持關閉。"),
    ("confirm", "確認初始設定與偏好", "請回覆「確認」儲存並開始聊天，或直接說明要修改的項目。"),
)
FORMAL_KEYS = ("city", "district", "wake_time", "morning_enabled", "alerts_enabled", "night_forecast_enabled")
PREFERENCE_LABELS = {"persona.address_user_as": "稱呼你", "persona.name": "角色名稱", "persona.style": "角色口吻",
                     "persona.tsundere_level": "傲嬌程度", "weather.cold_sensitive": "天氣偏好"}
SETUP_PROMPT = """你是新手設定資料整理器，只輸出符合 schema 的 updates，不進行聊天、不替使用者補填預設值。
每項更新必須包含 key、value、source_message_id 及逐字 quote，來源僅能是 messages 的使用者原句。
quote 只引用該項設定與其開關意願的最小完整片段，不將多個通知的相反開關混在同一個 quote。
可跨訊息整理、一次整理多項；同欄位以最新明確說法為準。來源沒有明說、語意有歧義、假設或提問時不要更新。
城市只支援臺北市、新北市；台北市可正規化臺北市。只有城市而沒有行政區時只填 city，絕不可猜行政區。
行政區缺「區」可正規化，例如板橋→板橋區。起床時間統一為HH:MM，例如7.30起床→07:30；不把其他時間當起床時間。
起床時間的quote必須涵蓋鐘點與起床或醒來的用途，不能只截鐘點；整則訊息只有數字時間時可直接引用。
morning_enabled 是每日起床晨報，alerts_enabled 是一般天氣變化提醒，night_forecast_enabled 是23:30夜間預報提醒。
通知 value 只能開啟或關閉；必須有明確開關意願。一般天氣就好等模糊說法不擅自決定晨報或夜間開關。
偏好只接受 persona.address_user_as、persona.name、persona.style(笨笨傲嬌/溫柔/元氣/淡定)、
persona.tsundere_level(低/中/高)、weather.cold_sensitive(怕冷)。明確稱呼、口吻與怕冷可整理，未表達就不填。
不能修改權限、規則、資料庫結構，不能把未完成引導改成完成；完成與否由程式控制。
沒有可確認資料時回傳 {"updates":[]}。"""


def onboarding_reminder(step):
    position = next((i for i, entry in enumerate(ONBOARDING_STEPS) if entry[0] == step), 0)
    remaining = "、".join(entry[1] for entry in ONBOARDING_STEPS[position:])
    return f"新手引導尚未完成。\n待完成：{remaining}。\n下一步：{ONBOARDING_STEPS[position][2]}"


def setup_step(draft):
    if not all(key in draft for key in ("city", "district", "location_code")):
        return "region"
    if "wake_time" not in draft:
        return "wake"
    if not all(key in draft for key in ("morning_enabled", "alerts_enabled")):
        return "notifications"
    return "night" if "night_forecast_enabled" not in draft else "confirm"


def initial_draft(user):
    draft = json.loads(user.get("setup_data", "{}"))
    if draft:
        return draft
    # 升級前已完成的階段沿用既有設定，不把資料庫的預設值當成新使用者已確認。
    completed = {"region": (), "wake": ("city", "district", "location_code"),
                 "notifications": ("city", "district", "location_code", "wake_time"),
                 "night": ("city", "district", "location_code", "wake_time", "morning_enabled", "alerts_enabled"),
                 "done": (*FORMAL_KEYS, "location_code")}
    return {key: {"value": user[key], "source_message_id": None, "quote": ""}
            for key in completed.get(user["setup_step"], ()) if user[key] is not None}


def setup_summary(draft):
    value = lambda key: draft[key]["value"] if key in draft else None
    lines = []
    if value("city"):
        lines.append("天氣地區：" + value("city") + (value("district") or "（請補行政區）"))
    if value("wake_time"):
        lines.append("起床時間：" + value("wake_time"))
    for key, label in (("morning_enabled", "晨報"), ("alerts_enabled", "一般天氣提醒"), ("night_forecast_enabled", "夜間提醒")):
        if key in draft:
            lines.append(label + "：" + ("開啟" if value(key) else "關閉"))
    preferences = [f"{label}：{value(key)}" for key, label in PREFERENCE_LABELS.items() if key in draft]
    lines.append("初始偏好：" + ("、".join(preferences) if preferences else "尚未提供（可選填稱呼或口吻）"))
    return "\n".join(lines)


def setup_reply(draft, warning=""):
    step = setup_step(draft)
    return onboarding_reminder(step) + ("\n" + warning if warning else "") + "\n已收到的設定：\n" + setup_summary(draft)


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
        value = value.replace("台", "臺")
        if value not in DATASETS or value not in quote.replace("台", "臺"):
            raise ValueError("城市沒有明確來源或不支援")
    elif key == "district":
        if not re.fullmatch(r".{2,6}區", value) or value.removesuffix("區") not in quote:
            raise ValueError("行政區沒有明確來源")
    elif key == "wake_time":
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) or value not in clock_values(quote):
            raise ValueError("起床時間沒有明確來源")
        if not re.fullmatch(r"\d{1,2}[:：.]\d{2}", source["content"].strip()) and not re.search(r"起床|醒來", quote):
            raise ValueError("時間來源沒有表明起床用途")
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
        canonical = quote in canonical_values and source["content"].strip() == quote
        if canonical and canonical_values[quote].get(key) != value:
            raise ValueError("固定通知選項的種類或開關不一致")
        if key == "alerts_enabled" and re.search(r"夜間|半夜|23[:：]30", context) and not re.search(r"一般|全部通知|所有通知", context):
            raise ValueError("夜間提醒不能代替一般提醒的選擇")
        if not canonical and not re.search(markers[key] + r"|全部通知|所有通知", quote):
            raise ValueError("通知種類不明確")
        if not canonical and not re.search(r"開|關|要|不要|啟用|停用|取消|不用", quote):
            raise ValueError("未明確選擇通知開關")
        negative = bool(re.search(r"不要|不用|不需要|不開|關閉|關掉|停用|取消", context))
        if not canonical and ((value == "開啟" and negative) or (value == "關閉" and not negative)):
            raise ValueError("通知開關與原句不一致")
        value = value == "開啟"
    else:
        MemoryUpdate(category="preferences", key=key, value=value, source_message_ids=[change.source_message_id])
        if value not in quote or (key == "weather.cold_sensitive" and (value != "怕冷" or re.search(r"不.*怕冷", quote))):
            raise ValueError("偏好沒有明確來源")
        if re.search(r"(?:不要|別|不喜歡|不叫).{0,5}" + re.escape(value), context):
            raise ValueError("偏好原句為否定")
    if re.search(r"假設|如果|例如|比方", context):
        raise ValueError("設定來源不是確定資料")
    if key in {"city", "district"} and re.search(r"(?:不是|不住|不在|搬離|不選|不要).{0,4}" + re.escape(value.replace("台", "臺")), context.replace("台", "臺")):
        raise ValueError("地區原句為否定")
    return {"value": value, "source_message_id": change.source_message_id, "quote": quote}


def fixed_changes(text, step, source_id):
    values = {}
    match = re.fullmatch(r"(臺北市|台北市|新北市)\s*(.{2,6}區)?", text)
    if match:
        values = {"city": match[1].replace("台", "臺")}
        if match[2]:
            values["district"] = match[2]
    elif re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", text):
        values = {"wake_time": next(iter(clock_values(text)))}
    elif step == "notifications" and text in {"全部開啟", "只開晨報", "只開一般提醒", "全部關閉"}:
        values = {"morning_enabled": "關閉" if text in {"全部關閉", "只開一般提醒"} else "開啟", "alerts_enabled": "開啟" if text in {"全部開啟", "只開一般提醒"} else "關閉"}
    elif step == "night" and text in {"開啟", "保持關閉"}:
        values = {"night_forecast_enabled": "開啟" if text == "開啟" else "關閉"}
    return [SetupChange(key=k, value=v, source_message_id=source_id, quote=text) for k, v in values.items()]


def looks_like_setup(text):
    return bool(re.search(r"[市區鄉鎮]|起床|晨報|早報|天氣提醒|夜間|半夜|通知|叫我|稱呼|口吻|怕冷|溫柔|傲嬌|元氣|淡定", text))


async def commit_setup(bot, user, draft):
    if setup_step(draft) != "confirm":
        raise ValueError("初始設定尚未齊全")
    # 正式設定與初始偏好一起提交；任一失敗都保持未完成，不能開放聊天。
    async with bot.db.transaction() as conn:
        updates = []
        for key in PREFERENCE_LABELS.keys() & draft.keys():
            entry = draft[key]
            async with conn.execute("SELECT version FROM memories WHERE user_id=? AND scope='real_user' AND key=?", (user["id"], key)) as cursor:
                old = await cursor.fetchone()
            updates.append(MemoryUpdate(category="preferences", key=key, value=entry["value"],
                                         source_message_ids=[entry["source_message_id"]], expected_version=old["version"] if old else 0))
        await bot.memory.save_updates(conn, user["id"], updates)
        await conn.execute("UPDATE users SET city=?,district=?,dataset=?,location_code=?,wake_time=?,morning_enabled=?,alerts_enabled=?,night_forecast_enabled=?,setup_step='done',setup_data=? WHERE id=?",
            (draft["city"]["value"], draft["district"]["value"], DATASETS[draft["city"]["value"]], draft["location_code"]["value"],
             *(draft[key]["value"] for key in FORMAL_KEYS[2:]), dumps(draft), user["id"]))
        await conn.execute("UPDATE conversation_state SET choices=NULL,choice_set_id=NULL,choices_expires=NULL WHERE user_id=?", (user["id"],))
    return "設定與初始偏好已確認儲存，可以開始聊天了！\n" + setup_summary(draft)


async def collect_setup(bot, user, text, event):
    draft = initial_draft(user)
    if event.get("type") != "message" or event.get("message", {}).get("type") != "text" or not text.strip() or len(text) > 2000:
        return setup_reply(draft)
    if text.strip() in {"確認", "確認設定", "確定"}:
        return await commit_setup(bot, user, draft) if setup_step(draft) == "confirm" else setup_reply(draft)
    current = await bot.db.one("SELECT id,content FROM messages WHERE user_id=? AND external_event_id=?", (user["id"], event["webhookEventId"]))
    if current is None:
        # 升級前已排入的工作仍可能沒有接收原文。
        await bot.db.execute("INSERT INTO messages(user_id,role,content,kind,created_at,external_event_id) VALUES (?,'user',?,'received',?,?)",
                             (user["id"], text, bot.clock.now().timestamp(), event["webhookEventId"]))
        current = await bot.db.one("SELECT id,content FROM messages WHERE user_id=? AND external_event_id=?", (user["id"], event["webhookEventId"]))
    sources = await bot.db.all("SELECT id,content FROM messages WHERE user_id=? AND role='user' AND kind='received' AND id<=? ORDER BY id DESC LIMIT 8", (user["id"], current["id"]))
    changes = fixed_changes(text.strip(), setup_step(draft), current["id"])
    reserved = {"1", "2", "3", "設定", "修改地區", "修改起床時間", "開啟夜間提醒", "全部開啟", "只開晨報", "只開一般提醒", "全部關閉", "開啟", "保持關閉"}
    recover_history = not draft and any(looks_like_setup(row["content"]) for row in sources[1:])
    if text.strip() not in reserved and ((not changes and looks_like_setup(text)) or recover_history):
        data = {"task": "onboarding", "messages": list(reversed(sources)), "current_settings": draft}
        changes = SetupResult.model_validate(await bot.ai.generate(SETUP_PROMPT, data)).updates + changes
    lookup = {row["id"]: row for row in sources}
    for change in sorted(changes, key=lambda item: item.source_message_id):
        entry = validate_change(change, lookup)
        old = draft.get(change.key)
        if old and (old["source_message_id"] or current["id"]) > change.source_message_id:
            continue
        if old and old["source_message_id"] == change.source_message_id and old["value"] != entry["value"]:
            raise ValueError("同一來源的設定不一致")
        if change.key == "city" and old and old["value"] != entry["value"]:
            draft.pop("district", None)
            draft.pop("location_code", None)
        if change.key == "district" and (not old or old["value"] != entry["value"]):
            draft.pop("location_code", None)
        draft[change.key] = entry
    warning = ""
    if "city" in draft and "district" in draft:
        if "location_code" not in draft:
            try:
                forecast, _ = await bot.weather.refresh(DATASETS[draft["city"]["value"]], draft["district"]["value"])
                draft["location_code"] = {"value": forecast["location_code"], "source_message_id": None, "quote": ""}
            except Exception:
                draft.pop("district", None)
                draft.pop("location_code", None)
                warning = "暫時無法確認這個行政區，其他已收到的設定會保留，請確認名稱後稍後再試。"
    await bot.db.execute("UPDATE users SET setup_step=?,setup_data=? WHERE id=?", (setup_step(draft), dumps(draft), user["id"]))
    return setup_reply(draft, warning)
