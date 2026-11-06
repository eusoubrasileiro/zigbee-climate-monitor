FROM python:3.13-alpine
RUN pip install --no-cache-dir paho-mqtt==2.2.0
COPY app.py index.html /app/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8090
CMD ["python", "/app/app.py"]
