# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1"]
