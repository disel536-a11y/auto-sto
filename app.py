"""
주식봇 웹 대시보드 서버.
봇이 저장한 data.json 을 읽어 대시보드(dashboard.html)로 보여줍니다.
실행: python3 app.py   (기본 포트 80)
"""
import os
import json
from flask import Flask, send_file, Response

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE, "data.json")

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


@app.route("/jobs")
def jobs():
    """채용 알림 봇의 '필터 통과 공고' 표 (python3 job_alert.py --dump 로 생성)."""
    p = os.path.join(BASE, "passed_jobs.html")
    if os.path.exists(p):
        return send_file(p)
    return Response("아직 생성되지 않았습니다. 서버에서 'python3 job_alert.py --dump' 를 먼저 실행하세요.",
                    mimetype="text/plain; charset=utf-8", status=404)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80)
