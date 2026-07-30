#!/bin/sh
# Wrapper around gmag11/metatrader5_vnc's default entrypoint (/init -> start.sh).
#
# start.sh's own final step runs:
#   python3 -m mt5linux --host 0.0.0.0 -p 8001 -w wine python.exe
# which only works with mt5linux==0.1.9 (the "-w" wine-launcher flag). Its own
# unpinned "pip install mt5linux>=0.1.9" resolves to the latest release, which
# dropped that flag entirely -- so the bridge never starts.
#
# Rather than fight that version drift, this starts the actual rpyc bridge
# directly: a plain rpyc SlaveService running under Wine's python (so
# "import MetaTrader5" succeeds), which is exactly what the mt5linux CLIENT
# (rpyc.classic.connect + "import MetaTrader5 as mt5") expects on the other
# end -- no dependency on mt5linux's server-side CLI at all.
#
# Runs in the background so start.sh's own (harmless, always-failing) step 7
# attempt and the rest of /init's normal boot (KasmVNC, X, etc.) proceed
# unmodified.

i=0
while [ $i -lt 60 ]; do
  wine python -c "import rpyc, MetaTrader5" 2>/dev/null && break
  i=$((i + 1))
  sleep 5
done

# Small buffer past the importability check so Wine's python is fully settled.
sleep 10

wine python -c "
import rpyc
from rpyc.utils.server import ThreadedServer
from rpyc.core.service import SlaveService
ThreadedServer(
    SlaveService,
    hostname='0.0.0.0',
    port=8001,
    reuse_addr=True,
    protocol_config={'allow_all_attrs': True},
).start()
" &

exec /init
