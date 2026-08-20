FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app.py /app/app.py

RUN mkdir -p /data

ENV PERIOD_HOST=0.0.0.0
ENV PERIOD_DATA=/data/period_state.json
ENV PORT=8080

EXPOSE 8080

CMD ["python", "/app/app.py"]
