import asyncio
from datetime import datetime
import hashlib
import json
import threading
from flask import Flask, render_template, request
import redis
from config import load_config
import crawler
from microsearch import SearchEngine

config = load_config()

r = redis.Redis(host=config["redis_host"], port=6379, decode_responses=True)

app = Flask(__name__)

entries = []
engine = SearchEngine()


def run_crawler(force=False):
    crawl_response = asyncio.run(crawler.crawl(force))
    if crawl_response:
        entries = crawler.fetch_entries()
        engine.bulk_index([(entry["link"], entry["content"])
                          for entry in entries])
        # Clear Redis cache for the search engine
        r.delete("avdl")
    return crawl_response


# Run the crawler on startup
t = threading.Thread(target=run_crawler, args=(True,))
t.start()


def hash(content):
    return hashlib.sha256(content.encode()).hexdigest()


def get_top_urls(scores_dict: dict, n: int):
    sorted_urls = sorted(scores_dict.items(), key=lambda x: x[1], reverse=True)
    top_n_urls = sorted_urls[:n]
    top_n_dict = [{"url": url, "score": score} for url, score in top_n_urls]
    return top_n_dict


@app.context_processor
def inject_config():
    return dict(config=config)


@ app.route("/")
def redirect_to_search():
    return search()


@app.route("/feeds")
def feeds():
    """
    List all feeds being indexed
    """
    feeds = crawler.fetch_feeds()
    return render_template("feeds.html", feeds=feeds)


@ app.route("/search")
def search():
    query = request.args.get("query")
    query = query[:80].strip() if query else ""
    query_hash = hash(query)
    sort = request.args.get("sort") or "relevance"
    if not query:
        return render_template("search.html", results=[])
    print("Searching for: " + query)
    # Check if query is in cache
    result_cached = False
    if r.exists(f"query:{query_hash}"):
        print("Cache hit")
        result_cached = True
        results = str(r.get(f"query:{query_hash}"))
        results = json.loads(results)
    else:
        print("Cache miss")
        results = engine.search(query)
        r.set(f"query:{query_hash}", json.dumps(results))
        r.expire(f"query:{query_hash}", 60 * 60)  # Cache for 1 hour
    # top_results = get_top_urls(results, 10)
    entries = crawler.fetch_entries()
    full_results = []
    for url, score in results.items():
        entry = next(
            (entry for entry in entries if entry["link"] == url), None)
        if not entry:
            continue
        # pprint.pprint(dict(entry))
        full_results.append({
            "title": entry["title"],
            "link": entry["link"],
            "score": score,
            "author": entry["author"],
            "published": entry["published"] if entry["published"] else datetime.now().isoformat(),
            "published_formatted": datetime.strptime(entry["published"], "%Y-%m-%dT%H:%M:%S").strftime("%d %b %Y") if entry["published"] else None,
            "feed_title": entry["feed_title"],
            "feed_link": entry["feed_link"]
        })
    if sort == "date":
        full_results = sorted(
            full_results, key=lambda x: x["published"], reverse=True)
    elif sort == "relevance":
        full_results = sorted(
            full_results, key=lambda x: x["score"], reverse=True)
    full_results = [
        result for result in full_results if result["score"] > config["score_threshold"]]
    last_crawl = str(r.get("last_crawl")).split(".")[0]
    return render_template("search.html", results=full_results, query=query, sort=sort, result_cached=result_cached, last_crawl=last_crawl)


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/api/crawl", methods=["POST"])
def api_crawl():
    t = threading.Thread(target=run_crawler)
    t.start()
    return {"status": "ok"}


if __name__ == '__main__':
    app.run(debug=True)
