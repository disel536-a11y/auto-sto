"""
채용 알림 전용 웹서버 (주식봇 app.py와 완전 분리, 8080 포트).
passed_jobs.html(= job_alert.py --web 가 생성)만 서빙한다.
config.py·kakao_token.json 등 다른 파일은 절대 노출하지 않음.

실행: python3 jobs_web.py        (기본 포트 8080)
접속: http://<서버IP>:8080
"""
import os
from flask import Flask, send_file, Response

BASE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(BASE, "passed_jobs.html")

app = Flask(__name__)


@app.route("/")
def home():
    if os.path.exists(PAGE):
        return send_file(PAGE)
    return Response("아직 생성되지 않았습니다. 서버에서 'python3 job_alert.py --web' 를 먼저 실행하세요.",
                    mimetype="text/plain; charset=utf-8", status=404)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
