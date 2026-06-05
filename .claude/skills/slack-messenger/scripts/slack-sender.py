"""
verification_result.json을 읽어 Slack 브리핑 메시지를 전송
사용법: python slack-sender.py <briefing_text>
        briefing_text가 없으면 verification_result.json을 직접 읽어 자동 생성
"""

import json
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL_ID = os.environ["SLACK_CHANNEL_ID"]

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "output")
MAX_RETRIES = 2


def load_json(filename: str) -> dict:
    path = os.path.join(OUTPUT_DIR, filename)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_message_from_result(data: dict) -> str:
    """verification_result.json 기반 기본 브리핑 메시지 생성 (LLM 생성 실패 시 fallback)"""
    tasks = data.get("tasks", [])
    if not tasks:
        return "이번 주 정기 업무 패턴을 찾지 못했습니다. 아직 데이터가 부족할 수 있어요."

    lines = ["📅 *다가오는 정기 업무 리스트*\n"]
    suggestions = []

    for t in tasks:
        name = t.get("name", "")
        urgency = t.get("urgency", "")
        confidence = t.get("confidence", 100)
        calendar_registered = t.get("calendar_registered", True)
        last_done = t.get("last_done", "")

        conf_tag = f" (신뢰도 {confidence}%)" if confidence and confidence < 80 else ""
        cal_tag = f" (캘린더 등록: {'O' if calendar_registered else 'X'})"
        last_tag = f" (이전 수행일: {last_done})" if last_done else ""

        lines.append(f"• [{urgency}] {name}{cal_tag}{conf_tag}{last_tag}")

        if not calendar_registered:
            suggestions.append(name)

    if suggestions:
        lines.append("\n💡 *비서의 제안*")
        for s in suggestions:
            lines.append(f'• "{s}" 캘린더에 추가할까요?')

    lines.append("\n[Action Required]")
    lines.append("완료 처리하거나 캘린더에 추가할 항목이 있으면 말씀해 주세요.")

    return "\n".join(lines)


def send_message(text: str, retries: int = 0) -> bool:
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "channel": SLACK_CHANNEL_ID,
        "text": text,
    }

    resp = requests.post("https://slack.com/api/chat.postMessage", headers=headers, json=payload)

    if resp.status_code == 200 and resp.json().get("ok"):
        print("Slack 전송 성공")
        return True

    error = resp.json().get("error", resp.text)
    print(f"Slack 전송 실패: {error}", file=sys.stderr)

    if retries < MAX_RETRIES:
        print(f"재시도 {retries + 1}/{MAX_RETRIES}...")
        return send_message(text, retries + 1)

    return False


def send_error_message(error_msg: str):
    """에스컬레이션용 오류 메시지 전송"""
    send_message(f"⚠️ 오류가 발생했습니다: {error_msg}")


def main():
    # CLI 인자로 메시지 텍스트가 전달되면 그대로 전송
    if len(sys.argv) > 1:
        text = " ".join(sys.argv[1:])
    else:
        # verification_result.json 기반 자동 생성 (fallback)
        data = load_json("verification_result.json")
        if not data:
            text = "이번 주 정기 업무 패턴을 찾지 못했습니다. 아직 데이터가 부족할 수 있어요."
        else:
            text = build_message_from_result(data)

    success = send_message(text)
    if not success:
        # 로그 기록
        log_path = os.path.join(OUTPUT_DIR, "agent.log")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[SLACK_FAIL] 전송 실패: {text[:100]}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
