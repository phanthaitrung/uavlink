"""Fake network probe for runner tests: FAKE_NET_EXIT (default 0; 3 = failed checks)."""
import os
import sys

print("args:", " ".join(sys.argv[1:]))
print('@@UAVLINK {"t":"check","id":"vps.tcp","ok":true,"code":"vps.tcp.ok","args":{"ip":"203.0.113.9","port":5760}}')
sys.exit(int(os.environ.get("FAKE_NET_EXIT", "0")))
