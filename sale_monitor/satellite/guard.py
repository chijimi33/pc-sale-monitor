"""The fail-closed boundary for all satellite HTTP attempts."""
from email.utils import parsedate_to_datetime
import math
import re
import time
from urllib.parse import urlsplit
import uuid

from ..http import Client, FetchError, Page
from .protocol import (ALLOWED_HOSTS, MIN_INTERVAL_FLOOR_SECONDS, NO_RESPONSE_LIMIT,
                       require, sha256_bytes, time_string)

# A refusal or a challenge is recognised by what the page says to a person
# (title and visible text) or by a challenge widget in the document. Words in
# comments, scripts and styles do not count: ordinary pages load a reCAPTCHA
# library everywhere. Body size is never a reason to skip the inspection.
REFUSAL_MARKERS = ("access denied", "captcha", "are you a robot", "unusual traffic",
                   "アクセスが拒否", "アクセスを拒否", "不正なアクセス", "ロボットではありません")
_CHALLENGE_CLASSES = ("g-recaptcha", "h-captcha", "cf-turnstile")
_CHALLENGE_FIELDS = ("g-recaptcha-response", "h-captcha-response", "cf-turnstile-response")
_CHALLENGE_XPATH = " | ".join(
    [f'//*[contains(concat(" ", normalize-space(@class), " "), " {name} ")]' for name in _CHALLENGE_CLASSES]
    + [f'//*[@name="{name}"]' for name in _CHALLENGE_FIELDS]
    + ['//iframe[contains(translate(@src, "CAPTCHA", "captcha"), "captcha")]'])
_INVISIBLE = re.compile(r"(?is)<!--.*?-->|<(script|style|template)\b.*?</\1\s*>|<[^>]+>")


def _normalise(text):
    return " ".join(text.casefold().split())


def refusal_feature(text):
    """Return the marker or widget that makes this response a refusal, else None."""
    visible, widget = text, False
    if "<" in text:
        try:
            from lxml import html
            tree = html.document_fromstring(text.encode("utf-8"), parser=html.HTMLParser(
                remove_comments=True, remove_pis=True, encoding="utf-8"))
            widget = bool(tree.xpath(_CHALLENGE_XPATH))
            for element in tree.xpath("//script | //style | //template"):
                element.drop_tree()
            visible = tree.text_content()
        except Exception:
            # Unparseable markup: inspect the text without its invisible parts.
            visible = _INVISIBLE.sub(" ", text)
    visible = _normalise(visible)
    marker = next((marker for marker in REFUSAL_MARKERS if _normalise(marker) in visible), None)
    return marker or ("challenge_widget" if widget else None)


class CommunicationForbidden(FetchError):
    pass


def allowed_satellite_url(url, store):
    if type(url) is not str or store not in ALLOWED_HOSTS:
        return False
    try:
        parts = urlsplit(url)
        return (not re.search(r"[\x00-\x20\x7f\\]", url)
                and parts.scheme == "https" and parts.hostname == ALLOWED_HOSTS[store]
                and parts.port in (None, 443) and parts.username is None and parts.password is None
                and parts.netloc.lower() in (ALLOWED_HOSTS[store], ALLOWED_HOSTS[store] + ":443"))
    except ValueError:
        return False


