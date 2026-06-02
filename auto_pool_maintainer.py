#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import datetime as dt
import hashlib
import json
import logging
import os
import random
import re
import secrets
import string
import sys
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlencode, urlparse

from curl_cffi import requests

try:
    import aiohttp
except Exception:
    aiohttp = None


OPENAI_AUTH_BASE = "https://auth.openai.com"
PLATFORM_BASE = "https://platform.openai.com"
PLATFORM_OAUTH_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"
PLATFORM_OAUTH_REDIRECT_URI = f"{PLATFORM_BASE}/auth/callback"
PLATFORM_OAUTH_AUDIENCE = "https://api.openai.com/v1"
PLATFORM_AUTH0_CLIENT = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)
DEFAULT_MGMT_UA = "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal"

SEC_CH_UA = '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"'
SEC_CH_UA_FULL_VERSION_LIST = '"Chromium";v="145.0.0.0", "Not:A-Brand";v="99.0.0.0", "Google Chrome";v="145.0.0.0"'

COMMON_HEADERS = {
    "accept": "application/json",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json",
    "origin": OPENAI_AUTH_BASE,
    "priority": "u=1, i",
    "user-agent": USER_AGENT,
    "sec-ch-ua": SEC_CH_UA,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": SEC_CH_UA_FULL_VERSION_LIST,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}
NAVIGATE_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": USER_AGENT,
    "sec-ch-ua": SEC_CH_UA,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": SEC_CH_UA_FULL_VERSION_LIST,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
}


class AddPhoneRequired(RuntimeError):
    """Codex 登录触发手机绑定, 该账号跳过重试"""


class InvalidAuthStep(RuntimeError):
    """OpenAI auth state is invalid for this account attempt; skip without retry."""


def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError(f"配置文件格式错误，顶层必须是对象: {path}")
    return data


