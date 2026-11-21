FROM python:3.11-slim-bookworm
RUN pip install --no-cache-dir requests==2.32.5 termcolor==3.2.0 tqdm==4.67.1
WORKDIR /broker
COPY . /broker
RUN mkdir /stb-trace
CMD ["python", "/broker/broker.py"]
