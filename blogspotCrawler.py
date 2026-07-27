#!/usr/bin/env python3

import sys
import signal
import os.path
import errno
import time
import re
import mimetypes
from urllib.parse import urlparse, urljoin

import argparse
from dataclasses import dataclass
from concurrent.futures import *

import requests
from bs4 import BeautifulSoup, Tag

REMAINING_FILE = "~/.config/scripts/PyBloggerRemaining.txt"
LOGIN_PROFILE = "~/.config/scripts/blogcrawl_profile"

# Hosts Blogger serves post images from.
IMAGE_HOSTS = ("bp.blogspot.com", "blogger.googleusercontent.com", "googleusercontent.com")

# Blogger encodes the requested size in the image URL, either as a path
# segment (".../s320/img.jpg", ".../w400-h225/img.jpg") or as a suffix
# ("...=s320", "...=w400-h225-no"). "s0" means the original, full-size image.
_SIZE_PATH_RE = re.compile(r"/(s\d+|w\d+-h\d+|s\d+-c)(/)")
_SIZE_SUFFIX_RE = re.compile(r"=(s\d+|w\d+-h\d+)(-[a-z0-9-]+)?$")

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
    url = _SIZE_PATH_RE.sub(r"/s0\2", url)
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
                 session: requests.Session=None, max_workers: int=None):
        self.baseurl = baseurl
        self.url = baseurl
        self.destination = destination
        self.images_dir = images_dir or os.path.join(destination, "images")
        self.session = session or requests.Session()
        self.running = True
        self.executor = ThreadPoolExecutor(max_workers)
        self.done = 0
        self.total = 0
        self.lastdone = JobInfo()
        self.remaining = {}

        # Create the images folder up front so worker threads don't race on it.
        os.makedirs(self.images_dir, exist_ok=True)

    def download_images(self, body: Tag, title: str, post_url: str):
        """Downloads the full-size version of every image in a post body.

        Images are saved to the flat images folder, named after the post title.
        A post with more than one image gets numbered suffixes ("Title_1.jpg",
        "Title_2.jpg", ...). Names are deterministic, so re-runs skip files that
        already exist. Failures on a single image are logged, not fatal.
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
            except Exception as exc:
                print(f"\nFailed to download image {url}: {exc}")

    def response_to_file(self, fname, content, post_url):
        soup = BeautifulSoup(content, 'html.parser')

        if not os.path.exists(os.path.dirname(fname)):
            try:
                os.makedirs(os.path.dirname(fname))
            except OSError as exc: # Guard against race condition
                if exc.errno != errno.EEXIST:
                    raise

        reglob = re.compile(".*")

        title = soup.find(reglob, itemprop="name").getText().strip()
        tags = [x.getText() for x in soup.find_all("a", {"rel":"tag"})]
        body = soup.find("div", class_="post-body")

        self.download_images(body, title, post_url)

        # TODO: "Flatten" html and/or convert to Markdown

        with open(fname, 'w+') as f:
            print("---", file=f)
            print("layout: default", file=f)
            print("title:", title, file=f)
            print("tags:", "[" + ",".join(tags) + "]", file=f)
            print("---", file=f)
            print(body, file=f)

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

        if future.result():
            self.done += 1
            self.lastdone = self.remaining[future]
        else:
            if self.remaining[future].remaining > 0:
                self.resubmit(self.remaining[future])

        del self.remaining[future]

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
            self.submit(x['href'])

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

def login_session(url: str) -> requests.Session:
    """Opens a headful browser so the user can log into Google, then returns a
    requests.Session carrying the authenticated cookies.

    Playwright is imported lazily so it is only needed when --login is used. A
    persistent profile is kept under LOGIN_PROFILE, so once you've logged in a
    later run may already be authenticated (just close the window / press Enter).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is required for --login. Install it with:\n"
              "    pip install playwright\n"
              "    playwright install chromium", file=sys.stderr)
        sys.exit(1)

    profile_dir = os.path.expanduser(LOGIN_PROFILE)
    os.makedirs(profile_dir, exist_ok=True)

    session = requests.Session()
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(profile_dir, headless=False)
        page = context.new_page() if not context.pages else context.pages[0]
        page.goto(url)
        print("\nA browser window has opened. Log into your Google account and "
              "make sure the blog is visible,\nthen press Enter here to continue...")
        input()

        for c in context.cookies():
            session.cookies.set(c["name"], c["value"],
                                domain=c.get("domain"), path=c.get("path", "/"))
        context.close()

    return session


def main():

    parser = argparse.ArgumentParser(description="Blogspot crawler",
        epilog="(C) David Davó - https://ddavo.me. Licensed under a MIT License.")
    parser.add_argument('url', type=str, help='Blog url')
    parser.add_argument('-o', '--output',
        dest='destination',
        type=str, 
        default="./",
        help="Output folder"
    )
    parser.add_argument('-t', '--threads',
        dest='threads',
        type=int,
        default=None,
        help="Number of threads"
    )
    parser.add_argument('-i', '--images-dir',
        dest='images_dir',
        type=str,
        default=None,
        help="Folder to save full-size images to (default: <output>/images)"
    )
    parser.add_argument('--login',
        dest='login',
        action='store_true',
        help="Open a browser to log into Google before crawling (for private blogs)"
    )

    args = parser.parse_args()

    session = login_session(args.url) if args.login else None

    process_pagination = ProcessPagination(
        baseurl=args.url,
        destination=args.destination,
        images_dir=args.images_dir,
        session=session,
        max_workers=args.threads)

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
