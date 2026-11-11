"""Owned local OAuth/MCP service for the hidden desktop flow."""
import json
import os
import socket
import secrets
from pathlib import Path
import sys

root=Path(__file__).resolve().parents[2]
sys.path.append(str(root/"tests"))
if not os.environ.get("XULOOM_TEST_EXECUTABLE"):sys.path.insert(0,str(root))
from test_mcp_oauth import OAuthService

service=OAuthService();service.mcp_mode=True
# A fixed callback must not be handed out and left free for a minute while the
# automatic flows run. Avoid the usual client ephemeral range as well.
listener = socket.socket()
if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
for attempt in range(128):
    try:
        listener.bind(("127.0.0.1", 20000 + secrets.randbelow(10000)))
        break
    except OSError:
        if attempt == 127:
            listener.close(); service.close()
            raise
port = listener.getsockname()[1]
fixed={"client_id":"owned-fixed-client","issuer":service.issuer,"redirect_uri":"http://127.0.0.1:"+str(port)+"/owned-fixed-callback","client_secret":"owned-client-fixture-secret"}
service.clients[fixed["client_id"]]={"redirect_uris":[fixed["redirect_uri"]],"client_secret":fixed["client_secret"],"token_endpoint_auth_method":"client_secret_basic"}
print(json.dumps({"url":service.url,"fixed":fixed}),flush=True)
try:
    for line in sys.stdin:
        if line.strip()=="stop":break
        if line.strip()=="release-fixed":
            listener.close()
            print(json.dumps({"fixed_callback_released":True}),flush=True)
finally:
    listener.close();service.close()
