# syntax=docker/dockerfile:1
# Container build for the network monitor. Design notes:
#  - Single stage: pure-stdlib Python, nothing compiles.
#  - The app is copied to /opt/netmon-dist (pristine); entrypoint.sh seeds it
#    into /app (the state volume) on every start, so the container always runs
#    the image's code and user state persists across rebuilds. See
#    entrypoint.sh for what is and isn't touched.
#  - Run with network_mode: host — ARP-table reads, /24 ping sweeps and LAN
#    router discovery are meaningless behind Docker's bridge NAT. ICMP ping
#    and nmap's ARP-mode scan need CAP_NET_RAW (in Docker's default cap set).
FROM python:3.13-slim

# Ookla speedtest CLI version - bump if the URL 404s on a rebuild.
ARG SPEEDTEST_VERSION=1.2.0

# Runtime tools the monitor shells out to (Linux branch):
#   iputils-ping -> ping -c/-W        net-tools -> arp -an / arp -n
#   iproute2     -> ip route          traceroute -> traceroute -n
#   nmap         -> nmap -sn ARP sweeps   tzdata -> TZ env support
RUN apt-get update && apt-get install -y --no-install-recommends \
        iputils-ping net-tools iproute2 traceroute nmap tzdata \
    && rm -rf /var/lib/apt/lists/*

# Ookla speedtest static binary (the monitor requires the Ookla CLI, NOT the
# pip speedtest-cli package — different JSON, no bufferbloat fields). ADD does
# the download because slim ships no curl/wget.
ADD https://install.speedtest.net/app/cli/ookla-speedtest-${SPEEDTEST_VERSION}-linux-x86_64.tgz /tmp/speedtest.tgz
RUN tar -xzf /tmp/speedtest.tgz -C /usr/local/bin speedtest \
    && chmod 755 /usr/local/bin/speedtest \
    && rm /tmp/speedtest.tgz \
    && /usr/local/bin/speedtest --version

# Pristine copy of the app; entrypoint seeds it into /app on every start.
# .dockerignore is an allowlist, so personal configs/data can never leak into
# the image even when building from a live install folder.
COPY *.py /opt/netmon-dist/
COPY *.example.json /opt/netmon-dist/
COPY vendor/ /opt/netmon-dist/vendor/

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 755 /usr/local/bin/entrypoint.sh

ENV PYTHONUNBUFFERED=1
WORKDIR /app
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
