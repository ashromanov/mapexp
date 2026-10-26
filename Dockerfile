FROM python:3.11-slim-bookworm
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && groupadd --gid 1000 mapexp \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin mapexp
COPY *.py ./
COPY static/ ./static/
COPY deploy/ ./deploy/
ARG VCS_REF=development
ENV APP_REVISION=$VCS_REF
RUN python - <<'PYCODE'
import os,re
from pathlib import Path
for name in ("static/index.html", "static/app.js"):
    path=Path(name)
    path.write_text(re.sub(r"(?<=\?v=)[\w.-]+", os.environ["APP_REVISION"], path.read_text()))
PYCODE
LABEL org.opencontainers.image.source="https://github.com/ashromanov/mapexp" \
      org.opencontainers.image.revision=$VCS_REF
RUN mkdir -p data media sessions && chown -R mapexp:mapexp data media sessions
USER mapexp
EXPOSE 8000
CMD ["uvicorn", "web:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
