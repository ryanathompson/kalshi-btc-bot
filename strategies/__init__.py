"""Strategies live as fully self-contained modules under this package.

Each strategy implements :class:`master.interfaces.Strategy` and depends
ONLY on stdlib, third-party libs, and ``master/``. Cross-strategy imports
are forbidden by architectural rule — see ``master/README.md``.
"""
