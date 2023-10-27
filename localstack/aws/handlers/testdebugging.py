from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from pathlib import Path

from localstack.aws.api import RequestContext
from localstack.aws.chain import HandlerChain
from localstack.http import Response

LOG = logging.getLogger(__name__)

# TODO: unify this with the type in the pytest plugin
TestKey = tuple[str, str, str]

Call = tuple[str, str]


class Database:
    def __init__(self, output_path: Path | str):
        self.conn = sqlite3.connect(output_path)
        self.initialise()

    def initialise(self):
        with self.conn as conn:
            conn.execute("drop table if exists api_calls")
            conn.execute(
                """
            create table api_calls (
                id integer primary key,
                test_key text not null,
                api_calls blob not null
                )
                """
            )

    def add(self, test_key: TestKey, api_calls: list[Call]):
        with self.conn as conn:
            conn.execute(
                "insert into api_calls (test_key, api_calls) values (?, ?)",
                (":".join(test_key), json.dumps(api_calls)),
            )


class LifetimeCapturer:
    def __init__(self, store: TestResourceLifetimesCapture, test_key: TestKey):
        self.store = store
        self.test_key = test_key

    def __enter__(self):
        LOG.debug("*** starting capture")
        self.store.current_test_key = self.test_key
        return self

    def __exit__(self, *args):
        # update store with information
        LOG.debug("*** ending capture")
        self.store.commit()
        self.store.current_test_key = None


class TestResourceLifetimesCapture:
    """
    Captures traces of resources by test name to determine resources that are left over at the
    end of a test
    """

    db: Database
    # TODO: what if there are multiple calls to create the same resource in the same test?
    results: dict[TestKey, list[Call]]
    current_test_key: TestKey | None

    def __init__(self, output_path: Path | str):
        self.db = Database(output_path)
        self.results = defaultdict(list)
        self.current_test_key = None

    # TODO: test key should be a shared type
    def capture(self, test_key: TestKey) -> LifetimeCapturer:
        return LifetimeCapturer(self, test_key)

    def commit(self):
        self.db.add(self.current_test_key, self.results[self.current_test_key])

    def __call__(self, chain: HandlerChain, context: RequestContext, response: Response):
        if not context.service or not context.operation:
            return

        service = context.service.service_name
        operation = context.operation.name

        # not in a test context
        if self.current_test_key is None:
            return

        LOG.warning("*** capturing call %s.%s", service, operation)
        self.results[self.current_test_key].append((service, operation))
