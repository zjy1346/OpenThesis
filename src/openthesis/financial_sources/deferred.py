"""Defer optional network configuration until an official call needs it.

Validated local evidence remains usable after contact settings are removed.
This does not bypass SEC requirements: the factory must validate configuration
before constructing the client and no unconfigured network call is sent.
"""
from threading import Lock
from typing import Any, Callable


class DeferredOfficialSource:
    def __init__(self, factory: Callable[[], Any]):
        self._factory = factory
        self._client = None
        self._lock = Lock()

    def _source(self):
        with self._lock:
            if self._client is None:
                self._client = self._factory()
        return self._client

    def list_annual_filings(self, company, *, limit=5):
        return self._source().list_annual_filings(company, limit=limit)

    def download_filing(self, filing, target):
        return self._source().download_filing(filing, target)

    def get_company_facts(self, company):
        return self._source().get_company_facts(company)
