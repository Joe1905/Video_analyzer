FROM python:3.11-slim

ARG HTTP_PROXY=
ARG HTTPS_PROXY=
ARG http_proxy=
ARG https_proxy=
ARG ALL_PROXY=
ARG all_proxy=
ARG NO_PROXY=
ARG no_proxy=

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
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
    && python -m playwright install chrome \
    && scrapling install

RUN apt-get -o Acquire::ForceIPv4=true -o Acquire::http::Timeout=20 update \
    && apt-get -o Acquire::ForceIPv4=true -o Acquire::http::Timeout=20 install -y --no-install-recommends \
        novnc \
        websockify \
        x11vnc \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 tikbrowser \
    && useradd --system --uid 10001 --gid tikbrowser --create-home --home-dir /home/tikbrowser tikbrowser

RUN pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn "curl_cffi>=0.15,<0.16"

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY scripts/ /workspace/scripts/
COPY sellersprite_mcp_chat/ /workspace/sellersprite_mcp_chat/

RUN chmod 0711 /root

EXPOSE 4000

CMD ["bash"]
