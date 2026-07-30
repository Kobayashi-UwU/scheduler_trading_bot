# Thin wrapper around gmag11/metatrader5_vnc: fixes its own start.sh's final
# step, which never starts the rpyc bridge (see entrypoint.sh for why).
# Deployed as its own Railway service, separate from the main bot -- it only
# runs an MT5 terminal + rpyc bridge, not the trading app itself.
FROM gmag11/metatrader5_vnc:latest

COPY mt5-terminal/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

CMD ["/entrypoint.sh"]
