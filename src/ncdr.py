"""NCDR 即時示警擷取。

資料來源：民生示警公開資料平台 Atom Feed
  https://alerts.ncdr.nat.gov.tw/RssAtomFeed.ashx
（網頁 /web/alerts/immediate 由 JavaScript 動態載入，無法直接用 requests 解析；
  該頁的「即時示警」= 未達 expires 的示警，與 Atom Feed 為同一資料來源。）

篩選條件（全部成立才保留）：
  1. CAP status = Actual，msgType 非 Cancel
  2. 生效時間（effective，缺則 onset、sent）為台灣時間「今日」
  3. 尚未過期
  4. 影響範圍含關注縣市（預設桃園市），以 CAP geocode 判斷
"""

import json
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

FEED_URL = "https://alerts.ncdr.nat.gov.tw/RssAtomFeed.ashx"
ATOM_NS = "http://www.w3.org/2005/Atom"
TAIWAN_TZ = timezone(timedelta(hours=8))
CONFIG_FILE = "config/config.json"

# 縣市代碼前兩碼（Taiwan_Geocode_103 / 113 的縣市層級前綴相同）
COUNTY_PREFIX = {
    "63": "臺北市",
    "64": "高雄市",
    "65": "新北市",
    "66": "臺中市",
    "67": "臺南市",
    "68": "桃園市",
}
GEOCODE_NAMES = ("taiwan_geocode_113", "taiwan_geocode_103")  # 113 優先

CAP_WORKERS = 4
HTTP_TIMEOUT = 20


# ------------------------------------------------------------
# helpers
# ------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _text(el) -> str:
    return (el.text or "").strip() if el is not None else ""


def _children(el, name: str) -> list:
    return [c for c in el if _local(c.tag) == name]


def _child_text(el, name: str) -> str:
    for c in el:
        if _local(c.tag) == name:
            return _text(c)
    return ""


def normalize_area(name: str) -> str:
    return (name or "").strip().replace("台", "臺")


def parse_time(value: str):
    """ISO8601 -> aware datetime；無法解析回傳 None；無時區視為台灣時間。"""

    value = (value or "").strip()

    if not value:
        return None

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TAIWAN_TZ)

    return dt


def city_from_geocode(value: str) -> str:
    value = re.sub(r"\D", "", value or "")

    if len(value) < 5:
        return ""

    return COUNTY_PREFIX.get(value[:2], "")


# ------------------------------------------------------------
# CAP
# ------------------------------------------------------------

def parse_cap(content: bytes, wanted: list, allow_text_fallback: bool) -> dict:
    root = ET.fromstring(content)

    cap = {
        "identifier": _child_text(root, "identifier"),
        "sender": _child_text(root, "sender"),
        "sent": _child_text(root, "sent"),
        "status": _child_text(root, "status"),
        "msgType": _child_text(root, "msgType"),
        "infos": [],
    }

    for info in _children(root, "info"):
        matched, raw_descs, geo_seen = [], [], []

        for area in _children(info, "area"):
            desc = _child_text(area, "areaDesc")
            if desc:
                raw_descs.append(desc)

            for geo in _children(area, "geocode"):
                geo_seen.append(
                    f"{_child_text(geo, 'valueName')}:{_child_text(geo, 'value')}"
                )

            # 同一 area 可能有多個 geocode（多個鄉鎮），逐一檢查
            cities = {
                city_from_geocode(_child_text(g, "value"))
                for g in _children(area, "geocode")
                if re.sub(r"\s+", "", _child_text(g, "valueName")).lower()
                in GEOCODE_NAMES
            }
            cities.discard("")

            for city in sorted(cities):
                if city in wanted and city not in matched:
                    matched.append(city)

            if (
                allow_text_fallback
                and not cities
                and desc
            ):
                for w in wanted:
                    if w in normalize_area(desc) and w not in matched:
                        matched.append(w)

        cap["infos"].append({
            "event": _child_text(info, "event"),
            "headline": _child_text(info, "headline"),
            "description": _child_text(info, "description"),
            "instruction": _child_text(info, "instruction"),
            "effective": _child_text(info, "effective"),
            "onset": _child_text(info, "onset"),
            "expires": _child_text(info, "expires"),
            "area_descs": raw_descs,
            "matched_areas": matched,
            "geo_seen": geo_seen[:8],
        })

    return cap


# ------------------------------------------------------------
# Feed
# ------------------------------------------------------------

def _entry_fields(entry) -> dict:
    cap_url = ""

    for link in entry.findall(f"{{{ATOM_NS}}}link"):
        href = (link.get("href") or "").strip()
        if ".cap" in href.lower():
            cap_url = href
            break

    category = entry.find(f"{{{ATOM_NS}}}category")

    return {
        "id": _text(entry.find(f"{{{ATOM_NS}}}id")),
        "title": _text(entry.find(f"{{{ATOM_NS}}}title")),
        "updated": _text(entry.find(f"{{{ATOM_NS}}}updated")),
        "summary": _text(entry.find(f"{{{ATOM_NS}}}summary")),
        "category": (category.get("term") or "").strip() if category is not None else "",
        "cap_url": cap_url,
    }


