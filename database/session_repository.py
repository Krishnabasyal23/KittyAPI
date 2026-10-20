"""Compatibility import: the v1 session store became database.wallet_store.WalletStore in v2."""

from .wallet_store import WalletStore as SQLiteSessionStore

__all__ = ["SQLiteSessionStore"]
