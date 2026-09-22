FROM python:3.12-slim

WORKDIR /app

# mimamori フォルダ内の requirements.txt をコピーしてインストール
COPY mimamori/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# mimamori フォルダ内の全ファイルを /app にコピー
COPY mimamori/ .

ENV PORT=8080
EXPOSE 8080

CMD ["python", "app.py"]
