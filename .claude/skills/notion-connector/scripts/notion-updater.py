"""
Notion 태스크 상태/날짜 업데이트 + 캘린더 일정 생성
사용법:
  python notion-updater.py complete <page_id>
  python notion-updater.py postpone <page_id> <new_date: YYYY-MM-DD>
  python notion-updater.py calendar_add <title> <date: YYYY-MM-DD>
  python notion-updater.py task_add <title> [YYYY-MM-DD]
"""

import json
import os
import sys

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

DATE_PROP_CANDIDATES = ["예정일", "완료일", "Due", "Date", "날짜", "마감일", "기한", "일정"]
STATUS_PROP_CANDIDATES = ["상태", "Status", "status"]
STATUS_DONE_VALUES = ["완료", "Done", "done", "Completed", "DONE"]


def get_page(page_id: str) -> dict:
    resp = requests.get(f"https://api.notion.com/v1/pages/{page_id}", headers=HEADERS, verify=False)
    resp.raise_for_status()
    return resp.json()


def detect_status_property(properties: dict) -> tuple[str | None, str]:
    """상태 속성명과 타입 반환 (name, type)"""
    for candidate in STATUS_PROP_CANDIDATES:
        if candidate in properties:
            return candidate, properties[candidate].get("type", "")
    return None, ""


def detect_date_property(properties: dict) -> str | None:
    for candidate in DATE_PROP_CANDIDATES:
        if candidate in properties and properties[candidate].get("type") == "date":
            return candidate
    for name, prop in properties.items():
        if prop.get("type") == "date":
            return name
    return None


def complete_task(page_id: str):
    """태스크 상태를 '완료'로 업데이트"""
    page = get_page(page_id)
    props = page.get("properties", {})
    status_prop, status_type = detect_status_property(props)

    if not status_prop:
        print("상태 속성을 찾을 수 없습니다.", file=sys.stderr)
        sys.exit(1)

    if status_type == "status":
        payload = {"properties": {status_prop: {"status": {"name": "완료"}}}}
    elif status_type == "select":
        payload = {"properties": {status_prop: {"select": {"name": "완료"}}}}
    else:
        print(f"지원하지 않는 상태 속성 타입: {status_type}", file=sys.stderr)
        sys.exit(1)

    resp = requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}", headers=HEADERS, json=payload, verify=False
    )
    resp.raise_for_status()
    print(f"완료 처리 성공: {page_id}")


def postpone_task(page_id: str, new_date: str):
    """태스크 날짜를 new_date(YYYY-MM-DD)로 변경"""
    page = get_page(page_id)
    props = page.get("properties", {})
    date_prop = detect_date_property(props)

    if not date_prop:
        print("날짜 속성을 찾을 수 없습니다.", file=sys.stderr)
        sys.exit(1)

    payload = {"properties": {date_prop: {"date": {"start": new_date}}}}
    resp = requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}", headers=HEADERS, json=payload, verify=False
    )
    resp.raise_for_status()
    print(f"연기 처리 성공: {page_id} → {new_date}")

    # 캘린더 DB에도 동일 일정이 있으면 업데이트
    if CALENDAR_DB_ID:
        _update_calendar_event(page_id, new_date)


def _update_calendar_event(task_page_id: str, new_date: str):
    """캘린더 DB에서 task_page_id와 연관된 일정의 날짜를 업데이트 (best-effort)"""
    # 캘린더 DB 전체를 조회해 동일 ID의 항목을 찾는 간단 구현
    url = f"https://api.notion.com/v1/databases/{CALENDAR_DB_ID}/query"
    resp = requests.post(url, headers=HEADERS, json={}, verify=False)
    if not resp.ok:
        print("캘린더 DB 조회 실패 (무시)", file=sys.stderr)
        return

    for page in resp.json().get("results", []):
        if page["id"].replace("-", "") == task_page_id.replace("-", ""):
            date_prop = detect_date_property(page.get("properties", {}))
            if date_prop:
                patch_resp = requests.patch(
                    f"https://api.notion.com/v1/pages/{page['id']}",
                    headers=HEADERS,
                    json={"properties": {date_prop: {"date": {"start": new_date}}}},
                    verify=False,
                )
                if patch_resp.ok:
                    print(f"캘린더 일정 날짜 업데이트 성공: {page['id']} → {new_date}")
                else:
                    print("캘린더 일정 날짜 업데이트 실패", file=sys.stderr)
            return


def detect_category_property(properties: dict, prop_name: str | None) -> tuple[str | None, str]:
    """카테고리 속성명과 타입(select/multi_select) 반환"""
    if prop_name and prop_name in properties:
        ptype = properties[prop_name].get("type", "")
        if ptype in ("select", "multi_select"):
            return prop_name, ptype
    for name, prop in properties.items():
        if prop.get("type") in ("select", "multi_select"):
            return name, prop["type"]
    return None, ""


