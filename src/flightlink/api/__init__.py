"""FastAPI control-plane & read API (`flightlink-api`).

Strictly read/enqueue only: NEVER holds systemd or Aliyun SG write permissions,
and NEVER starts listeners, capture, or cloud sync on startup, health, or GET.
"""
