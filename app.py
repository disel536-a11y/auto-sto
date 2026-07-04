"""
주식봇 웹 대시보드 서버.
봇이 저장한 data.json 을 읽어 대시보드(dashboard.html)로 보여줍니다.
실행: python3 app.py   (기본 포트 80)
"""
import os
import sys
import json
import time
import subprocess
from flask import Flask, send_file, Response, jsonify

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE, "data.json")
REFRESH_FLAG = os.path.join(BASE, "refresh.flag")     # 봇 루프가 감지 → 전체 재분석
HEARTBEAT_FILE = os.path.join(BASE, "bot.heartbeat")  # 봇 루프 생존 신호
REFRESH_LOCK = os.path.join(BASE, "refresh.lock")     # 장외 --once 중복 실행 방지

app = Flask(__name__)


@app.route("/")
def index():
    return send_file(os.path.join(BASE, "dashboard.html"))


@app.route("/api/data")
def api_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            return Response(f.read(), mimetype="application/json; charset=utf-8")
    empty = {"updated": "", "upper": [], "themes": []}
    return Response(json.dumps(empty, ensure_ascii=False), mimetype="application/json; charset=utf-8")


def _bot_alive() -> bool:
    """봇 루프가 최근 120초 내 heartbeat 를 남겼으면 가동 중으로 판단."""
    try:
        return os.path.exists(HEARTBEAT_FILE) and (time.time() - os.path.getmtime(HEARTBEAT_FILE)) < 120
    except OSError:
        return False


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """수동 새로고침 — 상한가+테마를 raw 부터 전체 재분석.
    봇 가동 중이면 플래그로 봇 루프가 처리(단일 기록자 유지),
    장외 등 봇 미가동이면 --once 서브프로세스로 즉시 1회 재수집."""
    if _bot_alive():
        try:
            with open(REFRESH_FLAG, "w") as f:
                f.write(str(time.time()))
            return jsonify(status="queued")   # 다음 스캔(≤60초)에서 전체 재분석
        except Exception as e:
            return jsonify(status="error", detail=str(e)), 500

    # 봇 미가동 → --once 즉시 실행(중복 방지 락 90초)
    if os.path.exists(REFRESH_LOCK) and (time.time() - os.path.getmtime(REFRESH_LOCK)) < 90:
        return jsonify(status="busy")
    try:
        with open(REFRESH_LOCK, "w") as f:
            f.write(str(time.time()))
        subprocess.Popen([sys.executable, os.path.join(BASE, "stock_monitor.py"), "--once"], cwd=BASE)
        return jsonify(status="started")      # 완료까지 수십 초 소요(LLM 포함)
    except Exception as e:
        return jsonify(status="error", detail=str(e)), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80)
