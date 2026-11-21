"""pytest plugin: record the ids of failing tests (any phase, any subtest) to a JSON file."""
import json
import os

OUT = os.environ['BR_LABEL_OUT']


class Rec:
    def __init__(self):
        self.failed = set()
        self.collect_errors = set()
        self.durations = {}

    def pytest_runtest_logreport(self, report):
        if report.outcome == 'failed':
            self.failed.add(report.nodeid)
        self.durations[report.nodeid] = self.durations.get(report.nodeid, 0.0) + report.duration

    def pytest_collectreport(self, report):
        if report.failed:
            self.collect_errors.add(report.nodeid)

    def pytest_sessionfinish(self, session):
        if hasattr(session.config, 'workerinput'):
            return
        with open(OUT, 'w') as f:
            json.dump({'failed': sorted(self.failed), 'collect_errors': sorted(self.collect_errors),
                       'durations': self.durations}, f)


def pytest_configure(config):
    config.pluginmanager.register(Rec(), 'br_rec')
