"""Fake X25 configurator for runner tests: FAKE_X25_EXIT (default 0)."""
import os
import sys

print("args:", " ".join(sys.argv[1:]))
print('@@UAVLINK {"t":"change","kind":"param","target":"NET_IPADDR3","old":"14","new":"1"}')
sys.exit(int(os.environ.get("FAKE_X25_EXIT", "0")))
