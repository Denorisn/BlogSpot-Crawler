#!/usr/bin/env python3

import sys
import signal
import os.path
import errno
import time
import re
import threading
import mimetypes
from datetime import date
from urllib.parse import urlparse, urljoin

import argparse
from dataclasses import dataclass
from concurrent.futures import *

import requests
from bs4 import BeautifulSoup, Tag

REMAINING_FILE = "~/.config/scripts/PyBloggerRemaining.txt"

# Hosts Blogger serves post images from.
IMAGE_HOSTS = ("bp.blogspot.com", "blogger.googleusercontent.com", "googleusercontent.com")

# Blogger encodes the requested size in the image URL, either as a path
# segment (".../s320/img.jpg", ".../w400-h225/img.jpg", ".../w72-h72-p-k-no-nu/img.jpg")
# or as a suffix ("...=s320", "...=w400-h225-no"). The size may carry trailing
# flags (-c, -p-k-no-nu, ...). "s0" means the original, full-size image.
_SIZE_PATH_RE = re.compile(r"/(?:s\d+|w\d+-h\d+)(?:-[a-z0-9-]+)?/")
_SIZE_SUFFIX_RE = re.compile(r"=(?:s\d+|w\d+-h\d+)(?:-[a-z0-9-]+)?$")

# Characters that are illegal in Windows filenames.
_ILLEGAL_FNAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def is_image_host(url: str) -> bool:
    """True if url points at a host Blogger uses for post images."""
    if not url:
        return False
    host = urlparse(url).netloc.lower()
    return any(host == h or host.endswith("." + h) or host.endswith(h) for h in IMAGE_HOSTS)


def to_fullsize_url(url: str) -> str:
    """Rewrites a Blogger image URL to request the original, full-size image."""
    url = _SIZE_PATH_RE.sub("/s0/", url)
    url = _SIZE_SUFFIX_RE.sub("=s0", url)
    return url


def sanitize_filename(name: str, maxlen: int = 120) -> str:
    """Turns an arbitrary string (e.g. a post title) into a safe filename stem."""
    name = _ILLEGAL_FNAME_RE.sub("_", name).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    if len(name) > maxlen:
        name = name[:maxlen].rstrip()
    return name or "untitled"


def guess_extension(url: str, content_type: str = "") -> str:
    """Best-effort file extension from a URL path, falling back to Content-Type."""
    ext = os.path.splitext(urlparse(url).path)[1]
    if ext and len(ext) <= 5:
        return ext
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guessed:
            return ".jpg" if guessed == ".jpe" else guessed
    return ".jpg"


_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _to_date(value: str):
    """Extracts a datetime.date from an ISO-ish string, or None."""
    if not value:
        return None
    m = _ISO_DATE_RE.search(value)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def extract_published_date(scope):
    """Best-effort published date for a post from a BS4 element/soup subtree.

    Looks at schema.org (`itemprop="datePublished"`) and Blogger's classic
    template markers (`abbr.published`, `time.published`, `.published`) in
    priority order, reading a machine-readable attribute where present. Returns
    a datetime.date or None when no parseable date is found.
    """
    if scope is None:
        return None
    selectors = ('[itemprop="datePublished"]', 'abbr.published',
                 'time.published', 'time[datetime]', '.published')
    for sel in selectors:
        for el in scope.select(sel):
            for attr in ("title", "datetime", "content"):
                d = _to_date(el.get(attr))
                if d:
                    return d
            d = _to_date(el.get_text())
            if d:
                return d
    return None


def parse_cli_date(value: str) -> date:
    """argparse type: accepts YYYY-MM-DD and returns a datetime.date."""
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid date '{value}', expected YYYY-MM-DD")


def parse_tag_list(value: str) -> set:
    """argparse type: 'a,b,c' -> {'a','b','c'} (lower-cased, blanks dropped).

    Tags are matched case-insensitively, so we normalise to lower case here.
    """
    if not value:
        return set()
    return {t.strip().lower() for t in value.split(',') if t.strip()}


@dataclass
class JobInfo:
    """Saves info from a job
    """
    url: str = ""
    fname: str = ""
    remaining: int = 5

