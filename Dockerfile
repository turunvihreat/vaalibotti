FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY ev27 /app/ev27
RUN useradd --uid 10001 --create-home ev27 && mkdir -p /data && chown ev27:ev27 /data
USER ev27
ENV EV27_DATABASE=/data/ev27.sqlite3 PYTHONUNBUFFERED=1
VOLUME ["/data"]
CMD ["python", "-m", "ev27.bot"]
