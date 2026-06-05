# Skill: notion-connector

## 역할
Notion API를 통한 데이터 읽기/쓰기 전담 스킬.

## 담당 스크립트

| 스크립트 | 용도 |
|----------|------|
| `scripts/notion-fetcher.py` | 태스크 DB 전체 조회 + 향후 14일 캘린더 조회 |
| `scripts/notion-updater.py` | 상태 완료 처리 / 날짜 연기 / 캘린더 일정 추가 |

## 호출 규칙

### 데이터 수집 (Step 2)
```
python .claude/skills/notion-connector/scripts/notion-fetcher.py
```
- 성공 시 `/output/notion_tasks.json`, `/output/notion_calendar.json` 생성
- 실패(exit code ≠ 0) 시: 슬랙에 "노션 연결에 실패했습니다. 잠시 후 다시 시도해주세요" 전송 후 중단

### 완료 처리 (Step 7A)
```
python .claude/skills/notion-connector/scripts/notion-updater.py complete <page_id>
```

### 연기 처리 (Step 7B)
```
python .claude/skills/notion-connector/scripts/notion-updater.py postpone <page_id> <YYYY-MM-DD>
```
- 날짜 파싱은 LLM이 먼저 수행한 후 이 스크립트에 전달

### 캘린더 일정 추가 (Step 7C)
```
python .claude/skills/notion-connector/scripts/notion-updater.py calendar_add "<title>" <YYYY-MM-DD>
```

## 출력 파일 스키마

### notion_tasks.json
```json
{
  "fetched_at": "<ISO datetime>",
  "tasks": [
    {
      "id": "<notion_page_id>",
      "name": "<string>",
      "status": "<string>",
      "date_property": "<속성명 | null>",
      "date": "<YYYY-MM-DD | null>",
      "date_end": "<YYYY-MM-DD | null>",
      "url": "<string>",
      "created_time": "<ISO datetime>",
      "last_edited_time": "<ISO datetime>"
    }
  ]
}
```

### notion_calendar.json
```json
{
  "fetched_at": "<ISO datetime>",
  "events": [ /* 동일 구조 */ ]
}
```

## 환경변수 (필수)
- `NOTION_TOKEN`: Notion Internal Integration Token
- `NOTION_TASK_DB_ID`: 태스크 데이터베이스 ID
- `NOTION_CALENDAR_DB_ID`: 캘린더 데이터베이스 ID (미설정 시 캘린더 기능 비활성)
