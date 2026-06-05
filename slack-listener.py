"""
Slack Socket Mode 리스너 — 업무 비서
@봇 멘션을 수신하여 업무 비서 워크플로우를 자동 실행한다.

실행: python slack-listener.py
종료: Ctrl+C
"""

import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

load_dotenv()

# Windows 터미널 CP949 인코딩 오류 방지
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
SKILLS_DIR = BASE_DIR / ".claude" / "skills"

app = App(token=os.environ["SLACK_BOT_TOKEN"])


# ──────────────────────────────────────────────────────────────
# 의도 분류 — Ollama LLM 기반 (정규식 폴백)
# ──────────────────────────────────────────────────────────────

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")

_LLM_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "add_task",
            "description": (
                "Notion to-do 리스트에 새 업무·회의·일정 항목을 추가한다. "
                "사용자가 '추가해줘', '등록해줘', '넣어줘' 등 신규 항목 생성을 요청할 때."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_name": {"type": "string", "description": "추가할 업무 또는 일정 이름"},
                    "date": {"type": "string", "description": "날짜 YYYY-MM-DD. 날짜 없으면 빈 문자열."},
                },
                "required": ["task_name", "date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_task",
            "description": "Notion에 등록된 기존 업무를 완료 처리한다. '완료', '끝났어', '했어' 등.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_name": {"type": "string", "description": "완료 처리할 업무명"},
                },
                "required": ["task_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "postpone_task",
            "description": "Notion에 등록된 기존 업무를 다른 날짜로 미룬다. '미뤄', '연기', '나중에' 등.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_name": {"type": "string", "description": "연기할 업무명"},
                    "new_date": {"type": "string", "description": "새 날짜 YYYY-MM-DD"},
                },
                "required": ["task_name", "new_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_calendar_event",
            "description": "Notion 캘린더 DB에 일정을 추가한다. '캘린더에 추가' 등.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "캘린더에 추가할 일정 제목"},
                    "date": {"type": "string", "description": "일정 날짜 YYYY-MM-DD"},
                },
                "required": ["title", "date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_briefing",
            "description": (
                "다가오는 정기 업무 현황을 브리핑한다. "
                "업무 목록 조회, 오늘/이번주 할 일, '뭐 있어', '알려줘' 등."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reset_cache",
            "description": "패턴 분석 캐시를 삭제하고 재분석한다. '패턴 갱신', '다시 분석' 등.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unknown",
            "description": "요청을 이해하지 못했거나 위 기능에 해당하지 않는 요청.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "사용자에게 전달할 안내 메시지"},
                },
                "required": ["message"],
            },
        },
    },
]


def classify_intent(text: str) -> tuple[str, dict]:
    try:
        return _llm_classify(text.strip())
    except Exception as e:
        print(f"LLM 분류 실패: {e} — 정규식 폴백", file=sys.stderr)
        return _regex_classify(text.strip())


def _llm_classify(text: str) -> tuple[str, dict]:
    today = date.today().isoformat()
    system_msg = (
        f"오늘 날짜: {today}. 당신은 업무 비서 Slack 봇입니다. "
        "사용자의 메시지를 분석해 반드시 적절한 함수를 정확히 한 번 호출하세요. "
        f"날짜 표현(내일, 다음주, 6/5, 6월5일 등)은 반드시 YYYY-MM-DD 형식으로 변환하세요. 오늘 기준 연도는 {today[:4]}입니다. "
        "업무/회의/일정 신규 생성은 add_task, 캘린더 추가는 add_calendar_event로 구분하세요. "
        "task_name은 사용자 원문에서 핵심 업무명만 추출하고 조사(을/를/이/가/에)와 부연 설명은 제거하세요."
    )

    resp = requests.post(
        f"{OLLAMA_URL}/v1/chat/completions",
        json={
            "model": OLLAMA_MODEL,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": text},
            ],
            "tools": _LLM_TOOLS,
            "tool_choice": "required",
            "stream": False,
        },
        timeout=30,
        verify=False,
    )
    resp.raise_for_status()
    data = resp.json()

    message = data["choices"][0]["message"]
    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        tc = tool_calls[0]
        name = tc["function"]["name"]
        raw_args = tc["function"].get("arguments", "{}")
        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        # 날짜 필드 정규화 (LLM이 "6/5" 형태로 반환할 수 있음)
        for key in ("date", "new_date"):
            if key in args and args[key]:
                args[key] = _normalize_date(args[key])
        print(f"  → LLM 도구: {name} / {args}")
        return name, args

    return _regex_classify(text)


