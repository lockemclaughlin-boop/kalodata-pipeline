"""
All Kalodata DOM selectors live here. When Kalodata changes their UI, this
is the only file you should need to touch.

How to update when something breaks:
  1. Run `python scripts/inspect_kalodata.py`
  2. Use the Playwright Inspector to click on the broken element
  3. Copy the suggested locator into the constant below
  4. Re-run `python scripts/scrape_test.py` to confirm

Prefer Playwright's role-based / text-based locators over CSS selectors —
they survive style refactors. Use CSS only as a last resort.
"""

# --- Login -----------------------------------------------------------------

LOGIN_URL = "https://www.kalodata.com/login"
DASHBOARD_URL_FRAGMENT = "/dashboard"  # Substring we expect in the URL post-login

EMAIL_INPUT = "input[type='email'], input[name='email']"
PASSWORD_INPUT = "input[type='password']"
LOGIN_BUTTON_TEXT = "Log in"  # Used as page.get_by_role("button", name=...)

# --- Product Search --------------------------------------------------------

PRODUCT_SEARCH_URL = "https://www.kalodata.com/product"

# Filter sidebar / top-bar controls. Labels match what Kalodata renders in
# their left filter rail (screenshot 2026-05-15). Adjust after running
# inspect.py if Kalodata renames them.
FILTER_REGION_LABEL = "Region"
FILTER_CATEGORY_LABEL = "Category"
FILTER_TIME_WINDOW_LABEL = "Period"

# Revenue Filters section
FILTER_REVENUE_LABEL = "Revenue($)"
FILTER_ITEM_SOLD_LABEL = "Item Sold"
FILTER_REVENUE_SOURCE_CONTENT_LABEL = "Revenue Source(Content)"
FILTER_REVENUE_SOURCE_CHANNEL_LABEL = "Revenue Source(Channel)"
FILTER_REVENUE_GROWTH_RATE_LABEL = "Revenue Growth Rate"

# Advanced section
FILTER_AVG_UNIT_PRICE_LABEL = "Avg. Unit Price($)"
FILTER_IS_AFFILIATE_LABEL = "Is Affiliate Product"
FILTER_CREATOR_NUMBER_LABEL = "Creator Number"
FILTER_CREATOR_CONVERSION_LABEL = "Creator Conversion Ratio"
FILTER_SHIPPING_OPTION_LABEL = "Shipping Option"
FILTER_LAUNCH_DATE_LABEL = "Launch Date"
FILTER_COMMISSION_RATE_LABEL = "Commission Rate"

# Numeric filter inputs (placeholder text or aria-label is the most stable hook).
# These are the input fields inside the popover that opens when you click a
# Revenue Filter / Advanced label above. Adjust after running inspect.py.
FILTER_MIN_PLACEHOLDER = "Min"
FILTER_MAX_PLACEHOLDER = "Max"

APPLY_FILTERS_BUTTON_TEXT = "Apply"

# --- Results Table ---------------------------------------------------------

# Each row in the product results table. Kalodata is built on Ant Design.
# Confirmed against live DOM via scripts/probe_selectors.py.
PRODUCT_ROW = "tr.ant-table-row"

# Header cells across the table (one per visible column).
TABLE_HEADER_CELL = "thead th.ant-table-cell"

# Product ID is exposed as data-row-key on the <tr>.
PRODUCT_ID_ATTR = "data-row-key"

# Title inside the row.
PRODUCT_TITLE_IN_ROW = "div.line-clamp-2"

# Cover image is rendered as a CSS background-image, not an <img>.
# Pattern observed on live site:
#   https://img.kalocdn.com/tiktok.product/<product_id>/cover.png
COVER_URL_TEMPLATE = "https://img.kalocdn.com/tiktok.product/{product_id}/cover.png"

# Within a product row, the link to the product detail page
PRODUCT_LINK_IN_ROW = "a[href*='/product/']"

# Pagination — verified against live DOM via dump_kalodata_dom.py.
# Next button: <li title="Next Page" class="ant-pagination-next" aria-disabled="false">.
# Page-size changer: <div class="ant-pagination-options-size-changer">.
NEXT_PAGE_BUTTON = "li.ant-pagination-next"
PAGE_SIZE_CHANGER = ".ant-pagination-options-size-changer"
PAGE_SIZE_OPTION_TEMPLATE = "{n} / page"   # e.g. "50 / page"

# The scrollable container for the Ant Design table (kept for future use; the
# DOM dump showed scrolling does NOT trigger lazy-load on Kalodata — the table
# is real pagination).
TABLE_SCROLL_CONTAINER = ".ant-table-body"

# --- Product Detail Page ---------------------------------------------------

# The image carousel on the product detail page
PRODUCT_PHOTO_IMAGES = "div[class*='gallery'] img, div[class*='carousel'] img"

# Spec block — title, price, GMV, sales, etc. Each spec is usually a label/value pair.
PRODUCT_TITLE = "h1, h2[class*='title']"
SPEC_LABEL_VALUE_PAIRS = "div[class*='spec'] dt, div[class*='spec'] dd"

# --- Anti-bot indicators ---------------------------------------------------

CAPTCHA_HINTS = [
    "iframe[src*='captcha']",
    "iframe[src*='recaptcha']",
    "div[class*='captcha']",
    "text=Verify you are human",
]
