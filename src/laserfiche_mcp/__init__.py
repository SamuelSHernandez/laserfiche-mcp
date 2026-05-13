"""Laserfiche MCP server.

Note: importing ``server`` eagerly instantiates ``Settings`` from the environment.
We deliberately don't re-export ``server`` here so that ``client``, ``auth``, and
``config`` can be imported (e.g. by tests) without requiring a valid env.
"""

__version__ = "1.4.1"
__all__: list[str] = []
