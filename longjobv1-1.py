import time
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from seleniumbase import sb_cdp
import requests

import os

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GITHUB_TOKEN = os.environ["GH_DISPATCH_TOKEN"]
GITHUB_REPOSITORY = os.environ["GITHUB_REPOSITORY"]


def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    response = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
        },
        timeout=30,
    )

    response.raise_for_status()


BASE_URL = (
    "https://www.lazada.sg/pokemon-store-online-singapore/"
    "?q=All-Products&shop_category_ids=762252&from=wangpu"
)


# --- Continuous polling settings ---
PAGE_SIZE = 40
SGT = ZoneInfo("Asia/Singapore")
# Target seconds between cycle starts. If a cycle takes longer than this, the next cycle starts immediately with no extra delay;
POLL_INTERVAL = 5  
# Only send a Telegram alert when the set of in-stock items actually changes
ALERT_ON_NO_CHANGE_EVERY = 120  # still send a heartbeat every N cycles
EMPTY_STREAK_ALERT_THRESHOLD = 2
# Hard ceiling on how long we'll event-wait for the product AJAX response on a single page load
AJAX_WAIT_HARD_CAP_MS = 20000



def switch_servers(event_type="run-longjobv1-1"):
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/dispatches"
    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
        },
        json={"event_type": event_type},
        timeout=30,
    )
    response.raise_for_status()


def build_page_url(base_url: str, page_num: int) -> str:
    parsed = urlparse(base_url)
    query = parse_qs(parsed.query)

    query["page"] = [str(page_num)]
    query["style"] = ["list"]

    return urlunparse(
        parsed._replace(query=urlencode(query, doseq=True))
    )


def human_pause(min_s=1.5, max_s=4.0):
    """Random delay to avoid perfectly uniform, machine-speed timing."""
    time.sleep(random.uniform(min_s, max_s))


def scrape_page(page, url: str, page_num: int = 1):
    """
    Loads the normal Lazada page and event-waits for the AJAX response
    that carries product data for THIS specific page

    Stock is taken from Lazada's `inStock` field.

    Returns (items, status):
      status == "ok"      -> items is the captured product list
      status == "blocked" -> the ajax response came back with a
                              non-200 status (likely rate-limited/blocked)
      status == "timeout" -> no matching ajax response arrived at all
                              within AJAX_WAIT_HARD_CAP_MS (page likely
                              served a captcha wall instead of the
                              normal storefront)
    """

    def matches_requested_page(response):
        # Check if AJAX response is from the page requested
        if "ajax=true" not in response.url or "lazada.sg" not in response.url:
            return False

        query = parse_qs(urlparse(response.url).query)
        resp_page = query.get("page", ["1"])[0]
        return resp_page == str(page_num)

    deadline = time.time() + AJAX_WAIT_HARD_CAP_MS / 1000
    response = None

    try:
        with page.expect_response(matches_requested_page, timeout=AJAX_WAIT_HARD_CAP_MS) as resp_info:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        response = resp_info.value
    except PlaywrightTimeoutError:
        print("  No matching AJAX response seen at all -- possible bot wall "
              "(or the `page` param check needs adjusting, see scrape_page docstring).")
        return [], "timeout"
    except Exception:
        print("  Warning: navigation error, checking for a response anyway...")

    # Keep waiting on the *next* page-matching response, within whatever
    # time budget remains, until we find one carrying listItems
    while response is not None:

        if response.status != 200:
            print(f"  AJAX responded with status {response.status} -- possible block.")
            return [], "blocked"

        try:
            data = response.json()
            items = data.get("mods", {}).get("listItems")
        except Exception:
            items = None

        if items is not None:
            # Human-like scroll happens after data retrieval, so it never costs any time on the critical path.
            try:
                page.mouse.wheel(0, random.randint(200, 800))
            except Exception:
                pass
            return items, "ok"

        remaining_ms = int((deadline - time.time()) * 1000)
        if remaining_ms <= 0:
            break

        try:
            with page.expect_response(matches_requested_page, timeout=remaining_ms) as resp_info:
                pass 
            response = resp_info.value
        except PlaywrightTimeoutError:
            response = None
            break

    print("  No product AJAX data captured within time budget.")
    return [], "timeout"


