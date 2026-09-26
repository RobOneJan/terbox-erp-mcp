FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

ENV MCP_TRANSPORT=streamable-http
ENV TERBOX_DOWNLOAD_DIR=/tmp/downloads

EXPOSE 8080

CMD ["terbox-mcp"]
