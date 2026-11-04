# 国内（默认）:
#   docker build .
# 海外:
#   docker build --build-arg USE_CHINA_MIRROR=0 \
#     --build-arg PLAYWRIGHT_IMAGE=mcr.microsoft.com/playwright/python:v1.64.1-noble .
ARG PLAYWRIGHT_IMAGE=mcr.azure.cn/playwright/python:v1.64.1-noble
FROM ${PLAYWRIGHT_IMAGE}

# 1=国内镜像（apt 清华 + pip 清华）；0=海外（apt 官方 + pip pypi.org）
ARG USE_CHINA_MIRROR=1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    LANG=zh_CN.UTF-8 \
    LANGUAGE=zh_CN:zh \
    LC_ALL=zh_CN.UTF-8 \
    DATA_DIR=/data \
    QUEUE_DB_PATH=/data/queue.db \
    ENABLE_VNC=true \
    PLAYWRIGHT_HEADLESS=false \
    DISPLAY=:99 \
    API_HOST=0.0.0.0 \
    API_PORT=8056 \
    VNC_PAGE_URL=/vnc/vnc.html?autoconnect=true&resize=scale&path=vnc/websockify \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN set -eux; \
    if [ "${USE_CHINA_MIRROR}" = "1" ]; then \
      find /etc/apt -type f \( -name '*.list' -o -name '*.sources' \) -exec sed -i \
        -e 's|http://archive.ubuntu.com/ubuntu|https://mirrors.tuna.tsinghua.edu.cn/ubuntu|g' \
        -e 's|https://archive.ubuntu.com/ubuntu|https://mirrors.tuna.tsinghua.edu.cn/ubuntu|g' \
        -e 's|http://security.ubuntu.com/ubuntu|https://mirrors.tuna.tsinghua.edu.cn/ubuntu|g' \
        -e 's|https://security.ubuntu.com/ubuntu|https://mirrors.tuna.tsinghua.edu.cn/ubuntu|g' \
        -e 's|http://ports.ubuntu.com/ubuntu-ports|https://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports|g' \
        -e 's|https://ports.ubuntu.com/ubuntu-ports|https://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports|g' \
        -e 's|http://azure.archive.ubuntu.com/ubuntu|https://mirrors.tuna.tsinghua.edu.cn/ubuntu|g' \
        -e 's|https://azure.archive.ubuntu.com/ubuntu|https://mirrors.tuna.tsinghua.edu.cn/ubuntu|g' \
        -e 's|http://azure.ports.ubuntu.com/ubuntu-ports|https://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports|g' \
        -e 's|https://azure.ports.ubuntu.com/ubuntu-ports|https://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports|g' \
        {} +; \
      PIP_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple/"; \
      PIP_TRUSTED_HOST="pypi.tuna.tsinghua.edu.cn"; \
    else \
      PIP_INDEX_URL="https://pypi.org/simple/"; \
      PIP_TRUSTED_HOST="pypi.org"; \
    fi; \
    apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      xvfb \
      x11vnc \
      novnc \
      websockify \
      fonts-noto-cjk \
      libzbar0 \
      iproute2 \
      locales \
      tzdata \
    && sed -i '/zh_CN.UTF-8/s/^# //g' /etc/locale.gen \
    && locale-gen zh_CN.UTF-8 \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/* \
    && echo "pip index=${PIP_INDEX_URL}" \
    && pip install --no-cache-dir --ignore-installed \
        -i "${PIP_INDEX_URL}" --trusted-host "${PIP_TRUSTED_HOST}" \
        "fastapi>=0.162.0" \
        "uvicorn[standard]>=0.54.0" \
        "httpx>=0.28.1" \
        "pillow>=13.0.0" \
        "playwright==1.64.1" \
        "pydantic-settings>=2.16.2" \
        "python-dotenv>=1.2.4" \
        pyzbar

COPY pyproject.toml README.md ./
COPY src ./src
COPY web ./web
COPY main.py login_worker.py run.sh ./
COPY scripts/entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh && mkdir -p /data/challenges

EXPOSE 8056 7900
VOLUME ["/data"]
ENTRYPOINT ["/entrypoint.sh"]
