# ruru in a container: mount the repository at /repo and pass ruru's arguments.
#   docker run --rm -v "$PWD:/repo" ruru check --all
# The image has git and ruru; the tests run with the image's Python, so install the project's test dependencies
# into a derived image, or pass --test-command.
FROM python:3.15-slim

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/* \
    && useradd -rm appuser && git config --system --add safe.directory '*'

COPY --chown=appuser src /build/src
COPY --chown=appuser pyproject.toml README.md LICENSE /build/
RUN --mount=source=.git,target=/build/.git,type=bind \
    python3 -m pip install --no-cache-dir /build pytest pytest-cov && rm -rf /build

USER appuser
WORKDIR /repo
ENTRYPOINT ["ruru"]
CMD ["--help"]
