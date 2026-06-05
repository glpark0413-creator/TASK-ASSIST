# Skill: calendar-checker

## 역할
정기 업무 후보와 Notion 캘린더를 교차 검증하여 등록 여부 및 긴급도(D-N)를 계산한다.

## 처리 구조

| 처리 주체 | 담당 |
|----------|------|
| 스크립트 | 완전 일치 비교, D-N 계산, 파일 저장 |
| LLM | `similar_calendar_candidates` 목록을 보고 유사 제목 여부 최종 판단 |

## 호출 순서

### 1단계: 스크립트 실행
```
python .claude/skills/calendar-checker/scripts/calendar-checker.py
```
- 입력: `/output/pattern_result.json`, `/output/notion_calendar.json`
- 출력: `/output/verification_result.json`
- 각 태스크에 `calendar_registered`, `days_until_due`, `urgency`, `similar_calendar_candidates` 포함

### 2단계: LLM 유사 제목 판단
`similar_calendar_candidates` 배열이 비어 있지 않은 항목에 대해 LLM이 판단:
- "주간 팀 미팅" vs "팀 위클리" → 동일 업무로 판단 → `calendar_registered: true`로 수정
- 판단 기준: 핵심 주제어(팀, 회의, 리뷰 등)가 동일하면 유사로 간주
- 판단 후 `verification_result.json`의 해당 항목 `calendar_registered` 값을 업데이트한다.

## 출력 파일 스키마

```json
{
  "verified_at": "<ISO datetime>",
  "tasks": [
    {
      "name": "<string>",
      "confidence": 85,
      "recurrence": "<string>",
      "basis": "<string>",
      "last_done": "<YYYY-MM-DD | null>",
      "next_expected": "<YYYY-MM-DD | null>",
      "days_until_due": 3,
      "urgency": "D-3",
      "calendar_registered": false,
      "similar_calendar_candidates": ["<캘린더 이벤트 이름>"],
      "notion_page_id": "<string>"
    }
  ]
}
```

## urgency 값 규칙

| 값 | 조건 |
|----|------|
| `D-0` | 오늘이 예정일 |
| `D-N` | N일 후가 예정일 |
| `기한초과` | 예정일이 과거 |
| `날짜없음` | next_expected가 null |

## 실패 처리
- 스크립트 실패 시: 스킵 + `/output/agent.log` 기록. 브리핑은 pattern_result.json만으로 생성 가능.