class ProcessPagination:
    """Process a pagination
    """
    def __init__(self, baseurl: str, destination: str, images_dir: str=None,
                 session: requests.Session=None, max_workers: int=None,
                 date_from: date=None, date_to: date=None, limit: int=-1,
                 downloadhtml: bool = False, downloadImages: bool=False,
                 tags: set=None, exclude_tags: set=None, match_any: bool=False
                 ):
        self.baseurl = baseurl
        self.url = baseurl
        self.destination = destination
        self.images_dir = images_dir or os.path.join(destination, "images")
        self.session = session or requests.Session()
        self.date_from = date_from
        self.date_to = date_to
        self.limit = limit
        self.tags = tags or set()
        self.exclude_tags = exclude_tags or set()
        self.match_any = match_any
        self.running = True
        self.executor = ThreadPoolExecutor(max_workers)
        self.done = 0
        self.total = 0
        self.saved_count = 0
        self.count_lock = threading.Lock()
        self.stop_paging = False
        self.lastdone = JobInfo()
        self.remaining = {}
        self.use_download_images = downloadImages
        self.use_download_html = downloadhtml

        # Create the images folder up front so worker threads don't race on it.
        os.makedirs(self.images_dir, exist_ok=True)

    def download_images(self, body: Tag, title: str, post_url: str, post_date: date=None):
        """Downloads the full-size version of every image in a post body.

        Images are saved to the flat images folder, named after the post title.
        A post with more than one image gets numbered suffixes ("Title_1.jpg",
        "Title_2.jpg", ...). Names are deterministic, so re-runs skip files that
        already exist. Failures on a single image are logged, not fatal.

        When post_date is given, each saved image file's access/modified time is
        set to the post's date so archives sort by publication date.
        """
        imgs = body.find_all("img") if body else []

        # Resolve each thumbnail to its full-size URL (preferring an enclosing
        # <a> that links to the large image), de-duped, in document order.
        urls = []
        seen = set()
        for img in imgs:
            src = img.get("src") or img.get("data-src")
            if not src:
                continue
            src = urljoin(post_url, src)

            candidate = None
            anchor = img.find_parent("a")
            if anchor and anchor.get("href") and is_image_host(anchor["href"]):
                candidate = urljoin(post_url, anchor["href"])
            elif is_image_host(src):
                candidate = src

            if not candidate:
                continue
            full = to_fullsize_url(candidate)
            if full not in seen:
                seen.add(full)
                urls.append(full)

        stem = sanitize_filename(title)
        multiple = len(urls) > 1
        for i, url in enumerate(urls, start=1):
            try:
                resp = self.session.get(url, timeout=15)
                resp.raise_for_status()
                ext = guess_extension(url, resp.headers.get("Content-Type", ""))
                name = f"{stem}_{i}{ext}" if multiple else f"{stem}{ext}"
                path = os.path.join(self.images_dir, name)
                if os.path.exists(path):
                    continue
                with open(path, "wb") as imgf:
                    imgf.write(resp.content)
                if post_date is not None:
                    ts = time.mktime(post_date.timetuple())
                    os.utime(path, (ts, ts))
            except Exception as exc:
                print(f"\nFailed to download image {url}: {exc}")

    def response_to_file(self, fname, content, post_url):
        soup = BeautifulSoup(content, 'html.parser')

        if self.use_download_html == True and not os.path.exists(os.path.dirname(fname)):
            try:
                os.makedirs(os.path.dirname(fname))
            except OSError as exc: # Guard against race condition
                if exc.errno != errno.EEXIST:
                    raise

        reglob = re.compile(".*")

        title = soup.find(reglob, itemprop="name").getText().strip()
        tags = [x.getText() for x in soup.find_all("a", {"rel":"tag"})]
        body = soup.find("div", class_="post-body")

        # The post page reliably carries a published date; use it both to
        # guarantee the date filter (even when the listing page hid the date)
        # and to stamp downloaded image files with the post's date.
        pub = extract_published_date(soup)
        if (self.date_from or self.date_to) and pub is not None \
                and not self.in_date_range(pub):
            return True  # in range-terms "handled" (won't retry), but not saved

        # Skip posts that don't pass the tag filters before downloading anything.
        if not self.tags_allowed(tags):
            return True  # "handled" (won't retry), but not saved

        # Reserve a save slot atomically so --limit counts posts actually saved
        # (after all filters), never merely submitted. Because submission is
        # concurrent, several workers may reach here at once; the lock ensures we
        # never save more than the limit, and signals pagination to stop.
        if self.limit >= 0:
            with self.count_lock:
                if self.saved_count >= self.limit:
                    self.stop_paging = True
                    return True  # limit already met; skip without downloading
                self.saved_count += 1

        if self.use_download_images == True:
            self.download_images(body, title, post_url, pub)

        # TODO: "Flatten" html and/or convert to Markdown

        if self.use_download_html == True:
            with open(fname, 'w+') as f:
                print("---", file=f)
                print("layout: default", file=f)
                print("title:", title, file=f)
                print("tags:", "[" + ",".join(tags) + "]", file=f)
                print("---", file=f)
                print(body, file=f)

            # Stamp the post file with the post's date so it sorts alongside its
            # images by publication date. Only when the HTML file was written.
            if pub is not None:
                ts = time.mktime(pub.timetuple())
                os.utime(fname, (ts, ts))

        return True

    def process_post(self, url, fname):
        """Downloads and process one post
        """
        response = self.session.get(url, timeout=5)

        response.raise_for_status()

        return self.response_to_file(fname, response.content, url)

    def process_post_callback(self, future):
        if future.cancelled():
            return

        # future.result() re-raises anything the worker threw (e.g. a network
        # error): treat that as a failed job rather than letting the exception
        # escape the callback, which would leave the job stuck in `remaining`.
        try:
            ok = future.result()
        except Exception:
            ok = False

        ji = self.remaining.pop(future, None)
        if ok:
            self.done += 1
            if ji is not None:
                self.lastdone = ji
        elif ji is not None and ji.remaining > 0:
            self.resubmit(ji)

        # Repaint the status line as each post finishes, so "Done X/Y" advances
        # live even while the main thread is blocked waiting for a page's jobs.
        self.printStatus(self.lastdone.url, self.total, self.done)

    def limit_reached(self) -> bool:
        """True once we've saved the user-requested number of posts."""
        return self.limit >= 0 and self.saved_count >= self.limit

    def in_date_range(self, d: date) -> bool:
        """True if date d falls within the (inclusive) --from/--to window."""
        if self.date_from and d < self.date_from:
            return False
        if self.date_to and d > self.date_to:
            return False
        return True

    def tags_allowed(self, post_tags) -> bool:
        """Whether a post passes the tag filters, given its list of tag labels.

        - --exclude-tags: reject the post if it carries ANY excluded tag.
        - --tags: require the post to carry the included tags. By default it must
          have them ALL (AND); with --any-tags it needs at least one (OR).
        Matching is case-insensitive. No filters set -> every post is allowed.
        """
        have = {t.strip().lower() for t in post_tags}

        if self.exclude_tags and (have & self.exclude_tags):
            return False

        if self.tags:
            if self.match_any:
                return bool(have & self.tags)
            return self.tags <= have  # every required tag present

        return True

    @staticmethod
    def _post_container(anchor):
        """Smallest ancestor of a post-title link that carries a published date
        while still wrapping only this one post.

        Climbing by date-presence (rather than by class name) keeps this working
        across Blogger templates, and the single-title guard stops us before we
        reach a container spanning several posts and pick up a neighbour's date.
        """
        node = anchor
        for _ in range(8):
            parent = node.parent
            if parent is None:
                break
            node = parent
            if len(node.select("h3.post-title a")) > 1:
                break  # crossed into a multi-post container; stop
            if extract_published_date(node) is not None:
                return node
        return None

    def process_one_page(self, url):
        """Adds urls from current page to the executor

        Args:
            executor (Executor): Executor
            url (str): Url to process

        Returns:
            url of next page to process or None if there isnt any
        """
        page = self.session.get(url)
        soup = BeautifulSoup(page.content, 'html.parser')

        for x in soup.select("h3.post-title a"):
            if self.limit_reached() or self.stop_paging:
                break

            # Throttle submissions: once enough posts are saved-or-in-flight to
            # possibly reach the limit, stop submitting and let them finish. If
            # some are then filtered out, the per-page barrier in process() lets
            # us come back for more, so we neither over-fetch nor under-deliver.
            if self.limit >= 0 and (self.saved_count + len(self.remaining)) >= self.limit:
                break

            # Filter by date at the listing level when the date is available
            # here (cheaper: avoids fetching out-of-range posts, and lets us
            # stop paging once we've walked past the start of the window).
            post_date = extract_published_date(self._post_container(x))
            if post_date is not None:
                if self.date_from and post_date < self.date_from:
                    # Blogger lists newest -> oldest, so everything from here
                    # on is older than the window: stop after this page.
                    self.stop_paging = True
                    continue
                if self.date_to and post_date > self.date_to:
                    continue  # too new; keep scanning toward the window

            self.submit(x['href'])

        if self.limit_reached() or self.stop_paging:
            return None

        next_page = soup.select("#blog-pager-older-link a")

        return next_page[0]['href'] if next_page else None

    def printStatus(self, url, total, current=None):
        if url != self.baseurl:
            url = url[len(self.baseurl):]

        w = os.get_terminal_size().columns

        outstr = f"Done {current}/{total} "
        donelen = len(outstr)
        remainingspace = w - len(outstr)-3
        if len(url) >= remainingspace:
            outstr += url[:remainingspace] + "..."
        else:
            outstr += url

        outstr += " "*(w - len(outstr))
        print(outstr, end="\r", flush=True)

    def submit(self, url: str, remaining_tries=5):
        fname = os.path.join(self.destination, url[len(self.baseurl):].strip('/'))
        future = self.executor.submit(self.process_post, url, fname)
        future.add_done_callback(self.process_post_callback)
        self.remaining[future] = JobInfo(url, fname, remaining_tries)
        self.total += 1

    def resubmit(self, ji: JobInfo):
        future = self.executor.submit(self.process_post, ji.url, ji.fname)
        self.remaining[future] = JobInfo(ji.url, ji.fname, ji.remaining-1)

    def write_remaining(self):
        """Writes remaining to REMAINING_FILE"""
        rmfile = os.path.expanduser(REMAINING_FILE)
        with open(rmfile, 'w') as f:
            print(self.url, file=f)

            for ji in self.remaining.values():
                print(ji.url, file=f)

        print("Saved remaining to", os.path.expanduser(REMAINING_FILE))

    def process(self):
        """Main processing function"""
        rmfile = os.path.expanduser(REMAINING_FILE)
        self.url = self.baseurl
        if os.path.isfile(rmfile):
            with open(rmfile, 'r') as f:
                aux = f.readline().strip()
                if aux:
                    self.url = aux
                    print("Ressuming from", self.url)

                for line in f:
                    self.submit(line.strip())

        while self.url and self.running:
            self.printStatus(self.url, self.total, self.done)
            self.url = self.process_one_page(self.url)

            # With a --limit, let this page's jobs finish before fetching the
            # next listing page, so saved_count reflects them (and the throttle
            # above can re-evaluate). Otherwise we'd race ahead and fetch every
            # listing page while workers are still catching up.
            if self.limit >= 0 and self.running:
                wait(list(self.remaining.keys()), return_when=ALL_COMPLETED)
                if self.limit_reached():
                    break

        self.printStatus(self.lastdone.url, self.total, self.done)
        _, not_done = wait(self.remaining.keys(), .5, ALL_COMPLETED)
        while self.running and not_done:
            self.printStatus(self.lastdone.url, self.total, self.done)
            _, not_done = wait(self.remaining.keys(), .5, ALL_COMPLETED)

        print("\nFinished!")

    def stop(self):
        """Stops the executor"""
        self.running = False
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.write_remaining()

