"""Shared schema registry — copied into BOTH container images.

`src/` (daily job) and `ingest/` (service) are separate images and cannot import
each other, which is why the schema lives here rather than in either of them.
Pure stdlib, no dependencies.
"""
