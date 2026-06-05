# Skill: slack-messenger

## 역할
LLM이 생성한 브리핑 텍스트를 Slack 채널/DM으로 전송한다.

## 호출 방법

### 브리핑 전송 (Step 5)
```
python .claude/skills/slack-messenger/scripts/slack-sender.py "<브리핑 텍스트>"
```
- 브리핑 텍스트를 인자로 전달. LLM이 생성한 완성 메시지를 그대로 전달한다.
- 인자 생략 시: `/output/verification_result.json` 기반으로 기본 메시지 자동 생성 (fallback)

### 에러 메시지 전송
```
python .claude/skills/slack-messenger/scripts/slack-sender.py "⚠️ <오류 내용>"
```

## 브리핑 메시지 작성 규칙 (LLM 생성 시 준수)

```
📅 다가오는 정기 업무 리스트

• [긴급/D-1] {업무명} (이전 수행일: {날짜})
• [예정/D-3] {업무명} (캘린더 등록: O/X) (신뢰도 65%)

💡 비서의 제안
• "{업무명}"은 매월 {시기}에 진행하셨습니다. 캘린더에 추가할까요?

[Action Required]
완료 처리하거나 캘린더에 추가할 항목이 있으면 말씀해 주세요.
```

**정렬 순서**: D-0 → D-1 → D-3 → D-7 이내 → 캘린더 미등록 → 날짜없음 → 기한초과  
**신뢰도 표시**: 80% 이상은 생략, 80% 미만은 `(신뢰도 N%)` 병기  
**후보 0건**: "이번 주 정기 업무 패턴을 찾지 못했습니다. 아직 데이터가 부족할 수 있어요."

## 재시도 정책
- 최대 2회 재시도
- 재시도 후에도 실패 시: `/output/agent.log`에 기록하고 중단

## 환경변수 (필수)
- `SLACK_BOT_TOKEN`: Slack Bot User OAuth Token (xoxb-...)
- `SLACK_CHANNEL_ID`: 메시지를 전송할 채널 또는 DM ID
