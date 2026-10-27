"""Test package.

Logging is silenced here because several tests deliberately exercise failure
paths, and the resulting WARNING/traceback output would otherwise bury the
test results.
"""

import logging

logging.getLogger("heli_notifier").setLevel(logging.CRITICAL)
logging.getLogger("heli_notifier").addHandler(logging.NullHandler())
