FROM python:3.12-slim

WORKDIR /srv

# System deps: rsync + openssh-client for the backup feature. Python's
# sqlite3 module is already part of the standard library.
RUN apt-get update \
    && apt-get install -y --no-install-recommends rsync openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 80

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "80"]
