# The TEAMLINE broker. Build from the repository root:
#
#     docker build -f deploy/Dockerfile -t teamline-broker:1 .
#
# Two things this image deliberately does NOT do:
#
#   * No ENTRYPOINT. The compose file (or your own `docker run`) names the command, so nobody can
#     start a broker by accident on the wrong bind address or port.
#   * No `pip install` into a running container. The packages belong in the image, so that a restart
#     after a power cut gives back exactly what it gave yesterday.
FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY teamline/ /app/teamline/

# The ledger and the call transcripts live here. Mount a volume over it, or they die with the
# container -- the ledger is the broker's only memory and it is replayed on every restart.
ENV TEAMLINE_ROOT=/data
VOLUME ["/data"]

# 0.0.0.0 is right inside a container; publishing the port is a deliberate act on the host side.
# Read "Security model" in the README before you publish it anywhere but a private network.
ENV TEAMLINE_BIND=0.0.0.0
EXPOSE 3790
