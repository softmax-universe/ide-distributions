FROM ghcr.io/astral-sh/uv:0.11.8@sha256:3b7b60a81d3c57ef471703e5c83fd4aaa33abcd403596fb22ab07db85ae91347 AS uv
FROM nousresearch/hermes-agent@sha256:d27689bc6e8581ec884a7227c9101fdaebf6c853cff8859020dac14f21452b4d
RUN apt-get update && apt-get install -y --no-install-recommends --reinstall tini poppler-utils ffmpeg \
    && rm -rf /var/lib/apt/lists/*
# Agents commit in their worktrees from the terminal; give git the identity research_core.gitops uses.
RUN git config --system user.name IDE && git config --system user.email ide@softmax.invalid
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=moby/buildkit@sha256:0ffa2fcf6b8757c47d569b3ef0f03f9d5eb3b9ff5ce68d858f994f89b749da0c /usr/bin/buildctl /usr/local/bin/buildctl
COPY pyproject.toml uv.lock /opt/distribution/
COPY core /opt/distribution/core
ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PROJECT_ENVIRONMENT=/opt/workspace-env \
    UV_CACHE_DIR=/tmp/uv-cache \
    UV_PYTHON=3.12 \
    IDE_WORKSPACE_ENV=/opt/workspace-env \
    PYTHONPYCACHEPREFIX=/opt/python-bytecode
# The Hermes launcher honors PYTHONPYCACHEPREFIX. Populate the same read-only cache that
# both bundled interpreters and the launcher will read, including the workspace dependencies.
RUN uv sync --frozen --no-dev --project /opt/distribution \
    && /opt/workspace-env/bin/python -m compileall -q -x '/tests/' \
       /opt/python /opt/workspace-env/lib /opt/distribution/core/src \
    && sha256sum /opt/distribution/uv.lock | cut -d ' ' -f1 > /opt/workspace-env/.lock-sha \
    && chmod -R a+rX /opt/python /opt/workspace-env /opt/distribution \
    && rm -rf /tmp/uv-cache
COPY orchestrator /opt/distribution/orchestrator
COPY researcher /opt/distribution/researcher
COPY restricted /opt/distribution/restricted
COPY image /opt/distribution/image
RUN bash /opt/distribution/image/install-native-attachments.sh \
    && /opt/hermes/.venv/bin/python -m compileall -q -l /opt/hermes \
    && /opt/hermes/.venv/bin/python -m compileall -q -x '/(tests|node_modules)/' \
       "$(/opt/hermes/.venv/bin/python -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')" \
       /opt/hermes/.venv/lib /opt/hermes/agent /opt/hermes/cron /opt/hermes/gateway \
       /opt/hermes/hermes_cli /opt/hermes/plugins /opt/hermes/pm /opt/hermes/tools \
       /opt/distribution/core/src /opt/distribution/image \
    && chmod -R a+rX /opt/models/whisper-base \
    && rm -rf /tmp/uv-cache
COPY image/workspace-path.sh /etc/profile.d/workspace-path.sh
# Hosted workspaces use the built-in API transport. Keep other bundled plugins,
# without discovering messaging adapters in the host or either profile.
RUN mkdir /opt/workspace-plugins \
    && for plugin in /opt/hermes/plugins/*; do \
         if [ "$(basename "$plugin")" != platforms ]; then \
           ln -s "$plugin" /opt/workspace-plugins/; \
         fi; \
       done
# What names this image on every telemetry record: a hash of the sources it was built from, because
# the build has no git history. image/runtime.py passes it on as IDE_IMAGE.
RUN cd /opt/distribution \
    && find pyproject.toml uv.lock core orchestrator researcher restricted image -type f \
         ! -path '*/__pycache__/*' -print0 \
       | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-16 > image-id
ENV PYTHONUNBUFFERED=1 \
    HERMES_NEMO_RELAY_PLUGINS_TOML=/opt/distribution/image/relay-plugins.toml \
    HOME=/workspace/host \
    HERMES_BUNDLED_PLUGINS=/opt/workspace-plugins \
    HERMES_HOME=/workspace/host \
    HERMES_REAL_HOME=/workspace/host \
    HERMES_WRITE_SAFE_ROOT=/workspace:/tmp \
    IDE_WORKSPACE=/workspace/research \
    XDG_RUNTIME_DIR=/tmp/hermes-runtime \
    PATH=/opt/workspace-env/bin:/opt/hermes/.venv/bin:/usr/local/bin:/usr/bin:/bin
RUN /opt/hermes/.venv/bin/python /opt/distribution/image/prepare-profiles.py \
    && chmod -R a+rX /opt/distribution/prepared-profiles
USER 10000:10000
WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/hermes/.venv/bin/python", "/opt/distribution/image/runtime.py"]
