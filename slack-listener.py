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
import uuid
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    _scheduler = BackgroundScheduler(timezone="Asia/Seoul")
    _HAS_SCHEDULER = True
except ImportError:
    _scheduler = None
    _HAS_SCHEDULER = False

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
            "name": "get_today_tasks",
            "description": (
                "Notion TO DO LIST에서 특정 날짜의 할 일을 조회해서 보여준다. "
                "'오늘 할 일', '내일 할 일', '6월10일 업무', '다음주 월요일 할 일', "
                "'to-do list 가져와', '오늘 업무 리스트', '오늘 태스크 목록' 등. "
                "날짜를 지정하지 않거나 '오늘'이면 target_date를 빈 문자열로 반환한다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_date": {
                        "type": "string",
                        "description": "조회할 날짜 YYYY-MM-DD. 오늘이거나 날짜 미지정이면 빈 문자열.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_briefing",
            "description": (
                "정기 업무 패턴을 분석하여 다가오는 반복 업무를 브리핑한다. "
                "'뭐 있어', '브리핑해줘', '이번주 뭐해', '업무 현황 알려줘' 등. "
                "단순 TO DO LIST 조회가 아닌 패턴 분석·예측 요청일 때만 사용한다."
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
            "name": "run_bat",
            "description": (
                "외부 BAT 파일 또는 프로그램을 실행한다. "
                "'멀티에이전트 실행', '서버 켜줘', 'RUN.BAT 실행', 'PM 시작' 등."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "실행 대상 키워드 (예: multi_agent)"},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_recurring_task",
            "description": (
                "매주/매달/매일 반복되는 업무를 Notion TO DO LIST에 자동 등록한다. "
                "'매주 월요일 X 등록해줘', '매달 1일 Y 업무 추가', '매일 아침 Z 등록' 등 "
                "정기·반복·자동 등록 요청일 때. "
                "단순 일회성 업무 추가는 add_task를 사용한다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_name": {
                        "type": "string",
                        "description": "정기 등록할 업무명",
                    },
                    "recurrence": {
                        "type": "string",
                        "enum": ["daily", "weekly", "monthly"],
                        "description": "반복 주기: daily(매일)/weekly(매주)/monthly(매달)",
                    },
                    "day_of_week": {
                        "type": "string",
                        "description": "recurrence=weekly일 때 요일 영어 약자: mon/tue/wed/thu/fri/sat/sun. 한국어(월화수목금토일)를 변환하여 입력.",
                    },
                    "day_of_month": {
                        "type": "integer",
                        "description": "recurrence=monthly일 때 날짜(1~31)",
                    },
                },
                "required": ["task_name", "recurrence"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_recurring_tasks",
            "description": "등록된 정기 업무 자동 등록 목록을 조회한다. '정기 업무 목록', '자동 등록 목록' 등.",
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


def _extract_task_name(text: str) -> str:
    """텍스트에서 업무명 추출 (날짜·액션동사 제거)"""
    t = text
    # 1. 날짜 표현 제거 (공백 포함)
    t = re.sub(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", "", t)
    t = re.sub(r"\d{1,2}[월/]\s*\d{1,2}\s*일?\s*에?\s*", "", t)
    t = re.sub(r"(?:오늘|내일|모레|이번\s*주|다음\s*주)\s*", "", t)
    # 2. "to-do list에 추가해줘" 등 액션+목적지 접미어 제거 (긴 패턴 먼저)
    t = re.sub(
        r"\s*(?:to[-\s]?do\s*(?:list)?|투두\s*리스트?|할\s*일\s*목록)\s*에?\s*(?:추가|등록|넣어)(?:줘|주세요|해줘|해주세요)?\s*$",
        "", t, flags=re.IGNORECASE
    )
    t = re.sub(
        r"\s*(?:to[-\s]?do|투두)\s*에?\s*(?:추가|등록|넣어)(?:줘|주세요|해줘|해주세요)?\s*$",
        "", t, flags=re.IGNORECASE
    )
    t = re.sub(r"\s*(?:to[-\s]?do\s*(?:list)?|투두|할\s*일|리스트)\s*$", "", t, flags=re.IGNORECASE)
    # 3. "업무/일정 추가해줘" → "업무/일정" 도 함께 제거 (단순 수식어인 경우)
    t = re.sub(r"\s*(?:업무|일정)\s*(?:추가|등록|넣어)(?:줘|주세요|해줘|해주세요)\s*$", "", t, flags=re.IGNORECASE)
    # 4. 순수 액션 동사 제거 ("추가해줘", "등록해줘", "넣어줘")
    t = re.sub(r"\s*(?:추가|등록|넣어)(?:줘|주세요|해줘|해주세요)\s*$", "", t, flags=re.IGNORECASE)
    # 5. 액션 동사 제거 후 남은 단독 "업무/일정" 접미사 제거
    t = re.sub(r"\s+(?:업무|일정)\s*$", "", t)
    return t.strip()


def _quick_classify(text: str) -> tuple[str, dict] | None:
    """LLM 호출 전 명확한 패턴을 즉시 분류 (오분류 방지)"""
    if re.search(r"멀티.?에이전트|multi.?agent|run\.bat|PM\s*시작|서버\s*켜", text, re.IGNORECASE):
        return "run_bat", {"target": "multi_agent"}
    # 정기 업무 목록 조회
    if re.search(r"정기\s*업무\s*목록|자동\s*등록\s*목록|반복\s*업무\s*목록", text, re.IGNORECASE):
        return "list_recurring_tasks", {}
    # "매주/매달/매일 + 등록/추가" → schedule_recurring_task (regex 직접 처리, LLM보다 정확)
    if re.search(r"매주|매달|매월|매일|정기\s*등록|자동\s*등록|반복\s*등록", text, re.IGNORECASE):
        if re.search(r"등록|추가|넣어|to.?do|할\s*일", text, re.IGNORECASE):
            recurrence = (
                "weekly" if re.search(r"매주", text)
                else "monthly" if re.search(r"매달|매월", text)
                else "daily"
            )
            dow = _parse_day_of_week(text)
            dom_m = re.search(r"(\d+)\s*일", text)
            day_of_month = int(dom_m.group(1)) if dom_m and recurrence == "monthly" else 0
            task_name = _extract_recurring_task_name(text)
            return "schedule_recurring_task", {
                "task_name": task_name,
                "recurrence": recurrence,
                "day_of_week": dow,
                "day_of_month": day_of_month,
            }
    # 업무 추가 요청 — "추가/등록/넣어+줘" 로 끝나는 경우 add_task 우선 (get_today_tasks 오분류 방지)
    # 단, "완료/미뤄/캘린더" 키워드가 있으면 패스
    if not re.search(r"완료|했어|끝났어|미뤄|연기|캘린더", text, re.IGNORECASE):
        if re.search(r"(?:추가|등록|넣어)(?:줘|주세요|해줘|해주세요)", text, re.IGNORECASE):
            task_name = _extract_task_name(text)
            if task_name:
                return "add_task", {"task_name": task_name, "date": _parse_date_str(text)}
    # "TO DO LIST 가져와" / "할 일 보여줘" / "내일 할 일" 등 → get_today_tasks
    if (
        re.search(r"(?:to[-\s]?do|투두|할\s*일).{0,25}(?:가져|불러|조회|보여|알려)", text, re.IGNORECASE)
        or re.search(r"(?:오늘|내일|모레|다음\s*주).{0,20}(?:to[-\s]?do|할\s*일|투두|업무|일정).{0,20}(?:list|리스트|목록|가져|보여)?", text, re.IGNORECASE)
        or re.search(r"\d{1,2}[월/]\d{1,2}.{0,20}(?:to[-\s]?do|할\s*일|투두|업무)", text, re.IGNORECASE)
    ):
        target_date = _parse_date_str(text)
        return "get_today_tasks", {"target_date": target_date}
    return None


def classify_intent(text: str) -> tuple[str, dict]:
    quick = _quick_classify(text.strip())
    if quick:
        print(f"  → 빠른 분류 (pre-LLM): {quick[0]}")
        return quick
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
    # "today", "오늘" 처리
    if val.strip().lower() in ("today", "오늘"):
        return today.isoformat()
    # "tomorrow", "내일"
    if val.strip().lower() in ("tomorrow", "내일"):
        return (today + timedelta(days=1)).isoformat()
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
    if re.search(r"멀티.?에이전트|multi.?agent|run\.bat|PM\s*시작|서버\s*켜", text, re.IGNORECASE):
        return "run_bat", {"target": "multi_agent"}
    if re.search(r"정기\s*업무\s*목록|자동\s*등록\s*목록|반복\s*업무\s*목록", text, re.IGNORECASE):
        return "list_recurring_tasks", {}
    if re.search(r"매주|매달|매월|매일", text, re.IGNORECASE) and re.search(r"등록|추가|넣어", text, re.IGNORECASE):
        recurrence = "weekly" if re.search(r"매주", text) else ("monthly" if re.search(r"매달|매월", text) else "daily")
        dow = _parse_day_of_week(text)
        dom_m = re.search(r"(\d+)\s*일", text)
        return "schedule_recurring_task", {
            "task_name": _extract_recurring_task_name(text),
            "recurrence": recurrence,
            "day_of_week": dow,
            "day_of_month": int(dom_m.group(1)) if dom_m and recurrence == "monthly" else 0,
        }
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
    # add_task: "to-do/할 일" 키워드 없이 "추가/등록/넣어" 만으로도 인식
    if re.search(r"추가|등록|넣어", text, re.IGNORECASE):
        return "add_task", {"task_name": _extract_task_name(text), "date": _parse_date_str(text)}
    if (
        re.search(r"오늘.{0,15}(?:to[-\s]?do|할\s*일|투두|리스트|목록|업무|태스크)", text, re.IGNORECASE)
        or re.search(r"(?:to[-\s]?do|투두|할\s*일).{0,15}(?:가져|불러|조회|보여|알려)", text, re.IGNORECASE)
        or re.search(r"(?:내일|모레|다음\s*주).{0,20}(?:to[-\s]?do|할\s*일|투두|업무)", text, re.IGNORECASE)
        or re.search(r"\d{1,2}[월/]\d{1,2}.{0,20}(?:to[-\s]?do|할\s*일|투두|업무)", text, re.IGNORECASE)
    ):
        return "get_today_tasks", {"target_date": _parse_date_str(text)}
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
# 정기 업무 스케줄러
# ──────────────────────────────────────────────────────────────

_DOW_MAP = {
    "월": "mon", "월요일": "mon",
    "화": "tue", "화요일": "tue",
    "수": "wed", "수요일": "wed",
    "목": "thu", "목요일": "thu",
    "금": "fri", "금요일": "fri",
    "토": "sat", "토요일": "sat",
    "일": "sun", "일요일": "sun",
    "mon": "mon", "tue": "tue", "wed": "wed", "thu": "thu",
    "fri": "fri", "sat": "sat", "sun": "sun",
}
_DOW_KR = {
    "mon": "월요일", "tue": "화요일", "wed": "수요일",
    "thu": "목요일", "fri": "금요일", "sat": "토요일", "sun": "일요일",
}
_RECURRENCE_KR = {"daily": "매일", "weekly": "매주", "monthly": "매달"}

_SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "")


def _extract_recurring_task_name(text: str) -> str:
    """정기 등록 요청 텍스트에서 업무명 추출"""
    t = text
    t = re.sub(r"매주|매달|매월|매일", "", t)
    t = re.sub(r"[월화수목금토일]요일?", "", t)
    t = re.sub(r"\d{1,2}\s*일", "", t)
    t = re.sub(
        r"\s*(?:to[-\s]?do\s*(?:list)?|투두|할\s*일|리스트)\s*에?\s*(?:추가|등록|넣어)(?:줘|주세요|해줘|해주세요)?\s*$",
        "", t, flags=re.IGNORECASE
    )
    t = re.sub(r"\s*(?:추가|등록|넣어)(?:줘|주세요|해줘|해주세요)\s*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+(?:업무|일정)\s*$", "", t)
    return t.strip()


def _parse_day_of_week(text: str) -> str:
    # "N일"(날짜), "매일"(매일 반복)은 요일이 아니므로 제거
    cleaned = re.sub(r"\d+\s*일", "", text)
    cleaned = re.sub(r"매일", "", cleaned)
    # 긴 패턴부터 먼저 검색 (예: "월요일" → "월" 보다 먼저)
    for kr in sorted(_DOW_MAP, key=len, reverse=True):
        if kr in cleaned:
            return _DOW_MAP[kr]
    return ""


def _load_recurring_tasks() -> list[dict]:
    path = OUTPUT_DIR / "recurring_tasks.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_recurring_tasks(tasks: list[dict]):
    OUTPUT_DIR.mkdir(exist_ok=True)
    (OUTPUT_DIR / "recurring_tasks.json").write_text(
        json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _notify_slack(text: str):
    """스케줄러에서 Slack 채널에 메시지 전송"""
    if not _SLACK_CHANNEL_ID:
        return
    try:
        app.client.chat_postMessage(channel=_SLACK_CHANNEL_ID, text=text)
    except Exception as e:
        print(f"[스케줄러] Slack 알림 실패: {e}", file=sys.stderr)


def _scheduled_add_task(task_name: str):
    """APScheduler가 호출하는 Notion 자동 등록 함수"""
    today = date.today().isoformat()
    print(f"[스케줄러] '{task_name}' 자동 등록 시작 ({today})")
    try:
        resp = requests.post(
            "https://api.notion.com/v1/pages",
            headers=_NOTION_HEADERS,
            json={
                "parent": {"database_id": _NOTION_TASK_DB_ID},
                "properties": {
                    "이름": {"title": [{"text": {"content": task_name}}]},
                    "날짜": {"date": {"start": today}},
                },
            },
            verify=False,
            timeout=15,
        )
        resp.raise_for_status()
        _notify_slack(f"📋 *정기 업무 자동 등록*: _{task_name}_ ({today})")
        print(f"[스케줄러] '{task_name}' 등록 완료")
    except Exception as e:
        print(f"[스케줄러] '{task_name}' 등록 실패: {e}", file=sys.stderr)
        _notify_slack(f"⚠️ 정기 업무 자동 등록 실패: _{task_name}_\n`{e}`")


def _register_scheduler_job(task: dict):
    if not _HAS_SCHEDULER:
        return
    job_id = task["job_id"]
    task_name = task["task_name"]
    recurrence = task["recurrence"]
    try:
        if recurrence == "daily":
            _scheduler.add_job(
                _scheduled_add_task, "cron", hour=9, minute=0,
                args=[task_name], id=job_id, replace_existing=True,
            )
        elif recurrence == "weekly":
            dow = task.get("day_of_week", "mon")
            _scheduler.add_job(
                _scheduled_add_task, "cron", day_of_week=dow, hour=9, minute=0,
                args=[task_name], id=job_id, replace_existing=True,
            )
        elif recurrence == "monthly":
            dom = task.get("day_of_month") or 1
            _scheduler.add_job(
                _scheduled_add_task, "cron", day=dom, hour=9, minute=0,
                args=[task_name], id=job_id, replace_existing=True,
            )
        print(f"[스케줄러] job 등록: '{task_name}' ({recurrence})")
    except Exception as e:
        print(f"[스케줄러] job 등록 실패: {e}", file=sys.stderr)


def _check_recurring_missing(info: dict) -> dict | None:
    """schedule_recurring_task에서 누락된 필드 반환. 없으면 None."""
    if not info.get("task_name", "").strip():
        return {"missing_field": "task_name", "question": "어떤 업무를 정기 등록할까요?\n업무명을 알려주세요."}
    if not info.get("recurrence"):
        return {"missing_field": "recurrence", "question": "매주, 매달, 매일 중 어떤 주기로 등록할까요?"}
    if info.get("recurrence") == "weekly" and not info.get("day_of_week"):
        return {
            "missing_field": "day_of_week",
            "question": "매주 무슨 요일에 등록할까요?\n(예: 월요일, 화요일, 수요일, 목요일, 금요일)",
        }
    if info.get("recurrence") == "monthly" and not info.get("day_of_month"):
        return {"missing_field": "day_of_month", "question": "매월 며칠에 등록할까요? (예: 1, 15, 25)"}
    return None


def workflow_schedule_recurring_task(
    say, task_name: str, recurrence: str,
    day_of_week: str = "", day_of_month: int = 0, user_id: str = "",
):
    job_id = f"recurring_{uuid.uuid4().hex[:8]}"
    task = {
        "job_id": job_id,
        "task_name": task_name.strip(),
        "recurrence": recurrence,
        "day_of_week": day_of_week.lower(),
        "day_of_month": int(day_of_month) if day_of_month else 0,
        "created_at": datetime.now().isoformat(),
    }
    existing = _load_recurring_tasks()
    existing.append(task)
    _save_recurring_tasks(existing)
    _register_scheduler_job(task)

    if recurrence == "weekly":
        dow_kr = _DOW_KR.get(day_of_week.lower(), day_of_week)
        schedule_str = f"매주 *{dow_kr}*"
    elif recurrence == "monthly":
        schedule_str = f"매달 *{day_of_month}일*"
    else:
        schedule_str = "매일"

    say(
        f"✅ *'{task_name}'* 업무를 {schedule_str} 오전 9시에 TO DO LIST에 자동 등록합니다.\n"
        f"등록된 정기 업무 전체 목록: `@봇 정기 업무 목록`"
    )


def workflow_list_recurring_tasks(say):
    tasks = _load_recurring_tasks()
    if not tasks:
        say("📋 등록된 정기 자동 업무가 없습니다.")
        return
    lines = [f"🔄 *정기 자동 등록 업무 목록* ({len(tasks)}건)\n"]
    for t in tasks:
        rec = t.get("recurrence", "")
        if rec == "weekly":
            dow_kr = _DOW_KR.get(t.get("day_of_week", ""), t.get("day_of_week", ""))
            sched = f"매주 {dow_kr}"
        elif rec == "monthly":
            sched = f"매달 {t.get('day_of_month', '?')}일"
        else:
            sched = "매일"
        lines.append(f"• *{t['task_name']}* — {sched} 오전 9시")
    say("\n".join(lines))


# ──────────────────────────────────────────────────────────────
# 태스크 추가 — 카테고리 확인 대기 상태
# ──────────────────────────────────────────────────────────────

# user_id → {task_name, date, category_prop}
_pending_task_add: dict[str, dict] = {}

# user_id → {action, task_name, new_date}
_pending_confirmation: dict[str, dict] = {}

# user_id → {intent, info, missing_field, question}
_pending_clarification: dict[str, dict] = {}

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


def _do_complete(say, task_name: str):
    """확인 없이 즉시 Notion 완료 처리"""
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


def workflow_complete(say, task_name: str, user_id: str = ""):
    if not task_name:
        say("어떤 업무를 완료하셨나요?\n예: `@봇 주간보고 완료했어`")
        return
    if user_id:
        _pending_confirmation[user_id] = {"action": "complete", "task_name": task_name}
        say(f"✅ *'{task_name}'* 을(를) 완료 처리할까요?\n`@봇 네` / `@봇 아니오`")
    else:
        _do_complete(say, task_name)


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
    # 카테고리 확인 없이 즉시 등록 (사용자 경험 개선)
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


def _fetch_all_notion_tasks() -> list[dict]:
    """Notion Task DB에서 전체 태스크 페이지 조회 (페이지네이션 포함)"""
    results = []
    payload: dict = {"page_size": 100}
    url = f"https://api.notion.com/v1/databases/{_NOTION_TASK_DB_ID}/query"
    while True:
        try:
            resp = requests.post(url, headers=_NOTION_HEADERS, json=payload, verify=False, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            results.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            payload["start_cursor"] = data["next_cursor"]
        except Exception as e:
            print(f"Notion 태스크 전체 조회 실패: {e}", file=sys.stderr)
            break
    return results


def _parse_notion_page(page: dict) -> dict:
    """Notion 페이지 properties에서 태스크 정보 추출"""
    props = page.get("properties", {})

    title = ""
    for prop in props.values():
        if prop.get("type") == "title":
            title = "".join(t.get("plain_text", "") for t in prop.get("title", []))
            break

    task_date = None
    task_date_end = None
    for prop in props.values():
        if prop.get("type") == "date" and prop.get("date"):
            task_date = prop["date"].get("start", "")[:10]
            raw_end = prop["date"].get("end", "")
            task_date_end = raw_end[:10] if raw_end else None
            break

    status = ""
    for name, prop in props.items():
        ptype = prop.get("type", "")
        if ptype == "checkbox":
            status = "완료" if prop.get("checkbox") else ""
            break
        if name.lower() in ("상태", "status") and ptype in ("status", "select"):
            status = (prop.get(ptype) or {}).get("name", "")
            break

    categories = []
    for prop in props.values():
        if prop.get("type") == "multi_select":
            categories = [opt["name"] for opt in prop.get("multi_select", [])]
            break
        if prop.get("type") == "select" and prop.get("select"):
            categories = [prop["select"]["name"]]
            break

    return {
        "id": page["id"],
        "title": title,
        "date": task_date,
        "date_end": task_date_end,
        "status": status,
        "categories": categories,
    }


_DONE_STATUSES = {"완료", "done", "completed", "closed", "archived"}


def workflow_get_today_tasks(say, target_date: str = ""):
    """TO DO LIST 조회 후 Slack 전송.
    target_date 지정 시 해당 날짜 업무만, 미지정 시 완료·기한초과 제외 전체.
    """
    say("잠깐만요, TO DO LIST를 가져오는 중입니다... ⏳")

    pages = _fetch_all_notion_tasks()
    if not pages:
        say("⚠️ Notion에서 태스크를 가져오지 못했습니다. 토큰과 DB ID를 확인해 주세요.")
        return

    today = date.today().isoformat()
    tasks = []

    for page in pages:
        info = _parse_notion_page(page)
        if not info["title"]:
            continue
        if info["status"].lower() in _DONE_STATUSES:
            continue

        task_start = info["date"]
        task_end = info.get("date_end")

        if target_date:
            # 특정 날짜 지정: 해당 날짜를 포함하는 업무만
            if task_start and task_end:
                # 날짜 범위 업무: target_date가 범위 내에 있으면 포함
                if not (task_start <= target_date <= task_end):
                    continue
            elif task_start:
                if task_start != target_date:
                    continue
            else:
                # 날짜 없는 태스크는 날짜 지정 조회에서 제외
                continue
        else:
            # 날짜 미지정: 기한 초과 제외
            if task_start and task_end:
                # 범위 업무: 종료일이 오늘 이후면 포함
                if task_end < today:
                    continue
            elif task_start:
                if task_start < today:
                    continue

        tasks.append(info)

    if not tasks:
        if target_date:
            say(f"✅ *{target_date}에 등록된 TO DO LIST가 없습니다.*")
        else:
            say("✅ *현재 등록된 TO DO LIST가 없습니다.* 수고하셨습니다! 🎉")
        return

    label = target_date if target_date else f"{today} 기준"
    lines = [f"📋 *TO DO LIST ({label}, 총 {len(tasks)}건)*\n"]
    for t in tasks:
        cat = f"  `{'` `'.join(t['categories'])}`" if t["categories"] else ""
        if t.get("date_end"):
            date_str = f" _({t['date']} ~ {t['date_end']})_"
        elif t["date"]:
            date_str = f" _({t['date']})_"
        else:
            date_str = ""
        lines.append(f"• {t['title']}{date_str}{cat}")

    say("\n".join(lines))


BAT_TARGETS = {
    "multi_agent": r"C:\Users\glpark0413\Desktop\업무 자동화-20260521T024618Z-3-001\업무 자동화\실 업무\Multi_agent\RUN.BAT",
}


def workflow_run_bat(say, target: str):
    bat_path = BAT_TARGETS.get(target)
    if not bat_path:
        say(f"⚠️ 등록되지 않은 실행 대상입니다: `{target}`")
        return

    from pathlib import Path as _Path
    if not _Path(bat_path).exists():
        say(f"⚠️ 파일을 찾을 수 없습니다:\n`{bat_path}`")
        return

    try:
        subprocess.Popen(
            ["cmd", "/c", bat_path],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            cwd=str(_Path(bat_path).parent),
        )
        say(
            "✅ *멀티에이전트 PM* 을 시작했습니다.\n"
            "잠시 후 <http://localhost:5050|http://localhost:5050> 에서 접속하세요."
        )
    except Exception as e:
        say(f"⚠️ 실행 실패: `{e}`")


def _do_postpone(say, task_name: str, new_date: str):
    """확인 없이 즉시 Notion 연기 처리"""
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


def workflow_postpone(say, task_name: str, new_date: str, user_id: str = ""):
    if not task_name:
        if user_id:
            _pending_clarification[user_id] = {
                "intent": "postpone_task",
                "info": {"task_name": "", "new_date": new_date},
                "missing_field": "task_name",
                "question": "어떤 업무를 연기할까요?",
            }
            say("어떤 업무를 연기할까요?")
        else:
            say("어떤 업무를 연기할까요?\n예: `@봇 주간보고 다음 주로 미뤄`")
        return
    if not new_date and user_id:
        _pending_clarification[user_id] = {
            "intent": "postpone_task",
            "info": {"task_name": task_name, "new_date": ""},
            "missing_field": "new_date",
            "question": f"*'{task_name}'* 을(를) 언제로 미룰까요?\n예: `내일`, `다음주 월요일`, `6월 15일`",
        }
        say(f"*'{task_name}'* 을(를) 언제로 미룰까요?\n예: `내일`, `다음주 월요일`, `6월 15일`")
        return
    if user_id:
        _pending_confirmation[user_id] = {
            "action": "postpone", "task_name": task_name, "new_date": new_date
        }
        date_str = f" ({new_date})" if new_date else ""
        say(f"📅 *'{task_name}'* 을(를){date_str}로 미룰까요?\n`@봇 네` / `@봇 아니오`")
    else:
        _do_postpone(say, task_name, new_date)


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

    # 누락 정보 보완 대기 중이면 현재 메시지로 빈 필드를 채움
    if user_id and user_id in _pending_clarification:
        pending = _pending_clarification.pop(user_id)
        intent = pending["intent"]
        info = pending["info"]
        missing_field = pending["missing_field"]
        print(f"  → 보완 답변 수신: '{user_text}' / field={missing_field}")

        if missing_field == "task_name":
            info["task_name"] = user_text.strip()
        elif missing_field == "recurrence":
            if re.search(r"매주|주간|weekly", user_text, re.IGNORECASE):
                info["recurrence"] = "weekly"
            elif re.search(r"매달|매월|monthly|월간", user_text, re.IGNORECASE):
                info["recurrence"] = "monthly"
            elif re.search(r"매일|daily", user_text, re.IGNORECASE):
                info["recurrence"] = "daily"
            else:
                _pending_clarification[user_id] = pending
                say("매주, 매달, 매일 중 하나로 알려주세요.")
                return
        elif missing_field == "day_of_week":
            dow = _parse_day_of_week(user_text)
            if dow:
                info["day_of_week"] = dow
            else:
                _pending_clarification[user_id] = pending
                say("요일을 알려주세요. (예: 월요일, 화요일, 수요일, 목요일, 금요일)")
                return
        elif missing_field == "day_of_month":
            m = re.search(r"\d+", user_text)
            if m:
                info["day_of_month"] = int(m.group())
            else:
                _pending_clarification[user_id] = pending
                say("숫자로 날짜를 알려주세요. (예: 1, 15, 25)")
                return
        elif missing_field == "new_date":
            parsed = _parse_date_str(user_text)
            info["new_date"] = parsed if parsed else user_text.strip()

        # schedule_recurring_task: 아직 빠진 필드 있으면 재질문
        if intent == "schedule_recurring_task":
            still_missing = _check_recurring_missing(info)
            if still_missing:
                _pending_clarification[user_id] = {
                    "intent": intent, "info": info, **still_missing
                }
                say(still_missing["question"])
                return

        _dispatch_intent(say, intent, info, user_id)
        return

    # 완료·연기 확인 대기 중이면 현재 메시지를 확인 답변으로 처리
    if user_id and user_id in _pending_confirmation:
        pending = _pending_confirmation.pop(user_id)
        print(f"  → 확인 답변 수신: '{user_text}' / pending={pending}")
        is_yes = re.search(r"^(네|예|응|ㅇ|ok|yes|확인|맞아|맞습니다)", user_text, re.IGNORECASE)
        is_no  = re.search(r"^(아니|아니오|취소|no|nope|ㄴ)", user_text, re.IGNORECASE)
        if is_yes:
            if pending["action"] == "complete":
                _do_complete(say, pending["task_name"])
            elif pending["action"] == "postpone":
                _do_postpone(say, pending["task_name"], pending.get("new_date", ""))
        elif is_no:
            say(f"❌ *'{pending['task_name']}'* 처리를 취소했습니다.")
        else:
            # 관련 없는 명령 → 취소 알림 후 새 명령으로 계속 처리
            say(
                f"⚠️ 확인 대기 중이던 *'{pending['task_name']}'* 처리를 취소했습니다.\n"
                "새 명령을 처리합니다..."
            )
            # fall-through: return 하지 않고 아래 intent 분류로 계속 진행
            intent, info = classify_intent(user_text)
            print(f"  → 의도(재분류): {intent} / {info}")
            # intent 디스패치를 위해 아래 로직을 재사용하도록 goto 대신 재귀 호출
            _dispatch_intent(say, intent, info, user_id)
        return

    intent, info = classify_intent(user_text)
    print(f"  → 의도: {intent} / {info}")
    _dispatch_intent(say, intent, info, user_id)


def _dispatch_intent(say, intent: str, info: dict, user_id: str = ""):
    """분류된 intent를 실제 워크플로우로 라우팅"""
    if intent == "reset_cache":
        (OUTPUT_DIR / "pattern_result.json").unlink(missing_ok=True)
        (OUTPUT_DIR / "verification_result.json").unlink(missing_ok=True)
        say("🔄 패턴 캐시를 삭제했습니다. 다시 브리핑을 요청하면 재분석합니다.")

    elif intent == "get_today_tasks":
        workflow_get_today_tasks(say, info.get("target_date", ""))

    elif intent == "get_briefing":
        workflow_briefing(say)

    elif intent == "complete_task":
        workflow_complete(say, info.get("task_name", ""), user_id)

    elif intent == "postpone_task":
        workflow_postpone(say, info.get("task_name", ""), info.get("new_date", ""), user_id)

    elif intent == "add_calendar_event":
        title = info.get("title", "")
        ev_date = info.get("date", "")
        if title and ev_date:
            workflow_calendar_add(say, title, ev_date)
        elif title:
            say(f"'{title}'을 캘린더에 추가할 날짜를 알려주세요.")
        else:
            say("캘린더에 추가할 일정명을 알려주세요.")

    elif intent == "run_bat":
        workflow_run_bat(say, info.get("target", "multi_agent"))

    elif intent == "add_task":
        workflow_task_add(say, info.get("task_name", ""), info.get("date", ""), user_id)

    elif intent == "schedule_recurring_task":
        # 누락 필드 있으면 먼저 질문
        missing = _check_recurring_missing(info)
        if missing and user_id:
            _pending_clarification[user_id] = {
                "intent": intent, "info": info, **missing
            }
            say(missing["question"])
        else:
            workflow_schedule_recurring_task(
                say,
                task_name=info.get("task_name", ""),
                recurrence=info.get("recurrence", ""),
                day_of_week=info.get("day_of_week", ""),
                day_of_month=info.get("day_of_month", 0),
                user_id=user_id,
            )

    elif intent == "list_recurring_tasks":
        workflow_list_recurring_tasks(say)

    elif intent == "unknown":
        say(info.get("message", "요청을 이해하지 못했습니다. 다시 말씀해 주세요."))

    else:
        say(
            "요청을 이해하지 못했습니다.\n\n사용 가능한 명령:\n"
            "• `@봇 [업무명] to-do에 추가해줘` — 업무 추가\n"
            "• `@봇 매주 월요일 [업무명] 등록해줘` — 정기 업무 자동 등록\n"
            "• `@봇 정기 업무 목록` — 정기 업무 확인\n"
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

    # APScheduler 시작 및 기존 정기 업무 복원
    if _HAS_SCHEDULER:
        _scheduler.start()
        existing_recurring = _load_recurring_tasks()
        if existing_recurring:
            for t in existing_recurring:
                _register_scheduler_job(t)
            print(f"[스케줄러] 정기 업무 {len(existing_recurring)}건 복원 완료")
        else:
            print("[스케줄러] 등록된 정기 업무 없음")
    else:
        print("[스케줄러] APScheduler 없음 — pip install apscheduler")

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
