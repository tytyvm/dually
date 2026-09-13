FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md config.toml ./
COPY radar ./radar
RUN pip install --no-cache-dir . && useradd -m radar && mkdir data && chown radar:radar data
USER radar
EXPOSE 8080
CMD ["dualradar-web"]
