"""Laserfiche MCP server.

Note: importing ``server`` eagerly instantiates ``Settings`` from the environment.
We deliberately don't re-export ``server`` here so that ``client``, ``auth``, and
``config`` can be imported (e.g. by tests) without requiring a valid env.
"""

__version__ = "0.2.0"
__all__: list[str] = []
