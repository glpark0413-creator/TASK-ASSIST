# Skill: pattern-analyzer

## 역할
Notion 태스크 이력에서 정기 업무 패턴을 추론하고 신뢰도를 부여한다.  
**처리 주체: LLM** (스크립트 없음 — 이 스킬은 LLM에게 추론 지침을 제공한다)

## 입력
- `/output/notion_tasks.json` (notion-connector 스킬이 생성)
- `references/pattern-keywords.md` (정기업무 판단 키워드 사전)
- 오늘 날짜

## 출력
- `/output/pattern_result.json`

## LLM 처리 절차

1. `references/pattern-keywords.md`를 읽어 판단 기준을 숙지한다.
2. `notion_tasks.json`의 모든 태스크를 분석한다:
   - 제목에 정기 키워드 포함 여부
   - 동일/유사 제목의 반복 등장 횟수
   - 날짜 속성이 있는 경우 간격 계산 (7/14/30/90일 ±2일)
3. 정기 업무 후보 목록과 각각의 신뢰도, 추론 근거를 작성한다.
4. **자기 검증**: 추론 근거가 실제 데이터에 존재하는지 재확인 후 수정한다.
5. `pattern_result.json` 형식으로 `/output/` 에 저장한다.

## 신뢰도 기준

| 근거 | 신뢰도 범위 |
|------|------------|
| 날짜 간격 + 키워드 모두 있음 | 80–95% |
| 날짜 간격만 있음 | 60–80% |
| 키워드만 있음 (날짜 없음) | 40–60% |
| 단순 유사 제목 2회 | 30–50% |

신뢰도 50% 미만 항목은 브리핑에서 "(추정, 확인 필요)" 태그가 붙는다.

## 출력 파일 스키마

```json
{
  "analyzed_at": "<ISO datetime>",
  "candidate_tasks": [
    {
      "name": "<string>",
      "confidence": 85,
      "recurrence": "weekly | biweekly | monthly | quarterly | annually | unknown",
      "basis": "<추론 근거 한 줄 설명>",
      "last_done": "<YYYY-MM-DD | null>",
      "next_expected": "<YYYY-MM-DD | null>",
      "notion_page_id": "<string | null>"
    }
  ]
}
```

## 실패 처리
- JSON 스키마 검증 실패 시: 프롬프트를 재실행하여 1회 재시도
- 재시도 실패 시: 빈 `candidate_tasks: []`로 진행하고 `/output/agent.log`에 기록
