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
    && pip install "video-analyzer @ git+https://github.com/byjlw/video-analyzer.git" requests openai-whisper "Pillow>=11.1,<13"

# Patch video_analyzer: raise reconstruct_video max_tokens (num_predict) from 1000 to 8192
# so the final video_description is not truncated mid-sentence. Kept as a separate RUN layer
# so the pip install layer above stays byte-identical and cache-friendly (no re-clone of the
# upstream git repo on rebuild, which matters when github is hard to reach from the build host).
RUN python -c "import video_analyzer, os; p=os.path.join(os.path.dirname(video_analyzer.__file__),'analyzer.py'); s=open(p,encoding='utf-8').read(); assert 'num_predict=1000' in s, 'num_predict=1000 not found in analyzer.py'; s=s.replace('num_predict=1000','num_predict=8192'); open(p,'w',encoding='utf-8').write(s); print('patched video_analyzer/analyzer.py: num_predict=1000 -> 8192')"

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

RUN chmod 0711 /root \
    && groupadd --system --gid 10001 tikbrowser \
    && useradd --system --uid 10001 --gid tikbrowser --create-home --home-dir /home/tikbrowser tikbrowser

RUN pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn "curl_cffi>=0.15,<0.16"

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY scripts/ /workspace/scripts/
COPY sellersprite_mcp_chat/ /workspace/sellersprite_mcp_chat/

EXPOSE 4000

CMD ["bash"]
