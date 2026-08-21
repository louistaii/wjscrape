import asyncio
import time
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
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
SGT = ZoneInfo("Asia/Singapore")
# Target seconds between cycle starts. If a cycle takes longer than this, the next cycle starts immediately with no extra delay;
POLL_INTERVAL = 2.5
ALERT_ON_NO_CHANGE_EVERY = 240  # send a heartbeat every N cycles
EMPTY_STREAK_ALERT_THRESHOLD = 2
MAX_PAGES = 2
AJAX_WAIT_SHORT_MS = 5000
AJAX_WAIT_HARD_CAP_MS = 20000

# Delay between starting page 1's navigation and starting page 2's, to avoid racing the server-side session/pagination bootstrap.
PAGE_STAGGER_SECONDS = 0.5

# Retry-in-place settings
RETRY_PAUSE_MINUTES = 10


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


def human_pause_seconds(min_s=1.5, max_s=4.0):
    return random.uniform(min_s, max_s)


async def block_unneeded(route):
    # Block images, fonts, css, media to decrease page load time

    if route.request.resource_type in ("image", "media", "font", "stylesheet"):
        await route.abort()
    else:
        await route.continue_()


async def scrape_page(page, url: str, page_num: int = 1):
    """
    Loads the normal Lazada page and event-waits for the AJAX response
    that carries product data for THIS specific page.

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
        if "ajax=true" not in response.url or "lazada.sg" not in response.url:
            return False

        query = parse_qs(urlparse(response.url).query)
        resp_page = query.get("page", ["1"])[0]
        return resp_page == str(page_num)

    deadline = time.time() + AJAX_WAIT_HARD_CAP_MS / 1000
    response = None

    try:
        async with page.expect_response(matches_requested_page, timeout=AJAX_WAIT_SHORT_MS) as resp_info:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        response = await resp_info.value
    except PlaywrightTimeoutError:
        remaining_ms = int((deadline - time.time()) * 1000)
        if remaining_ms <= 0:
            print(f"  [page {page_num}] No matching AJAX response seen -- possible bot wall.")
            return [], "timeout"
        try:
            async with page.expect_response(matches_requested_page, timeout=remaining_ms) as resp_info:
                pass
            response = await resp_info.value
        except PlaywrightTimeoutError:
            print(f"  [page {page_num}] No matching AJAX response seen -- possible bot wall.")
            return [], "timeout"
    except Exception:
        print(f"  [page {page_num}] Warning: navigation error, checking for a response anyway...")

    while response is not None:

        if response.status != 200:
            print(f"  [page {page_num}] AJAX responded with status {response.status} -- possible block.")
            return [], "blocked"

        try:
            data = await response.json()
            items = data.get("mods", {}).get("listItems")
        except Exception:
            items = None

        if items is not None:
            try:
                await page.mouse.wheel(0, random.randint(200, 800))
            except Exception:
                pass
            return items, "ok"

        remaining_ms = int((deadline - time.time()) * 1000)
        if remaining_ms <= 0:
            break

        try:
            async with page.expect_response(matches_requested_page, timeout=remaining_ms) as resp_info:
                pass
            response = await resp_info.value
        except PlaywrightTimeoutError:
            response = None
            break

    print(f"  [page {page_num}] No product AJAX data captured within time budget.")
    return [], "timeout"


async def scrape_all_pages(pages, on_page_result=None):
    """
    Fetches all MAX_PAGES pages concurrently
    Each page is processed as soon as ITS OWN ajax response lands.
    Returns (all_items, block_reason). block_reason is None on a clean
    run, or "blocked"/"timeout" if any page hit that during this cycle.
    """

    urls = [build_page_url(BASE_URL, i + 1) for i in range(MAX_PAGES)]

    async def labeled(page_num, page, url):
        items, status = await scrape_page(page, url, page_num=page_num)
        return page_num, items, status

    # Small stagger between navigations. Firing both tabs at the exact
    # same instant races the server-side session/pagination state -- if
    # page 2's request lands before page 1 has finished establishing
    # whatever cookie/token the search context needs, the server falls
    # back to page 1 for both, and you get identical results. A short
    # head start avoids that while still overlapping most of the wait.
    tasks = []
    for i in range(MAX_PAGES):
        tasks.append(asyncio.create_task(labeled(i + 1, pages[i], urls[i])))
        if i < MAX_PAGES - 1:
            await asyncio.sleep(PAGE_STAGGER_SECONDS)

    all_items = []
    seen_ids = set()
    block_reason = None

    # as_completed() yields whichever page finishes first, so we can
    # act on it right away instead of waiting for every page (gather()).
    for coro in asyncio.as_completed(tasks):
        page_num, items, status = await coro

        if status != "ok":
            print(f"  Page {page_num} status: {status}.")
            block_reason = block_reason or status
            continue

        print(f"  Page {page_num}: found {len(items)} products")

        new_items = []
        for item in items:
            item_id = item.get("itemId")

            if not item_id or item_id in seen_ids:
                continue

            seen_ids.add(item_id)

            item_url = item.get("itemUrl", "")

            if item_url.startswith("//"):
                item_url = "https:" + item_url

            entry = {
                "itemId": item_id,
                "name": item.get("name"),
                "price": item.get("price"),
                "url": item_url,
                "inStock": item.get("inStock") is True,
            }
            all_items.append(entry)
            new_items.append(entry)

        if on_page_result is not None:
            await on_page_result(page_num, new_items)

    print(f"  Total products: {len(all_items)}")

    return all_items, block_reason


async def get_all_items(pages):
    """
    Returns all products with name, price and URL.
    """
    items, _status = await scrape_all_pages(pages)

    return [
        {
            "name": item["name"],
            "price": item["price"],
            "url": item["url"],
        }
        for item in items
    ]


async def get_in_stock_items(pages):
    """
    Returns only products where Lazada explicitly reports
    inStock == True.
    """
    items, _status = await scrape_all_pages(pages)

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
        message += "@legalisedbankrobbing25 @x404notfound @louistzx"
    else:
        message = "🔴 No products currently in stock.\n\n"
    return message


def format_new_stock_message(new_items, page_num):
    message = f"🟢 NEW in stock (page {page_num}):\n\n"
    for item in new_items:
        message += (
            f"{item['name']}\n"
            f"${item['price']}\n"
            f"{item['url']}\n\n"
        )
    message += "@legalisedbankrobbing25 @x404notfound @louistzx"
    return message


async def launch_browser():
    print("Launching stealth Chrome via SeleniumBase CDP mode...")
    sb = await asyncio.to_thread(sb_cdp.Chrome, locale="en")
    endpoint_url = sb.get_endpoint_url()

    playwright = await async_playwright().start()
    browser = await playwright.chromium.connect_over_cdp(endpoint_url)
    context = browser.contexts[0]

    existing_pages = context.pages
    page1 = existing_pages[0] if existing_pages else await context.new_page()

    pages = [page1]
    for _ in range(MAX_PAGES - 1):
        pages.append(await context.new_page())

    # Keep these tabs open and reuse them every cycle
    for p in pages:
        await p.route("**/*", block_unneeded)

    await asyncio.sleep(human_pause_seconds(1.0, 2.5))
    return sb, playwright, browser, context, pages


async def close_browser(sb, playwright, browser):
    try:
        await browser.close()
    except Exception:
        pass
    try:
        await playwright.stop()
    except Exception:
        pass
    try:
        await asyncio.to_thread(sb.quit)
    except Exception:
        pass


async def pause_and_relaunch(sb, playwright, browser, minutes):
    # Closes the current browser, sleeps, relaunches, and does a single probe page-1 fetch to see whether the pause was enough.

    print(f"  Closing browser and pausing {minutes} min before retrying...")
    await close_browser(sb, playwright, browser)
    await asyncio.sleep(minutes * 60)

    sb, playwright, browser, context, pages = await launch_browser()

    probe_url = build_page_url(BASE_URL, 1)
    print("  Probing page 1 after pause...")
    _items, probe_status = await scrape_page(pages[0], probe_url, page_num=1)
    print(f"  Probe result after {minutes} min pause: {probe_status}")

    return sb, playwright, browser, context, pages, probe_status


async def main():
    send_telegram_message("Started v1.2-beta")

    # Calculate stop time exactly 3 hours from script start
    stop_time = datetime.now(SGT) + timedelta(hours=3)

    sb, playwright, browser, context, pages = await launch_browser()

    last_in_stock_ids = set()
    empty_streak = 0
    empty_streak_alerted = False
    cycle_count = 0

    # Items already alerted-on THIS cycle via the instant per-page path.
    # Reset at the top of every cycle; read/written by on_page_result
    # below, and consulted again after the cycle to avoid double-sending
    # the same new items in the end-of-cycle summary.
    newly_alerted_ids = set()

    async def on_page_result(page_num, new_items):
        # Fires the moment a single page's ajax data comes back --
        # does NOT wait for the other page/tab.
        fresh = [
            item for item in new_items
            if item["inStock"]
            and item["itemId"] not in last_in_stock_ids
            and item["itemId"] not in newly_alerted_ids
        ]
        if not fresh:
            return

        newly_alerted_ids.update(item["itemId"] for item in fresh)
        print(f"  [page {page_num}] Instant alert: {len(fresh)} new in-stock item(s).")
        # Run the blocking HTTP call in a thread so it can't stall the
        # other page's scrape that may still be in flight.
        await asyncio.to_thread(
            send_telegram_message, format_new_stock_message(fresh, page_num)
        )

    print(f"Starting poll loop, stopping at {stop_time.strftime('%Y-%m-%d %H:%M:%S')} SGT...")

    while datetime.now(SGT) < stop_time:

        cycle_start = time.time()
        cycle_count += 1
        newly_alerted_ids.clear()

        print(f"\n--- Cycle {cycle_count} "
              f"({datetime.now(SGT).strftime('%H:%M:%S')} SGT) ---")

        all_items, block_reason = await scrape_all_pages(pages, on_page_result=on_page_result)

        if block_reason is not None or not all_items:
            empty_streak += 1
            reason_label = block_reason or "no items"
            print(f"  Empty/blocked capture ({reason_label}) (streak: {empty_streak})")

            if (
                empty_streak >= EMPTY_STREAK_ALERT_THRESHOLD
                and not empty_streak_alerted
            ):
                empty_streak_alerted = True

                send_telegram_message(
                    f"⚠️ No product data captured for "
                    f"{empty_streak} cycles in a row "
                    f"(last reason: {reason_label}). "
                    f"Trying pause-and-retry before switching servers..."
                )

                send_telegram_message(
                    f"  Pausing {RETRY_PAUSE_MINUTES} min before retrying..."
                )
                sb, playwright, browser, context, pages, probe_status = await pause_and_relaunch(
                    sb, playwright, browser, RETRY_PAUSE_MINUTES
                )

                if probe_status == "ok":
                    send_telegram_message(
                        f"✅ Recovered after {RETRY_PAUSE_MINUTES} min pause. "
                        "Resuming normal polling."
                    )
                    empty_streak = 0
                    empty_streak_alerted = False
                else:
                    send_telegram_message(
                        f"❌ Still {probe_status} after {RETRY_PAUSE_MINUTES} "
                        "min pause. Switching servers..."
                    )
                    await close_browser(sb, playwright, browser)
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
            disappeared = last_in_stock_ids - current_ids
            heartbeat_due = (
                cycle_count % ALERT_ON_NO_CHANGE_EVERY == 0
            )

            if disappeared or heartbeat_due:
                message = format_stock_message(in_stock_items)
                if heartbeat_due and not changed:
                    message = "(heartbeat, no change)\n\n" + message
                elif disappeared and not heartbeat_due:
                    message = "(update: item(s) went out of stock)\n\n" + message
                send_telegram_message(message)

            last_in_stock_ids = current_ids

        # Count how long this cycle took. If it was already over cycle time, start the next one immediately (no extra sleep)
        elapsed = time.time() - cycle_start
        sleep_time = max(0, POLL_INTERVAL - elapsed)

        print(f"  Cycle took {elapsed:.1f}s, "
              f"sleeping {sleep_time:.1f}s")

        if sleep_time > 0:
            await asyncio.sleep(sleep_time)

    await close_browser(sb, playwright, browser)

    print("Stop time reached, exiting.")


if __name__ == "__main__":
    asyncio.run(main())