def _normalize_date(val: str) -> str:
    """LLM이 반환한 날짜를 YYYY-MM-DD로 정규화"""
    if not val:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}$", val):
        return val
    today = date.today()
    m = re.search(r"(\d{1,2})[월/](\d{1,2})", val)
    if m:
        mo, dy = int(m.group(1)), int(m.group(2))
        try:
            c = date(today.year, mo, dy)
            return (date(today.year + 1, mo, dy) if c < today else c).isoformat()
        except ValueError:
            pass
    return val


def _regex_classify(text: str) -> tuple[str, dict]:
    """폴백: 정규식 기반 분류"""
    if re.search(r"패턴\s*갱신|캐시\s*삭제|다시\s*분석", text):
        return "reset_cache", {}
    if re.search(r"완료|했어|끝났어|했다|마쳤어", text):
        task = re.split(r"완료|했어|끝났어|했다|마쳤어", text)[0].strip()
        return "complete_task", {"task_name": task}
    if re.search(r"미뤄|연기|나중에", text):
        task = re.split(r"미뤄|연기|나중에", text)[0].strip()
        return "postpone_task", {"task_name": task, "new_date": _parse_date_str(text)}
    if re.search(r"캘린더", text) and re.search(r"추가|넣어|저장", text):
        title = re.sub(r"\d{1,2}[월/]\d{1,2}일?에?\s*", "", text)
        title = re.sub(r"캘린더에?\s*(?:추가|넣어|저장)해줘.*", "", title).strip()
        return "add_calendar_event", {"title": title, "date": _parse_date_str(text)}
    if re.search(r"to-do|할\s*일|태스크|리스트", text, re.IGNORECASE) and re.search(r"추가|넣어|등록", text):
        cleaned = re.sub(r"\d{1,2}[월/]\d{1,2}일?에?\s*", "", text)
        cleaned = re.sub(
            r"(?:to-do\s*(?:list)?|할\s*일|태스크|리스트)[에를]?\s*(?:추가|넣어|등록)해줘.*",
            "", cleaned, flags=re.IGNORECASE
        ).strip()
        return "add_task", {"task_name": cleaned, "date": _parse_date_str(text)}
    return "get_briefing", {}


def _parse_date_str(text: str) -> str:
    today = date.today()
    if "다음 주" in text or "다음주" in text:
        return (today + timedelta(days=7 - today.weekday())).isoformat()
    if "내일" in text:
        return (today + timedelta(days=1)).isoformat()
    if "모레" in text:
        return (today + timedelta(days=2)).isoformat()
    m = re.search(r"(\d{1,2})[월/](\d{1,2})", text)
    if m:
        mo, dy = int(m.group(1)), int(m.group(2))
        c = date(today.year, mo, dy)
        return (date(today.year + 1, mo, dy) if c < today else c).isoformat()
    return ""


# ──────────────────────────────────────────────────────────────
# 패턴 분석 (규칙 기반)
# ──────────────────────────────────────────────────────────────

RECURRENCE_KEYWORDS = {
    "weekly":    ["주간", "주별", "weekly", "매주", "주마다"],
    "biweekly":  ["격주", "biweekly", "2주"],
    "monthly":   ["월간", "월별", "monthly", "매월", "월마다", "정산", "결산"],
    "quarterly": ["분기", "quarterly", "3개월"],
    "annually":  ["연간", "연도", "yearly", "annual", "연말", "연초"],
}


def _detect_recurrence(name: str) -> str | None:
    lower = name.lower()
    for rec, keywords in RECURRENCE_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return rec
    return None


def _interval_days(recurrence: str) -> int:
    return {"weekly": 7, "biweekly": 14, "monthly": 30, "quarterly": 90, "annually": 365}.get(recurrence, 0)


