#!/usr/bin/env python

import argparse
import json
import logging
import sqlite3
from collections import defaultdict

from localstack.utils.bootstrap import setup_logging

LOG = logging.getLogger("find_leaking_test_resources")

METHOD_PAIRS = {
    "kms": {
        "create": [
            "CreateKey",
        ],
        "delete": [
            "ScheduleKeyDeletion",
        ],
    },
}

if __name__ == "__main__":
    setup_logging()

    parser = argparse.ArgumentParser()
    parser.add_argument("db")
    parser.add_argument("-o", "--output", type=argparse.FileType("w"), default="-")
    args = parser.parse_args()

    test_report = defaultdict(dict)
    with sqlite3.connect(args.db) as conn:
        cursor = conn.cursor()
        cursor.execute("select test_key, api_calls from api_calls")

        for test_key, api_calls_raw in cursor:
            LOG.debug("test: %s", test_key)
            api_calls = json.loads(api_calls_raw)

            services = set(service for (service, operation) in api_calls)
            for tested_service in services:
                # skip services where we have not yet defined the method pairs
                if tested_service not in METHOD_PAIRS:
                    LOG.debug("service %s not defined", tested_service)
                    continue

                LOG.debug("testing %s", tested_service)
                called_methods = [
                    (service, operation)
                    for (service, operation) in api_calls
                    if service == tested_service
                ]

                service_methods = METHOD_PAIRS[tested_service]

                created_score = len(
                    [
                        method
                        for (_, method) in called_methods
                        if method in service_methods["create"]
                    ]
                )
                deleted_score = len(
                    [
                        method
                        for (_, method) in called_methods
                        if method in service_methods["delete"]
                    ]
                )

                if created_score > deleted_score:
                    report_message = "not enough deletes"
                elif created_score < deleted_score:
                    report_message = "too many deletes"
                else:
                    continue

                operations = [operation for (_, operation) in called_methods]
                test_report[test_key][tested_service] = {
                    "outcome": report_message,
                    "operations": [operation for (_, operation) in called_methods],
                }

                LOG.error(
                    "test %s has unbalanced resource creation with %s operations; %s: %s",
                    test_key,
                    tested_service,
                    report_message,
                    operations,
                )

        json.dump(test_report, args.output, indent=2)
