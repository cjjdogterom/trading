# Slank image voor de always-on worker (Fly.io).
FROM python:3.12-slim

WORKDIR /app

# Alleen de slanke bot-dependencies (geen streamlit/pandas nodig in de worker).
COPY requirements-bot.txt .
RUN pip install --no-cache-dir -r requirements-bot.txt

COPY . .

CMD ["python", "worker.py"]