class Guard:
    def __init__(self, store, control, deadline, collect_until, *, min_interval_seconds=5,
                 ticket=None, input_retry_after=None, wall_clock=time.time, sleep=time.sleep,
                 refusal_id=lambda: uuid.uuid4().hex, robots_transport=None, progress=None):
        require(store in ALLOWED_HOSTS, "invalid_store")
        require(type(min_interval_seconds) in (int, float) and math.isfinite(min_interval_seconds)
                and min_interval_seconds >= 0, "invalid_interval")
        self.store, self.control, self.deadline = store, control, deadline
        self.collect_until = collect_until
        self.interval = max(MIN_INTERVAL_FLOOR_SECONDS, min_interval_seconds)
        self.wall_clock, self.sleep, self.refusal_id = wall_clock, sleep, refusal_id
        self.robots_transport = robots_transport
        self.progress = progress
        self.input_retry_after = input_retry_after or {}
        self.communication_forbidden = False
        self.reason = None
        self.consecutive_no_response = 0
        self.counters = dict.fromkeys(("requests", "ordinary_attempted", "ordinary_succeeded", "no_response", "refusal"), 0)
        reason = control.decline_reason(ticket or {})
        if reason:
            self.communication_forbidden, self.reason = True, reason

    def forbid(self, reason):
        self.communication_forbidden, self.reason = True, reason
        self._checkpoint()
        raise CommunicationForbidden(reason)

    def _checkpoint(self):
        if self.progress is not None:
            self.progress(self)

    def check_url(self, url):
        """Preflight before robots or other request-producing policy checks."""
        if self.communication_forbidden:
            self.forbid(self.reason)
        if not allowed_satellite_url(url, self.store):
            # A bad task/link is a local policy failure, not a store refusal.
            raise CommunicationForbidden("excluded_source")

    def _check(self, url):
        self.check_url(url)
        if self.control.active_refusals:
            self.forbid("refused")
        if not self.deadline.may_request(self.collect_until):
            self.forbid(self.deadline.reason or "deadline")
        host = ALLOWED_HOSTS[self.store]
        until = self.control.retry_until(host, self.input_retry_after.get(host, 0))
        if until > self.wall_clock():
            self.forbid("retry_after")

    def before_request(self, url):
        self._check(url)
        host = ALLOWED_HOSTS[self.store]
        previous = self.control.data["last_request_at"].get(host)
        if previous is not None:
            wait = previous + self.interval - self.wall_clock()
            if wait > 0:
                self.sleep(wait)
        # Waiting must never extend the collection deadline.
        self._check(url)
        now = self.wall_clock()
        if previous is not None and now - previous < self.interval:
            self.forbid("clock_unverified")
        self.control.request_started(host, now)
        self.counters["requests"] += 1
        if not self._robots(url):
            self.counters["ordinary_attempted"] += 1
        self._checkpoint()
        # Persistence can block or span a suspend.
        self._check(url)

    @staticmethod
    def _robots(url):
        return urlsplit(url).path == "/robots.txt"

    def after_response(self, url, status, headers, body, *, parseable=True):
        self.consecutive_no_response = 0
        marker = refusal_feature(Page(url, body, "", content_type=next(
            (v for k, v in headers.items() if k.lower() == "content-type"), "")).text)
        if status in (401, 403) or marker:
            row = {"refusal_id": self.refusal_id(), "at": time_string(self.wall_clock()),
                   "kind": "http_" + str(status) if status in (401, 403) else "body_marker",
                   "url_sha256": sha256_bytes(url.encode()), "store": self.store}
            # Forbid even if persistence itself fails; never silently continue.
            try:
                self.control.add_refusal(row)
            finally:
                self.communication_forbidden, self.reason = True, "refused"
            self.counters["refusal"] += 1
            self.forbid("refused")
        retry = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
        if status == 429 or retry is not None:
            now = self.wall_clock()
            try:
                seconds = float(retry)
                if not math.isfinite(seconds):
                    raise ValueError("invalid_retry")
                until = now + max(0, seconds)
            except (TypeError, ValueError):
                try:
                    date = parsedate_to_datetime(retry)
                    if date.tzinfo is None:
                        raise ValueError("invalid_retry")
                    until = max(now, date.timestamp())
                except (TypeError, ValueError, OverflowError):
                    until = now + 300
            try:
                self.control.set_retry_after(ALLOWED_HOSTS[self.store], until)
            finally:
                self.communication_forbidden, self.reason = True, "retry_after"
            self.forbid("retry_after")
        # 404 bodies reach the collector's missing-product classifier.
        if parseable and not self._robots(url) and (200 <= status < 300 or status == 404):
            self.counters["ordinary_succeeded"] += 1
        self._checkpoint()

    def after_no_response(self, url, exc):
        if not isinstance(exc, (OSError, TimeoutError)):
            return
        self.consecutive_no_response += 1
        self.counters["no_response"] += 1
        self._checkpoint()
        if self.consecutive_no_response >= NO_RESPONSE_LIMIT:
            self.forbid("no_response")

    def fetch_robots(self, url):
        """Single attempt, no redirect navigation, suitable for Robots(fetch=)."""
        if self.robots_transport is not None:
            self.before_request(url)
            try:
                status, headers, body = self.robots_transport(url)
            except (OSError, TimeoutError) as exc:
                self.after_no_response(url, exc)
                raise
            self.after_response(url, status, headers, body)
            return status, body.decode("utf-8", "replace")
        client = Client(guard=self, delay=0, timeout=15)
        try:
            page = client.get(url, max_attempts=1)
        except CommunicationForbidden:
            raise
        except FetchError as exc:
            if exc.page is None:
                raise
            page = exc.page
        return page.status, page.text
