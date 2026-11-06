FROM mcr.microsoft.com/playwright/python:v1.61.0-noble
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright MPLCONFIGDIR=/tmp/matplotlib XDG_CACHE_HOME=/tmp/cache
WORKDIR /srv
COPY mediacrawler /opt/MediaCrawler
COPY crawler_service /srv/crawler_service
RUN pip install --no-cache-dir -r crawler_service/requirements.txt \
    && python -c "import pathlib,playwright,os; os.symlink(str(pathlib.Path(playwright.__file__).parent/'driver'/'node'),'/usr/local/bin/node')" \
    && groupadd -g 10001 videoinsight && useradd -u 10001 -g 10001 -M videoinsight \
    && mkdir /state && chown -R 10001:10001 /state /srv /opt/MediaCrawler
USER 10001:10001
EXPOSE 8090
CMD ["uvicorn", "crawler_service.api:app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1"]
