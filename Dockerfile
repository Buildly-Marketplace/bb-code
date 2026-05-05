FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY bb_code ./bb_code

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir .

EXPOSE 8791

CMD ["build", "--host", "0.0.0.0", "--port", "8791", "."]
