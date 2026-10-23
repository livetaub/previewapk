FROM python:3.12-slim
WORKDIR /app
COPY previewapk_mcp.py .
ENV PREVIEWAPK_API_KEY=""
CMD ["python3", "previewapk_mcp.py"]
