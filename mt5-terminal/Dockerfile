# syntax=docker/dockerfile:1
# Thin wrapper around gmag11/metatrader5_vnc: adds mt5-bridge as a
# linuxserver.io "custom service" (see mt5-bridge for why start.sh's own
# rpyc-server step never works). Deployed as its own Railway service,
# separate from the main bot -- it only runs an MT5 terminal + rpyc bridge,
# not the trading app itself. No CMD override: the image's own entrypoint
# and boot sequence (KasmVNC, X, MT5 terminal) run completely unmodified;
# custom-services.d is the officially supported extension point for adding
# a persistent background process without touching any of that.
FROM gmag11/metatrader5_vnc:latest

COPY mt5-terminal/mt5-bridge /custom-services.d/mt5-bridge
RUN chmod +x /custom-services.d/mt5-bridge
