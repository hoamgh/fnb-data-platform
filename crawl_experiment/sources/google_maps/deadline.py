"""Cooperative per-attempt budget; never cancels an in-flight WebDriver call."""
import time


class StoreAttemptTimeout(TimeoutError):
    pass


class AttemptDeadline:
    def __init__(self, end, clock):
        self.end, self.clock = end, clock
        self.driver = None
        self.original_timeouts = None

    def check(self):
        remaining = self.end - self.clock()
        if remaining <= 0:
            raise StoreAttemptTimeout('Store attempt time budget exhausted')
        return remaining

    def sleep(self, seconds, sleeper=time.sleep):
        sleeper(min(seconds, self.check()))
        self.check()

    def prepare(self):
        remaining = self.check()
        if self.original_timeouts is not None:
            page, script, implicit = self.original_timeouts
            self.driver.set_page_load_timeout(min(page, remaining))
            self.driver.set_script_timeout(min(script, self.check()))
            self.driver.implicitly_wait(min(implicit, self.check()))
        self.check()

    def bind(self, component):
        component.check_deadline = self.check
        component.prepare_interaction = self.prepare
        component.clock = self.clock
        sleeper = getattr(component, 'sleeper', time.sleep)
        component.sleeper = lambda seconds: self.sleep(seconds, sleeper)

    def configure(self, driver):
        self.check()
        timeouts = getattr(driver, 'timeouts', None)
        if timeouts is not None:
            self.driver = driver
            self.original_timeouts = (timeouts.page_load, timeouts.script, timeouts.implicit_wait)
            self.prepare()

    def restore(self):
        # Configuration cleanup is allowed after expiry; no page work is begun.
        if self.original_timeouts is not None:
            page, script, implicit = self.original_timeouts
            self.driver.set_page_load_timeout(page)
            self.driver.set_script_timeout(script)
            self.driver.implicitly_wait(implicit)