def analyze_patterns(tasks: list[dict]) -> list[dict]:
    """업무 이력에서 정기 업무 후보 추출 (규칙 기반)"""
    today = date.today()
    candidates = []
    seen_names = set()

    # 1) 키워드 기반 탐지
    for task in tasks:
        name = task.get("name", "").strip()
        if not name or name in seen_names:
            continue

        recurrence = _detect_recurrence(name)
        if not recurrence:
            continue

        seen_names.add(name)
        task_date = task.get("date")
        last_done = task_date[:10] if task_date else None

        next_expected = None
        if last_done:
            last_dt = date.fromisoformat(last_done)
            interval = _interval_days(recurrence)
            next_dt = last_dt + timedelta(days=interval)
            while next_dt < today:
                next_dt += timedelta(days=interval)
            next_expected = next_dt.isoformat()

        candidates.append({
            "name": name,
            "confidence": 75,
            "recurrence": recurrence,
            "basis": f"제목에 '{recurrence}' 패턴 키워드 포함",
            "last_done": last_done,
            "next_expected": next_expected,
            "notion_page_id": task.get("id"),
        })

    # 2) 반복 제목 기반 탐지 (같은 제목 2회 이상)
    name_groups: dict[str, list[dict]] = defaultdict(list)
    for task in tasks:
        name = task.get("name", "").strip()
        if name:
            name_groups[name].append(task)

    for name, group in name_groups.items():
        if name in seen_names or len(group) < 2:
            continue

        seen_names.add(name)
        dated = sorted(
            [t for t in group if t.get("date")],
            key=lambda t: t["date"]
        )

        recurrence = "unknown"
        confidence = 45
        next_expected = None
        last_done = None

        if len(dated) >= 2:
            intervals = []
            for i in range(1, len(dated)):
                d1 = date.fromisoformat(dated[i - 1]["date"][:10])
                d2 = date.fromisoformat(dated[i]["date"][:10])
                intervals.append((d2 - d1).days)

            avg = sum(intervals) / len(intervals)
            if abs(avg - 7) <= 2:
                recurrence, confidence = "weekly", 70
            elif abs(avg - 14) <= 3:
                recurrence, confidence = "biweekly", 70
            elif abs(avg - 30) <= 5:
                recurrence, confidence = "monthly", 70
            elif abs(avg - 90) <= 10:
                recurrence, confidence = "quarterly", 65

            last_done = dated[-1]["date"][:10]
            if recurrence != "unknown":
                last_dt = date.fromisoformat(last_done)
                interval = _interval_days(recurrence)
                next_dt = last_dt + timedelta(days=int(avg))
                while next_dt < today:
                    next_dt += timedelta(days=int(avg))
                next_expected = next_dt.isoformat()

        candidates.append({
            "name": name,
            "confidence": confidence,
            "recurrence": recurrence,
            "basis": f"동일 제목 {len(group)}회 등장",
            "last_done": last_done,
            "next_expected": next_expected,
            "notion_page_id": group[-1].get("id"),
        })

    # 신뢰도 낮은 순 제거 후 정렬
    candidates.sort(key=lambda c: -c["confidence"])
    return candidates


# ──────────────────────────────────────────────────────────────
# 파일 유틸
# ──────────────────────────────────────────────────────────────

def load_json(filename: str) -> dict:
    path = OUTPUT_DIR / filename
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(data: dict, filename: str):
    OUTPUT_DIR.mkdir(exist_ok=True)
    (OUTPUT_DIR / filename).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def run_script(relative_path: str, args: list[str] = None) -> tuple[bool, str]:
    cmd = ["python", str(BASE_DIR / relative_path)] + (args or [])
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    output = (stdout + stderr).strip()
    return result.returncode == 0, output


def find_page_id(task_name: str) -> str | None:
    data = load_json("notion_tasks.json")
    tasks = data.get("tasks", [])

    for task in tasks:
        if task.get("name", "").strip() == task_name.strip():
            return task.get("id")
    for task in tasks:
        if task_name in task.get("name", ""):
            return task.get("id")
    return None


# ──────────────────────────────────────────────────────────────
# 브리핑 메시지 생성
# ──────────────────────────────────────────────────────────────

def _urgency_sort_key(t: dict) -> int:
    d = t.get("days_until_due")
    if d is None:
        return 9999
    return 10000 if d < 0 else d


