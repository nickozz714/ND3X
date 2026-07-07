FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PIPX_HOME=/opt/pipx \
    PIPX_BIN_DIR=/usr/local/bin

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg \
    lsb-release \
    ffmpeg \
    pipx \
    nodejs \
    npm \
    pandoc \
    texlive-xetex \
    texlive-fonts-recommended \
    texlive-fonts-extra \
    texlive-latex-extra \
    fontconfig \
    poppler-utils \
    chromium \
    unzip \
    fonts-open-sans \
 && \
    curl -sL https://aka.ms/InstallAzureCLIDeb | bash \
 && \
    PUPPETEER_SKIP_DOWNLOAD=true npm install -g @mermaid-js/mermaid-cli \
 && \
    pip install --no-cache-dir pandoc-mermaid-filter \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /app/src/

ENV FFMPEG_BIN=/usr/bin/ffmpeg \
    FFPROBE_BIN=/usr/bin/ffprobe \
    PUPPETEER_SKIP_DOWNLOAD=true \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

EXPOSE 8088

CMD ["bash", "-lc", "if [ -f bootstrap_check.py ]; then python bootstrap_check.py || true; fi; python src/server.py"]
