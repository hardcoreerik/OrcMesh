"""Provisioning support for the setup wizard.

Deliberately named ``provisioning`` rather than ``setup``: a package called
``setup`` would shadow nothing today but reads as the distutils module and
invites confusion in tracebacks.

Nothing here touches Qt or the network. Estimating, choosing and describing
regions is pure logic so it can be tested without a radio, a map pack, or an
OrcMaps checkout.
"""
