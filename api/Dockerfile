# build from repo root: docker build -f api/Dockerfile .
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY api ./api
COPY web/data ./web/data
ENV DATA_DIR=/app/web/data
EXPOSE 8766
USER nobody
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8766"]
