FROM python:3.12-alpine

COPY app.py /usr/local/bin/bt-cf-sync

EXPOSE 8080

VOLUME ["/data"]

CMD ["python", "/usr/local/bin/bt-cf-sync"]
