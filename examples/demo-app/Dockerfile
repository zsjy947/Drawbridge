# Business Dockerfile — built inside the isolated rootless BuildKit only.
FROM python:3.12-slim

WORKDIR /app
COPY app.py /app/app.py
COPY config/app.json /app/config/app.json
USER 65532:65532
EXPOSE 8080
CMD ["python3", "/app/app.py"]
