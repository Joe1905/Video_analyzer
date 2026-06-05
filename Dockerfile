FROM python:3.11-slim

ARG HTTP_PROXY=
ARG HTTPS_PROXY=
ARG http_proxy=
ARG https_proxy=
ARG ALL_PROXY=
ARG all_proxy=
ARG NO_PROXY=
ARG no_proxy=

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HTTP_PROXY= \
    HTTPS_PROXY= \
    http_proxy= \
    https_proxy= \
    ALL_PROXY= \
    all_proxy= \
    NO_PROXY= \
    no_proxy=

WORKDIR /workspace

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip \
    && pip install "video-analyzer @ git+https://github.com/byjlw/video-analyzer.git" requests openai-whisper

RUN pip install yt-dlp playwright httpx \
    && python -m playwright install --with-deps chromium

RUN pip install "curl_cffi>=0.14,<0.15"

COPY scripts/ /workspace/scripts/

EXPOSE 4000

CMD ["bash"]