def _fetch_cap(session, entry_fields: dict, wanted, allow_text_fallback):
    try:
        r = session.get(entry_fields["cap_url"], timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return entry_fields, parse_cap(r.content, wanted, allow_text_fallback), None
    except Exception as error:  # 單筆 CAP 失敗不可中斷整批
        return entry_fields, None, error


def get_alerts(now: datetime | None = None) -> list:
    config = load_config()

    wanted = [normalize_area(a) for a in config.get("areas", []) if a]
    lookback = int(config.get("feed_lookback_days", 2))
    allow_text = bool(config.get("allow_areadesc_text_fallback", False))

    if not wanted:
        raise RuntimeError("config.areas 不可為空")

    now = (now or datetime.now(TAIWAN_TZ)).astimezone(TAIWAN_TZ)
    today = now.date()

    print(f"NOW (Asia/Taipei) = {now.isoformat()}")
    print(f"areas = {wanted} | today = {today} | lookback_days = {lookback}")

    session = requests.Session()
    session.headers.update({
        "Accept": "application/atom+xml, application/xml, text/xml",
        "User-Agent": "NCDR-Telegram-Alert-Bot/2.0",
    })

    try:
        resp = session.get(FEED_URL, timeout=HTTP_TIMEOUT)
        print(f"NCDR HTTP STATUS: {resp.status_code}")
        resp.raise_for_status()

        feed = ET.fromstring(resp.content)
        entries = [_entry_fields(e) for e in feed.findall(f"{{{ATOM_NS}}}entry")]
        print(f"Feed entries = {len(entries)}")

        # 預篩：entry.updated 在回溯天數內（避免對數百筆舊資料逐一下載 CAP）。
        # 生效時間可能晚於發布時間（預告型示警），故回溯天數預設 2。
        cutoff = datetime.combine(
            today - timedelta(days=lookback), datetime.min.time(), tzinfo=TAIWAN_TZ
        )

        candidates = []
        for e in entries:
            if not e["id"] or not e["cap_url"]:
                continue
            upd = parse_time(e["updated"])
            if upd is not None and upd < cutoff:
                continue
            candidates.append(e)

        print(f"CAP candidates = {len(candidates)}")

        results = []
        with ThreadPoolExecutor(max_workers=CAP_WORKERS) as pool:
            futures = [
                pool.submit(_fetch_cap, session, e, wanted, allow_text)
                for e in candidates
            ]
            results = [f.result() for f in futures]
    finally:
        session.close()

    counters = dict(
        cap_error=0, not_actual=0, cancelled=0, not_today=0,
        expired=0, area_skip=0, kept=0,
    )
    alerts = []

    for entry, cap, error in results:
        ident = entry["id"]

        if error is not None or cap is None:
            counters["cap_error"] += 1
            print(f"CAP ERROR: {ident} | {type(error).__name__}")
            continue

        if cap["status"] and cap["status"].lower() != "actual":
            counters["not_actual"] += 1
            continue

        if cap["msgType"].lower() == "cancel":
            counters["cancelled"] += 1
            continue

        for info in cap["infos"]:
            eff_raw = info["effective"] or info["onset"] or cap["sent"]
            eff = parse_time(eff_raw)

            if eff is None or eff.astimezone(TAIWAN_TZ).date() != today:
                counters["not_today"] += 1
                continue

            exp = parse_time(info["expires"])
            if exp is not None and exp < now:
                counters["expired"] += 1
                continue

            if not info["matched_areas"]:
                counters["area_skip"] += 1
                print(
                    f"AREA SKIP: {ident} | {entry['category']} | "
                    f"geo={info['geo_seen']}"
                )
                continue

            counters["kept"] += 1
            alerts.append({
                "id": ident,
                "category": entry["category"],
                "event": info["event"] or entry["category"],
                "headline": info["headline"] or entry["title"],
                "description": info["description"] or entry["summary"],
                "instruction": info["instruction"],
                "effective": eff.astimezone(TAIWAN_TZ).isoformat(),
                "expires": exp.astimezone(TAIWAN_TZ).isoformat() if exp else "",
                "sender": cap["sender"],
                "matched_areas": info["matched_areas"],
                "raw_area": "；".join(info["area_descs"]),
                "cap_url": entry["cap_url"],
            })
            break  # 同一 CAP 多個 info（多語言）只取第一個符合者，避免重複

    print("========== NCDR FILTER SUMMARY ==========")
    for k, v in counters.items():
        print(f"{k:<12} = {v}")
    print("=========================================")

    return alerts