def scrape_all_pages(page, max_pages=3):
    """
    Scrapes pages sequentially.

    Stops when:
    - A page has fewer than 40 products
    - A duplicate product is found
    - No products are returned
    - A page reports "blocked" or "timeout" (suspected bot detection)

    Returns (all_items, block_reason). block_reason is None on a clean
    run, or "blocked"/"timeout" if any page hit that during this cycle.
    """

    all_items = []
    seen_ids = set()

    for page_num in range(1, max_pages + 1):

        # Delay between pages, randomized instead of a flat 3s.
        if page_num > 1:
            print("  Waiting before next page...")
            human_pause(2.5, 5.5)

        url = build_page_url(BASE_URL, page_num)

        print(f"\nScraping page {page_num}...")
        items, status = scrape_page(page, url, page_num=page_num)

        if status != "ok":
            print(f"  Page {page_num} status: {status}. Stopping this cycle.")
            return all_items, status

        print(f"  Found {len(items)} products")

        if not items:
            print("  No products found. Stopping.")
            break

        # Check whether this page contains products we've already seen.
        duplicate = any(
            item.get("itemId") in seen_ids
            for item in items
            if item.get("itemId")
        )

        if duplicate:
            print("  Duplicate product found. Stopping.")
            break

        # Add products
        for item in items:
            item_id = item.get("itemId")

            if not item_id or item_id in seen_ids:
                continue

            seen_ids.add(item_id)

            item_url = item.get("itemUrl", "")

            if item_url.startswith("//"):
                item_url = "https:" + item_url

            all_items.append({
                "itemId": item_id,
                "name": item.get("name"),
                "price": item.get("price"),
                "url": item_url,
                "inStock": item.get("inStock") is True,
            })

        print(f"  Total products: {len(all_items)}")

        # Fewer than 40 means we've reached the final page.
        if len(items) < PAGE_SIZE:
            print(
                f"  Page contains fewer than {PAGE_SIZE} products. "
                "Stopping."
            )
            break

    return all_items, None


def get_all_items(page):
    """
    Returns all products with name, price and URL.
    """

    items, _status = scrape_all_pages(page)

    return [
        {
            "name": item["name"],
            "price": item["price"],
            "url": item["url"],
        }
        for item in items
    ]


def get_in_stock_items(page):
    """
    Returns only products where Lazada explicitly reports
    inStock == True.
    """

    items, _status = scrape_all_pages(page)

    return [
        {
            "name": item["name"],
            "price": item["price"],
            "url": item["url"],
        }
        for item in items
        if item["inStock"]
    ]


def format_stock_message(in_stock_items):
    if in_stock_items:
        message = "🟢 Items currently in stock:\n\n"
        for item in in_stock_items:
            message += (
                f"{item['name']}\n"
                f"${item['price']}\n"
                f"{item['url']}\n\n"
            )
    else:
        message = "🔴 No products currently in stock.\n\n"
    return message


if __name__ == "__main__":

    send_telegram_message("Started v1.1-beta")

    # Calculate stop time exactly 3 hours from script start
    stop_time = datetime.now(SGT) + timedelta(hours=3)

    print("Launching stealth Chrome via SeleniumBase CDP mode...")

    sb = sb_cdp.Chrome(
        locale="en",
    )
    endpoint_url = sb.get_endpoint_url()

    with sync_playwright() as p:

        browser = p.chromium.connect_over_cdp(endpoint_url)
        context = browser.contexts[0]
        page = context.pages[0]

        human_pause(1.0, 2.5)

        last_in_stock_ids = set()
        empty_streak = 0
        empty_streak_alerted = False
        cycle_count = 0

        print(f"Starting poll loop, stopping at {stop_time.strftime('%Y-%m-%d %H:%M:%S')} SGT...")

        # Stop condition using datetime
        while datetime.now(SGT) < stop_time:

            cycle_start = time.time()
            cycle_count += 1

            print(f"\n--- Cycle {cycle_count} "
                  f"({datetime.now(SGT).strftime('%H:%M:%S')} SGT) ---")

            all_items, block_reason = scrape_all_pages(page)

            if block_reason is not None or not all_items:
                empty_streak += 1
                reason_label = block_reason or "no items"
                print(f"  Empty/blocked capture ({reason_label}) (streak: {empty_streak})")

                if (
                    empty_streak >= EMPTY_STREAK_ALERT_THRESHOLD
                    and not empty_streak_alerted
                ):
                    send_telegram_message(
                        f"⚠️ No product data captured for "
                        f"{empty_streak} cycles in a row "
                        f"(last reason: {reason_label}). "
                        "Switching servers..."
                    )
                    empty_streak_alerted = True
                    switch_servers()
                    print("  Alert sent. Stopping run.")
                    break

            else:
                empty_streak = 0
                empty_streak_alerted = False

                in_stock_items = [
                    item for item in all_items if item["inStock"]
                ]
                current_ids = {
                    item["itemId"] for item in all_items
                    if item["inStock"]
                }

                print(
                    f"  Total: {len(all_items)} | "
                    f"In stock: {len(in_stock_items)}"
                )

                changed = current_ids != last_in_stock_ids
                heartbeat_due = (
                    cycle_count % ALERT_ON_NO_CHANGE_EVERY == 0
                )

                if changed or heartbeat_due:
                    message = format_stock_message(in_stock_items)
                    if not changed:
                        message = "(heartbeat, no change)\n\n" + message
                    send_telegram_message(message)

                last_in_stock_ids = current_ids

            # Count how long this cycle took. If it was already over 5s,
            # start the next one immediately (no extra sleep). If it was
            # under 5s, wait out the remainder.
            elapsed = time.time() - cycle_start
            sleep_time = max(0, POLL_INTERVAL - elapsed)

            print(f"  Cycle took {elapsed:.1f}s, "
                  f"sleeping {sleep_time:.1f}s")

            if sleep_time > 0:
                time.sleep(sleep_time)

        browser.close()

    sb.quit() 

    print("Stop time reached, exiting.")