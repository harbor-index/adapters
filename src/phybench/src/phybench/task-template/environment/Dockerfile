FROM python:3.11-slim

# Scoring dependencies (pinned). The verifier runs in a fresh container built
# from this same image, so these must be present here.
RUN pip install --no-cache-dir \
    sympy==1.14.0 \
    numpy==2.4.6 \
    latex2sympy2_extended==1.10.1 \
    antlr4-python3-runtime==4.13.2 \
    timeout-decorator==0.5.0

WORKDIR /app
