import os
import re

import requests


# ============================================================
# Telegram Config
# ============================================================

TELEGRAM_API_BASE = "https://api.telegram.org"


def _get_config():
    token = (
        os.getenv("TELEGRAM_BOT_TOKEN")
        or ""
    ).strip()

    chat_id = (
        os.getenv("TELEGRAM_CHAT_ID")
        or ""
    ).strip()

    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN 未設定"
        )

    if not chat_id:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID 未設定"
        )

    return token, chat_id


# ============================================================
# Secret redaction
#
# requests 的例外訊息會包含完整 URL（含 /bot<TOKEN>/），
# 直接 print 會把 Bot Token 寫進 GitHub Actions log，
# 或經由系統錯誤通知傳到 Telegram。
# ============================================================

def redact(text) -> str:
    text = str(text)

    token = (
        os.getenv("TELEGRAM_BOT_TOKEN")
        or ""
    ).strip()

    if token:
        text = text.replace(token, "***")

    # 保險：即使 env 不同，也遮蔽 /bot<...>/ 樣式
    return re.sub(
        r"/bot[^/\s]+/",
        "/bot***/",
        text,
    )


# ============================================================
# Send message
# ============================================================

def send_message(
    message: str,
) -> int | None:
    """
    發送 Telegram 訊息。

    成功：
        回傳 Telegram message_id

    失敗：
        印出 Telegram API 詳細錯誤
        並重新 raise，讓 main.py 可以正確處理。
    """

    token, chat_id = _get_config()

    url = (
        f"{TELEGRAM_API_BASE}"
        f"/bot{token}"
        f"/sendMessage"
    )

    payload = {
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": True,
    }

    print(
        "========== TELEGRAM SEND =========="
    )

    print(
        f"chat_id = {chat_id}"
    )

    print(
        f"message_length = {len(message)}"
    )

    try:

        response = requests.post(
            url,
            json=payload,
            timeout=20,
        )

    except requests.RequestException as error:

        print(
            "TELEGRAM NETWORK ERROR"
        )

        print(
            redact(error)
        )

        raise RuntimeError(
            "Telegram 網路錯誤: "
            f"{redact(error)}"
        ) from None

    print(
        f"TELEGRAM HTTP STATUS: "
        f"{response.status_code}"
    )

    print(
        f"TELEGRAM CONTENT TYPE: "
        f"{response.headers.get('Content-Type')}"
    )

    # --------------------------------------------------------
    # Telegram API 即使 HTTP 400，
    # body 通常會告訴我們真正原因。
    # --------------------------------------------------------

    try:

        data = response.json()

    except ValueError:

        data = None

    if not response.ok:

        print(
            "========== TELEGRAM ERROR =========="
        )

        print(
            f"response_text = "
            f"{redact(response.text[:2000])}"
        )

        if isinstance(data, dict):

            print(
                f"ok = "
                f"{data.get('ok')}"
            )

            print(
                f"error_code = "
                f"{data.get('error_code')}"
            )

            print(
                f"description = "
                f"{data.get('description')}"
            )

        print(
            "===================================="
        )

        raise RuntimeError(
            f"Telegram HTTP {response.status_code}: "
            f"{redact((data or {}).get('description', '')) if isinstance(data, dict) else ''}"
        )

    # --------------------------------------------------------
    # 檢查 Telegram API 回傳
    # --------------------------------------------------------

    if not isinstance(data, dict):

        raise RuntimeError(
            "Telegram API 回傳不是 JSON"
        )

    if not data.get("ok"):

        raise RuntimeError(
            "Telegram API 回傳 ok=false: "
            f"{data}"
        )

    result = data.get(
        "result"
    )

    if not isinstance(
        result,
        dict
    ):

        raise RuntimeError(
            "Telegram API 缺少 result"
        )

    message_id = result.get(
        "message_id"
    )

    print(
        f"TELEGRAM MESSAGE ID: "
        f"{message_id}"
    )

    print(
        "===================================="
    )

    return message_id
