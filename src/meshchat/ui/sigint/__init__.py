"""SIGINT: a receive-only signal-intelligence workbench.

Three panes over one question — *what is on the air around me*:

* a **spectrum and waterfall** of the band, live and in 3D, from an RTL-SDR;
* a **band survey** saying which of the mesh's channel slots carry traffic;
* **packet intelligence** from the connected radio, which needs no SDR at all.

Everything here is receive-only. It reads what is already in the air on public
settings; there is nothing that transmits, and nothing that attempts to break
another mesh's channel key.
"""
