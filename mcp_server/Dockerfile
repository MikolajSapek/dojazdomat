# build from repo root: docker build -f mcp_server/Dockerfile -t dojazdomat-mcp .
# data is baked in; to use a mounted dir instead: -v /path/to/data:/data -e DOJAZDOMAT_DATA=/data
FROM python:3.13-slim
WORKDIR /app
COPY mcp_server/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY api/__init__.py api/engine.py api/car.py ./api/
COPY web/model-constants.json ./web/model-constants.json
COPY pipeline/city.py ./pipeline/city.py
COPY cities ./cities
COPY mcp_server ./mcp_server
COPY web/data ./web/data
ENV DOJAZDOMAT_DATA=/app/web/data HOST=0.0.0.0 PORT=8790
EXPOSE 8790
USER nobody
CMD ["python", "-m", "mcp_server"]
