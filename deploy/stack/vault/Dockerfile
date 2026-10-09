FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --system --uid 10001 vault && mkdir /data && chown vault /data
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY brightly_vault ./brightly_vault
USER vault
EXPOSE 8080 8081
CMD ["python", "-m", "brightly_vault"]
