# Restore the testbed name expected by this instance's original evaluation script.
RUN set -eu; prefix=/opt/miniconda3/envs/testbed; \
    if [ ! -x "$prefix/bin/python" ]; then \
      for e in /envs/testbed /opt/conda/envs/testbed /opt/miniconda/envs/testbed /opt/miniconda3/envs/python3.9 /opt/miniconda3/envs/python3.8 /opt/miniconda3/envs/sandbox; do \
        if [ -x "$e/bin/python" ]; then ln -s "$e" "$prefix"; break; fi; \
      done; \
    fi; test -x "$prefix/bin/python"
ENV PATH="/opt/miniconda3/envs/testbed/bin:${PATH}"
ENV PYTHONPATH="/testbed"
