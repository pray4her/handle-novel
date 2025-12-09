FROM python:3.11-slim

WORKDIR /app

# 安装基础依赖
RUN apt-get update && apt-get install -y --no-install-recommends \ 
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 默认以 CLI 模式运行；如需 GUI，建议在宿主机直接执行 `python main.py --mode gui`
CMD ["python", "main.py", "--mode", "cli", "--input-dir", "/data", "--db-path", "/data/journal_cleaner.db"]


