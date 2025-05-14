FROM python:3.9-slim

# Set timezone
ENV TZ=Asia/Bangkok
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Install dependencies
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    xvfb \
    libgl1 \
    libnss3 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxi6 \
    libxrandr2 \
    libxrender1 \
    libxss1 \
    libxtst6 \
    libgeoclue-2-0 \
    fonts-ipafont \
    --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Chrome config
ENV CHROMIUM_BIN=/usr/bin/chromium \
    CHROME_OPTS="--no-sandbox --disable-dev-shm-usage --ignore-certificate-errors"

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "-u", "CB-Auto7.py"]
