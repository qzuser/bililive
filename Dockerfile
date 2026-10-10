# 国内网络构建时可覆盖：--build-arg BASE_IMAGE=docker.1ms.run/library/python:3.11-slim
ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    BLR_DATA_DIR=/data

RUN sed -i 's#deb.debian.org#mirrors.ustc.edu.cn#g; s#security.debian.org#mirrors.ustc.edu.cn#g' \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null || true \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
        -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY app ./app

EXPOSE 8000
VOLUME ["/data"]

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
