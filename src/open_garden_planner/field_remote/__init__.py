"""Field Remote: an opt-in HTTPS listener for a phone (design: docs/design/field-remote-design.md).

Separate from the Agent API on purpose: that server is loopback-only and keeps its own threat
model. This one may be bound to a LAN or Tailscale address, so every request passes the guards
in ``server.py`` (Host, Origin, no token in the URL, bearer token, lockout, rate, body size).

Import ``server``/``tls`` lazily: importing this package must not pull in uvicorn or
cryptography.
"""
