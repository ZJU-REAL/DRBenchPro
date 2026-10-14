"""Wikipedia HTTP client and title/url normalization helpers."""

from __future__ import annotations

from dataclasses import dataclass
from threading import local
from urllib.parse import quote, unquote, urlparse

import requests
from requests import HTTPError, RequestException

from drbenchpro.common.text import normalize_wikipedia_title


class WikipediaRequestError(RuntimeError):
    """Raised when Wikipedia page fetch fails."""

    def __init__(self, message: str, *, attempt_count: int = 0):
        super().__init__(message)
        self.attempt_count = max(0, int(attempt_count))


@dataclass
class WikipediaPage:
    """Container for fetched Wikipedia page response data."""

    requested_title: str
    final_title: str
    url: str
    html: str
    attempt_count: int = 1


class WikipediaClient:
    """Small client for fetching English Wikipedia pages and random roots."""

    def __init__(
        self,
        language: str = "en",
        timeout: int = 15,
        max_retries: int = 0,
        user_agent: str = "DRBenchProBot/0.1",
    ):
        self.language = language
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.base_url = f"https://{language}.wikipedia.org"
        self.user_agent = user_agent
        self._thread_local = local()

    def _get_session(self) -> requests.Session:
        """Use one requests session per thread for concurrent page fetches."""
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            # Keep wiki crawling aligned with shell/system proxy env vars.
            session.trust_env = True
            session.headers.update({"User-Agent": self.user_agent})
            self._thread_local.session = session
        return session

    def build_page_url(self, title: str) -> str:
        """Build canonical Wikipedia page URL for a title."""
        normalized = normalize_wikipedia_title(title).replace(" ", "_")
        return f"{self.base_url}/wiki/{quote(normalized)}"

    def extract_title_from_url(self, url: str) -> str:
        """Extract normalized page title from final URL."""
        parsed = urlparse(url)
        marker = "/wiki/"
        if marker not in parsed.path:
            return ""
        raw = parsed.path.split(marker, 1)[1]
        return normalize_wikipedia_title(unquote(raw))

    def fetch_page(self, title: str) -> WikipediaPage:
        """Fetch one Wikipedia page by title, following redirects."""
        if not title or not title.strip():
            raise WikipediaRequestError("Empty title is not allowed.", attempt_count=0)

        requested_title = normalize_wikipedia_title(title)
        url = self.build_page_url(requested_title)
        response, attempt_count = self._get_with_retries(
            url,
            request_label=f"fetch '{requested_title}'",
            allow_redirects=True,
        )

        final_url = response.url
        final_title = self.extract_title_from_url(final_url) or requested_title
        return WikipediaPage(
            requested_title=requested_title,
            final_title=final_title,
            url=final_url,
            html=response.text,
            attempt_count=attempt_count,
        )

    def get_random_title(self) -> str:
        """Fetch one random Wikipedia title through Special:Random redirect."""
        random_url = f"{self.base_url}/wiki/Special:Random"
        response, attempt_count = self._get_with_retries(
            random_url,
            request_label="get random root",
            allow_redirects=True,
            stream=True,
        )
        with response:
            final_url = response.url

        title = self.extract_title_from_url(final_url)
        if not title or "Special:Random" in final_url:
            raise WikipediaRequestError(f"Unexpected random redirect target: {final_url}", attempt_count=attempt_count)
        return title

    def _get_with_retries(self, url: str, *, request_label: str, **kwargs: object) -> tuple[requests.Response, int]:
        """Issue one GET request with immediate retry attempts on transient failures."""
        last_error: RequestException | None = None
        total_attempts = self.max_retries + 1
        attempted = 0

        for attempt in range(1, total_attempts + 1):
            attempted = attempt
            try:
                response = self._get_session().get(url, timeout=self.timeout, **kwargs)
                response.raise_for_status()
                return response, attempt
            except RequestException as exc:
                last_error = exc
                if attempt >= total_attempts or not self._should_retry_exception(exc):
                    break

        if last_error is None:
            raise WikipediaRequestError(f"Failed to {request_label}: unknown request error", attempt_count=attempted)
        if total_attempts <= 1:
            raise WikipediaRequestError(f"Failed to {request_label}: {last_error}", attempt_count=attempted) from last_error
        raise WikipediaRequestError(
            f"Failed to {request_label} after {attempted} attempts: {last_error}",
            attempt_count=attempted,
        ) from last_error

    def _should_retry_exception(self, exc: RequestException) -> bool:
        """Retry transient network failures and retry-friendly HTTP status codes."""
        if isinstance(exc, HTTPError):
            status_code = int(exc.response.status_code) if exc.response is not None else 0
            return status_code == 408 or status_code == 429 or 500 <= status_code <= 599
        return True
