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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80)
