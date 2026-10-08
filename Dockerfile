FROM mcr.microsoft.com/playwright/python:v1.63.0-noble
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DEV_MODE=0 PORT=8000
CMD ["sh", "-c", "python seed.py && python -m server.backfill && uvicorn server.app:app --host 0.0.0.0 --port ${PORT}"]