def setup_logger(log_dir: Path) -> tuple[logging.Logger, Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"pool_maintainer_{ts}.log"

    logger = logging.getLogger("pool_maintainer")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger, log_path


def ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def mgmt_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def get_item_type(item: Dict[str, Any]) -> str:
    return str(item.get("type") or item.get("typo") or "")


def extract_chatgpt_account_id(item: Dict[str, Any]) -> Optional[str]:
    for key in ("chatgpt_account_id", "chatgptAccountId", "account_id", "accountId"):
        val = item.get(key)
        if val:
            return str(val)
    return None


def safe_json_text(text: str) -> Dict[str, Any]:
    try:
        return json.loads(text)
    except Exception:
        return {}


def pick_conf(root: Dict[str, Any], section: str, key: str, *legacy_keys: str, default: Any = None) -> Any:
    sec = root.get(section)
    if not isinstance(sec, dict):
        sec = {}

    v = sec.get(key)
    if v is None:
        for lk in legacy_keys:
            v = sec.get(lk)
            if v is not None:
                break
    if v is not None:
        return v

    v = root.get(key)
    if v is None:
        for lk in legacy_keys:
            v = root.get(lk)
            if v is not None:
                break
    if v is not None:
        return v
    return default


def get_candidates_count(base_url: str, token: str, target_type: str, timeout: int) -> tuple[int, int]:
    url = f"{base_url.rstrip('/')}/v0/management/auth-files"
    resp = requests.get(url, headers=mgmt_headers(token), timeout=timeout)
    resp.raise_for_status()
    raw = resp.json()
    payload = raw if isinstance(raw, dict) else {}
    files = payload.get("files", []) if isinstance(payload, dict) else []
    candidates = []
    for f in files:
        if get_item_type(f).lower() != target_type.lower():
            continue
        candidates.append(f)
    return len(files), len(candidates)


def create_session(proxy: str = "") -> requests.Session:
    kwargs: Dict[str, Any] = {"impersonate": "chrome", "verify": False}
    if proxy:
        kwargs["proxy"] = proxy
    return requests.Session(**kwargs)


def _safe_json(resp) -> dict:
    try:
        if resp is None:
            return {}
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def request_with_local_retry(session, method: str, url: str, retry_attempts: int = 3, **kwargs):
    last_error = ""
    for _ in range(max(1, retry_attempts)):
        try:
            return session.request(method.upper(), url, timeout=30, **kwargs), ""
        except Exception as error:
            last_error = str(error)
            time.sleep(1)
    return None, last_error


def generate_pkce() -> tuple[str, str]:
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def generate_datadog_trace() -> Dict[str, str]:
    trace_id = str(random.getrandbits(64))
    parent_id = str(random.getrandbits(64))
    return {
        "traceparent": f"00-{uuid.uuid4().hex}-{format(int(parent_id), '016x')}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


def generate_random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    pwd = list(
        secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + secrets.choice(string.digits)
        + secrets.choice("!@#$%")
        + "".join(secrets.choice(chars) for _ in range(length - 4))
    )
    random.shuffle(pwd)
    return "".join(pwd)


def generate_random_name() -> tuple[str, str]:
    first = ["James", "Robert", "John", "Michael", "David", "Mary", "Jennifer", "Linda", "Emma", "Olivia"]
    last = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller"]
    return random.choice(first), random.choice(last)


def generate_random_birthday() -> str:
    year = random.randint(1996, 2006)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    return f"{year:04d}-{month:02d}-{day:02d}"


class SentinelTokenGenerator:
    MAX_ATTEMPTS = 500000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(self, device_id: str, ua: str):
        self.device_id = device_id
        self.user_agent = ua
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a_32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= h >> 16
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= h >> 16
        return format(h & 0xFFFFFFFF, "08x")

    def _get_config(self) -> list:
        perf_now = random.uniform(1000, 50000)
        return [
            "1920x1080",
            time.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime()),
            4294705152,
            random.random(),
            self.user_agent,
            "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js",
            None,
            None,
            "en-US",
            random.random(),
            random.choice(["vendorSub-undefined", "plugins-undefined", "mimeTypes-undefined", "hardwareConcurrency-undefined"]),
            random.choice(["location", "implementation", "URL", "documentURI", "compatMode"]),
            random.choice(["Object", "Function", "Array", "Number", "parseFloat", "undefined"]),
            perf_now,
            self.sid,
            "",
            random.choice([4, 8, 12, 16]),
            time.time() * 1000 - perf_now,
        ]

    @staticmethod
    def _b64(data) -> str:
        return base64.b64encode(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).decode("ascii")

    def generate_requirements_token(self) -> str:
        data = self._get_config()
        data[3] = 1
        data[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + self._b64(data)

    def generate_token(self, seed: str, difficulty: str) -> str:
        start = time.time()
        data = self._get_config()
        difficulty = str(difficulty or "0")
        for i in range(self.MAX_ATTEMPTS):
            data[3] = i
            data[9] = round((time.time() - start) * 1000)
            payload = self._b64(data)
            if self._fnv1a_32(seed + payload)[: len(difficulty)] <= difficulty:
                return "gAAAAAB" + payload + "~S"
        return "gAAAAAB" + self.ERROR_PREFIX + self._b64(str(None))


def build_sentinel_token(session: requests.Session, device_id: str, flow: str) -> Optional[str]:
    generator = SentinelTokenGenerator(device_id, USER_AGENT)
    try:
        resp = session.post(
            "https://sentinel.openai.com/backend-api/sentinel/req",
            data=json.dumps({"p": generator.generate_requirements_token(), "id": device_id, "flow": flow}),
            headers={
                "Content-Type": "text/plain;charset=UTF-8",
                "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html",
                "Origin": "https://sentinel.openai.com",
                "User-Agent": USER_AGENT,
                "sec-ch-ua": SEC_CH_UA,
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            },
            timeout=20,
            verify=False,
        )
        if resp.status_code != 200:
            return None
        data = resp.json() if callable(getattr(resp, "json", None)) else {}
        if not isinstance(data, dict):
            return None
        token = str(data.get("token") or "").strip()
        if not token:
            return None
        pow_data = data.get("proofofwork") or {}
        p_value = (
            generator.generate_token(str(pow_data.get("seed") or ""), str(pow_data.get("difficulty") or "0"))
            if pow_data.get("required") and pow_data.get("seed")
            else generator.generate_requirements_token()
        )
        return json.dumps({"p": p_value, "t": "", "c": token, "id": device_id, "flow": flow}, separators=(",", ":"))
    except Exception:
        return None


def create_temp_email(
    session: requests.Session,
    worker_domain: str,
    email_domains: List[str],
    admin_password: str,
    logger: logging.Logger,
) -> tuple[Optional[str], Optional[str]]:
    name_len = random.randint(10, 14)
    name_chars = list(random.choices(string.ascii_lowercase, k=name_len))
    for _ in range(random.choice([1, 2])):
        pos = random.randint(2, len(name_chars) - 1)
        name_chars.insert(pos, random.choice(string.digits))
    name = "".join(name_chars)

    chosen_domain = random.choice(email_domains) if email_domains else "tuxixilax.cfd"

    try:
        res = session.post(
            f"https://{worker_domain}/admin/new_address",
            json={"enablePrefix": True, "name": name, "domain": chosen_domain},
            headers={"x-admin-auth": admin_password, "Content-Type": "application/json"},
            timeout=10,
            verify=False,
        )
        if res.status_code == 200:
            data = res.json()
            email = data.get("address")
            token = data.get("jwt")
            if email:
                logger.info("创建临时邮箱成功: %s (domain=%s)", email, chosen_domain)
                return str(email), str(token or "")
        logger.warning("创建临时邮箱失败: HTTP %s", res.status_code)
    except Exception as e:
        logger.warning("创建临时邮箱异常: %s", e)
    return None, None


def fetch_emails(session: requests.Session, worker_domain: str, cf_token: str) -> List[Dict[str, Any]]:
    try:
        res = session.get(
            f"https://{worker_domain}/api/mails",
            params={"limit": 10, "offset": 0},
            headers={"Authorization": f"Bearer {cf_token}"},
            verify=False,
            timeout=30,
        )
        if res.status_code == 200:
            rows = res.json().get("results", [])
            return rows if isinstance(rows, list) else []
    except Exception:
        pass
    return []


def extract_verification_code(content: str) -> Optional[str]:
    if not content:
        return None
    m = re.search(r"background-color:\s*#F3F3F3[^>]*>[\s\S]*?(\d{6})[\s\S]*?</p>", content, re.I)
    if m:
        return m.group(1)
    m = re.search(r"(?:Verification code|code is|代码为|验证码|Subject:)[:\s]*(\d{6})", content, re.I)
    if m and m.group(1) != "177010":
        return m.group(1)
    for code in re.findall(r">\s*(\d{6})\s*<|(?<![#&])\b(\d{6})\b", content):
        if isinstance(code, tuple):
            code = code[0] or code[1]
        if code and code != "177010":
            return code
    return None


def wait_for_verification_code(
    session: requests.Session,
    worker_domain: str,
    cf_token: str,
    timeout: int = 120,
    logger: Optional[logging.Logger] = None,
    email: str = "",
    provider_name: str = "cf",
) -> Optional[str]:
    old_ids = set()
    old = fetch_emails(session, worker_domain, cf_token)
    if old:
        old_ids = {e.get("id") for e in old if isinstance(e, dict) and "id" in e}
        for item in old:
            if not isinstance(item, dict):
                continue
            raw = str(item.get("raw") or "")
            code = extract_verification_code(raw)
            if code:
                return code

    start = time.time()
    next_log_at = start
    while time.time() - start < timeout:
        emails = fetch_emails(session, worker_domain, cf_token)
        now = time.time()
        if logger and now >= next_log_at:
            logger.info(
                "等待验证码中: provider=%s, email=%s, elapsed=%.0fs, mails=%s",
                provider_name,
                email,
                now - start,
                len(emails) if isinstance(emails, list) else 0,
            )
            next_log_at = now + 15
        if emails:
            for item in emails:
                if not isinstance(item, dict):
                    continue
                if item.get("id") in old_ids:
                    continue
                raw = str(item.get("raw") or "")
                code = extract_verification_code(raw)
                if code:
                    return code
        time.sleep(3)
    return None


class EmailProviderError(RuntimeError):
    pass


class EmailMailboxProvider:
    def __init__(self, session: requests.Session, logger: logging.Logger):
        self.session = session
        self.logger = logger

    def create_mailbox(self) -> Optional[str]:
        raise NotImplementedError

    def wait_for_verification_code(self, timeout: int = 120) -> Optional[str]:
        raise NotImplementedError


class CloudflareEmailProvider(EmailMailboxProvider):
    def __init__(self, session: requests.Session, worker_domain: str, email_domains: List[str], admin_password: str, logger: logging.Logger):
        super().__init__(session=session, logger=logger)
        self.worker_domain = worker_domain
        self.email_domains = email_domains
        self.admin_password = admin_password
        self.email: Optional[str] = None
        self.cf_token: Optional[str] = None

    def create_mailbox(self) -> Optional[str]:
        email, token = create_temp_email(
            self.session,
            worker_domain=self.worker_domain,
            email_domains=self.email_domains,
            admin_password=self.admin_password,
            logger=self.logger,
        )
        if not email or not token:
            return None
        self.email = email
        self.cf_token = token
        return email

    def wait_for_verification_code(self, timeout: int = 120) -> Optional[str]:
        if not self.cf_token:
            return None
        return wait_for_verification_code(
            self.session,
            self.worker_domain,
            self.cf_token,
            timeout=timeout,
            logger=self.logger,
            email=self.email or "",
            provider_name="cf",
        )


def _response_data(resp) -> Dict[str, Any]:
    payload = _safe_json(resp)
    data = payload.get("data") if isinstance(payload, dict) else {}
    return data if isinstance(data, dict) else payload


class GptMailProvider(EmailMailboxProvider):
    def __init__(self, session: requests.Session, base_url: str, api_key: str, logger: logging.Logger):
        super().__init__(session=session, logger=logger)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.email: Optional[str] = None

    def _headers(self) -> Dict[str, str]:
        return {"X-API-Key": self.api_key}

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def create_mailbox(self) -> Optional[str]:
        try:
            res = self.session.get(self._url("/api/generate-email"), headers=self._headers(), timeout=30, verify=False)
            if res.status_code != 200:
                self.logger.warning("gptmail create mailbox failed: HTTP %s", res.status_code)
                return None
            data = _response_data(res)
            email = str(data.get("email") or "").strip()
            if not email:
                self.logger.warning("gptmail create mailbox failed: missing email in response")
                return None
            self.email = email
            self.logger.info("gptmail create mailbox success: %s", email)
            return email
        except Exception as e:
            self.logger.warning("gptmail create mailbox error: %s", e)
            return None

    def _list_emails(self) -> List[Dict[str, Any]]:
        if not self.email:
            return []
        try:
            res = self.session.get(
                self._url("/api/emails"),
                params={"email": self.email},
                headers=self._headers(),
                timeout=30,
                verify=False,
            )
            if res.status_code != 200:
                self.logger.warning("gptmail list emails failed: HTTP %s | email=%s", res.status_code, self.email)
                return []
            data = _response_data(res)
            rows = data.get("emails", [])
            return rows if isinstance(rows, list) else []
        except Exception as e:
            self.logger.warning("gptmail list emails error: %s | email=%s", e, self.email)
            return []

    def _get_email_detail(self, email_id: str) -> Dict[str, Any]:
        if not email_id:
            return {}
        try:
            res = self.session.get(
                self._url(f"/api/email/{quote(email_id)}"),
                headers=self._headers(),
                timeout=30,
                verify=False,
            )
            if res.status_code != 200:
                self.logger.warning("gptmail email detail failed: HTTP %s | id=%s", res.status_code, email_id)
                return {}
            return _response_data(res)
        except Exception as e:
            self.logger.warning("gptmail email detail error: %s | id=%s", e, email_id)
            return {}

    @staticmethod
    def _message_text(message: Dict[str, Any]) -> str:
        parts = [
            str(message.get("content") or ""),
            str(message.get("html_content") or ""),
            str(message.get("subject") or ""),
            str(message.get("from_address") or ""),
            str(message.get("email_address") or ""),
            str(message.get("raw") or ""),
        ]
        return "\n".join(part for part in parts if part)

    def wait_for_verification_code(self, timeout: int = 120) -> Optional[str]:
        if not self.email:
            return None

        old_ids: set[str] = set()
        old = self._list_emails()
        if old:
            old_ids = {str(item.get("id") or "") for item in old if isinstance(item, dict) and item.get("id")}
            for item in old:
                if not isinstance(item, dict):
                    continue
                email_id = str(item.get("id") or "").strip()
                if not email_id:
                    continue
                detail = self._get_email_detail(email_id)
                code = extract_verification_code(self._message_text(detail or item))
                if code:
                    return code

        start = time.time()
        next_log_at = start
        while time.time() - start < timeout:
            emails = self._list_emails()
            now = time.time()
            if now >= next_log_at:
                self.logger.info(
                    "等待验证码中: provider=gptmail, email=%s, elapsed=%.0fs, mails=%s",
                    self.email,
                    now - start,
                    len(emails) if isinstance(emails, list) else 0,
                )
                next_log_at = now + 15
            if emails:
                for item in emails:
                    if not isinstance(item, dict):
                        continue
                    email_id = str(item.get("id") or "").strip()
                    if not email_id or email_id in old_ids:
                        continue
                    detail = self._get_email_detail(email_id)
                    code = extract_verification_code(self._message_text(detail or item))
                    if code:
                        return code
            time.sleep(3)
        return None


def create_email_provider(runtime: "RegisterRuntime", session: requests.Session) -> EmailMailboxProvider:
    if runtime.email_type == "cf":
        return CloudflareEmailProvider(
            session=session,
            worker_domain=runtime.worker_domain,
            email_domains=runtime.email_domains,
            admin_password=runtime.admin_password,
            logger=runtime.logger,
        )
    if runtime.email_type == "gptmail":
        return GptMailProvider(
            session=session,
            base_url=runtime.gptmail_base_url,
            api_key=runtime.gptmail_api_key,
            logger=runtime.logger,
        )
    raise EmailProviderError(f"unsupported email.type: {runtime.email_type}")


class ProtocolRegistrar:
    def __init__(self, proxy: str, logger: logging.Logger):
        self.session = create_session(proxy=proxy)
        self.device_id = str(uuid.uuid4())
        self.logger = logger
        self.code_verifier: str = ""
        self.platform_auth_code: str = ""

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    def _navigate_headers(self, referer: str = "") -> Dict[str, str]:
        headers = dict(NAVIGATE_HEADERS)
        if referer:
            headers["referer"] = referer
        return headers

    def _json_headers(self, referer: str) -> Dict[str, str]:
        headers = dict(COMMON_HEADERS)
        headers["referer"] = referer
        headers["oai-device-id"] = self.device_id
        headers.update(generate_datadog_trace())
        return headers

    def _platform_authorize(self, email: str) -> None:
        self.logger.info("开始 platform authorize ...")
        self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
        self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com")
        self.code_verifier, code_challenge = generate_pkce()
        params = {
            "issuer": OPENAI_AUTH_BASE,
            "client_id": PLATFORM_OAUTH_CLIENT_ID,
            "audience": PLATFORM_OAUTH_AUDIENCE,
            "redirect_uri": PLATFORM_OAUTH_REDIRECT_URI,
            "device_id": self.device_id,
            "screen_hint": "login_or_signup",
            "max_age": "0",
            "login_hint": email,
            "scope": "openid profile email offline_access",
            "response_type": "code",
            "response_mode": "query",
            "state": secrets.token_urlsafe(32),
            "nonce": secrets.token_urlsafe(32),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "auth0Client": PLATFORM_AUTH0_CLIENT,
        }
        url = f"{OPENAI_AUTH_BASE}/api/accounts/authorize?{urlencode(params)}"
        resp, error = request_with_local_retry(
            self.session, "get", url,
            headers=self._navigate_headers(f"{PLATFORM_BASE}/"),
            allow_redirects=True, verify=False,
        )
        if resp is None or resp.status_code != 200:
            body = (getattr(resp, "text", "") or "")[:300] if resp is not None else ""
            if resp is not None and ("cloudflare" in (str(resp.headers.get("server") or "")).lower() or "challenges.cloudflare.com" in body.lower()):
                raise RuntimeError("被 Cloudflare 拦截，请更换 IP 重试")
            raise RuntimeError(error or f"platform_authorize HTTP {getattr(resp, 'status_code', 'unknown')}: {body}")
        self.logger.info("platform authorize 完成")

    def _register_user(self, email: str, password: str) -> None:
        self.logger.info("开始提交注册密码 ...")
        headers = self._json_headers(f"{OPENAI_AUTH_BASE}/create-account/password")
        sentinel = build_sentinel_token(self.session, self.device_id, "username_password_create")
        if not sentinel:
            raise RuntimeError("获取 Sentinel token 失败 (register)")
        headers["openai-sentinel-token"] = sentinel
        resp, error = request_with_local_retry(
            self.session, "post", f"{OPENAI_AUTH_BASE}/api/accounts/user/register",
            json={"username": email, "password": password},
            headers=headers, verify=False,
        )
        if resp is None or resp.status_code != 200:
            try:
                data = resp.json() if resp is not None else {}
            except Exception:
                data = {}
            if isinstance(data, dict) and data.get("message") == "Failed to create account. Please try again.":
                self.logger.warning("注册失败提示: 邮箱域名很可能因滥用被封禁，请更换邮箱域名")
            error_data = data.get("error") if isinstance(data, dict) else {}
            if isinstance(error_data, dict):
                error_code = str(error_data.get("code") or "").strip()
                error_message = str(error_data.get("message") or "").strip()
                if (
                    error_code in {"invalid_auth_step", "account_creation_failed"}
                    or error_message in {
                        "Invalid authorization step.",
                        "Failed to create account. Please try again.",
                    }
                ):
                    detail = f", detail={json.dumps(data, ensure_ascii=False)[:200]}" if data else ""
                    raise InvalidAuthStep(error or f"user_register HTTP {getattr(resp, 'status_code', 'unknown')}{detail}")
            detail = ""
            if data:
                detail = f", detail={json.dumps(data, ensure_ascii=False)[:200]}"
            raise RuntimeError(error or f"user_register HTTP {getattr(resp, 'status_code', 'unknown')}{detail}")
        self.logger.info("提交注册密码完成")

    def _send_otp(self) -> None:
        self.logger.info("开始发送验证码 ...")
        resp, error = request_with_local_retry(
            self.session, "get", f"{OPENAI_AUTH_BASE}/api/accounts/email-otp/send",
            headers=self._navigate_headers(f"{OPENAI_AUTH_BASE}/create-account/password"),
            allow_redirects=True, verify=False,
        )
        if resp is None or resp.status_code not in (200, 302):
            raise RuntimeError(error or f"send_otp HTTP {getattr(resp, 'status_code', 'unknown')}")
        self.logger.info("发送验证码完成")

    def _validate_otp(self, code: str) -> None:
        self.logger.info("开始校验验证码 %s ...", code)
        headers = self._json_headers(f"{OPENAI_AUTH_BASE}/email-verification")
        resp, error = request_with_local_retry(
            self.session, "post", f"{OPENAI_AUTH_BASE}/api/accounts/email-otp/validate",
            json={"code": code}, headers=headers, verify=False,
        )
        if resp is not None and resp.status_code == 200:
            self.logger.info("验证码校验完成")
            return

        sentinel = build_sentinel_token(self.session, self.device_id, "authorize_continue")
        if sentinel:
            headers["openai-sentinel-token"] = sentinel
        resp, error = request_with_local_retry(
            self.session, "post", f"{OPENAI_AUTH_BASE}/api/accounts/email-otp/validate",
            json={"code": code}, headers=headers, verify=False,
        )
        if resp is None or resp.status_code != 200:
            body = ""
            try:
                body = (resp.text or "")[:300] if resp is not None else ""
            except Exception:
                pass
            raise RuntimeError(error or f"validate_otp HTTP {getattr(resp, 'status_code', 'unknown')}, body={body}")

    @staticmethod
    def _extract_oauth_callback_params_from_url(url: str) -> Optional[Dict[str, str]]:
        if not url:
            return None
        try:
            params = parse_qs(urlparse(url).query)
        except Exception:
            return None
        code = str((params.get("code") or [""])[0]).strip()
        if not code:
            return None
        return {
            "code": code,
            "state": str((params.get("state") or [""])[0]).strip(),
            "scope": str((params.get("scope") or [""])[0]).strip(),
        }

    def _create_account(self, name: str, birthdate: str) -> None:
        self.logger.info("开始创建账号资料 ...")
        headers = self._json_headers(f"{OPENAI_AUTH_BASE}/about-you")
        sentinel = build_sentinel_token(self.session, self.device_id, "oauth_create_account")
        if not sentinel:
            raise RuntimeError("获取 Sentinel token 失败 (create_account)")
        headers["openai-sentinel-token"] = sentinel
        resp, error = request_with_local_retry(
            self.session, "post", f"{OPENAI_AUTH_BASE}/api/accounts/create_account",
            json={"name": name, "birthdate": birthdate},
            headers=headers, verify=False,
        )
        if resp is None or resp.status_code not in (200, 302):
            try:
                data = resp.json() if resp is not None else {}
            except Exception:
                data = {}
            if isinstance(data, dict) and data.get("message") == "Failed to create account. Please try again.":
                self.logger.warning("创建账号失败提示: 邮箱域名很可能因滥用被封禁，请更换邮箱域名")
            detail = ""
            if data:
                detail = f", detail={json.dumps(data, ensure_ascii=False)[:200]}"
            raise RuntimeError(error or f"create_account HTTP {getattr(resp, 'status_code', 'unknown')}{detail}")

        try:
            data = resp.json()
        except Exception:
            data = {}
        callback_params = self._extract_oauth_callback_params_from_url(str(data.get("continue_url") or "").strip())
        if not callback_params or not callback_params.get("code"):
            raise RuntimeError("create_account 未返回有效的 OAuth code")
        self.platform_auth_code = callback_params["code"]
        self.logger.info("创建账号资料完成, 获取到 OAuth code")

    def _exchange_registered_tokens(self) -> Dict[str, Any]:
        self.logger.info("开始换取 token ...")
        headers = {
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9",
            "auth0-client": PLATFORM_AUTH0_CLIENT,
            "cache-control": "no-cache",
            "content-type": "application/json",
            "origin": PLATFORM_BASE,
            "pragma": "no-cache",
            "priority": "u=1, i",
            "referer": f"{PLATFORM_BASE}/",
            "sec-ch-ua": SEC_CH_UA,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": USER_AGENT,
        }
        resp, error = request_with_local_retry(
            self.session, "post", f"{OPENAI_AUTH_BASE}/api/accounts/oauth/token",
            headers=headers,
            json={
                "client_id": PLATFORM_OAUTH_CLIENT_ID,
                "code_verifier": self.code_verifier,
                "grant_type": "authorization_code",
                "code": self.platform_auth_code,
                "redirect_uri": PLATFORM_OAUTH_REDIRECT_URI,
            },
            verify=False,
        )
        if resp is None or resp.status_code != 200:
            body = ""
            try:
                body = (resp.text or "")[:300] if resp is not None else ""
            except Exception:
                pass
            raise RuntimeError(error or f"exchange_token HTTP {getattr(resp, 'status_code', 'unknown')}: {body}")

        tokens = _safe_json(resp)
        if not tokens.get("access_token"):
            raise RuntimeError("exchange_token 返回缺少 access_token")
        self.logger.info("token 换取完成")
        return tokens

    def register(self, email: str, password: str) -> bool:
        """步骤 0-3: OAuth 授权 → 提交注册 → 发送验证码"""
        self._platform_authorize(email)
        self._register_user(email, password)
        self._send_otp()
        return True

    def complete(self, code: str, first_name: str, last_name: str, birthdate: str) -> Dict[str, Any]:
        """步骤 4-6: 校验验证码 → 创建账号资料 → 换取 Token"""
        self._validate_otp(code)
        self._create_account(f"{first_name} {last_name}", birthdate)
        tokens = self._exchange_registered_tokens()
        return {
            "access_token": str(tokens.get("access_token") or "").strip(),
            "refresh_token": str(tokens.get("refresh_token") or "").strip(),
            "id_token": str(tokens.get("id_token") or "").strip(),
        }

    def codex_exchange_tokens(self, email: str, password: str) -> Dict[str, Any]:
        """注册完成后, 通过 Codex OAuth 二次登录获取完整的 codex token JSON"""
        CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
        CODEX_REDIRECT_URI = "http://localhost:1455/auth/callback"
        CODEX_SCOPE = "openid profile email offline_access api.connectors.read api.connectors.invoke"

        self.logger.info("开始 Codex OAuth 登录换取 token ...")

        code_verifier, code_challenge = generate_pkce()
        state = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")

        params = {
            "client_id": CODEX_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": CODEX_REDIRECT_URI,
            "scope": CODEX_SCOPE,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "codex_cli_simplified_flow": "true",
            "id_token_add_organizations": "true",
            "originator": "codex_cli_rs",
        }
        url = f"{OPENAI_AUTH_BASE}/oauth/authorize?{urlencode(params)}"
        navigate_headers = self._navigate_headers(f"{PLATFORM_BASE}/")
        resp, error = request_with_local_retry(
            self.session, "get", url,
            headers=navigate_headers,
            allow_redirects=False, verify=False,
        )
        if resp is None or resp.status_code not in (200, 302):
            raise RuntimeError(error or f"codex_authorize HTTP {getattr(resp, 'status_code', 'unknown')}")

        for _ in range(5):
            location = str(resp.headers.get("Location") or "")
            if not location:
                break
            if resp.url and "choose-an-account" in resp.url:
                break
            if "choose-an-account" in location:
                break
            if resp.url and "log-in" in resp.url:
                raise RuntimeError("codex authorize 意外跳转到登录页")
            if "log-in" in location:
                raise RuntimeError("codex authorize 意外跳转到登录页")
            resp, error = request_with_local_retry(
                self.session, "get", location,
                headers=navigate_headers,
                allow_redirects=False, verify=False,
            )
            if resp is None:
                raise RuntimeError(error or "codex authorize 重定向链中断")

        raw = self.session.cookies.get("oai-client-auth-session", domain=".auth.openai.com") or self.session.cookies.get("oai-client-auth-session")
        if not raw:
            raise RuntimeError("codex 选择账号页面未获取到 oai-client-auth-session cookie")
        try:
            first_part = raw.split(".")[0]
            padding = 4 - len(first_part) % 4
            if padding != 4:
                first_part += "=" * padding
            payload = json.loads(base64.urlsafe_b64decode(first_part))
            session_id = (payload.get("unified_sessions") or [{}])[0].get("id") or payload.get("session_id")
            if not session_id:
                raise RuntimeError("cookie 中无 session_id")
        except Exception as e:
            raise RuntimeError(f"解析 cookie session_id 失败: {e}")

        self.logger.info("取得 codex session_id, 选择 session ...")
        headers = self._json_headers(f"{OPENAI_AUTH_BASE}/choose-an-account")
        resp, error = request_with_local_retry(
            self.session, "post", f"{OPENAI_AUTH_BASE}/api/accounts/session/select",
            json={"session_id": session_id},
            headers=headers, allow_redirects=False, verify=False,
        )
        if resp is None or resp.status_code != 200:
            raise RuntimeError(error or f"session_select HTTP {getattr(resp, 'status_code', 'unknown')}")

        data = _safe_json(resp)
        continue_url = str(data.get("continue_url") or "").strip()
        page_type = str(((data.get("page") or {}).get("type")) or "")

        if page_type == "choose_an_account":
            auth_session = (data.get("oai-client-auth-session") or {})
            session_id = str((((auth_session.get("unified_sessions") or {})).get("id") or ""))
            if session_id:
                resp, error = request_with_local_retry(
                    self.session, "post", f"{OPENAI_AUTH_BASE}/api/accounts/session/select",
                    json={"session_id": session_id},
                    headers=headers, allow_redirects=False, verify=False,
                )
                if resp is not None and resp.status_code == 200:
                    data = _safe_json(resp)
                    continue_url = str(data.get("continue_url") or "").strip()
                    page_type = str(((data.get("page") or {}).get("type")) or "")

        if page_type == "email_otp_verification" or "email-verification" in continue_url or "email-otp" in continue_url:
            raise RuntimeError("codex 登录触发了邮箱验证码，不应该发生")

        if page_type == "add_phone":
            raise AddPhoneRequired("codex 登录触发了手机验证, 跳过该账号")

        if not continue_url:
            continue_url = f"{OPENAI_AUTH_BASE}/sign-in-with-chatgpt/codex/consent"

        self.logger.info("codex session 选择完成, 提取 workspace/org ...")
        raw = self.session.cookies.get("oai-client-auth-session", domain=".auth.openai.com") or self.session.cookies.get("oai-client-auth-session")
        if not raw:
            raise RuntimeError("codex consent 阶段未获取到 oai-client-auth-session cookie")
        try:
            first_part = raw.split(".")[0]
            padding = 4 - len(first_part) % 4
            if padding != 4:
                first_part += "=" * padding
            session_data = json.loads(base64.urlsafe_b64decode(first_part))
        except Exception:
            raise RuntimeError("解析 codex consent cookie 失败")

        workspace_id = None
        workspaces = session_data.get("workspaces") or []
        if isinstance(workspaces, list) and workspaces:
            workspace_id = (workspaces[0] or {}).get("id")

        if not workspace_id:
            raise RuntimeError("codex consent cookie 中无 workspace_id")

        headers = dict(COMMON_HEADERS)
        headers["referer"] = "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"
        headers["oai-device-id"] = self.device_id
        headers.update(generate_datadog_trace())

        resp, error = request_with_local_retry(
            self.session, "post", f"{OPENAI_AUTH_BASE}/api/accounts/workspace/select",
            json={"workspace_id": workspace_id},
            headers=headers, verify=False, allow_redirects=False,
        )
        if resp is None:
            raise RuntimeError(error or "workspace_select 网络错误")
        ws_data = _safe_json(resp)

        callback_params = self._extract_oauth_callback_params_from_url(
            str(resp.headers.get("Location") or ws_data.get("continue_url") or "").strip()
        )

        if not callback_params:
            orgs = (ws_data.get("data") or {}).get("orgs") or [] if isinstance(ws_data, dict) else []
            if orgs:
                org_id = str((orgs[0] or {}).get("id") or "").strip()
                projects = (orgs[0] or {}).get("projects") or []
                project_id = str((projects[0] or {}).get("id") or "").strip() if projects else ""
                if org_id:
                    org_headers = dict(COMMON_HEADERS)
                    org_headers["referer"] = str(ws_data.get("continue_url") or continue_url)
                    org_headers["oai-device-id"] = self.device_id
                    org_headers.update(generate_datadog_trace())
                    body: Dict[str, str] = {"org_id": org_id}
                    if project_id:
                        body["project_id"] = project_id
                    org_resp, org_error = request_with_local_retry(
                        self.session, "post", f"{OPENAI_AUTH_BASE}/api/accounts/organization/select",
                        json=body,
                        headers=org_headers, verify=False, allow_redirects=False,
                    )
                    callback_params = self._extract_oauth_callback_params_from_url(
                        str(org_resp.headers.get("Location") or "").strip() if org_resp is not None else ""
                    )

        if not callback_params or not callback_params.get("code"):
            raise RuntimeError("codex consent 阶段未提取到 OAuth code")

        code = callback_params["code"]
        self.logger.info("codex 获取到 OAuth code, 开始换取 token ...")

        token_session = requests.Session()
        token_session.verify = False
        if self.session.proxies:
            token_session.proxies.update(self.session.proxies)
        try:
            resp = token_session.post(
                f"{OPENAI_AUTH_BASE}/oauth/token",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "originator": "codex_cli_rs",
                    "User-Agent": "codex_cli_rs/0.130.0 (Windows 10.0; x86_64)",
                },
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": CODEX_REDIRECT_URI,
                    "client_id": CODEX_CLIENT_ID,
                    "code_verifier": code_verifier,
                },
                verify=False,
                timeout=60,
            )
        except Exception as e:
            raise RuntimeError(f"codex token exchange 网络错误: {e}")
        finally:
            token_session.close()

        if resp.status_code != 200:
            body = ""
            try:
                body = (resp.text or "")[:300]
            except Exception:
                pass
            raise RuntimeError(f"codex token exchange HTTP {resp.status_code}: {body}")

        data = _safe_json(resp)
        if not data.get("access_token"):
            raise RuntimeError("codex token exchange 返回缺少 access_token")

        jwt_payload = decode_jwt_payload(str(data.get("id_token") or data.get("access_token") or ""))
        auth_claims = jwt_payload.get("https://api.openai.com/auth") or {}
        account_id = str(auth_claims.get("chatgpt_account_id") or "").strip()
        user_id = str(auth_claims.get("user_id") or "").strip()
        plan_type = str(auth_claims.get("chatgpt_plan_type") or "").strip()
        expires_in = int(data.get("expires_in") or 0)
        now = int(time.time())

        self.logger.info("codex token 换取完成")
        return {
            "email": email,
            "password": password,
            "access_token": str(data.get("access_token") or "").strip(),
            "refresh_token": str(data.get("refresh_token") or "").strip(),
            "id_token": str(data.get("id_token") or "").strip(),
            "account_id": account_id,
            "user_id": user_id,
            "plan_type": plan_type,
            "expires_in": expires_in,
            "expired": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + max(expires_in, 0))),
            "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "token_type": str(data.get("token_type") or "Bearer"),
            "type": "codex",
            "register_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        }


