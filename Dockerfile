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
    PIP_NO_CACHE_DIR=1

WORKDIR /workspace

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        docker-cli \
        docker.io \
        ffmpeg \
        git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip \
    && pip install "video-analyzer @ git+https://github.com/byjlw/video-analyzer.git@2b095faf8a965eda5ba055ef29eb0cd71698ae6f" requests openai-whisper

COPY scripts/patch_video_analyzer.py /tmp/patch_video_analyzer.py
RUN python /tmp/patch_video_analyzer.py && rm /tmp/patch_video_analyzer.py

RUN pip install yt-dlp playwright httpx "scrapling[ai]" \
    && python -m playwright install --with-deps chromium \
    && scrapling install

RUN pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn "curl_cffi>=0.15,<0.16"

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY scripts/ /workspace/scripts/
COPY sellersprite_mcp_chat/ /workspace/sellersprite_mcp_chat/

EXPOSE 4000

CMD ["bash"]
