"""已推播狀態（去重）。

去重鍵 = 示警 ID + 內容 hash。
  * 同 ID、同內容  -> 永不重複推播
  * 同 ID、內容變更 -> 視為「更新」，推播一次
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_FILE = Path("data/sent_alerts.json")
SYSTEM_ERROR_KEY = "__system_error__"


def load_state() -> dict:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not STATE_FILE.exists():
        return {}

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        # 狀態檔損毀時不可靜默當成空白，否則會把今日所有示警重推一遍
        raise RuntimeError(f"狀態檔無法讀取: {error}") from None

    if not isinstance(data, dict):
        raise RuntimeError("狀態檔格式錯誤（非 JSON object）")

    return data


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(STATE_FILE)


def calculate_hash(alert: dict) -> str:
    fields = {
        "headline": alert.get("headline", ""),
        "description": alert.get("description", ""),
        "instruction": alert.get("instruction", ""),
        "effective": alert.get("effective", ""),
        "expires": alert.get("expires", ""),
        "areas": alert.get("matched_areas", []),
    }

    raw = json.dumps(fields, ensure_ascii=False, sort_keys=True)

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cleanup_state(state: dict, retention_days: int = 7) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    cleaned = {}

    for key, record in state.items():
        if not isinstance(record, dict):
            continue

        sent_at = record.get("sent_at")

        try:
            dt = datetime.fromisoformat(str(sent_at).replace("Z", "+00:00"))
        except ValueError:
            continue

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        if dt >= cutoff:
            cleaned[key] = record

    return cleaned
