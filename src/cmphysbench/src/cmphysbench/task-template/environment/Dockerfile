FROM python:3.11-slim

# Scoring dependencies (pinned to versions available at the upstream scorer
# commit). The verifier runs in a fresh container built from this same image.
RUN pip install --no-cache-dir \
    sympy==1.14.0 \
    numpy==2.4.6 \
    latex2sympy2_extended==1.10.2 \
    antlr4-python3-runtime==4.13.2 \
    timeout-decorator==0.5.0 \
    pint==0.25

WORKDIR /app
