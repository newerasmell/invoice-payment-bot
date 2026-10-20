import asyncio
import logging
import re
from functools import lru_cache
from typing import Any

log = logging.getLogger(__name__)

# Only local files (logos) and Google Fonts may be fetched while rendering a PDF.
_ALLOWED_REMOTE = re.compile(r"^https://fonts\.(googleapis|gstatic)\.com/")


class PdfUnavailable(RuntimeError):
    """WeasyPrint or its system libraries (Pango) are missing."""


@lru_cache(maxsize=1)
def _weasyprint() -> Any:
    try:
        import weasyprint  # imported lazily: needs Pango system libraries
    except (ImportError, OSError) as e:
        raise PdfUnavailable(f"PDF engine unavailable: {e}") from e
    return weasyprint


def pdf_engine_status() -> str:
    try:
        return f"weasyprint {_weasyprint().__version__}"
    except PdfUnavailable as e:
        return str(e)


@lru_cache(maxsize=1)
def _fetcher() -> Any:
    wp = _weasyprint()

    cache: dict[str, tuple[bytes, dict[str, str], str]] = {}

    class AllowlistFetcher(wp.urls.URLFetcher):
        """Local files plus Google Fonts only; fonts are cached in memory after first use."""

        def fetch(self, url: str, headers: Any = None) -> Any:
            if url.startswith("file://"):
                return super().fetch(url, headers)
            if not _ALLOWED_REMOTE.match(url):
                raise ValueError(f"blocked resource in invoice HTML: {url}")
            if url not in cache:
                resp = super().fetch(url, headers)
                try:
                    cache[url] = (resp.read(), dict(resp.headers), resp.url)
                finally:
                    resp.close()
            body, hdrs, final_url = cache[url]
            return wp.urls.URLFetcherResponse(final_url, body, hdrs)

    return AllowlistFetcher(timeout=5)


def _render(html: str) -> bytes:
    wp = _weasyprint()
    return wp.HTML(string=html, url_fetcher=_fetcher()).write_pdf()


async def html_to_pdf(html: str) -> bytes:
    return await asyncio.to_thread(_render, html)
