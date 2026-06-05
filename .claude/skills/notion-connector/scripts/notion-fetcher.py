"""
Notion DB 전체 조회 + 향후 14일 캘린더 조회
출력: /output/notion_tasks.json, /output/notion_calendar.json
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import urllib3
import requests
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv()

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
TASK_DB_ID = os.environ["NOTION_TASK_DB_ID"]
CALENDAR_DB_ID = os.environ.get("NOTION_CALENDAR_DB_ID", "")

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "output")

# 날짜 속성명 탐색 우선순위
DATE_PROP_CANDIDATES = ["예정일", "완료일", "Due", "Date", "날짜", "마감일", "기한", "일정"]


def fetch_all_pages(db_id: str, body: dict | None = None) -> list[dict]:
    """페이지네이션을 처리하며 DB의 모든 페이지를 반환"""
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    results = []
    payload = body or {}

    while True:
        resp = requests.post(url, headers=HEADERS, json=payload, verify=False)
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get("results", []))

        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]

    return results


def detect_date_property(properties: dict) -> str | None:
    """태스크 속성에서 날짜 속성명을 자동 탐지"""
    for candidate in DATE_PROP_CANDIDATES:
        if candidate in properties and properties[candidate].get("type") == "date":
            return candidate
    # fallback: 첫 번째 date 타입 속성
    for name, prop in properties.items():
        if prop.get("type") == "date":
            return name
    return None


def extract_task(page: dict) -> dict:
    props = page.get("properties", {})

    # 제목 추출 (title 타입 속성)
    name = ""
    for prop in props.values():
        if prop.get("type") == "title":
            rich_texts = prop.get("title", [])
            name = "".join(t.get("plain_text", "") for t in rich_texts)
            break

    # 상태 추출
    status = ""
    for key in ["상태", "Status", "status"]:
        if key in props:
            p = props[key]
            if p.get("type") == "status":
                status = (p.get("status") or {}).get("name", "")
            elif p.get("type") == "select":
                status = (p.get("select") or {}).get("name", "")
            break

    # 날짜 추출
    date_prop = detect_date_property(props)
    date_value = None
    date_end = None
    if date_prop:
        date_obj = props[date_prop].get("date") or {}
        date_value = date_obj.get("start")
        date_end = date_obj.get("end")

    return {
        "id": page["id"],
        "name": name,
        "status": status,
        "date_property": date_prop,
        "date": date_value,
        "date_end": date_end,
        "url": page.get("url", ""),
        "created_time": page.get("created_time", ""),
        "last_edited_time": page.get("last_edited_time", ""),
    }


def fetch_tasks() -> list[dict]:
    print("태스크 DB 조회 중...")
    pages = fetch_all_pages(TASK_DB_ID)
    tasks = [extract_task(p) for p in pages]
    print(f"  → {len(tasks)}건 수집 완료")
    return tasks


def fetch_calendar() -> list[dict]:
    if not CALENDAR_DB_ID:
        print("NOTION_CALENDAR_DB_ID 미설정 — 캘린더 조회 생략")
        return []

    today = datetime.now(timezone.utc).date()
    end_date = today + timedelta(days=14)

    print("캘린더 DB 조회 중 (향후 14일)...")
    body = {
        "filter": {
            "and": [
                {
                    "or": [
                        {"property": cand, "date": {"on_or_after": today.isoformat()}}
                        for cand in DATE_PROP_CANDIDATES
                    ]
                },
                {
                    "or": [
                        {"property": cand, "date": {"on_or_before": end_date.isoformat()}}
                        for cand in DATE_PROP_CANDIDATES
                    ]
                },
            ]
        }
    }
    try:
        pages = fetch_all_pages(CALENDAR_DB_ID, body)
    except requests.HTTPError:
        # 필터 속성명이 DB에 없을 경우 필터 없이 전체 조회 후 파이썬에서 필터링
        pages = fetch_all_pages(CALENDAR_DB_ID)

    events = []
    for page in pages:
        task = extract_task(page)
        if task["date"]:
            event_date = task["date"][:10]
            if today.isoformat() <= event_date <= end_date.isoformat():
                events.append(task)

    print(f"  → {len(events)}건 수집 완료")
    return events


def save_json(data: dict, filename: str):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  저장: {path}")


def main():
    try:
        tasks = fetch_tasks()
        save_json({"fetched_at": datetime.now().isoformat(), "tasks": tasks}, "notion_tasks.json")

        events = fetch_calendar()
        save_json({"fetched_at": datetime.now().isoformat(), "events": events}, "notion_calendar.json")

    except requests.HTTPError as e:
        msg = f"Notion API 오류: {e.response.status_code} {e.response.text}"
        print(msg, file=sys.stderr)
        sys.exit(1)
    except KeyError as e:
        print(f"환경변수 누락: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
