"""Sandbox backend: an in-process Redis-on-Kubernetes cluster.

Why this exists
---------------
The project must be runnable and *evaluated* without touching a real cluster.
The sandbox renders the same observable surfaces a real cluster exposes — pod
status, events, descriptions, pod logs, Redis INFO/SLOWLOG/CONFIG, Prometheus
samples — from an explicit state object. Fault injection mutates that state.

Tools are written against the renderers, not against the state, so the exact
same tool code runs against a real k3s cluster (``RD_BACKEND=real``); only the
backend implementation changes. Nothing in this package can reach the host.
"""

from sandbox.cluster import PodState, RedisPodState, SimCluster, WallClock

__all__ = ["PodState", "RedisPodState", "SimCluster", "WallClock"]
