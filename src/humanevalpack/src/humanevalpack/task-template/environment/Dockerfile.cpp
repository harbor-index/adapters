
# --- C++: g++ 11.4.0 (/usr/bin/g++), Boost headers, OpenSSL (-lcrypto -lssl) ---
RUN apt-get update \
    && apt-get install -y --no-install-recommends g++ libboost-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/* \
    && /usr/bin/g++ --version
