"""百度网盘 Web 转存与 OpenList 分享。所有网络 I/O 均为异步。

百度 Web API 并非稳定的公开 API；发生风控或协议变化时明确报错，不绕过验证码。
为避免 ondup 重命名、目录缓存及 BDCLND 并发污染，每次使用独立目标目录，
并在插件级锁内完成整个流水线。成功响应不等于异步转存任务已经完成。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import posixpath
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

BAIDU = "https://pan.baidu.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
LINK_RE = re.compile(
    r"(?<![\w.@/-])(?:https?://)?pan\.baidu\.com/"
    r"(?:s/1[A-Za-z0-9_-]+|(?:wap/)?init\?[^\s<>\"'，。；！？（）【】]+)"
    r"(?:\?[^\s<>\"'，。；！？（）【】]*)?",
    re.IGNORECASE,
)
PASSWORD_RE = re.compile(
    r"(?:提取码|提取碼|密码|密碼|访问码|訪問碼|pwd)\s*[:：=]?\s*"
    r"([a-zA-Z0-9]{4})(?![a-zA-Z0-9])",
    re.IGNORECASE,
)
BASE_PARAMS = {"channel": "chunlei", "web": "1", "clienttype": "0"}
RESULT_LIMIT = 20


class PluginError(Exception):
    """可以安全展示给用户的业务错误（上游报错仍需脱敏）。"""


def redact(text: str, secrets: list[str]) -> str:
    """过滤配置值与常见凭据，避免异常日志泄露 Cookie、Token、动态码。"""
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            text = text.replace(secret, "[已隐藏]")
            text = text.replace(quote(secret, safe=""), "[已隐藏]")
    return re.sub(
        r"(?i)((?:BDUSS|STOKEN|BDCLND|bdstoken|token|password|otp_code)"
        r"[\"'\s]*[:=][\"'\s]*)([^\s;&\"'<>]+)",
        r"\1[已隐藏]",
        text,
    )


def message_text(value: Any) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value))[:240]


def disk_path(value: str, label: str) -> str:
    if (
        not value.startswith("/")
        or "\\" in value
        or any(ord(c) < 32 for c in value)
        or any(p in (".", "..") for p in value.split("/"))
    ):
        raise PluginError(f"{label} 必须是以 / 开头且不含 .、.. 或控制字符的网盘路径。")
    return "/" + "/".join(p for p in value.split("/") if p)


def safe_filename(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in (".", "..")
        or "/" in value
        or "\\" in value
        or any(ord(c) < 32 for c in value)
    ):
        raise PluginError("百度返回了无法安全拼接的文件名，已停止操作。")
    return value


@dataclass(frozen=True)
class ShareLink:
    surl: str
    pwd: str = ""

    @property
    def url(self) -> str:
        return f"{BAIDU}/s/1{self.surl}"


def parse_share_links(text: str) -> list[ShareLink]:
    """按每个链接与下一个链接之间的文本绑定密码，避免多链接密码串用。

    /s/1xxx 仅去掉前导的一个 1；init 的 surl 保持原样（它也可能以 1 开头）。
    链接 query 的密码优先，其次外置标签密码，最后紧随链接的独立四位密码。
    """
    text = html.unescape(text)
    matches = list(LINK_RE.finditer(text))
    links: dict[str, ShareLink] = {}
    for index, match in enumerate(matches):
        raw = match.group().rstrip(".,;!?)]}，。；！？）】")
        parsed = urlsplit(raw if "://" in raw else "https://" + raw)
        if parsed.hostname is None or parsed.hostname.lower() != "pan.baidu.com":
            continue
        query = parse_qs(parsed.query)
        if parsed.path.startswith("/s/1"):
            surl = parsed.path[len("/s/1") :]
        else:
            surl = query.get("surl", [""])[0]
        if not re.fullmatch(r"[A-Za-z0-9_-]{5,128}", surl):
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        tail = text[match.end() : end]
        pwd = query.get("pwd", [""])[0]
        if not re.fullmatch(r"[A-Za-z0-9]{4}", pwd):
            tagged = PASSWORD_RE.search(tail[:256])
            bare = re.match(
                r"^[\s)\]}>，,；;：:]*([A-Za-z0-9]{4})(?![A-Za-z0-9])", tail
            )
            pwd = tagged.group(1) if tagged else bare.group(1) if bare else ""
        # 同一消息重复粘贴时保留首次有效密码，不重复转存。
        if surl not in links or (not links[surl].pwd and pwd):
            links[surl] = ShareLink(surl, pwd)
    return list(links.values())


@dataclass(frozen=True)
class Settings:
    cookie: str
    baidu_path: str
    url: str
    username: str
    password: str
    token: str
    otp_key: str
    openlist_path: str
    directory_password: str
    share_pwd: str
    max_accessed: int
    share_prefix: str
    share_format: str
    mode: str
    max_links: int
    max_files: int
    http_timeout: int
    task_timeout: int

    @classmethod
    def from_config(cls, config: Any) -> Settings:
        def string(key: str, default: str = "") -> str:
            value = config.get(key, default)
            if not isinstance(value, str):
                raise PluginError(f"配置 {key} 必须是字符串。")
            return value

        def integer(key: str, default: int, low: int, high: int) -> int:
            value = config.get(key, default)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not low <= value <= high
            ):
                raise PluginError(f"配置 {key} 必须是 {low}～{high} 的整数。")
            return value

        cookie = string("baidu_cookie").strip()
        cookies = parse_cookies(cookie)
        if not cookies.get("BDUSS") or not cookies.get("STOKEN"):
            raise PluginError("请先配置百度网盘 Cookie，必须包含非空 BDUSS 和 STOKEN。")
        url = string("openlist_url", "http://127.0.0.1:5244").strip().rstrip("/")
        try:
            parsed = urlsplit(url)
            valid_url = (
                parsed.scheme in ("http", "https")
                and parsed.hostname
                and parsed.port != 0
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
                and not any(c.isspace() for c in url)
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise PluginError(
                "openlist_url 必须是无账户、查询参数及片段的完整 http(s) 地址。"
            )
        prefix = string("openlist_share_prefix", "/s/")
        if not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)+", prefix):
            raise PluginError(
                "openlist_share_prefix 应类似 /s/ 或 /sd/，以 / 开头和结尾。"
            )
        mode = string("output_mode", "share")
        share_format = string("openlist_share_api_format", "files")
        if mode not in ("share", "direct") or share_format not in ("files", "paths"):
            raise PluginError("output_mode 或 openlist_share_api_format 配置无效。")
        max_accessed = integer("share_max_accessed", 0, 0, 2147483647)
        if mode == "share" and share_format == "paths" and max_accessed:
            raise PluginError(
                "paths 格式未定义访问次数限制；请改用 files 格式或将次数设为 0。"
            )
        return cls(
            cookie=cookie,
            baidu_path=disk_path(
                string("baidu_save_path", "/AstrBot转存"), "baidu_save_path"
            ),
            url=url,
            username=string("openlist_username", "admin"),
            password=string("openlist_password"),
            token=string("openlist_token").strip(),
            otp_key=string("openlist_otp_key").strip(),
            openlist_path=disk_path(
                string("openlist_save_path", "/baidu/AstrBot转存"), "openlist_save_path"
            ),
            directory_password=string("openlist_directory_password"),
            share_pwd=string("openlist_share_pwd", config.get("share_pwd", "")),
            max_accessed=max_accessed,
            share_prefix=prefix,
            share_format=share_format,
            mode=mode,
            max_links=integer("max_links_per_message", 3, 1, 10),
            max_files=integer("max_files_per_share", 1000, 1, 10000),
            http_timeout=integer("http_timeout", 30, 5, 120),
            task_timeout=integer("task_timeout", 120, 10, 600),
        )

    def secrets(self) -> list[str]:
        return [
            self.cookie,
            *parse_cookies(self.cookie).values(),
            self.password,
            self.token,
            self.otp_key,
            self.directory_password,
            self.share_pwd,
        ]


def parse_cookies(cookie: str) -> dict[str, str]:
    if "\r" in cookie or "\n" in cookie or cookie.lower().startswith("cookie:"):
        raise PluginError(
            "baidu_cookie 应为单行 Cookie 值，不要包含 Cookie: 前缀或换行。"
        )
    result = {}
    for part in cookie.split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and re.fullmatch(r"[A-Za-z0-9_-]+", key):
            result[key] = value.strip()
    return result


async def request(
    client: httpx.AsyncClient, method: str, url: str, stage: str, **kwargs: Any
) -> httpx.Response:
    try:
        response = await client.request(method, url, **kwargs)
    except httpx.TimeoutException as exc:
        suffix = (
            "写入可能已提交，请先检查对应目录，不要直接重复提交。"
            if method == "POST"
            else "请稍后重试。"
        )
        raise PluginError(f"{stage}请求超时。{suffix}") from exc
    except httpx.RequestError as exc:
        raise PluginError(f"{stage}网络连接失败，请检查网络和服务地址。") from exc
    return response


def json_response(response: httpx.Response, stage: str) -> dict[str, Any]:
    if response.status_code >= 400:
        raise PluginError(f"{stage}失败（HTTP {response.status_code}）。")
    try:
        value = response.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise PluginError(
            f"{stage}未返回 JSON，可能被登录页、风控或反向代理拦截。"
        ) from exc
    if not isinstance(value, dict):
        raise PluginError(f"{stage}返回的数据结构无效。")
    return value


def errno(data: dict[str, Any], field: str = "errno") -> int:
    try:
        return int(data[field])
    except (KeyError, ValueError, TypeError) as exc:
        raise PluginError(f"接口响应缺少有效的 {field} 字段。") from exc


def check_baidu(data: dict[str, Any], stage: str) -> None:
    code = errno(data)
    if code == 0:
        return
    if stage == "验证提取码" and code in (-9, -12):
        reason = "提取码错误或已失效"
    else:
        reason = {
            -6: "百度登录态失效，请更新包含 BDUSS 和 STOKEN 的 Cookie",
            -9: "分享已取消、已过期或文件/目录不存在",
            -12: "分享访问校验失败，请检查提取码及 Cookie",
            -62: "百度要求验证码，请先在浏览器完成验证后更新 Cookie",
            -20: "百度触发账户或访问频率限制，请在浏览器检查账户",
            12: "文件或目录不存在，或无访问权限",
            120: "转存文件数量超出百度限制，请减少文件数或分批转存",
            130: "转存文件数量超出百度账户限制",
            -32: "网盘剩余空间不足",
        }.get(code, "百度接口拒绝了请求")
    detail = data.get("errmsg") or data.get("err_msg") or data.get("show_msg")
    extra = f"；服务端：{message_text(detail)}" if detail else ""
    raise PluginError(f"{stage}失败：{reason}（errno={code}）{extra}")


def share_page_fields(page: str) -> dict[str, str]:
    """只提取明确的标量字段，不 eval 分享页中的 JavaScript。"""
    names = {
        "shareid": "shareid",
        "share_id": "shareid",
        "share_uk": "uk",
        "uk": "uk",
        "bdstoken": "bdstoken",
    }
    result = {}
    pattern = (
        r"[\"']?(shareid|share_id|share_uk|uk|bdstoken)[\"']?\s*[:=]\s*"
        r"(?:\"([^\"]*)\"|'([^']*)'|(\d+))"
    )
    for match in re.finditer(pattern, html.unescape(page), re.IGNORECASE):
        key = names[match.group(1).lower()]
        value = next((part for part in match.groups()[1:] if part is not None), "")
        if key != "bdstoken" and not re.fullmatch(r"\d+", value):
            continue
        # share_uk 比通用 uk 更明确，后者在某些页面也代表当前登录用户。
        if key not in result or match.group(1).lower() == "share_uk":
            result[key] = value
    return result


@dataclass(frozen=True)
class BaiduItem:
    fs_id: int
    name: str
    is_dir: bool


@dataclass(frozen=True)
class TransferResult:
    path: str
    folder: str
    items: list[BaiduItem]


class BaiduClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.AsyncClient(
            timeout=settings.http_timeout,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Referer": f"{BAIDU}/disk/home"},
        )

    async def close(self) -> None:
        await self.client.aclose()

    def reset_cookies(self) -> None:
        self.client.cookies.clear()
        for key, value in parse_cookies(self.settings.cookie).items():
            if key != "BDCLND":
                self.client.cookies.set(key, value, domain="pan.baidu.com", path="/")

    async def api(
        self, method: str, endpoint: str, stage: str, **kwargs: Any
    ) -> dict[str, Any]:
        response = await request(self.client, method, BAIDU + endpoint, stage, **kwargs)
        if response.is_redirect:
            raise PluginError(f"{stage}被重定向到登录/验证页面，请更新百度 Cookie。")
        return json_response(response, stage)

    async def page(self, link: ShareLink) -> str:
        url = link.url
        for _ in range(5):
            response = await request(self.client, "GET", url, "读取百度分享页")
            if not response.is_redirect:
                if response.status_code >= 400:
                    raise PluginError(
                        f"分享页无法访问（HTTP {response.status_code}），链接可能已失效。"
                    )
                return response.text
            url = urljoin(url, response.headers.get("location", ""))
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "pan.baidu.com"
                or parsed.port not in (None, 443)
            ):
                raise PluginError(
                    "分享页跳转至登录或外部验证页面，请更新百度 Cookie 后重试。"
                )
        raise PluginError("百度分享页重定向次数过多，请在浏览器检查链接。")

    async def token(self, fields: dict[str, str]) -> str:
        value = fields.get("bdstoken", "")
        if re.fullmatch(r"[A-Za-z0-9]{16,128}", value) and set(value) != {"0"}:
            return value
        data = await self.api(
            "GET",
            "/api/gettemplatevariable",
            "获取百度登录令牌",
            params={**BASE_PARAMS, "fields": '["bdstoken"]'},
        )
        check_baidu(data, "获取百度登录令牌")
        result = data.get("result") or data.get("data") or {}
        value = result.get("bdstoken") if isinstance(result, dict) else None
        if (
            not isinstance(value, str)
            or not re.fullmatch(r"[A-Za-z0-9]{16,128}", value)
            or set(value) == {"0"}
        ):
            raise PluginError("无法取得登录态 bdstoken，请更新百度网盘 Cookie。")
        return value

    async def share_items(
        self, fields: dict[str, str], link: ShareLink
    ) -> list[BaiduItem]:
        items: dict[int, BaiduItem] = {}
        for page in range(1, self.settings.max_files // 100 + 2):
            data = await self.api(
                "GET",
                "/share/list",
                "获取分享文件列表",
                params={
                    **BASE_PARAMS,
                    "uk": fields["uk"],
                    "shareid": fields["shareid"],
                    "root": "1",
                    "page": page,
                    "num": 100,
                    "order": "name",
                    "desc": "0",
                },
                headers={"Referer": link.url},
            )
            check_baidu(data, "获取分享文件列表")
            entries = data.get("list")
            if not isinstance(entries, list):
                raise PluginError("百度未返回有效的分享文件列表。")
            previous = len(items)
            for entry in entries:
                try:
                    item = BaiduItem(
                        int(entry["fs_id"]),
                        safe_filename(entry["server_filename"]),
                        bool(int(entry.get("isdir", 0))),
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise PluginError(
                        "百度分享文件条目缺少有效 fs_id 或文件名。"
                    ) from exc
                items[item.fs_id] = item
            if len(items) > self.settings.max_files:
                raise PluginError(
                    f"分享顶层条目超过配置上限 {self.settings.max_files}，请减少条目或提高配置。"
                )
            more = data.get("has_more")
            if more in (0, False, "0") or (more is None and len(entries) < 100):
                if not items:
                    raise PluginError("此分享没有可转存的文件，可能已被清空或取消。")
                return list(items.values())
            if len(items) == previous:
                raise PluginError("分享文件列表分页没有进展，为避免遗漏文件已停止。")
        raise PluginError("分享文件分页超过配置上限，未执行转存。")

    async def own_list(self, path: str, token: str) -> dict[str, Any]:
        return await self.api(
            "GET",
            "/api/list",
            "读取百度保存目录",
            params={
                **BASE_PARAMS,
                "bdstoken": token,
                "dir": path,
                "num": 1000,
                "page": 1,
                "order": "name",
                "desc": "0",
            },
        )

    async def create_dir(self, path: str, token: str) -> None:
        data = await self.api(
            "POST",
            "/api/create",
            "创建百度保存目录",
            params={**BASE_PARAMS, "a": "commit", "bdstoken": token},
            data={"path": path, "isdir": "1", "block_list": "[]"},
        )
        if errno(data) == -8:
            # 只接受确实存在的目录，不把同名文件当目录，也不使用 newcopy 改名。
            check_baidu(await self.own_list(path, token), "验证已存在目录")
            return
        check_baidu(data, "创建百度保存目录")
        if data.get("path") and data["path"] != path:
            raise PluginError("百度创建目录时改变了目标路径，已停止转存。")

    async def ensure_base(self, token: str) -> None:
        path = ""
        for part in self.settings.baidu_path.strip("/").split("/"):
            if not part:
                continue
            path += "/" + part
            data = await self.own_list(path, token)
            if errno(data) in (-9, 12):
                await self.create_dir(path, token)
            else:
                check_baidu(data, "检查百度保存目录")

    async def wait_task(self, task_id: Any, token: str) -> None:
        deadline = time.monotonic() + self.settings.task_timeout
        while time.monotonic() < deadline:
            remaining = max(0.01, deadline - time.monotonic())
            try:
                data = await asyncio.wait_for(
                    self.api(
                        "GET",
                        "/share/taskquery",
                        "查询百度转存任务",
                        params={
                            **BASE_PARAMS,
                            "bdstoken": token,
                            "taskid": str(task_id),
                        },
                    ),
                    timeout=remaining,
                )
            except asyncio.TimeoutError as exc:
                raise PluginError(
                    "等待百度异步转存超时；任务可能仍在后台执行。"
                ) from exc
            check_baidu(data, "查询百度转存任务")
            state = data.get("data") if isinstance(data.get("data"), dict) else data
            task_error = state.get("task_errno", 0)
            if str(task_error) != "0":
                check_baidu({"errno": task_error}, "百度异步转存")
            status = str(state.get("status", "")).lower()
            if status in ("success", "finished", "completed"):
                self.check_transfer_info(state)
                return
            if status in ("failed", "failure", "error"):
                raise PluginError(
                    "百度异步转存任务失败，请检查空间、文件数和账户限制。"
                )
            await asyncio.sleep(min(2, max(0, deadline - time.monotonic())))
        raise PluginError("等待百度异步转存超时；任务可能仍在后台执行。")

    @staticmethod
    def check_transfer_info(data: dict[str, Any]) -> None:
        entries = data.get("info", [])
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and str(entry.get("errno", 0)) != "0":
                    check_baidu(entry, "转存单个文件（可能已有部分文件成功）")

    async def confirm_files(
        self, path: str, token: str, items: list[BaiduItem]
    ) -> None:
        # 只在本次新目录中核实文件名；不会从根目录挑选同名旧文件。
        wanted = {item.name for item in items}
        for attempt in range(3):
            found = set()
            for page in range(1, len(items) // 1000 + 2):
                data = await self.api(
                    "GET",
                    "/api/list",
                    "确认百度转存结果",
                    params={
                        **BASE_PARAMS,
                        "bdstoken": token,
                        "dir": path,
                        "page": page,
                        "num": 1000,
                        "order": "name",
                        "desc": "0",
                    },
                )
                check_baidu(data, "确认百度转存结果")
                entries = data.get("list")
                if not isinstance(entries, list):
                    raise PluginError("确认转存结果时百度未返回文件列表。")
                found.update(
                    entry.get("server_filename")
                    for entry in entries
                    if isinstance(entry, dict)
                )
                if len(entries) < 1000:
                    break
            if wanted <= found:
                return
            if attempt < 2:
                await asyncio.sleep(2)
        raise PluginError("百度转存尚未完整落盘，或部分文件受限；请检查本次保存目录。")

    async def transfer(self, link: ShareLink) -> TransferResult:
        self.reset_cookies()
        fields = share_page_fields(await self.page(link))
        if link.pwd:
            data = await self.api(
                "POST",
                "/share/verify",
                "验证提取码",
                params={**BASE_PARAMS, "surl": link.surl, "t": int(time.time() * 1000)},
                data={"pwd": link.pwd, "vcode": "", "vcode_str": ""},
                headers={"Referer": f"{BAIDU}/disk/home"},
            )
            check_baidu(data, "验证提取码")
            randsk = data.get("randsk")
            if not isinstance(randsk, str) or not randsk:
                raise PluginError(
                    "提取码验证没有返回 randsk，请在浏览器检查分享是否需要验证码。"
                )
            # 响应可能同时 Set-Cookie 到 .baidu.com；先清掉同名的不同域 Cookie，
            # 避免请求头携带两个 BDCLND，导致服务器采用旧值。
            for cookie in list(self.client.cookies.jar):
                if cookie.name == "BDCLND":
                    self.client.cookies.delete(
                        "BDCLND", domain=cookie.domain, path=cookie.path
                    )
            self.client.cookies.set(
                "BDCLND", unquote(randsk), domain="pan.baidu.com", path="/"
            )
            fields.update(share_page_fields(await self.page(link)))
        if not fields.get("shareid") or not fields.get("uk"):
            raise PluginError(
                "无法读取 shareid/share_uk：分享可能已取消、提取码缺失，或百度 Cookie 已失效。"
            )
        token = await self.token(fields)
        items = await self.share_items(fields, link)
        await self.ensure_base(token)
        folder = (
            datetime.now(timezone.utc).strftime("bdpan_%Y%m%dT%H%M%SZ_")
            + uuid.uuid4().hex[:12]
        )
        path = posixpath.join(self.settings.baidu_path, folder)
        await self.create_dir(path, token)
        try:
            data = await self.api(
                "POST",
                "/share/transfer",
                "提交百度转存",
                params={
                    **BASE_PARAMS,
                    "shareid": fields["shareid"],
                    "from": fields["uk"],
                    "ondup": "newcopy",
                    "async": "1",
                    "bdstoken": token,
                },
                data={
                    "fsidlist": json.dumps([item.fs_id for item in items]),
                    "path": path,
                },
                headers={"Referer": link.url},
            )
            check_baidu(data, "提交百度转存")
            self.check_transfer_info(data)
            nested = data.get("data") if isinstance(data.get("data"), dict) else {}
            task_id = data.get("taskid") or data.get("task_id") or nested.get("taskid")
            if task_id:
                await self.wait_task(task_id, token)
            await self.confirm_files(path, token, items)
        except PluginError as exc:
            raise PluginError(
                f"{exc}\n本次百度目录：{path}。可能已有文件转入，请先检查再重试。"
            ) from exc
        return TransferResult(path, folder, items)


def totp(key: str, timestamp: float | None = None) -> str:
    cleaned = re.sub(r"\s+", "", key).upper().rstrip("=")
    try:
        secret = base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))
    except (ValueError, base64.binascii.Error) as exc:
        raise PluginError("openlist_otp_key 不是有效的 Base32 密钥。") from exc
    if not secret:
        raise PluginError("openlist_otp_key 不能为空密钥。")
    counter = int((time.time() if timestamp is None else timestamp) // 30)
    digest = hmac.new(secret, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 15
    number = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return f"{number % 1000000:06d}"


class OpenListClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.AsyncClient(
            timeout=settings.http_timeout, follow_redirects=False
        )
        self._token = settings.token
        self._login_lock = asyncio.Lock()

    async def close(self) -> None:
        await self.client.aclose()

    async def login(self) -> str:
        async with self._login_lock:
            if self._token:
                return self._token
            body = {
                "username": self.settings.username,
                "password": self.settings.password,
            }
            if self.settings.otp_key:
                body["otp_code"] = totp(self.settings.otp_key)
            response = await request(
                self.client,
                "POST",
                self.settings.url + "/api/auth/login",
                "OpenList 登录",
                json=body,
            )
            data = json_response(response, "OpenList 登录")
            if errno(data, "code") != 200:
                raise PluginError(
                    f"OpenList 登录失败：{message_text(data.get('message', '请检查用户名、密码或两步验证'))}"
                )
            nested = data.get("data")
            token = nested.get("token") if isinstance(nested, dict) else None
            if not isinstance(token, str) or not token:
                raise PluginError("OpenList 登录响应缺少 Token。")
            self._token = token
            return token

    async def call(self, endpoint: str, body: dict[str, Any]) -> Any:
        for attempt in range(2):
            token = await self.login()
            response = await request(
                self.client,
                "POST",
                self.settings.url + endpoint,
                "OpenList " + endpoint,
                json=body,
                headers={"Authorization": token},
            )
            data = (
                None
                if response.status_code == 401
                else json_response(response, "OpenList " + endpoint)
            )
            code = 401 if data is None else errno(data, "code")
            if code == 401:
                if self.settings.token:
                    raise PluginError(
                        "OpenList 静态 Token 无效或已过期，请更新 openlist_token。"
                    )
                if attempt == 0:
                    if self._token == token:
                        self._token = ""
                    continue
                raise PluginError("OpenList 重新登录后仍未通过认证，请检查账户权限。")
            if code != 200:
                raise PluginError(
                    f"OpenList {endpoint} 失败（code={code}）：{message_text(data.get('message', '未知原因'))}"
                )
            return data.get("data")
        raise PluginError("OpenList 认证失败。")

    def frontend_url(self, path: str) -> str:
        return self.settings.url + quote(path, safe="/")

    async def refreshed_items(
        self, result: TransferResult
    ) -> tuple[str, list[dict[str, Any]]]:
        folder_path = posixpath.join(self.settings.openlist_path, result.folder)
        expected = {item.name: item.is_dir for item in result.items}
        last_error = "目录中尚未出现本次转存的完整文件列表"
        for attempt in range(3):
            try:
                # 必须先刷新父目录，才能让挂载驱动看到新建的独立子目录。
                parent = await self.call(
                    "/api/fs/list",
                    {
                        "path": self.settings.openlist_path,
                        "password": self.settings.directory_password,
                        "page": 1,
                        "per_page": 0,
                        "refresh": True,
                    },
                )
                entries = parent.get("content") if isinstance(parent, dict) else None
                if not isinstance(entries, list) or not any(
                    isinstance(e, dict)
                    and e.get("name") == result.folder
                    and e.get("is_dir") is True
                    for e in entries
                ):
                    raise PluginError("OpenList 父目录中尚未出现本次转存目录")
                listing = await self.call(
                    "/api/fs/list",
                    {
                        "path": folder_path,
                        "password": self.settings.directory_password,
                        "page": 1,
                        "per_page": 0,
                        "refresh": True,
                    },
                )
                entries = listing.get("content") if isinstance(listing, dict) else None
                if not isinstance(entries, list):
                    raise PluginError("OpenList 未返回目录 content 列表")
                actual = {e.get("name"): e for e in entries if isinstance(e, dict)}
                if all(
                    name in actual and actual[name].get("is_dir") == is_dir
                    for name, is_dir in expected.items()
                ):
                    return folder_path, [actual[item.name] for item in result.items]
                raise PluginError("OpenList 尚未同步本次转存的全部文件")
            except PluginError as exc:
                last_error = str(exc)
            if attempt < 2:
                await asyncio.sleep(2)
        raise PluginError(
            f"强制刷新 3 次后仍无法取得对应文件：{last_error}。请核对两个保存路径是否对应同一百度账户及目录。"
        )

    async def create_share(self, paths: list[str]) -> str:
        if self.settings.share_format == "files":
            body = {
                "files": paths,
                "pwd": self.settings.share_pwd,
                "max_accessed": self.settings.max_accessed,
            }
        else:
            body = {"paths": paths, "password": self.settings.share_pwd}
        data = await self.call("/api/share/create", body)
        share_id = (
            data.get("id") or data.get("share_id") if isinstance(data, dict) else data
        )
        if not isinstance(share_id, (str, int)) or not str(share_id):
            raise PluginError(
                "OpenList 创建分享响应缺少分享 ID，请检查接口格式与版本。"
            )
        return (
            self.settings.url
            + self.settings.share_prefix
            + quote(str(share_id), safe="")
        )

    async def direct_links(self, folder: str, entries: list[dict[str, Any]]) -> str:
        lines = []
        for item in entries[:RESULT_LIMIT]:
            path = posixpath.join(folder, safe_filename(item["name"]))
            url = self.frontend_url(path)
            kind = "目录入口" if item["is_dir"] else "文件入口"
            if not item["is_dir"]:
                try:
                    data = await self.call(
                        "/api/fs/get",
                        {"path": path, "password": self.settings.directory_password},
                    )
                    raw = data.get("raw_url") if isinstance(data, dict) else None
                    if (
                        isinstance(raw, str)
                        and urlsplit(raw).scheme in ("http", "https")
                        and urlsplit(raw).netloc
                    ):
                        url, kind = raw, "文件直链"
                except PluginError as exc:
                    logger.error(
                        redact(
                            f"[bdpan_openlist] 获取直链失败，返回前端入口：{exc}",
                            self.settings.secrets() + [self._token],
                        )
                    )
            lines.append(f"{message_text(item['name'])}（{kind}）\n{url}")
        if len(entries) > RESULT_LIMIT:
            lines.append(
                f"另有 {len(entries) - RESULT_LIMIT} 项，请打开本次目录：\n{self.frontend_url(folder)}"
            )
        return "\n\n".join(lines)

    async def publish(self, result: TransferResult) -> str:
        folder, entries = await self.refreshed_items(result)
        names = "\n".join(
            f"• {message_text(item.name)}" for item in result.items[:RESULT_LIMIT]
        )
        if len(result.items) > RESULT_LIMIT:
            names += f"\n• ……共 {len(result.items)} 项"
        heading = f"✅ 百度转存成功（{len(result.items)} 项）\n{names}"
        fallback_reason = ""
        if self.settings.mode == "share":
            try:
                url = await self.create_share(
                    [posixpath.join(folder, item.name) for item in result.items]
                )
                password = (
                    f"\n提取码：{self.settings.share_pwd}"
                    if self.settings.share_pwd
                    else ""
                )
                maximum = (
                    f"\n最大访问次数：{self.settings.max_accessed}"
                    if self.settings.max_accessed
                    else ""
                )
                return f"{heading}\n\n🔗 OpenList 分享：\n{url}{password}{maximum}"
            except PluginError as exc:
                fallback_reason = redact(
                    str(exc), self.settings.secrets() + [self._token]
                )
                logger.error(f"[bdpan_openlist] 分享失败，降级链接：{fallback_reason}")
        direct = await self.direct_links(folder, entries)
        note = "以下为直链/目录入口，不受新建分享的密码与次数限制；前端入口可能需要登录，直链可能过期。"
        reason = f"\n分享创建失败，已降级：{fallback_reason}" if fallback_reason else ""
        return f"{heading}{reason}\n\n{note}\n\n{direct}"


class BdpanOpenlistPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config
        self._settings: Settings | None = None
        self._baidu: BaiduClient | None = None
        self._openlist: OpenListClient | None = None
        self._job_lock = asyncio.Lock()
        self._active: set[asyncio.Task] = set()
        self._closing = False

    def services(self) -> tuple[Settings, BaiduClient, OpenListClient]:
        if self._closing:
            raise PluginError("插件正在卸载，请稍后再试。")
        if self._settings is None:
            self._settings = Settings.from_config(self.config or {})
            self._baidu = BaiduClient(self._settings)
            self._openlist = OpenListClient(self._settings)
        assert self._baidu is not None and self._openlist is not None
        return self._settings, self._baidu, self._openlist

    def enabled(self, event: AstrMessageEvent) -> bool:
        key = "enable_private" if event.is_private_chat() else "enable_group"
        return bool((self.config or {}).get(key, True))

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        if not (self.config or {}).get("enable_auto_detect", True) or not self.enabled(
            event
        ):
            return
        links = parse_share_links(event.message_str)
        if not links:
            return
        async for result in self.handle_links(event, links):
            yield result

    @filter.command("bdpan")
    async def bdpan(self, event: AstrMessageEvent):
        """/bdpan <百度分享链接> [四位提取码]；也可粘贴带提取码的整段文本。"""
        if not self.enabled(event):
            return
        links = parse_share_links(event.message_str)
        if not links:
            event.stop_event()
            yield event.plain_result(
                "用法：/bdpan https://pan.baidu.com/s/1xxxxxx 6666\n也支持 wap/init?surl=xxxxxx&pwd=6666。"
            )
            return
        async for result in self.handle_links(event, links):
            yield result

    async def handle_links(self, event: AstrMessageEvent, links: list[ShareLink]):
        # 命令与全消息监听可能同时命中同一 event，必须在首次 await 前认领。
        if event.get_extra("bdpan_openlist_handled", False):
            return
        event.set_extra("bdpan_openlist_handled", True)
        task = asyncio.current_task()
        if task is not None:
            self._active.add(task)
        try:
            try:
                settings, baidu, openlist = self.services()
            except PluginError as exc:
                logger.error(f"[bdpan_openlist] 配置错误：{exc}")
                yield event.plain_result(f"❌ {exc}")
                return
            await event.send(
                event.plain_result(
                    "📦 检测到百度网盘链接，正在为您转存并生成链接，请稍候……"
                )
            )
            if len(links) > settings.max_links:
                await event.send(
                    event.plain_result(
                        f"本消息包含 {len(links)} 个不同分享，本次仅处理前 {settings.max_links} 个。"
                    )
                )
            for index, link in enumerate(links[: settings.max_links], 1):
                secrets = settings.secrets() + [link.pwd]
                try:
                    async with self._job_lock:
                        if self._closing:
                            raise PluginError("插件正在卸载，本次任务已停止。")
                        # 先获取登录令牌；静态 Token 会在后续 API 调用时验证。
                        await openlist.login()
                        saved = await baidu.transfer(link)
                        try:
                            reply = await openlist.publish(saved)
                        except PluginError as exc:
                            raise PluginError(
                                f"百度已转存成功：{saved.path}\nOpenList 发布失败：{exc}\n无需重复转存，请检查 OpenList 挂载后手动分享此目录。"
                            ) from exc
                    yield event.plain_result(
                        f"[{index}/{min(len(links), settings.max_links)}] {reply}"
                    )
                except PluginError as exc:
                    error = redact(str(exc), secrets + [openlist._token])
                    logger.error(f"[bdpan_openlist] 任务失败：{error}")
                    yield event.plain_result(f"❌ 第 {index} 个分享处理失败：{error}")
                except Exception as exc:  # noqa: BLE001 -- 隔离插件边界的意外异常
                    # 不打印带请求 URL、Cookie 或响应正文的 traceback。
                    logger.error(
                        f"[bdpan_openlist] 未预期错误类型：{type(exc).__name__}"
                    )
                    yield event.plain_result(
                        f"❌ 第 {index} 个分享发生内部错误（{type(exc).__name__}），请检查日志。任务可能已写入，请先检查网盘。"
                    )
        finally:
            if task is not None:
                self._active.discard(task)
            event.stop_event()

    async def terminate(self):
        self._closing = True
        current = asyncio.current_task()
        pending = [
            task for task in self._active if task is not current and not task.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._baidu is not None:
            await self._baidu.close()
        if self._openlist is not None:
            await self._openlist.close()
