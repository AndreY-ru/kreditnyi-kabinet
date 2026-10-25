# Моки ФНС и БКИ (СМОДЕЛИРОВАНО). Один образ, сервис выбирается командой.
FROM python:3.12.7-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv
COPY mocks/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt
COPY mocks ./mocks
RUN useradd --create-home --uid 10002 mock && mkdir -p /data && chown mock /data
USER mock
# Правки тестовых данных (страница «Тестовые данные»): том mockdata, переживают перезапуск
ENV MOCK_DATA_DIR=/data
ENV PORT=8101
HEALTHCHECK --interval=5s --timeout=3s --retries=10 \
  CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:'+os.environ['PORT']+'/health', timeout=2).status == 200 else 1)"
CMD ["sh", "-c", "uvicorn mocks.fns:app --host 0.0.0.0 --port ${PORT}"]
