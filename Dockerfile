FROM python:3.12-slim

# ffmpeg: 인스타그램이 영상/음성을 따로 줄 때 합치는 데 씁니다.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY app.py igdl.py ./
COPY static ./static

RUN useradd --system --uid 10001 app && mkdir /data && chown -R app /app /data
USER app

# 컨테이너는 APP_TOKEN 없이는 시작되지 않습니다(app.py가 확인).
# /data: 앱 화면에서 등록한 인스타그램 로그인 쿠키가 저장되는 곳(볼륨으로 유지).
ENV HOST=0.0.0.0 PORT=5000 PYTHONUNBUFFERED=1 REQUIRE_APP_TOKEN=1 IGDL_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 5000

HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
    CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/login' % os.environ['PORT'], timeout=4)"

# 영상 다운로드는 오래 걸릴 수 있어 timeout을 넉넉히, 스트리밍을 위해 스레드를 씁니다.
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT} --workers 1 --threads 4 --timeout 300 app:app"]
