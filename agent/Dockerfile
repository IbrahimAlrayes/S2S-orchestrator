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

# Pin HuggingFace cache to /opt (outside /app) so it survives compose
# bind-mounts in dev AND stays reachable when the container runs as a
# non-root user (uid 1000 in production GKE). Without this, MultilingualModel()
# crashes at first session with `Could not find file "languages.json"`
# because uid 1000's $HOME is `/` and `/root/.cache` is permission-denied.
ENV HF_HOME=/opt/hf-cache
RUN mkdir -p /opt/hf-cache && chmod -R 0755 /opt/hf-cache

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

# `download-files` ran as root (build user); make the cache world-readable
# so the runtime user (uid 1000 in production) can read it.
RUN chmod -R a+rX /opt/hf-cache

CMD ["python", "agent.py", "start"]
