"""Brightday client vault: a link a client opens to drop sensitive files to the practice.

Two web apps in one process:

* public (port 8080, reached through Caddy at /v/<token>): the client enters the 6-digit code
  they were given separately, then drags files in. Each file is virus-scanned (ClamAV) and
  encrypted (AES-256-GCM) while it streams in, so it never sits on disk in the clear.
* staff (port 8081, reached only through oauth2-proxy at /vault/, i.e. after Microsoft sign-in):
  create links, see what arrived, download, delete, close or extend links. Every action is
  audited.

Data lives in /data: vault.db (SQLite), files/<link>/<file>.enc and snapshot/vault.db (a
consistent copy for the nightly backup).
"""
