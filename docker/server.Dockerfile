# 관제 서버 이미지. 빌드 컨텍스트는 프로젝트 루트 (common/과 server/가 필요)
FROM python:3.12-slim

WORKDIR /app
COPY server/requirements.txt server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt

COPY common/ common/
COPY server/ server/

# 비밀값은 이미지에 넣지 않는다. docker-compose가 .env를 환경변수로 넘겨준다
EXPOSE 9000
CMD ["python", "-m", "server.main"]