def login_session(browser: str = "firefox", cookie_file: str = None) -> requests.Session:
    """Builds a requests.Session pre-loaded with cookies from a local browser.

    Rather than automating a Google login (which Google blocks as an insecure /
    automated browser), we reuse the cookies from a browser you've already logged
    into. Firefox is the default and the most reliable cross-platform source:
    browser_cookie3 locates its profile automatically on Windows and Linux, and
    Firefox isn't affected by Chrome's on-disk cookie encryption.

    Log into the target blog in that browser first, then run with --login. Pass
    --cookie-file to point at a specific cookies database (e.g. a Firefox profile
    copied over from another machine).
    """
    try:
        import browser_cookie3
    except ImportError:
        print("browser_cookie3 is required for --login. Install it with:\n"
              "    pip install browser_cookie3", file=sys.stderr)
        sys.exit(1)

    loader = getattr(browser_cookie3, browser, None)
    if loader is None:
        print(f"Unsupported browser '{browser}'. Try: firefox, chrome, edge.",
              file=sys.stderr)
        sys.exit(1)

    try:
        # No domain filter: we load every cookie so both the Google auth cookies
        # (.google.com) and the blog's own cookies (.blogspot.com) come along;
        # requests only sends the ones matching each request's host.
        jar = loader(cookie_file=cookie_file) if cookie_file else loader()
    except Exception as exc:
        print(f"Could not read {browser} cookies: {exc}\n"
              "Make sure the browser is installed, you're logged into the blog, "
              "and (on some systems) that the browser is closed.", file=sys.stderr)
        sys.exit(1)

    session = requests.Session()
    session.cookies.update(jar)
    print(f"Loaded {len(session.cookies)} cookies from {browser}.")
    return session


