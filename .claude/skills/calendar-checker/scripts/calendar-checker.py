"""
캘린더 교차 검증 + D-N 계산
입력: /output/pattern_result.json, /output/notion_calendar.json
출력: /output/verification_result.json

LLM이 호출하는 스크립트. 완전 일치는 스크립트가, 유사 제목 판단은 SKILL.md를 통해 LLM이 수행.
"""

import json
import os
import sys
from datetime import date, datetime

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "output")


def load_json(filename: str) -> dict:
    path = os.path.join(OUTPUT_DIR, filename)
    if not os.path.exists(path):
        print(f"파일 없음: {path}", file=sys.stderr)
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def days_until(date_str: str | None) -> int | None:
    """오늘로부터 date_str까지 남은 일수 (음수 = 과거)"""
    if not date_str:
        return None
    target = date.fromisoformat(date_str[:10])
    return (target - date.today()).days


def urgency_label(days: int | None) -> str:
    if days is None:
        return "날짜없음"
    if days < 0:
        return "기한초과"
    if days == 0:
        return "D-0"
    return f"D-{days}"


def normalize(text: str) -> str:
    """비교용 정규화: 공백/특수문자 제거 + 소문자"""
    return "".join(c for c in text if c.isalnum()).lower()


def is_exact_match(task_name: str, event_name: str) -> bool:
    return normalize(task_name) == normalize(event_name)


def main():
    pattern_data = load_json("pattern_result.json")
    calendar_data = load_json("notion_calendar.json")

    candidates = pattern_data.get("candidate_tasks", [])
    events = calendar_data.get("events", [])

    # 캘린더 이벤트 이름 집합 (정규화)
    event_names_normalized = {normalize(e["name"]) for e in events if e.get("name")}
    event_names_raw = [e["name"] for e in events if e.get("name")]

    results = []
    for task in candidates:
        task_name = task.get("name", "")
        next_expected = task.get("next_expected")
        days = days_until(next_expected)

        # 완전 일치 체크
        exact = is_exact_match(task_name, task_name) and normalize(task_name) in event_names_normalized
        calendar_registered = exact

        # 유사 제목 후보 수집 (LLM 판단 위한 데이터 포함)
        similar_candidates = [
            name for name in event_names_raw
            if not is_exact_match(task_name, name)
            and (
                normalize(task_name)[:4] in normalize(name)
                or normalize(name)[:4] in normalize(task_name)
            )
        ]

        results.append({
            "name": task_name,
            "confidence": task.get("confidence"),
            "recurrence": task.get("recurrence"),
            "basis": task.get("basis"),
            "last_done": task.get("last_done"),
            "next_expected": next_expected,
            "days_until_due": days,
            "urgency": urgency_label(days),
            "calendar_registered": calendar_registered,
            "similar_calendar_candidates": similar_candidates,
            "notion_page_id": task.get("notion_page_id", ""),
        })

    # 긴급도 순 정렬: D-0 > D-1 > D-3 > ... > 날짜없음 > 기한초과
    def sort_key(t):
        d = t.get("days_until_due")
        if d is None:
            return 9999
        if d < 0:
            return 10000
        return d

    results.sort(key=sort_key)

    output = {
        "verified_at": datetime.now().isoformat(),
        "tasks": results,
    }

    out_path = os.path.join(OUTPUT_DIR, "verification_result.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"검증 완료: {len(results)}건 → {out_path}")


if __name__ == "__main__":
    main()
