FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

RUN mkdir -p /data

ENV PERIOD_HOST=0.0.0.0
ENV PERIOD_DATA=/data/period_state.json
ENV PORT=8080

EXPOSE 8080

CMD ["python", "server.py"]
