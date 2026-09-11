FROM python:3.13-slim

WORKDIR /app
ARG BUILD_SOURCE=""
ARG BUILD_GIT_SHA=""
ARG BUILD_COMMIT_DATE=""
ENV BIKE_GARAGE_BUILD_SOURCE=${BUILD_SOURCE} \
    BIKE_GARAGE_BUILD_GIT_SHA=${BUILD_GIT_SHA} \
    BIKE_GARAGE_BUILD_COMMIT_DATE=${BUILD_COMMIT_DATE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

LABEL org.opencontainers.image.revision=${BUILD_GIT_SHA}

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
