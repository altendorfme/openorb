import asyncio
import aiohttp
from bs4 import BeautifulSoup
from scraper import fetch_content
import feedparser
import sqlite3
import hashlib
from typing import TypedDict
from datetime import datetime
from urllib.parse import urlparse
import redis
from config import load_config

config = load_config()

r = redis.Redis(host=config["redis_host"], port=6379, decode_responses=True)


class FeedParserOutput(TypedDict):
    feed: dict
    entries: list[dict]


class Feed(TypedDict):
    id: int
    site_url: str
    feed_url: str
    config_url: str
    title: str


class Entry(TypedDict):
    id: int
    feed_id: int
    title: str
    link: str
    published: str
    content: str
    content_type: str
    content_hash: str
    author: str
    feed_title: str
    feed_link: str


def hash(content):
    return hashlib.sha256(content.encode()).hexdigest()


def create_db():
    conn = sqlite3.connect("./data/index.db")
    c = conn.cursor()
    c.execute(
        "CREATE TABLE IF NOT EXISTS feeds (id INTEGER PRIMARY KEY, site_url TEXT UNIQUE, feed_url TEXT UNIQUE, config_url TEXT, title TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS entries (id INTEGER PRIMARY KEY, feed_id INTEGER, title TEXT, link TEXT UNIQUE, published TEXT, content TEXT, content_type TEXT, content_hash TEXT, author TEXT, FOREIGN KEY(feed_id) REFERENCES feeds(id))")
    # Create indexes
    c.execute("CREATE INDEX IF NOT EXISTS title_index ON entries (title)")
    c.execute("CREATE INDEX IF NOT EXISTS content_index ON entries (content)")
    conn.commit()
    conn.close()


def get_or_insert_feed(feed: dict, config_url: str) -> tuple[int, bool]:
    conn = sqlite3.connect("./data/index.db")
    c = conn.cursor()
    maybe_feed = c.execute(
        "SELECT * FROM feeds WHERE config_url = ?", (config_url,)).fetchone()
    if maybe_feed:
        print("Feed already exists: " + feed["title"])
        return (maybe_feed[0], True)
    feed_url = [link["href"] for link in feed["links"]
                if link["rel"] == "self"] if "links" in feed else []
    if len(feed_url) > 0:
        feed_url = feed_url[0]
    else:
        feed_url = feed["link"]
    feed_title = feed["title"] if "title" in feed and feed["title"] != "" else urlparse(
        feed["link"]).netloc
    c.execute("INSERT INTO feeds (site_url, feed_url, config_url, title) VALUES (?, ?, ?, ?)",
              (feed["link"], feed_url, config_url, feed_title))
    conn.commit()
    conn.close()
    print("Inserted feed: " + feed["title"])
    last_row_id = c.lastrowid
    if last_row_id:
        return (last_row_id, False)
    last_row_id = c.execute(
        "SELECT id FROM feeds WHERE config_url = ?", (config_url,)).fetchone()[0]
    return (last_row_id, False)

# From https://www.alexmolas.com/2024/02/05/a-search-engine-in-80-lines.html


def clean_content(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
    for script in soup(["script", "style"]):
        script.extract()
    text = soup.get_text()
    lines = (line.strip() for line in text.splitlines())
    chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
    cleaned_text = " ".join(chunk for chunk in chunks if chunk)
    return cleaned_text


def insert_entry(feed_id, entry):
    print("  Inserting entry: " + entry["title"])
    content = ""
    if "content" in entry:
        content = entry["content"][0]["value"]
    elif "summary" in entry:
        content = entry["summary"]
    elif "description" in entry:
        content = entry["description"]
    cleaned_content = clean_content(content)
    if len(cleaned_content) == 0:
        print("  Entry has no content, attempting to scrape from article link")
        content = fetch_content(entry["link"])
        cleaned_content = clean_content(content)
        if len(cleaned_content) == 0:
            print("  Entry still has no content, skipping: " + entry["title"])
            return
    content_type = entry["content"][0]["type"] if "content" in entry else None
    content_hash = hash(cleaned_content)
    entry_date = entry["published_parsed"] if "published_parsed" in entry else entry["updated_parsed"] if "updated_parsed" in entry else None
    if entry_date:
        entry_date = datetime(*entry_date[:6]).isoformat()
    entry_author = entry["author"] if "author" in entry else None
    conn = sqlite3.connect("./data/index.db")
    c = conn.cursor()
    maybe_entry = c.execute(
        "SELECT * FROM entries WHERE content_hash = ?", (hash(cleaned_content),)).fetchone()
    if maybe_entry:
        # We already have this exact entry, we can skip it
        print("  Skipping entry: " + entry["title"])
        return
    c.execute("INSERT INTO entries (feed_id, title, link, published, content, content_type, content_hash, author) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
              (feed_id, entry["title"], entry["link"], entry_date, cleaned_content, content_type, content_hash, entry_author))
    conn.commit()
    conn.close()
    print("  Inserted entry: " + entry["title"])


def cleanup_feeds():
    """
    Remove feeds from database which have been removed from the configuration
    """
    for feed in fetch_feeds():
        if feed["config_url"] not in config["feeds"]:
            print("Removing feed: " + feed["feed_url"])
            conn = sqlite3.connect("./data/index.db")
            c = conn.cursor()
            c.execute("DELETE FROM feeds WHERE id = ?", (feed["id"],))
            c.execute("DELETE FROM entries WHERE feed_id = ?", (feed["id"],))
            conn.commit()
            conn.close()


async def fetch_feed(session, url):
    try:
        async with asyncio.timeout(10):
            async with session.get(url) as response:
                return await response.text()
    except Exception as e:
        print("Error fetching feed: " + str(e))
        return ""


async def parse_feeds() -> list[tuple[str, FeedParserOutput]]:
    feeds = []

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(verify_ssl=False)) as session:
        tasks = [fetch_feed(session, feed_url)
                 for feed_url in config["feeds"]]
        feed_responses = await asyncio.gather(*tasks)
        for feed, html in zip(config["feeds"], feed_responses):
            parsed_feed = feedparser.parse(html)
            feeds.append((feed, parsed_feed))

    return feeds

