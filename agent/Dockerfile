FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# libgomp1 is required by torch/numpy on Linux for OpenMP-backed kernels.
# python:3.11-slim ships without it; torch's `_dlopen` then dies with
# "libgomp.so.1: cannot open shared object file". libsndfile1 is required
# by soundfile (transitive via deepfilternet's audio loading paths). git
# is shelled-out by `df.utils.get_git_root` during init_df(); without it
# init raises FileNotFoundError on /bin/git even though the call is purely
# informational.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    libgomp1 \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/

RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py /app/
COPY config.py /app/
COPY metrics.py /app/
COPY plugins /app/plugins
# prompts/ holds prompts.json + voice_prompt.py — required by R-025's
# self-healing system-prompt regen (AgentSettings._load_prompt_file).
# Without this copy, AgentSettings() construction fails at validate-time
# before the download-files entrypoint can run.
COPY prompts /app/prompts

# Download turn detector model weights (cached in image layer)
RUN python agent.py download-files

CMD ["python", "agent.py", "start"]