def decode_jwt_payload(token: str) -> Dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        decoded = base64.urlsafe_b64decode(payload)
        data = json.loads(decoded)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


class RegisterRuntime:
    def __init__(self, conf: Dict[str, Any], target_tokens: int, logger: logging.Logger):
        self.conf = conf
        self.target_tokens = target_tokens
        self.logger = logger

        self.file_lock = threading.Lock()
        self.counter_lock = threading.Lock()
        self.token_success_count = 0
        self.phone_verification_count = 0
        self.stop_event = threading.Event()

        run_workers = int(pick_conf(conf, "run", "workers", default=1) or 1)
        self.concurrent_workers = max(1, run_workers)
        self.proxy = str(pick_conf(conf, "run", "proxy", default="") or "")

        self.email_type = str(pick_conf(conf, "email", "type", default="cf") or "cf").strip().lower() or "cf"
        self.worker_domain = str(pick_conf(conf, "email", "worker_domain", default="email.tuxixilax.cfd") or "")
        old_domain = str(pick_conf(conf, "email", "email_domain", default="tuxixilax.cfd") or "tuxixilax.cfd")
        domains = pick_conf(conf, "email", "email_domains", default=None)
        parsed_domains: List[str] = []
        if isinstance(domains, list):
            parsed_domains = [str(x).strip() for x in domains if str(x).strip()]
        if not parsed_domains:
            parsed_domains = [old_domain]
        self.email_domains = parsed_domains
        self.admin_password = str(pick_conf(conf, "email", "admin_password", default="") or "")
        self.gptmail_base_url = str(
            pick_conf(conf, "email", "gptmail_base_url", "base_url", default="https://mail.chatgpt.org.uk")
            or "https://mail.chatgpt.org.uk"
        ).strip() or "https://mail.chatgpt.org.uk"
        self.gptmail_api_key = str(pick_conf(conf, "email", "gptmail_apikey", "apikey", default="") or "").strip()

        self.oauth_issuer = str(pick_conf(conf, "oauth", "issuer", default="https://auth.openai.com") or "https://auth.openai.com")
        self.oauth_client_id = str(
            pick_conf(conf, "oauth", "client_id", default=PLATFORM_OAUTH_CLIENT_ID) or PLATFORM_OAUTH_CLIENT_ID
        )
        self.oauth_redirect_uri = str(
            pick_conf(conf, "oauth", "redirect_uri", default=PLATFORM_OAUTH_REDIRECT_URI)
            or PLATFORM_OAUTH_REDIRECT_URI
        )
        self.oauth_retry_attempts = int(pick_conf(conf, "oauth", "retry_attempts", default=3) or 3)
        self.oauth_retry_backoff_base = float(pick_conf(conf, "oauth", "retry_backoff_base", default=2.0) or 2.0)
        self.oauth_retry_backoff_max = float(pick_conf(conf, "oauth", "retry_backoff_max", default=15.0) or 15.0)

        upload_base = str(pick_conf(conf, "upload", "cli_proxy_api_base", "base_url", default="") or "").strip()
        if not upload_base:
            upload_base = str(pick_conf(conf, "clean", "base_url", default="") or "").strip()
        self.cli_proxy_api_base = upload_base.rstrip("/")

        upload_token = str(pick_conf(conf, "upload", "token", "cpa_password", default="") or "").strip()
        if not upload_token:
            upload_token = str(pick_conf(conf, "clean", "token", "cpa_password", default="") or "").strip()
        self.upload_api_token = upload_token

        self.upload_url = f"{self.cli_proxy_api_base}/v0/management/auth-files" if self.cli_proxy_api_base else ""

        output_cfg = conf.get("output")
        if not isinstance(output_cfg, dict):
            output_cfg = {}

        save_local_raw = output_cfg.get("save_local", True)
        if isinstance(save_local_raw, bool):
            self.save_local = save_local_raw
        else:
            self.save_local = str(save_local_raw).strip().lower() in ("1", "true", "yes", "on")

        self.run_dir = os.getcwd()
        if self.save_local:
            self.fixed_out_dir = os.path.join(self.run_dir, "output_fixed")
            self.tokens_parent_dir = os.path.join(self.run_dir, "output_tokens")
            os.makedirs(self.fixed_out_dir, exist_ok=True)
            os.makedirs(self.tokens_parent_dir, exist_ok=True)
            self.tokens_out_dir = self._ensure_unique_dir(self.tokens_parent_dir, f"{target_tokens}个账号")

            self.accounts_file = self._resolve_output_path(str(output_cfg.get("accounts_file", "accounts.txt")))
            self.csv_file = self._resolve_output_path(str(output_cfg.get("csv_file", "registered_accounts.csv")))
            self.ak_file = self._resolve_output_path(str(output_cfg.get("ak_file", "ak.txt")))
            self.rk_file = self._resolve_output_path(str(output_cfg.get("rk_file", "rk.txt")))
        else:
            self.fixed_out_dir = ""
            self.tokens_parent_dir = ""
            self.tokens_out_dir = ""
            self.accounts_file = ""
            self.csv_file = ""
            self.ak_file = ""
            self.rk_file = ""

    def _resolve_output_path(self, value: str) -> str:
        if os.path.isabs(value):
            return value
        return os.path.join(self.fixed_out_dir, value)

    def _ensure_unique_dir(self, parent_dir: str, base_name: str) -> str:
        os.makedirs(parent_dir, exist_ok=True)

        candidates = [os.path.join(parent_dir, base_name)] + [
            os.path.join(parent_dir, f"{base_name}-{idx}") for idx in range(1, 1000000)
        ]
        for candidate in candidates:
            try:
                os.makedirs(candidate)
                return candidate
            except FileExistsError:
                continue
        raise RuntimeError(f"无法创建唯一目录: {parent_dir}/{base_name}")

    def get_token_success_count(self) -> int:
        with self.counter_lock:
            return self.token_success_count

    def increment_phone_verification_count(self) -> int:
        with self.counter_lock:
            self.phone_verification_count += 1
            return self.phone_verification_count

    def get_phone_verification_count(self) -> int:
        with self.counter_lock:
            return self.phone_verification_count

    def claim_token_slot(self) -> tuple[bool, int]:
        with self.counter_lock:
            if self.token_success_count >= self.target_tokens:
                return False, self.token_success_count
            self.token_success_count += 1
            if self.token_success_count >= self.target_tokens:
                self.stop_event.set()
            return True, self.token_success_count

    def release_token_slot(self) -> None:
        with self.counter_lock:
            if self.token_success_count > 0:
                self.token_success_count -= 1
            if self.token_success_count < self.target_tokens:
                self.stop_event.clear()

    def save_token_json(self, token: Dict[str, Any]) -> bool:
        email = str(token.get("email") or "")
        if not email:
            return False
        try:
            token_data = {
                "type": str(token.get("type") or "codex"),
                "email": email,
                "expired": str(token.get("expired") or ""),
                "id_token": str(token.get("id_token") or ""),
                "account_id": str(token.get("account_id") or ""),
                "user_id": str(token.get("user_id") or ""),
                "plan_type": str(token.get("plan_type") or ""),
                "access_token": str(token.get("access_token") or "").strip(),
                "last_refresh": str(token.get("last_refresh") or ""),
                "refresh_token": str(token.get("refresh_token") or "").strip(),
                "token_type": str(token.get("token_type") or "Bearer"),
                "expires_in": int(token.get("expires_in") or 0),
                "register_time": str(token.get("register_time") or ""),
                "password": str(token.get("password") or ""),
            }

            if self.save_local:
                filename = os.path.join(self.tokens_out_dir, f"{email}.json")
                ensure_parent_dir(filename)
                with open(filename, "w", encoding="utf-8") as f:
                    json.dump(token_data, f, ensure_ascii=False)

                if self.upload_url and self.upload_api_token:
                    self.upload_token_json(filename)
            else:
                if self.upload_url and self.upload_api_token:
                    self.upload_token_data(f"{email}.json", token_data)

            return True
        except Exception as e:
            self.logger.warning("保存 Token JSON 失败: %s", e)
            return False

    def upload_token_json(self, filename: str) -> None:
        if not self.upload_url or not self.upload_api_token:
            return
        try:
            s = create_session(proxy=self.proxy)
            with open(filename, "rb") as f:
                files = {"file": (os.path.basename(filename), f, "application/json")}
                headers = {"Authorization": f"Bearer {self.upload_api_token}"}
                resp = s.post(self.upload_url, files=files, headers=headers, verify=False, timeout=30)
                if resp.status_code != 200:
                    self.logger.warning("上传 token 失败: %s %s", resp.status_code, resp.text[:200])
        except Exception as e:
            self.logger.warning("上传 token 异常: %s", e)

    def upload_token_data(self, filename: str, token_data: Dict[str, Any]) -> None:
        if not self.upload_url or not self.upload_api_token:
            return
        try:
            s = create_session(proxy=self.proxy)
            content = json.dumps(token_data, ensure_ascii=False).encode("utf-8")
            files = {"file": (filename, content, "application/json")}
            headers = {"Authorization": f"Bearer {self.upload_api_token}"}
            resp = s.post(self.upload_url, files=files, headers=headers, verify=False, timeout=30)
            if resp.status_code != 200:
                self.logger.warning("上传 token 失败: %s %s", resp.status_code, resp.text[:200])
        except Exception as e:
            self.logger.warning("上传 token 异常: %s", e)

    def save_tokens(self, token: Dict[str, Any]) -> bool:
        access_token = str(token.get("access_token") or "")
        refresh_token = str(token.get("refresh_token") or "")

        if self.save_local:
            try:
                with self.file_lock:
                    if access_token:
                        ensure_parent_dir(self.ak_file)
                        with open(self.ak_file, "a", encoding="utf-8") as f:
                            f.write(f"{access_token}\n")
                    if refresh_token:
                        ensure_parent_dir(self.rk_file)
                        with open(self.rk_file, "a", encoding="utf-8") as f:
                            f.write(f"{refresh_token}\n")
            except Exception as e:
                self.logger.warning("AK/RK 保存失败: %s", e)
                return False

        if access_token:
            return self.save_token_json(token)
        return False

    def save_account(self, email: str, password: str) -> None:
        if not self.save_local:
            return

        with self.file_lock:
            ensure_parent_dir(self.accounts_file)
            ensure_parent_dir(self.csv_file)

            with open(self.accounts_file, "a", encoding="utf-8") as f:
                f.write(f"{email}:{password}\n")

            file_exists = os.path.exists(self.csv_file)
            with open(self.csv_file, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(["email", "password", "timestamp"])
                writer.writerow([email, password, time.strftime("%Y-%m-%d %H:%M:%S")])

    def collect_token_emails(self) -> set[str]:
        emails = set()
        if not os.path.isdir(self.tokens_out_dir):
            return emails
        for name in os.listdir(self.tokens_out_dir):
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.tokens_out_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                email = data.get("email") or name[:-5]
                if email:
                    emails.add(str(email))
            except Exception:
                continue
        return emails

    def reconcile_account_outputs_from_tokens(self) -> int:
        if not self.save_local:
            return 0

        token_emails = self.collect_token_emails()

        pwd_map: Dict[str, str] = {}
        if os.path.exists(self.accounts_file):
            try:
                with open(self.accounts_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or ":" not in line:
                            continue
                        email, pwd = line.split(":", 1)
                        pwd_map[email] = pwd
            except Exception:
                pass

        ordered_emails = sorted(token_emails)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

        with self.file_lock:
            ensure_parent_dir(self.accounts_file)
            ensure_parent_dir(self.csv_file)

            with open(self.accounts_file, "w", encoding="utf-8") as f:
                for email in ordered_emails:
                    f.write(f"{email}:{pwd_map.get(email, '')}\n")

            with open(self.csv_file, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["email", "password", "timestamp"])
                for email in ordered_emails:
                    writer.writerow([email, pwd_map.get(email, ""), timestamp])

        return len(ordered_emails)


def register_one(runtime: RegisterRuntime, worker_id: int = 0) -> tuple[Optional[str], Optional[bool], float, float]:
    if runtime.stop_event.is_set() and runtime.get_token_success_count() >= runtime.target_tokens:
        return None, None, 0.0, 0.0

    t_start = time.time()
    email_session = create_session(proxy=runtime.proxy)

    provider = create_email_provider(runtime, email_session)
    email = provider.create_mailbox()
    if not email:
        return None, False, 0.0, time.time() - t_start

    password = generate_random_password()
    first_name, last_name = generate_random_name()
    birthdate = generate_random_birthday()

    attempts = max(1, runtime.oauth_retry_attempts)
    for attempt in range(1, attempts + 1):
        if runtime.stop_event.is_set() and runtime.get_token_success_count() >= runtime.target_tokens:
            return None, None, 0.0, time.time() - t_start

        runtime.logger.info("注册尝试 %s/%s: %s", attempt, attempts, email)
        registrar = ProtocolRegistrar(proxy=runtime.proxy, logger=runtime.logger)
        try:
            registrar.register(email=email, password=password)
        except InvalidAuthStep as e:
            runtime.logger.warning("register stage1 non-retryable, skip current account: %s | email=%s", e, email)
            registrar.close()
            return email, None, 0.0, time.time() - t_start
        except Exception as e:
            runtime.logger.warning("注册阶段1失败 (尝试 %s/%s): %s | email=%s", attempt, attempts, e, email)
            registrar.close()
            if attempt < attempts:
                backoff = min(runtime.oauth_retry_backoff_max, runtime.oauth_retry_backoff_base ** (attempt - 1))
                time.sleep(backoff + random.uniform(0.2, 0.8))
            continue

        t_reg = time.time() - t_start

        code = provider.wait_for_verification_code()
        if not code:
            runtime.logger.warning("注册失败: 未收到验证码 | email=%s", email)
            registrar.close()
            if attempt < attempts:
                backoff = min(runtime.oauth_retry_backoff_max, runtime.oauth_retry_backoff_base ** (attempt - 1))
                time.sleep(backoff + random.uniform(0.2, 0.8))
            continue

        runtime.logger.info("收到验证码: %s | email=%s", code, email)

        try:
            tokens = registrar.complete(code=code, first_name=first_name, last_name=last_name, birthdate=birthdate)
        except Exception as e:
            runtime.logger.warning("注册阶段2失败 (尝试 %s/%s): %s | email=%s", attempt, attempts, e, email)
            registrar.close()
            if attempt < attempts:
                backoff = min(runtime.oauth_retry_backoff_max, runtime.oauth_retry_backoff_base ** (attempt - 1))
                time.sleep(backoff + random.uniform(0.2, 0.8))
            continue

        try:
            codex_token = registrar.codex_exchange_tokens(email=email, password=password)
        except AddPhoneRequired as e:
            phone_count = runtime.increment_phone_verification_count()
            runtime.logger.warning("Codex 触发手机验证, 跳过: %s | email=%s", e, email)
            runtime.logger.info("Codex phone verification count: %s", phone_count)
            registrar.close()
            return email, None, t_reg, time.time() - t_start
        except Exception as e:
            runtime.logger.warning("Codex token 换取失败 (尝试 %s/%s): %s | email=%s", attempt, attempts, e, email)
            registrar.close()
            if attempt < attempts:
                backoff = min(runtime.oauth_retry_backoff_max, runtime.oauth_retry_backoff_base ** (attempt - 1))
                time.sleep(backoff + random.uniform(0.2, 0.8))
            continue

        registrar.close()
        t_total = time.time() - t_start

        claimed, current = runtime.claim_token_slot()
        if not claimed:
            return email, None, t_reg, t_total

        saved = runtime.save_tokens(codex_token)
        if not saved:
            runtime.release_token_slot()
            return email, False, t_reg, t_total

        runtime.save_account(email, password)
        runtime.logger.info(
            "注册成功: %s | 注册 %.1fs + code等待 %.1fs = %.1fs | token %s/%s",
            email,
            t_reg,
            t_total - t_reg,
            t_total,
            current,
            runtime.target_tokens,
        )
        return email, True, t_reg, t_total

    runtime.logger.warning("注册失败 (已达最大重试次数): %s", email)
    return email, False, 0.0, time.time() - t_start


def run_batch_register(conf: Dict[str, Any], target_tokens: int, logger: logging.Logger) -> tuple[int, int, int]:
    if target_tokens <= 0:
        return 0, 0, 0

    runtime = RegisterRuntime(conf=conf, target_tokens=target_tokens, logger=logger)
    if runtime.email_type not in {"cf", "gptmail"}:
        logger.error("unsupported email.type: %s", runtime.email_type)
        return 0, 0, 0
    if runtime.email_type == "cf" and not runtime.admin_password:
        logger.error("email.admin_password 未配置，无法创建临时邮箱。")
        return 0, 0, 0
    if runtime.email_type == "gptmail" and not runtime.gptmail_api_key:
        logger.error("email.gptmail_apikey 未配置，无法创建 gptmail 临时邮箱。")
        return 0, 0, 0
    workers = runtime.concurrent_workers

    logger.info(
        "开始补号: 目标 token=%s, 并发=%s, email_type=%s, worker_domain=%s, email_domains=%s, gptmail_base_url=%s",
        target_tokens,
        workers,
        runtime.email_type,
        runtime.worker_domain,
        ",".join(runtime.email_domains),
        runtime.gptmail_base_url,
    )

    ok = 0
    fail = 0
    skip = 0
    attempts = 0
    reg_times: List[float] = []
    total_times: List[float] = []
    lock = threading.Lock()
    batch_start = time.time()

    if workers == 1:
        while runtime.get_token_success_count() < target_tokens:
            attempts += 1
            email, success, t_reg, t_total = register_one(runtime, worker_id=1)
            if success is True:
                ok += 1
                reg_times.append(t_reg)
                total_times.append(t_total)
            elif success is False:
                fail += 1
            else:
                skip += 1
            logger.info(
                "补号进度: token %s/%s | ✅%s ❌%s ⏭️%s 📱%s | 用时 %.1fs",
                runtime.get_token_success_count(),
                target_tokens,
                ok,
                fail,
                skip,
                runtime.get_phone_verification_count(),
                time.time() - batch_start,
            )
            if runtime.get_token_success_count() >= target_tokens:
                break
            time.sleep(random.randint(2, 6))
    else:
        def worker_task(task_index: int, worker_id: int):
            if task_index > 1:
                jitter = random.uniform(0.5, 2.0) * worker_id
                time.sleep(jitter)
            if runtime.stop_event.is_set() and runtime.get_token_success_count() >= target_tokens:
                return task_index, None, None, 0.0, 0.0
            email, success, t_reg, t_total = register_one(runtime, worker_id=worker_id)
            return task_index, email, success, t_reg, t_total

        executor = ThreadPoolExecutor(max_workers=workers)
        futures = {}
        next_task_index = 1

        def submit_one() -> bool:
            nonlocal next_task_index
            remaining = target_tokens - runtime.get_token_success_count()
            if remaining <= 0:
                return False
            if len(futures) >= remaining:
                return False

            wid = ((next_task_index - 1) % workers) + 1
            fut = executor.submit(worker_task, next_task_index, wid)
            futures[fut] = next_task_index
            next_task_index += 1
            return True

        try:
            for _ in range(min(workers, target_tokens)):
                if not submit_one():
                    break

            while futures:
                if runtime.get_token_success_count() >= target_tokens:
                    runtime.stop_event.set()
                    break

                done_set, _ = wait(list(futures.keys()), return_when=FIRST_COMPLETED, timeout=1.0)
                if not done_set:
                    continue

                for fut in done_set:
                    _ = futures.pop(fut, None)
                    attempts += 1
                    try:
                        _, _, success, t_reg, t_total = fut.result()
                    except Exception:
                        success, t_reg, t_total = False, 0.0, 0.0

                    with lock:
                        if success is True:
                            ok += 1
                            reg_times.append(t_reg)
                            total_times.append(t_total)
                        elif success is False:
                            fail += 1
                        else:
                            skip += 1

                        logger.info(
                            "补号进度: token %s/%s | ✅%s ❌%s ⏭️%s 📱%s | 用时 %.1fs",
                            runtime.get_token_success_count(),
                            target_tokens,
                            ok,
                            fail,
                            skip,
                            runtime.get_phone_verification_count(),
                            time.time() - batch_start,
                        )

                    if runtime.get_token_success_count() < target_tokens:
                        submit_one()
        finally:
            runtime.stop_event.set()
            for f in list(futures.keys()):
                f.cancel()
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                executor.shutdown(wait=False)

    synced = runtime.reconcile_account_outputs_from_tokens()
    elapsed = time.time() - batch_start
    avg_reg = (sum(reg_times) / len(reg_times)) if reg_times else 0
    avg_total = (sum(total_times) / len(total_times)) if total_times else 0
    logger.info(
        "补号完成: token=%s/%s, fail=%s, skip=%s, phone_verify=%s, attempts=%s, elapsed=%.1fs, avg(注册)=%.1fs, avg(总)=%.1fs, 收敛账号=%s",
        runtime.get_token_success_count(),
        target_tokens,
        fail,
        skip,
        runtime.get_phone_verification_count(),
        attempts,
        elapsed,
        avg_reg,
        avg_total,
        synced,
    )
    return runtime.get_token_success_count(), fail, synced


def fetch_auth_files(base_url: str, token: str, timeout: int) -> List[Dict[str, Any]]:
    resp = requests.get(f"{base_url}/v0/management/auth-files", headers=mgmt_headers(token), timeout=timeout)
    resp.raise_for_status()
    raw = resp.json()
    data = raw if isinstance(raw, dict) else {}
    files = data.get("files", [])
    return files if isinstance(files, list) else []


def build_probe_payload(auth_index: str, user_agent: str, chatgpt_account_id: Optional[str] = None) -> Dict[str, Any]:
    call_header = {
        "Authorization": "Bearer $TOKEN$",
        "Content-Type": "application/json",
        "User-Agent": user_agent or DEFAULT_MGMT_UA,
    }
    if chatgpt_account_id:
        call_header["Chatgpt-Account-Id"] = chatgpt_account_id
    return {
        "authIndex": auth_index,
        "method": "GET",
        "url": "https://chatgpt.com/backend-api/wham/usage",
        "header": call_header,
    }


async def probe_account_async(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    base_url: str,
    token: str,
    item: Dict[str, Any],
    user_agent: str,
    timeout: int,
    retries: int,
) -> Dict[str, Any]:
    auth_index = item.get("auth_index")
    name = item.get("name") or item.get("id")
    account = item.get("account") or item.get("email") or ""
    result = {
        "name": name,
        "account": account,
        "auth_index": auth_index,
        "type": get_item_type(item),
        "provider": item.get("provider"),
        "status_code": None,
        "invalid_401": False,
        "error": None,
    }
    if not auth_index:
        result["error"] = "missing auth_index"
        return result

    chatgpt_account_id = extract_chatgpt_account_id(item)
    payload = build_probe_payload(str(auth_index), user_agent, chatgpt_account_id)

    for attempt in range(retries + 1):
        try:
            async with semaphore:
                async with session.post(
                    f"{base_url}/v0/management/api-call",
                    headers={**mgmt_headers(token), "Content-Type": "application/json"},
                    json=payload,
                    timeout=timeout,
                ) as resp:
                    text = await resp.text()
                    if resp.status >= 400:
                        raise RuntimeError(f"management api-call http {resp.status}: {text[:200]}")
                    data = safe_json_text(text)
                    sc = data.get("status_code")
                    result["status_code"] = sc
                    result["invalid_401"] = sc == 401
                    if sc is None:
                        result["error"] = "missing status_code in api-call response"
                    return result
        except Exception as e:
            result["error"] = str(e)
            if attempt >= retries:
                return result
    return result


async def delete_account_async(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    base_url: str,
    token: str,
    name: str,
    timeout: int,
) -> Dict[str, Any]:
    if not name:
        return {"name": None, "deleted": False, "error": "missing name"}
    encoded_name = quote(name, safe="")
    url = f"{base_url}/v0/management/auth-files?name={encoded_name}"
    try:
        async with semaphore:
            async with session.delete(url, headers=mgmt_headers(token), timeout=timeout) as resp:
                text = await resp.text()
                data = safe_json_text(text)
                ok = resp.status == 200 and data.get("status") == "ok"
                return {
                    "name": name,
                    "deleted": ok,
                    "status_code": resp.status,
                    "error": None if ok else f"delete failed, response={text[:200]}",
                }
    except Exception as e:
        return {"name": name, "deleted": False, "error": str(e)}


async def run_probe_async(
    base_url: str,
    token: str,
    target_type: str,
    workers: int,
    timeout: int,
    retries: int,
    user_agent: str,
    logger: Optional[logging.Logger] = None,
) -> tuple[List[Dict[str, Any]], int, int]:
    files = fetch_auth_files(base_url, token, timeout)
    candidates: List[Dict[str, Any]] = []
    for f in files:
        if str(get_item_type(f)).lower() != target_type.lower():
            continue
        candidates.append(f)

    if not candidates:
        return [], len(files), 0

    connector = aiohttp.TCPConnector(limit=max(1, workers), limit_per_host=max(1, workers))
    client_timeout = aiohttp.ClientTimeout(total=max(1, timeout))
    semaphore = asyncio.Semaphore(max(1, workers))

    probe_results = []
    total_candidates = len(candidates)
    checked = 0
    invalid_count = 0

    async with aiohttp.ClientSession(connector=connector, timeout=client_timeout, trust_env=True) as session:
        tasks = [
            asyncio.create_task(
                probe_account_async(
                    session=session,
                    semaphore=semaphore,
                    base_url=base_url,
                    token=token,
                    item=item,
                    user_agent=user_agent,
                    timeout=timeout,
                    retries=retries,
                )
            )
            for item in candidates
        ]
        for task in asyncio.as_completed(tasks):
            result = await task
            probe_results.append(result)
            checked += 1
            if result.get("invalid_401"):
                invalid_count += 1

            if logger and (checked % 50 == 0 or checked == total_candidates):
                logger.info("401探测进度: 已检查=%s/%s, 命中401=%s", checked, total_candidates, invalid_count)

    invalid_401 = [r for r in probe_results if r.get("invalid_401")]
    return invalid_401, len(files), len(candidates)


async def run_delete_async(
    base_url: str,
    token: str,
    names_to_delete: List[str],
    delete_workers: int,
    timeout: int,
) -> tuple[int, int]:
    if not names_to_delete:
        return 0, 0

    connector = aiohttp.TCPConnector(limit=max(1, delete_workers), limit_per_host=max(1, delete_workers))
    client_timeout = aiohttp.ClientTimeout(total=max(1, timeout))
    semaphore = asyncio.Semaphore(max(1, delete_workers))

    delete_results = []
    async with aiohttp.ClientSession(connector=connector, timeout=client_timeout, trust_env=True) as session:
        tasks = [
            asyncio.create_task(
                delete_account_async(
                    session=session,
                    semaphore=semaphore,
                    base_url=base_url,
                    token=token,
                    name=name,
                    timeout=timeout,
                )
            )
            for name in names_to_delete
        ]
        for task in asyncio.as_completed(tasks):
            delete_results.append(await task)

    success = [r for r in delete_results if r.get("deleted")]
    failed = [r for r in delete_results if not r.get("deleted")]
    return len(success), len(failed)


async def run_clean_401_async(
    *,
    base_url: str,
    token: str,
    target_type: str,
    workers: int,
    delete_workers: int,
    timeout: int,
    retries: int,
    user_agent: str,
    logger: logging.Logger,
) -> tuple[int, int, int]:
    invalid_401, total_files, codex_files = await run_probe_async(
        base_url=base_url,
        token=token,
        target_type=target_type,
        workers=workers,
        timeout=timeout,
        retries=retries,
        user_agent=user_agent,
        logger=logger,
    )
    names = [str(r.get("name")) for r in invalid_401 if r.get("name")]
    logger.info("探测完成: 总账号=%s, codex账号=%s, 401失效=%s", total_files, codex_files, len(names))

    deleted_ok, deleted_fail = await run_delete_async(
        base_url=base_url,
        token=token,
        names_to_delete=names,
        delete_workers=delete_workers,
        timeout=timeout,
    )
    logger.info("删除完成: 成功=%s, 失败=%s", deleted_ok, deleted_fail)
    return len(names), deleted_ok, deleted_fail


def run_clean_401(conf: Dict[str, Any], logger: logging.Logger) -> tuple[int, int, int]:
    if aiohttp is None:
        raise RuntimeError("未安装 aiohttp，请先安装: pip install aiohttp")

    base_url = str(pick_conf(conf, "clean", "base_url", default="") or "").rstrip("/")
    token = str(pick_conf(conf, "clean", "token", "cpa_password", default="") or "").strip()
    target_type = str(pick_conf(conf, "clean", "target_type", default="codex") or "codex")
    workers = int(pick_conf(conf, "clean", "workers", default=20) or 20)
    delete_workers = int(pick_conf(conf, "clean", "delete_workers", default=40) or 40)
    timeout = int(pick_conf(conf, "clean", "timeout", default=10) or 10)
    retries = int(pick_conf(conf, "clean", "retries", default=1) or 1)
    user_agent = str(pick_conf(conf, "clean", "user_agent", default=DEFAULT_MGMT_UA) or DEFAULT_MGMT_UA)

    if not base_url or not token:
        raise RuntimeError("clean 配置缺少 base_url 或 token/cpa_password")

    logger.info("开始清理 401: base_url=%s target_type=%s", base_url, target_type)
    return asyncio.run(
        run_clean_401_async(
            base_url=base_url,
            token=token,
            target_type=target_type,
            workers=workers,
            delete_workers=delete_workers,
            timeout=timeout,
            retries=retries,
            user_agent=user_agent,
            logger=logger,
        )
    )


def parse_args() -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    default_cfg = script_dir / "config.json"
    default_log_dir = script_dir / "logs"

    parser = argparse.ArgumentParser(description="账号池自动维护（三合一：清理+补号+收敛）")
    parser.add_argument("--config", default=str(default_cfg), help="统一配置文件路径")
    parser.add_argument(
        "--min-candidates",
        type=int,
        default=None,
        help="候选账号最小阈值（默认读取 maintainer.min_candidates / 顶层 min_candidates，最终默认 100）",
    )
    parser.add_argument("--timeout", type=int, default=15, help="统计 candidates 时接口超时秒数")
    parser.add_argument("--log-dir", default=str(default_log_dir), help="日志目录")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = Path(args.config).resolve()
    logger, log_path = setup_logger(Path(args.log_dir).resolve())
    logger.info("=== 账号池自动维护开始（二合一）===")
    logger.info("配置文件: %s", config_path)
    logger.info("日志文件: %s", log_path)

    if not config_path.exists():
        logger.error("配置文件不存在: %s", config_path)
        return 2

    conf = load_json(config_path)

    base_url = str(pick_conf(conf, "clean", "base_url", default="") or "").rstrip("/")
    token = str(pick_conf(conf, "clean", "token", "cpa_password", default="") or "").strip()
    target_type = str(pick_conf(conf, "clean", "target_type", default="codex") or "codex")

    cfg_min_candidates = pick_conf(conf, "maintainer", "min_candidates", default=None)
    if cfg_min_candidates is None:
        cfg_min_candidates = conf.get("min_candidates")

    if args.min_candidates is not None:
        min_candidates = int(args.min_candidates)
    elif cfg_min_candidates is not None:
        min_candidates = int(cfg_min_candidates)
    else:
        min_candidates = 100

    if min_candidates < 0:
        logger.error("min_candidates 不能小于 0（当前值=%s）", min_candidates)
        return 2
    if not base_url or not token:
        logger.error("缺少 clean.base_url 或 clean.token/cpa_password")
        return 2

    try:
        probed_401, deleted_ok, deleted_fail = run_clean_401(conf, logger)
        logger.info("清理阶段汇总: 401命中=%s, 删除成功=%s, 删除失败=%s", probed_401, deleted_ok, deleted_fail)
    except Exception as e:
        logger.error("清理 401 失败: %s", e)
        logger.info("=== 账号池自动维护结束（失败）===")
        return 3

    try:
        total_after_clean, candidates_after_clean = get_candidates_count(
            base_url=base_url,
            token=token,
            target_type=target_type,
            timeout=args.timeout,
        )
    except Exception as e:
        logger.error("删除后统计失败: %s", e)
        logger.info("=== 账号池自动维护结束（失败）===")
        return 4

    logger.info(
        "删除401后统计: 总账号=%s, candidates=%s, 阈值=%s",
        total_after_clean,
        candidates_after_clean,
        min_candidates,
    )

    if candidates_after_clean >= min_candidates:
        logger.info("当前 candidates 已达标，无需补号。")
        logger.info("=== 账号池自动维护结束（成功）===")
        return 0

    gap = min_candidates - candidates_after_clean
    logger.info("当前 candidates 未达标，缺口=%s，开始补号。", gap)

    try:
        filled, failed, synced = run_batch_register(conf=conf, target_tokens=gap, logger=logger)
        logger.info("补号阶段汇总: 成功token=%s, 失败=%s, 收敛账号=%s", filled, failed, synced)
    except Exception as e:
        logger.error("补号阶段失败: %s", e)
        logger.info("=== 账号池自动维护结束（失败）===")
        return 5

    try:
        total_final, candidates_final = get_candidates_count(
            base_url=base_url,
            token=token,
            target_type=target_type,
            timeout=args.timeout,
        )
    except Exception as e:
        logger.error("补号后统计失败: %s", e)
        logger.info("=== 账号池自动维护结束（失败）===")
        return 6

    logger.info(
        "补号后统计: 总账号=%s, codex账号=%s, codex目标=%s",
        total_final,
        candidates_final,
        min_candidates,
    )
    if candidates_final < min_candidates:
        logger.warning("最终 codex账号数 仍低于阈值，请检查邮箱/OAuth/上传链路。")
    logger.info("=== 账号池自动维护结束（成功）===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
