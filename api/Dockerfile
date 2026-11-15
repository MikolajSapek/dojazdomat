# build from repo root: docker build -f api/Dockerfile .
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY api ./api
COPY mcp_server/__init__.py mcp_server/core.py ./mcp_server/
COPY db ./db
COPY web/model-constants.json ./web/model-constants.json
COPY pipeline/city.py ./pipeline/city.py
COPY cities ./cities
# web/data is bind-mounted by compose
ENV DATA_DIR=/app/web/data
EXPOSE 8766
USER nobody
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8766"]