def build_briefing(tasks: list[dict]) -> str:
    if not tasks:
        return (
            "⚠️ *정기 업무 패턴을 찾지 못했습니다.*\n\n"
            "Notion에 과거 업무를 10~15개 역입력한 후 다시 시도해 주세요.\n"
            "`@봇 패턴 갱신` 으로 재분석할 수 있습니다."
        )

    lines = ["📅 *다가오는 정기 업무 리스트*\n"]
    suggestions = []

    for t in sorted(tasks, key=_urgency_sort_key):
        name = t.get("name", "")
        urgency = t.get("urgency", "")
        confidence = t.get("confidence", 100)
        calendar_registered = t.get("calendar_registered", True)
        last_done = t.get("last_done", "")

        urgency_tag = f"[{urgency}] " if urgency else ""
        conf_tag = f" (신뢰도 {confidence}%)" if confidence and confidence < 80 else ""
        cal_tag = f" (캘린더: {'✓' if calendar_registered else '✗'})"
        last_tag = f" — 이전: {last_done}" if last_done else ""

        lines.append(f"• {urgency_tag}{name}{cal_tag}{conf_tag}{last_tag}")

        if not calendar_registered:
            suggestions.append(name)

    if suggestions:
        lines.append("\n💡 *비서의 제안*")
        for s in suggestions[:3]:
            lines.append(f'• "{s}" 캘린더에 추가할까요?')

    lines.append("\n*완료 처리나 연기가 필요한 항목이 있으면 말씀해 주세요.*")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# 태스크 추가 — 카테고리 확인 대기 상태
# ──────────────────────────────────────────────────────────────

# user_id → {task_name, date, category_prop}
_pending_task_add: dict[str, dict] = {}

_NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
_NOTION_TASK_DB_ID = os.environ.get("NOTION_TASK_DB_ID", "")
_NOTION_HEADERS = {
    "Authorization": f"Bearer {_NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

_CATEGORY_PROP_NAMES = {"분류", "카테고리", "category", "type", "유형", "태그", "tag", "업무유형", "구분", "항목"}


def fetch_task_categories() -> tuple[str | None, list[str]]:
    """Task DB schema에서 select/multi_select 카테고리 속성명과 옵션 목록 반환"""
    if not _NOTION_TOKEN or not _NOTION_TASK_DB_ID:
        return None, []
    try:
        resp = requests.get(
            f"https://api.notion.com/v1/databases/{_NOTION_TASK_DB_ID}",
            headers=_NOTION_HEADERS,
            verify=False,
            timeout=10,
        )
        resp.raise_for_status()
        props = resp.json().get("properties", {})

        # 이름으로 먼저 찾기
        for name, prop in props.items():
            if name.lower() in _CATEGORY_PROP_NAMES and prop.get("type") in ("select", "multi_select"):
                ptype = prop["type"]
                options = [opt["name"] for opt in prop[ptype].get("options", [])]
                return name, options

        # 이름 무관, title 제외 첫 번째 select/multi_select
        for name, prop in props.items():
            if prop.get("type") in ("select", "multi_select"):
                ptype = prop["type"]
                options = [opt["name"] for opt in prop[ptype].get("options", [])]
                if options:
                    return name, options
    except Exception as e:
        print(f"카테고리 조회 실패: {e}", file=sys.stderr)
    return None, []


# ──────────────────────────────────────────────────────────────
# 워크플로우
# ──────────────────────────────────────────────────────────────

def workflow_briefing(say):
    say("잠깐만요, 업무 현황 확인 중입니다... ⏳")

    # Step 1: Notion 데이터 수집
    ok, out = run_script(".claude/skills/notion-connector/scripts/notion-fetcher.py")
    if not ok:
        say(f"⚠️ Notion 데이터 수집 실패:\n```{out}```")
        return

    # Step 2: 패턴 분석
    tasks_data = load_json("notion_tasks.json")
    tasks = tasks_data.get("tasks", [])
    candidates = analyze_patterns(tasks)

    pattern_result = {
        "analyzed_at": datetime.now().isoformat(),
        "candidate_tasks": candidates,
    }
    save_json(pattern_result, "pattern_result.json")

    # Step 3: 캘린더 교차 검증
    run_script(".claude/skills/calendar-checker/scripts/calendar-checker.py")

    # Step 4: 브리핑 생성 및 전송
    result = load_json("verification_result.json")
    final_tasks = result.get("tasks", candidates)

    briefing = build_briefing(final_tasks)
    say(briefing)


def workflow_complete(say, task_name: str):
    if not task_name:
        say("어떤 업무를 완료하셨나요?\n예: `@봇 주간보고 완료했어`")
        return

    page_id = find_page_id(task_name)
    if not page_id:
        say(f"'{task_name}' 업무를 Notion에서 찾지 못했습니다. 업무명을 정확히 입력해 주세요.")
        return

    ok, out = run_script(
        ".claude/skills/notion-connector/scripts/notion-updater.py",
        ["complete", page_id]
    )
    if ok:
        say(f"✅ '{task_name}' 완료 처리했습니다.")
    else:
        say(f"⚠️ 완료 처리 실패:\n```{out}```")


def workflow_calendar_add(say, task_name: str, event_date: str):
    if not task_name:
        say("어떤 일정을 캘린더에 추가할까요?\n예: `@봇 속초 여행 캘린더 추가 6월 7일`")
        return

    ok, out = run_script(
        ".claude/skills/notion-connector/scripts/notion-updater.py",
        ["calendar_add", task_name, event_date]
    )
    if ok:
        say(f"📆 '{task_name}' {event_date} 캘린더에 추가했습니다.")
    else:
        say(f"⚠️ 캘린더 추가 실패:\n```{out}```")


def workflow_task_add(say, task_name: str, event_date: str, user_id: str = ""):
    if not task_name:
        say("어떤 업무를 추가할까요?\n예: `@봇 마케팅 회의 to-do list에 추가해줘 6/5`")
        return

    if user_id:
        category_prop, categories = fetch_task_categories()
        _pending_task_add[user_id] = {
            "task_name": task_name,
            "date": event_date,
            "category_prop": category_prop,
            "valid_categories": categories,
        }
        date_str = f" ({event_date})" if event_date else ""

        if categories:
            options_text = "  ".join(f"`{c}`" for c in categories)
            msg = (
                f"*'{task_name}'*{date_str}을(를) to-do 리스트에 추가할게요. ✍️\n\n"
                f"어떤 항목의 업무인가요?\n{options_text}\n\n"
                f"@봇 [항목명] 으로 답해주세요. 바로 추가: `@봇 바로추가`"
            )
        else:
            msg = (
                f"*'{task_name}'*{date_str}을(를) to-do 리스트에 추가할게요. ✍️\n\n"
                f"어떤 항목의 업무인가요? _(예: 회의, 보고서, 개발, 마케팅, 운영 등)_\n"
                f"@봇 [항목명] 으로 답해주세요. 바로 추가: `@봇 바로추가`"
            )
        say(msg)
    else:
        _execute_task_add(say, task_name, event_date, None, "", [])


def _execute_task_add(
    say,
    task_name: str,
    event_date: str,
    category_prop: str | None,
    category: str,
    valid_categories: list[str],
):
    """카테고리 확인 후 실제 Notion 태스크 추가 실행"""
    skip_keywords = {"바로추가", "바로 추가", "skip", ""}
    if category.lower() in skip_keywords:
        category = ""

    args = ["task_add", task_name]
    if event_date:
        args.append(event_date)
    if category and category_prop:
        args += ["--category-prop", category_prop, "--category", category]

    ok, out = run_script(
        ".claude/skills/notion-connector/scripts/notion-updater.py",
        args
    )
    if ok:
        date_str = f" ({event_date})" if event_date else ""
        cat_str = f" [{category}]" if category else ""
        say(f"✅ '{task_name}'{cat_str}{date_str} To-Do 리스트에 추가했습니다.")
    else:
        say(f"⚠️ 태스크 추가 실패:\n```{out}```")


def workflow_postpone(say, task_name: str, new_date: str):
    if not task_name:
        say("어떤 업무를 연기할까요?\n예: `@봇 주간보고 다음 주로 미뤄`")
        return

    page_id = find_page_id(task_name)
    if not page_id:
        say(f"'{task_name}' 업무를 Notion에서 찾지 못했습니다.")
        return

    ok, out = run_script(
        ".claude/skills/notion-connector/scripts/notion-updater.py",
        ["postpone", page_id, new_date]
    )
    if ok:
        say(f"📅 '{task_name}' {new_date}로 미뤘습니다.")
    else:
        say(f"⚠️ 연기 처리 실패:\n```{out}```")


# ──────────────────────────────────────────────────────────────
# Slack 이벤트 핸들러
# ──────────────────────────────────────────────────────────────

@app.event("app_mention")
def handle_mention(event, say):
    raw_text = event.get("text", "")
    user_text = re.sub(r"<@[A-Z0-9]+>", "", raw_text).strip()
    user_id = event.get("user", "")

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 멘션 수신: {user_text!r} (user={user_id})")

    # 태스크 추가 카테고리 대기 중이면 현재 메시지를 카테고리 답변으로 처리
    if user_id and user_id in _pending_task_add:
        pending = _pending_task_add.pop(user_id)
        print(f"  → 카테고리 답변 수신: '{user_text}' / pending={pending}")
        _execute_task_add(
            say,
            pending["task_name"],
            pending["date"],
            pending.get("category_prop"),
            user_text,
            pending.get("valid_categories", []),
        )
        return

    intent, info = classify_intent(user_text)
    print(f"  → 의도: {intent} / {info}")

    if intent == "reset_cache":
        (OUTPUT_DIR / "pattern_result.json").unlink(missing_ok=True)
        (OUTPUT_DIR / "verification_result.json").unlink(missing_ok=True)
        say("🔄 패턴 캐시를 삭제했습니다. 다시 브리핑을 요청하면 재분석합니다.")

    elif intent == "get_briefing":
        workflow_briefing(say)

    elif intent == "complete_task":
        workflow_complete(say, info.get("task_name", ""))

    elif intent == "postpone_task":
        workflow_postpone(say, info.get("task_name", ""), info.get("new_date", ""))

    elif intent == "add_calendar_event":
        title = info.get("title", "")
        ev_date = info.get("date", "")
        if title and ev_date:
            workflow_calendar_add(say, title, ev_date)
        elif title:
            say(f"'{title}'을 캘린더에 추가할 날짜를 알려주세요.")
        else:
            say("캘린더에 추가할 일정명을 알려주세요.")

    elif intent == "add_task":
        workflow_task_add(say, info.get("task_name", ""), info.get("date", ""), user_id)

    elif intent == "unknown":
        say(info.get("message", "요청을 이해하지 못했습니다. 다시 말씀해 주세요."))

    else:
        say(
            "요청을 이해하지 못했습니다.\n\n사용 가능한 명령:\n"
            "• `@봇 [업무명] to-do에 추가해줘` — 업무 추가\n"
            "• `@봇 [업무명] 완료했어` — 완료 처리\n"
            "• `@봇 [업무명] 미뤄 [날짜]` — 연기\n"
            "• `@봇 [업무명] 캘린더에 추가 [날짜]` — 캘린더 추가\n"
            "• `@봇 브리핑` — 업무 현황\n"
            "• `@봇 패턴 갱신` — 재분석"
        )


# ──────────────────────────────────────────────────────────────
# 진입점
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not app_token:
        print("오류: SLACK_APP_TOKEN이 .env에 없습니다.", file=sys.stderr)
        print("Slack App → Settings → Socket Mode에서 App-Level Token을 생성하세요.", file=sys.stderr)
        sys.exit(1)

    OUTPUT_DIR.mkdir(exist_ok=True)
    print("=" * 50)
    print("업무비서 리스너 시작")
    print(f"출력 경로: {OUTPUT_DIR}")
    print(f"LLM: {OLLAMA_MODEL} @ {OLLAMA_URL}")
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3, verify=False)
        models = [m["name"] for m in r.json().get("models", [])]
        if OLLAMA_MODEL in models:
            print(f"  → Ollama 연결 OK (모델 준비됨)")
        else:
            print(f"  → 경고: {OLLAMA_MODEL} 없음. 정규식 모드로 폴백합니다.")
            print(f"     설치: ollama pull {OLLAMA_MODEL}")
    except Exception:
        print("  → 경고: Ollama 연결 실패. 정규식 모드로 폴백합니다.")
    print("Slack 채널에서 @봇 멘션으로 호출하세요.")
    print("종료: Ctrl+C")
    print("=" * 50)

    handler = SocketModeHandler(app, app_token)
    handler.start()
