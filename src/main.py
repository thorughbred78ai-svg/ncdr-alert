"""NCDR 即時示警 -> Telegram（桃園市、今日生效、不重複推播）。"""

import json
import sys
from datetime import datetime, timezone

from ncdr import TAIWAN_TZ, get_alerts, load_config
from state import (
    SYSTEM_ERROR_KEY,
    calculate_hash,
    cleanup_state,
    load_state,
    save_state,
)
from telegram import redact, send_message


# ------------------------------------------------------------
# Message
# ------------------------------------------------------------

def _clip(text: str, limit: int) -> str:
    text = text or ""

    if len(text) <= limit:
        return text

    return text[:limit].rstrip() + "…（內容過長已截斷，請見 NCDR 原文）"


def _fmt_time(iso_value: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_value).astimezone(TAIWAN_TZ)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso_value or "未提供"


def format_alert(alert: dict, updated: bool) -> str:
    title = "🔄 NCDR 示警更新" if updated else "🚨 NCDR 示警"
    areas = "、".join(alert.get("matched_areas") or []) or "未提供"

    lines = [
        title,
        "",
        "━━━━━━━━━━━━━━",
        f"⚠️ {_clip(alert.get('headline') or alert.get('event') or '災害示警', 200)}",
        "━━━━━━━━━━━━━━",
        "",
        f"📍 影響地區\n{areas}",
        "",
        f"🕐 生效時間\n{_fmt_time(alert.get('effective'))}",
        "",
        f"⛔ 失效時間\n{_fmt_time(alert.get('expires')) if alert.get('expires') else '未提供'}",
        "",
        f"📋 示警內容\n{_clip(alert.get('description') or '未提供', 1500)}",
    ]

    if alert.get("raw_area"):
        lines += ["", f"🗺️ 區域說明\n{_clip(alert['raw_area'], 500)}"]

    if alert.get("instruction"):
        lines += ["", f"🛡️ 建議措施\n{_clip(alert['instruction'], 800)}"]

    if alert.get("sender"):
        lines += ["", f"發布單位：{_clip(alert['sender'], 100)}"]

    lines += ["", "━━━━━━━━━━━━━━", "資料來源：NCDR 民生示警公開資料平台"]

    return "\n".join(lines)


def covers_wanted_area(text: str, wanted: list) -> bool:
    """推播前最後閘門：訊息必須明確含關注縣市。"""

    normalized = (text or "").replace("台", "臺")
    return any(w in normalized for w in wanted)


# ------------------------------------------------------------
# Process
# ------------------------------------------------------------

def process_alert(alert: dict, state: dict, now: datetime, wanted: list) -> str:
    alert_id = str(alert.get("id") or "").strip()

    if not alert_id:
        return "invalid"

    content_hash = calculate_hash(alert)
    previous = state.get(alert_id)

    if previous is not None and previous.get("hash") == content_hash:
        print(f"SKIP SAME: {alert_id}")
        return "same"

    updated = previous is not None
    message = format_alert(alert, updated=updated)

    if not covers_wanted_area(message, wanted):
        print(f"SEND BLOCKED (no wanted area in message): {alert_id}")
        return "blocked"

    print(f"{'UPDATE' if updated else 'NEW'} ALERT: {alert_id}")

    message_id = send_message(message)

    # 只有 Telegram 成功後才寫入 state；失敗者下次排程會重試
    state[alert_id] = {
        "hash": content_hash,
        "sent_at": now.isoformat(),
        "telegram_message_id": message_id,
    }

    return "update" if updated else "new"


def notify_system_error(error: Exception, state: dict, cooldown_min: int, now):
    last = (state.get(SYSTEM_ERROR_KEY) or {}).get("sent_at")

    try:
        prev = datetime.fromisoformat(last) if last else None
    except ValueError:
        prev = None

    if prev is not None and (now - prev).total_seconds() < cooldown_min * 60:
        print("SKIP SYSTEM ERROR NOTIFICATION: cooldown")
        return

    text = (
        "⚠️ NCDR Bot 系統異常\n\n"
        "目前無法取得 NCDR 資料。\n\n"
        f"時間：{now.astimezone(TAIWAN_TZ).strftime('%Y-%m-%d %H:%M')}\n"
        f"錯誤：{redact(error)[:300]}\n\n"
        "請檢查 GitHub Actions。"
    )

    try:
        send_message(text)
        state[SYSTEM_ERROR_KEY] = {"sent_at": now.isoformat()}
    except Exception as tg_error:
        print(f"SYSTEM ERROR NOTIFICATION FAILED: {redact(tg_error)}")


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

def main() -> int:
    print("=" * 40)
    print("NCDR Telegram Alert Bot")
    print("=" * 40)

    config = load_config()
    wanted = [a.replace("台", "臺") for a in config.get("areas", [])]
    now = datetime.now(timezone.utc)

    try:
        state = load_state()
    except RuntimeError as error:
        # 狀態檔異常時停止，避免把今日所有示警重新推播
        print(f"STATE ERROR: {error}")
        return 1

    state = cleanup_state_keep_system(state, config.get("state_retention_days", 7))

    try:
        alerts = get_alerts()
    except Exception as error:
        print(f"NCDR API ERROR: {redact(error)}")
        notify_system_error(
            error, state,
            int(config.get("error_notification_cooldown_minutes", 60)), now,
        )
        save_state(state)
        return 1

    state.pop(SYSTEM_ERROR_KEY, None)

    counts = {"new": 0, "update": 0, "same": 0, "blocked": 0, "invalid": 0, "error": 0}

    for alert in alerts:
        try:
            result = process_alert(alert, state, now, wanted)
        except Exception as error:
            print(f"ALERT PROCESS ERROR: {alert.get('id')} | {redact(error)[:300]}")
            result = "error"

        counts[result] += 1

    save_state(state)

    print("=" * 40)
    print(f"NCDR alerts (today, {'/'.join(wanted)}) = {len(alerts)}")
    for k, v in counts.items():
        print(f"{k.upper():<8} = {v}")
    print("=" * 40)

    return 1 if counts["error"] else 0


def cleanup_state_keep_system(state: dict, days: int) -> dict:
    system = state.get(SYSTEM_ERROR_KEY)
    cleaned = cleanup_state(state, days)

    if system is not None and SYSTEM_ERROR_KEY not in cleaned:
        cleaned[SYSTEM_ERROR_KEY] = system

    return cleaned


if __name__ == "__main__":
    sys.exit(main())