def main():

    parser = argparse.ArgumentParser(description="Blogspot crawler",
        epilog="(C) David Davó - https://ddavo.me. Licensed under a MIT License.")
    parser.add_argument('url', type=str, help='Blog url')
    parser.add_argument('--output',
        dest='destination',
        type=str, 
        default="./",
        help="Output folder"
    )
    parser.add_argument('--threads',
        dest='threads',
        type=int,
        default=1,
        help="Number of threads"
    )
    parser.add_argument('--images-dir',
        dest='images_dir',
        type=str,
        default="./images",
        help="Folder to save full-size images to (default: <output>/images)"
    )
    parser.add_argument('--login',
        dest='login',
        action='store_true',
        help="Reuse cookies from a local browser for private/login-gated blogs "
             "(log into the blog in that browser first)"
    )
    parser.add_argument('--browser',
        dest='browser',
        type=str,
        default='firefox',
        choices=['firefox', 'chrome', 'edge', 'brave', 'chromium', 'opera', 'vivaldi'],
        help="Browser to read cookies from when using --login (default: firefox). "
             "Note: on Windows, recent Chrome/Edge versions encrypt their cookie "
             "store and may fail to read — use --cookie-file or firefox if so."
    )
    parser.add_argument('--cookie-file',
        dest='cookie_file',
        type=str,
        default=None,
        help="Path to a specific cookies database for --login "
             "(e.g. a Firefox cookies.sqlite copied from another machine)"
    )
    parser.add_argument('--from', '--start-date',
        dest='date_from',
        type=parse_cli_date,
        default='2000-01-01',
        metavar='YYYY-MM-DD',
        help="Only download posts published on or after this date"
    )
    parser.add_argument('--to', '--end-date',
        dest='date_to',
        type=parse_cli_date,
        default='2030-01-01',
        metavar='YYYY-MM-DD',
        help="Only download posts published on or before this date"
    )
    parser.add_argument('--limit',
        dest='limit',
        type=int,
        default=-1,
        help="Max number of posts to crawl (-1 = all posts within the date range)"
    )
    parser.add_argument('--html',
        dest='downloadhtml',
        type=bool,
        default=False,
        help="Toggle the ability to download the html for posts"
    )
    parser.add_argument('--images',
        dest='downloadImages',
        type=bool,
        default=True,
        help="Toggle the ability to download the images for posts"
    )
    parser.add_argument('--tags',
        dest='tags',
        type=parse_tag_list,
        default=set(),
        metavar='TAG1,TAG2,...',
        help="Only download posts carrying these tags (comma-separated). "
             "By default a post must have ALL of them; see --any-tags."
    )
    parser.add_argument('--any-tags',
        dest='match_any',
        action='store_true',
        help="With --tags, match posts that have ANY of the tags (OR) "
             "instead of ALL of them (AND)"
    )
    parser.add_argument('--exclude-tags',
        dest='exclude_tags',
        type=parse_tag_list,
        default=set(),
        metavar='TAG1,TAG2,...',
        help="Skip posts carrying any of these tags (comma-separated)"
    )


    args = parser.parse_args()

    if args.date_from and args.date_to and args.date_from > args.date_to:
        parser.error("--from date must not be later than --to date")

    session = login_session(args.browser, args.cookie_file) if args.login else None

    process_pagination = ProcessPagination(
        baseurl=args.url,
        destination=args.destination,
        images_dir=args.images_dir,
        session=session,
        max_workers=args.threads,
        date_from=args.date_from,
        date_to=args.date_to,
        limit=args.limit,
        downloadhtml=args.downloadhtml,
        downloadImages=args.downloadImages,
        tags=args.tags,
        exclude_tags=args.exclude_tags,
        match_any=args.match_any)

    def signal_handler(sig, frame):
        # TODO: Add 5 seconds timeout or something like
        # that to threads, before killing them (as they are daemons)

        print("Received SIGINT")
        process_pagination.stop()

        sys.exit(1)

    signal.signal(signal.SIGINT, signal_handler)

    process_pagination.process()

    sys.exit(0)

if __name__ == "__main__":
    main()