def get_feed_last_updated(feed: FeedParserOutput) -> datetime:
    if "updated_parsed" in feed["feed"]:
        return datetime(*feed["feed"]["updated_parsed"][:6])
    # If the feed doesn't have an updated time, we'll use the latest entry time
    latest_entry = max(feed["entries"], key=lambda x: x["published_parsed"])
    return datetime(*latest_entry["published_parsed"][:6])

def insert_feeds(feeds: list[tuple[str, FeedParserOutput]]):
    for feed in feeds:
        if "feed" not in feed[1] or not feed[1]["feed"]:
            print("Skipping feed due to parsing error")
            continue
        (feed_id, feed_existed) = get_or_insert_feed(feed[1]["feed"], feed[0])
        # Check if the feed last updated time is later than our last crawl time for this feed
        # If it is, we should crawl it again
        if feed_existed:
            last_updated = get_feed_last_updated(feed[1])
            print("Feed last updated: " + str(last_updated))
            if last_updated:
                last_crawl = r.get(f"last_crawl:{feed_id}")
                if last_crawl is not None:
                    last_crawl = datetime.fromisoformat(str(last_crawl))
                    if last_updated < last_crawl:
                        print("Feed is up to date, skipping: " +
                              feed[1]["feed"]["title"])
                        continue
        r.set(f"last_crawl:{feed_id}", datetime.now().isoformat())
        for entry in feed[1]["entries"]:
            insert_entry(feed_id, entry)


def fetch_entries() -> list[Entry]:
    conn = sqlite3.connect("./data/index.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    columns = ", ".join([
        "entries.id",
        "entries.feed_id",
        "entries.title",
        "entries.link",
        "entries.published",
        "entries.content",
        "entries.content_type",
        "entries.content_hash",
        "entries.author",
        "feeds.title AS feed_title",
        "feeds.feed_url AS feed_link"
    ])
    documents = c.execute(f"SELECT {
        columns} FROM entries INNER JOIN feeds ON entries.feed_id = feeds.id ORDER BY entries.published DESC").fetchall()
    conn.close()
    return documents


def fetch_entries_for_feed(feed_id: int) -> list[Entry]:
    conn = sqlite3.connect("./data/index.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    columns = ", ".join([
        "entries.id",
        "entries.feed_id",
        "entries.title",
        "entries.link",
        "entries.published",
        "entries.content",
        "entries.content_type",
        "entries.content_hash",
        "entries.author",
        "feeds.title AS feed_title",
        "feeds.feed_url AS feed_link"
    ])
    documents = c.execute(f"SELECT {
        columns} FROM entries INNER JOIN feeds ON entries.feed_id = feeds.id WHERE feed_id = ? ORDER BY entries.published DESC", (feed_id,)).fetchall()
    conn.close()
    return documents


def fetch_feeds() -> list[Feed]:
    conn = sqlite3.connect("./data/index.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    feeds = c.execute("SELECT * FROM feeds").fetchall()
    conn.close()
    return feeds


async def crawl(force=False) -> bool:
    create_db()
    cleanup_feeds()

    print("Crawling...")

    if not force:
        last_crawl = r.get("last_crawl")
        if last_crawl is not None:
            last_crawl = datetime.fromisoformat(str(last_crawl))
            if (datetime.now() - last_crawl).seconds < 60 * 15:  # 15 minutes
                print("Crawling too soon, skipping")
                return False

    r.set("last_crawl", datetime.now().isoformat())
    feeds = await parse_feeds()
    insert_feeds(feeds)
    return True
