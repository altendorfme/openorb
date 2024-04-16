import httpx
from newspaper import fulltext


def fetch_content(url: str) -> str:
    try:
        # Disable SSL verification
        client = httpx.Client(verify=False)
        response = client.get(url)
        article = fulltext(response.text)
        return article
    except Exception as e:
        print("Error fetching content: " + str(e))
        return ""
