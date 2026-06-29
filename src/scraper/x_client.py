import re
import math
import time
import random
import base64
import hashlib
import httpx
import bs4
from functools import reduce

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

INDICES_RE = re.compile(r"(\(\w{1}\[(\d{1,2})\],\s*16\))+")


def _float_to_hex(x):
    result, quotient, fraction = [], int(x), x - int(x)
    while quotient > 0:
        quotient = int(x / 16)
        remainder = int(x - float(quotient) * 16)
        result.insert(0, chr(remainder + 55) if remainder > 9 else str(remainder))
        x = float(quotient)
    if fraction == 0:
        return "".join(result)
    result.append(".")
    while fraction > 0:
        fraction *= 16
        integer = int(fraction)
        fraction -= float(integer)
        result.append(chr(integer + 55) if integer > 9 else str(integer))
    return "".join(result)


def _is_odd(num):
    return -1.0 if num % 2 else 0.0


def _interpolate(from_list, to_list, f):
    return [a * (1 - f) + b * f for a, b in zip(from_list, to_list)]


def _cubic_bezier_value(curves, time):
    start, end = 0.0, 1.0
    mid = 0.0
    if time <= 0:
        return (
            curves[1] / curves[0]
            if curves[0] > 0
            else curves[3] / curves[2] if curves[1] == 0 and curves[2] > 0 else 0
        ) * time
    if time >= 1:
        return 1 + (
            (curves[3] - 1) / (curves[2] - 1)
            if curves[2] < 1
            else (
                (curves[1] - 1) / (curves[0] - 1)
                if curves[2] == 1 and curves[0] < 1
                else 0
            )
        ) * (time - 1)

    def calc(a, b, m):
        return 3 * a * (1 - m) ** 2 * m + 3 * b * (1 - m) * m**2 + m**3

    while start < end:
        mid = (start + end) / 2
        x_est = calc(curves[0], curves[2], mid)
        if abs(time - x_est) < 0.00001:
            return calc(curves[1], curves[3], mid)
        start, end = (mid, end) if x_est < time else (start, mid)
    return calc(curves[1], curves[3], mid)


def _convert_rotation_to_matrix(degrees):
    rad = math.radians(degrees)
    return [math.cos(rad), -math.sin(rad), math.sin(rad), math.cos(rad)]


def _solve(value, min_val, max_val, rounding):
    result = value * (max_val - min_val) / 255 + min_val
    return math.floor(result) if rounding else round(result, 2)


class XClientTransaction:
    DEFAULT_KEYWORD = "obfiowerehiring"
    ADDITIONAL_RANDOM_NUMBER = 3

    def __init__(self):
        self.key = self.key_bytes = self.animation_key = None
        self.row_index = self.key_byte_indices = None

    def init(self, html: str, ondemand_js: str):
        soup = bs4.BeautifulSoup(html, "lxml")
        self.row_index, self.key_byte_indices = self._get_indices(ondemand_js)
        self.key = self._get_key(soup)
        self.key_bytes = list(base64.b64decode(self.key.encode()))
        self.animation_key = self._get_animation_key(soup)

    def _get_key(self, soup):
        el = soup.select_one("[name='twitter-site-verification']")
        if not el:
            raise ValueError("twitter-site-verification meta tag not found")
        return el["content"]

    def _get_indices(self, js_text):
        matches = [int(m.group(2)) for m in INDICES_RE.finditer(js_text)]
        if not matches:
            raise ValueError("Could not extract key-byte indices from JS")
        return matches[0], matches[1:]

    def _get_2d_array(self, soup):
        frames = soup.select("[id^='loading-x-anim']")
        chosen = frames[self.key_bytes[5] % 4]
        path_d = list(list(chosen.children)[0].children)[1]["d"][9:]
        return [
            [int(x) for x in re.sub(r"[^\d]+", " ", seg).strip().split()]
            for seg in path_d.split("C")
        ]

    def _get_animation_key(self, soup):
        kb = self.key_bytes
        row_index = kb[self.row_index] % 16
        frame_time = reduce(
            lambda a, b: a * b, [kb[i] % 16 for i in self.key_byte_indices]
        )
        arr = self._get_2d_array(soup)
        frame_row = arr[row_index]
        target = float(frame_time) / 4096.0

        from_color = [float(v) for v in [*frame_row[:3], 1]]
        to_color = [float(v) for v in [*frame_row[3:6], 1]]
        from_rot = [0.0]
        to_rot = [_solve(float(frame_row[6]), 60.0, 360.0, True)]
        curves = [
            _solve(float(v), _is_odd(i), 1.0, False) for i, v in enumerate(frame_row[7:])
        ]

        val = _cubic_bezier_value(curves, target)
        color = _interpolate(from_color, to_color, val)
        color = [max(0, v) for v in color]
        rot = _interpolate(from_rot, to_rot, val)
        matrix = _convert_rotation_to_matrix(rot[0])

        parts = [format(round(v), "x") for v in color[:-1]]
        for v in matrix:
            v = abs(round(v, 2))
            h = _float_to_hex(v)
            parts.append(("0" + h) if h.startswith(".") else (h or "0"))
        parts += ["0", "0"]
        return re.sub(r"[.-]", "", "".join(parts))

    def generate(self, method: str, path: str) -> str:
        t = math.floor((time.time() * 1000 - 1682924400_000) / 1000)
        t_bytes = [(t >> (i * 8)) & 0xFF for i in range(4)]
        h = hashlib.sha256(
            f"{method}!{path}!{t}{self.DEFAULT_KEYWORD}{self.animation_key}".encode()
        ).digest()
        rand = random.randint(0, 255)
        payload = bytes(
            [
                rand,
                *[
                    b ^ rand
                    for b in [
                        *self.key_bytes,
                        *t_bytes,
                        *h[:16],
                        self.ADDITIONAL_RANDOM_NUMBER,
                    ]
                ],
            ]
        )
        return base64.b64encode(payload).decode().rstrip("=")


def fetch_and_init() -> XClientTransaction:
    """Fetch x.com and the ondemand JS, then initialise an XClientTransaction."""
    with httpx.Client(follow_redirects=True, timeout=15) as client:
        r = client.get("https://x.com/home", headers=BROWSER_HEADERS)
        html = r.text

        chunk_id = re.search(r'(\d+):"ondemand\.s"', html).group(1)
        chunk_hash = re.search(rf'{chunk_id}:"([a-f0-9]+)"', html).group(1)
        js_url = f"https://abs.twimg.com/responsive-web/client-web/ondemand.s.{chunk_hash}a.js"
        js = client.get(js_url, headers=BROWSER_HEADERS).text

    ct = XClientTransaction()
    ct.init(html, js)
    return ct
