"""
Shared Brave Web Search helper for newsletter section scripts.

Call `search_web(query_specs, api_key, trusted_domains=...)` with a list of
query dicts. Each dict must have a "q" key (the search string); any other
keys are attached to every result row from that query (useful for tagging
results with the topic/category/section they came from).
"""
import time
import requests

DEFAULT_MAX_RESULTS = 10
DEFAULT_PAUSE_BETWEEN = 0.5


def domain_of(url: str) -> str:
    """Strip scheme, path, and leading 'www.' — return bare host."""
    try:
        host = url.split("//", 1)[-1].split("/", 1)[0].lower()
        return host.removeprefix("www.")
    except Exception:
        return ""


def is_trusted_domain(url: str, trusted_domains: set) -> bool:
    """True if the URL's host is in trusted_domains (allowing subdomains)."""
    host = domain_of(url)
    return any(host == d or host.endswith("." + d) for d in trusted_domains)


def search_web(
    query_specs: list[dict],
    api_key: str,
    trusted_domains: set | None = None,
    max_per_query: int = DEFAULT_MAX_RESULTS,
    pause_between: float = DEFAULT_PAUSE_BETWEEN,
) -> list[dict]:
    """
    Run Brave Web Search across a list of query specs.

    query_specs: [{"q": "...", ...extras to tag onto every result from this query}, ...]
    api_key: Brave API subscription token
    trusted_domains: if set, drop results whose host isn't in the set
    max_per_query: count parameter to pass Brave per call
    pause_between: seconds to sleep between queries (rate-limit buffer)

    Returns a deduped (by URL) list of result dicts with keys:
      title, url, source, summary, + any extras from the query spec.
    """
    headers = {
        "Accept":              "application/json",
        "Accept-Encoding":     "gzip",
        "X-Subscription-Token": api_key,
    }

    all_results = []
    seen_urls = set()

    for spec in query_specs:
        q = spec["q"]
        extras = {k: v for k, v in spec.items() if k != "q"}
        print(f"  Searching: {q}")
        try:
            res = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers=headers,
                params={"q": q, "count": max_per_query},
                timeout=30,
            )
            if res.status_code != 200:
                print(f"    Brave API status {res.status_code}")
                continue

            web_results = res.json().get("web", {}).get("results", [])
            kept = 0
            for item in web_results:
                url = item.get("url", "")
                if not url or url in seen_urls:
                    continue
                if trusted_domains is not None and not is_trusted_domain(url, trusted_domains):
                    continue
                seen_urls.add(url)
                all_results.append({
                    "title":   item.get("title", ""),
                    "url":     url,
                    "source":  domain_of(url),
                    "summary": item.get("description", ""),
                    **extras,
                })
                kept += 1
            filter_label = "trusted-domain" if trusted_domains else "total"
            print(f"    Kept {kept} {filter_label} results out of {len(web_results)}")

        except Exception as e:
            print(f"    Brave API error: {e}")

        time.sleep(pause_between)

    return all_results
