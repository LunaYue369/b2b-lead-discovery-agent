"""抓公司官网首页，找上面公开写着的、属于这家公司的邮箱。不花钱。

两个词，下面一直这么用：

    域名        网址或邮箱里 @ 后面、公司自己买下的那一段。
                'https://home.hushly.example/about' 的域名是 hushly.example（去掉 https://、home 这种前缀和路径）；
                'contact@hushly.example' 的域名也是 hushly.example
    这家的邮箱  域名跟这家官网的域名一样的邮箱。contact@hushly.example 是 Hushly 的；
                同一页上的 hi@wix.com（做网站的公司）不是

只抓首页：这家的邮箱一般就写在首页的页脚；/contact、/about 这些路径很多站根本没有（404）。
两个例子：

    http://www.hushly.example    -> 跳了三次到 https://home.hushly.example   找到 contact@hushly.example
    http://www.orbobrand.example -> 跳到 https://orboaudio.example            找到 mark@orboaudio.example

两条规矩：

    一、官网会跳转，用跳转之后的地址。ORBO 在 LinkedIn 上登记的是 orbobrand.example，真正的站在
        orboaudio.example，邮箱也在 orboaudio.example。拿跳转之前的域名去比，mark@orboaudio.example
        就会被当成外站邮箱扔掉
    二、只收这家的邮箱，按域名比。Hushly 的站落在 home.hushly.example，邮箱是 contact@hushly.example，
        两者的域名都是 hushly.example，是这家的。首页上别的邮箱（做网站的公司、第三方插件、
        图片文件名里碰巧长得像邮箱的 logo@2x.png）一律不收

这一层不写库、不判断这家该不该抓。抓失败（超时、证书错、403）不抛异常，交回一个带着
原因的结果：一家官网打不开不该让这一批的别家也找不成。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import unquote

import httpx

from models import ContactChannel, ContactSource, FoundContact, normalize_domain

# 浏览器的 UA。用 httpx 默认的那个（python-httpx/0.x）会被一部分站拦掉：
# Cloudflare 对脚本 UA 直接回 403，acmedevices.example 那边踩过同样的坑（挡的是 python-urllib）
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)

# 一家官网最多等多久。官网慢不是我们的问题，但一家卡住会拖着整批推卡一起等
TIMEOUT_SECONDS = 15.0

# 首页最多读多少字符。正常首页几十 KB 到几百 KB；有的站首页是一整个打包好的前端应用，
# 几 MB。邮箱一般在页脚，但再往后读也只是在一堆脚本里找，截断的代价很小
MAX_CHARS = 2_000_000

# 邮箱的样子。故意宽：宁可多匹配几个，再靠「域名必须跟官网一样」那一道筛掉
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Cloudflare 的邮箱混淆：开了这个功能的站，页面里不写邮箱原文，写成
#     <a href="/cdn-cgi/l/email-protection#<十六进制>">  或  <span data-cfemail="<十六进制>">
# 浏览器里一段脚本把它还原成邮箱。不还原的话，这种站在我们这里就是「首页没有邮箱」
_CF_EMAIL = re.compile(r'(?:data-cfemail="|/cdn-cgi/l/email-protection#)([0-9a-fA-F]+)')

# 两段式的公共后缀：取域名时要往前多取一段。只列我们的客户最可能出现的几个国家，
# 不是完整的公共后缀表（那张表有几千条、要联网更新）。不在表里的两段式后缀会被取短一段，
# 比如 example.com.tw 的域名会被取成 com.tw —— 后果是那家的邮箱比对偏宽，不是偏严
_TWO_PART_SUFFIXES = frozenset(
    {
        "co.uk", "org.uk", "ac.uk",
        "com.au", "net.au", "org.au",
        "co.jp", "co.kr", "co.nz", "co.in", "co.za",
        "com.cn", "net.cn", "org.cn",
        "com.hk", "com.tw", "com.sg", "com.my", "com.br", "com.mx", "com.tr",
    }
)


@dataclass(frozen=True)
class Homepage:
    """抓一次官网首页的结果。

    三种：

        抓到了
        Homepage(requested_url="http://www.hushly.example", final_url="https://home.hushly.example",
                 html="<!DOCTYPE html>...", error=None)

        对方回了错误码
        Homepage(requested_url="http://www.x.com", final_url="https://x.com/", html=None,
                 error="HTTP 403")

        根本没连上
        Homepage(requested_url="http://www.x.com", final_url=None, html=None,
                 error="ConnectTimeout")
    """

    requested_url: str
    final_url: str | None
    html: str | None
    error: str | None


def fetch_homepage(url: str) -> Homepage:
    """抓官网首页，跟着跳转走到底。不抛异常。

    输入：'http://www.hushly.example'，LinkedIn 上登记的原样；没写协议的（'hushly.example'）补 http://
    输出：Homepage，三种见那个类

    跟着跳转走到底：hushly.example 跳了三次（http -> https -> 去掉 www -> home 子域名），
    httpx 默认最多跟 20 次
    """
    # 输入   'hushly.example'
    # 输出   'http://hushly.example'。先用 http：很多小站没配 https 证书，从 https 起步会直接失败；
    #        配了 https 的站会自己把 http 跳过去
    target = url if re.match(r"^https?://", url, re.IGNORECASE) else f"http://{url}"

    try:
        response = httpx.get(
            target,
            follow_redirects=True,
            timeout=TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT},
        )
    except httpx.HTTPError as err:
        # 超时、DNS 查不到、证书错、连接被拒，都在这里。记异常的类名，那是查原因的钥匙
        # 输出   Homepage(..., final_url=None, error="ConnectTimeout")
        return Homepage(requested_url=url, final_url=None, html=None, error=type(err).__name__)

    # 输入   跳转之后的 httpx.URL('https://home.hushly.example')
    # 输出   'https://home.hushly.example'
    final_url = str(response.url)
    if response.status_code >= 400:
        return Homepage(
            requested_url=url, final_url=final_url, html=None, error=f"HTTP {response.status_code}"
        )
    return Homepage(requested_url=url, final_url=final_url, html=response.text[:MAX_CHARS], error=None)


def registrable_domain(host_or_url: str) -> str:
    """取域名：网址或邮箱里公司自己买下来的那一段。

    四个例子：
        'https://home.hushly.example/about' -> 'hushly.example'
        'mail.hushly.example'               -> 'hushly.example'
        'www.example.co.uk'             -> 'example.co.uk'   两段式后缀往前多取一段
        'localhost'                     -> 'localhost'       只有一段，原样

    先过一遍 normalize_domain（小写、去协议、去 www、去路径），跟库里 leads.domain 同一套写法
    """
    host = normalize_domain(host_or_url).split(":")[0]
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in _TWO_PART_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def extract_emails(page_html: str, site_url: str) -> tuple[str, ...]:
    """从首页里挑出这家的邮箱（域名跟官网一样的），按在页面里出现的先后排，去重。纯函数，不连网。

    输入：首页的 html；这一页跳转之后的地址，如 'https://home.hushly.example'
    输出：如 ('contact@hushly.example',)；一个都没有是 ()

    四步，一步一个例子：

        一、还原 Cloudflare 混淆的邮箱
            输入   <a href="/cdn-cgi/l/email-protection#9df5f8f1f1f2ddf8e5fcf0edf1f8b3fef2f0">
            输出   页面文字后面多拼一个 'hello@example.com'
        二、把转义还原成原字符，再在里面找所有长得像邮箱的
            输入   'mailto:contact&#64;hushly.example' 或 'mailto:%20contact@hushly.example'
            输出   ['contact@hushly.example', ...]
        三、只留域名跟这一页一样的
            输入   ['contact@hushly.example', 'logo@2x.png', 'hi@wix.com']，这一页是 home.hushly.example
            输出   ['contact@hushly.example']
        四、按小写去重，保留第一次出现的原样写法
            输入   ['Contact@Hushly.example', 'contact@hushly.example']
            输出   ('Contact@Hushly.example',)
    """
    # 一、Cloudflare 混淆的邮箱还原出来，拼在页面文字后面一起找
    decoded = [_decode_cloudflare(code) for code in _CF_EMAIL.findall(page_html)]
    text = page_html + "\n" + "\n".join(d for d in decoded if d)

    # 二、&#64; 这种 html 转义、%40 %20 这种链接转义都还原，再找
    #     输出   ['contact@hushly.example', 'logo@2x.png', ...]，按出现先后
    text = unquote(html.unescape(text))
    candidates = _EMAIL.findall(text)

    # 三、域名要跟这一页一样
    #     输入   site_url='https://home.hushly.example'
    #     输出   site='hushly.example'
    site = registrable_domain(site_url)

    seen: set[str] = set()
    kept: list[str] = []
    for email in candidates:
        # 输入   'contact@hushly.example.' 句尾带个点
        # 输出   'contact@hushly.example'
        email = email.strip(".")
        domain = email.rsplit("@", 1)[1]
        if registrable_domain(domain) != site:
            continue
        # 四、按小写去重
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append(email)
    return tuple(kept)


def to_contacts(emails: tuple[str, ...], page_url: str) -> tuple[FoundContact, ...]:
    """官网上找到的几个邮箱包成 FoundContact。

    输入：('contact@hushly.example',)；在哪一页找到的，跳转之后的地址 'https://home.hushly.example'
    输出：(FoundContact(channel=EMAIL, value='contact@hushly.example', source=WEBSITE,
                        source_url='https://home.hushly.example', 四样可信度全是 None),)

    四样可信度是 None 不是「很可信」：它就是公开写在官网上的，没有人验证过它收不收得到信
    """
    return tuple(
        FoundContact(
            channel=ContactChannel.EMAIL,
            value=email,
            source=ContactSource.WEBSITE,
            source_url=page_url,
            email_status=None,
            catch_all=None,
            found_in_google=None,
            quality_score=None,
        )
        for email in emails
    )


def _decode_cloudflare(code: str) -> str | None:
    """还原一个 Cloudflare 混淆过的邮箱。

    输入：一串十六进制，如 '9df5f8f1f1f2ddf8e5fcf0edf1f8b3fef2f0'
          第一个字节 9d 是钥匙，后面每个字节跟钥匙异或一下就是原字符：f5 ^ 9d = 68 = 'h'
    输出：'hello@example.com'；那串东西不合格式（长度是单数、不是十六进制）是 None

    这是 Cloudflare 公开的做法（浏览器里那段还原脚本就是这么算的），不是我们猜的
    """
    if len(code) < 4 or len(code) % 2:
        return None
    try:
        key = int(code[:2], 16)
        return "".join(chr(int(code[i : i + 2], 16) ^ key) for i in range(2, len(code), 2))
    except ValueError:
        return None