def add_task(title: str, date: str = "", category_prop: str = "", category: str = ""):
    """Notion 태스크 DB에 신규 업무 추가"""
    sample_resp = requests.post(
        f"https://api.notion.com/v1/databases/{TASK_DB_ID}/query",
        headers=HEADERS,
        json={"page_size": 1},
        verify=False,
    )
    sample_resp.raise_for_status()
    sample_pages = sample_resp.json().get("results", [])

    date_prop = None
    detected_cat_prop = None
    detected_cat_type = ""
    if sample_pages:
        props = sample_pages[0].get("properties", {})
        date_prop = detect_date_property(props)
        if category:
            detected_cat_prop, detected_cat_type = detect_category_property(props, category_prop or None)

    payload = {
        "parent": {"database_id": TASK_DB_ID},
        "properties": {
            "title": {"title": [{"text": {"content": title}}]},
        },
    }
    if date and date_prop:
        payload["properties"][date_prop] = {"date": {"start": date}}
    if category and detected_cat_prop:
        if detected_cat_type == "select":
            payload["properties"][detected_cat_prop] = {"select": {"name": category.strip()}}
        elif detected_cat_type == "multi_select":
            # 사용자가 "NC/KR, NC/GL" 처럼 쉼표로 여러 항목을 입력할 수 있으므로 분리
            cat_values = [c.strip() for c in category.split(",") if c.strip()]
            payload["properties"][detected_cat_prop] = {
                "multi_select": [{"name": c} for c in cat_values]
            }

    resp = requests.post("https://api.notion.com/v1/pages", headers=HEADERS, json=payload, verify=False)
    resp.raise_for_status()
    cat_str = f" [{category}]" if category else ""
    print(f"태스크 추가 성공: '{title}'{cat_str}" + (f" ({date})" if date else ""))


def add_calendar_event(title: str, date: str):
    """Notion 캘린더 DB에 신규 일정 생성"""
    if not CALENDAR_DB_ID:
        print("NOTION_CALENDAR_DB_ID 미설정 — 캘린더 추가 불가", file=sys.stderr)
        sys.exit(1)

    # 캘린더 DB의 날짜 속성명 탐지 (빈 쿼리로 샘플 1건 조회)
    sample_resp = requests.post(
        f"https://api.notion.com/v1/databases/{CALENDAR_DB_ID}/query",
        headers=HEADERS,
        json={"page_size": 1},
        verify=False,
    )
    sample_resp.raise_for_status()
    sample_pages = sample_resp.json().get("results", [])
    date_prop = "Date"
    if sample_pages:
        detected = detect_date_property(sample_pages[0].get("properties", {}))
        if detected:
            date_prop = detected

    payload = {
        "parent": {"database_id": CALENDAR_DB_ID},
        "properties": {
            "title": {"title": [{"text": {"content": title}}]},
            date_prop: {"date": {"start": date}},
        },
    }
    resp = requests.post("https://api.notion.com/v1/pages", headers=HEADERS, json=payload, verify=False)
    resp.raise_for_status()
    print(f"캘린더 일정 추가 성공: '{title}' ({date})")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]

    try:
        if command == "complete":
            if len(sys.argv) < 3:
                print("사용법: python notion-updater.py complete <page_id>", file=sys.stderr)
                sys.exit(1)
            complete_task(sys.argv[2])

        elif command == "postpone":
            if len(sys.argv) < 4:
                print("사용법: python notion-updater.py postpone <page_id> <YYYY-MM-DD>", file=sys.stderr)
                sys.exit(1)
            postpone_task(sys.argv[2], sys.argv[3])

        elif command == "calendar_add":
            if len(sys.argv) < 4:
                print("사용법: python notion-updater.py calendar_add <title> <YYYY-MM-DD>", file=sys.stderr)
                sys.exit(1)
            add_calendar_event(sys.argv[2], sys.argv[3])

        elif command == "task_add":
            if len(sys.argv) < 3:
                print("사용법: python notion-updater.py task_add <title> [YYYY-MM-DD] [--category-prop <prop>] [--category <value>]", file=sys.stderr)
                sys.exit(1)
            remaining = sys.argv[3:]
            date_arg = ""
            cat_prop_arg = ""
            cat_arg = ""
            i = 0
            while i < len(remaining):
                if remaining[i] == "--category-prop" and i + 1 < len(remaining):
                    cat_prop_arg = remaining[i + 1]; i += 2
                elif remaining[i] == "--category" and i + 1 < len(remaining):
                    cat_arg = remaining[i + 1]; i += 2
                elif not remaining[i].startswith("--"):
                    date_arg = remaining[i]; i += 1
                else:
                    i += 1
            add_task(sys.argv[2], date_arg, cat_prop_arg, cat_arg)

        else:
            print(f"알 수 없는 명령: {command}", file=sys.stderr)
            sys.exit(1)

    except requests.HTTPError as e:
        print(f"Notion API 오류: {e.response.status_code} {e.response.text}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
