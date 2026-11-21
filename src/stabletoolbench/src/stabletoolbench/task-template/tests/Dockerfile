FROM python:3.11-slim-bookworm
RUN pip install --no-cache-dir pydantic==1.10.22 openai==1.109.1 httpx==0.28.1 requests==2.32.5 PyYAML==6.0.3 tenacity==9.1.2 tqdm==4.67.1 numpy==1.26.4 backoff==2.2.1
WORKDIR /tests
COPY . /tests/
