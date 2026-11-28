FROM python:3.11-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends bash ca-certificates curl unzip git bubblewrap util-linux procps build-essential xvfb xauth ffmpeg libgl1 libegl1 libglx-mesa0 libgl1-mesa-dri mesa-vulkan-drivers libvulkan1 libx11-6 libxcursor1 libxinerama1 libxrandr2 libxi6 libasound2 libpulse0 libfontconfig1 libdbus-1-3 fonts-dejavu fonts-liberation nodejs npm && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.8.24
COPY runtime/ /opt/gamedevbench/
RUN cd /tmp && curl -fL --retry 3 -o godot.zip https://github.com/godotengine/godot-builds/releases/download/4.4.1-stable/Godot_v4.4.1-stable_linux.x86_64.zip && curl -fL --retry 3 -o SHA512-SUMS.txt https://github.com/godotengine/godot-builds/releases/download/4.4.1-stable/SHA512-SUMS.txt && awk '$2 == "Godot_v4.4.1-stable_linux.x86_64.zip" {print $1 "  godot.zip"}' SHA512-SUMS.txt | sha512sum -c - && unzip -p godot.zip Godot_v4.4.1-stable_linux.x86_64 > /usr/local/bin/godot && chmod 755 /usr/local/bin/godot && rm godot.zip SHA512-SUMS.txt
RUN cd /opt/gamedevbench && UV_PYTHON_DOWNLOADS=never uv sync --frozen --no-dev --python /usr/local/bin/python3
ENV GODOT_EXEC_PATH=/usr/local/bin/godot
ENV UV_OFFLINE=1
