from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from contextlib import asynccontextmanager
from starlette.background import BackgroundTask
from dotenv import load_dotenv
import httpx
import json
import os
import re
import base64
import asyncio
import hashlib
from typing import Iterable
from urllib.parse import urlparse

# ===== WAF Cookie 求解器 =====
def solve_acw_challenge(html_content):
    """
    从阿里云 WAF 挑战页面中计算 acw_sc__v2 cookie 值
    用于绕过 anyrouter.top 的 JavaScript 挑战
    """
    match = re.search(r"var arg1='([A-F0-9]+)'", html_content)
    if not match:
        return None
    
    arg1 = match.group(1)
    mask_b64 = 'MzAwMDE3NjAwMDg1NjAwNjA2MTUwMTUzMzAwMzY5MDAyNzgwMDM3NQ=='
    mask = base64.b64decode(mask_b64).decode()
    
    posList = [0xf,0x23,0x1d,0x18,0x21,0x10,0x1,0x26,0xa,0x9,0x13,0x1f,0x28,0x1b,0x16,0x17,0x19,0xd,0x6,0xb,0x27,0x12,0x14,0x8,0xe,0x15,0x20,0x1a,0x2,0x1e,0x7,0x4,0x11,0x5,0x3,0x1c,0x22,0x25,0xc,0x24]
    
    outPutList = [''] * len(posList)
    for i in range(len(arg1)):
        for j in range(len(posList)):
            if posList[j] == i + 1:
                outPutList[j] = arg1[i]
    
    arg2 = ''.join(outPutList)
    
    arg3 = ''
    for i in range(0, min(len(arg2), len(mask)), 2):
        strChar = int(arg2[i:i+2], 16)
        maskChar = int(mask[i:i+2], 16)
        xorChar = hex(strChar ^ maskChar)[2:]
        if len(xorChar) == 1:
            xorChar = '0' + xorChar
        arg3 += xorChar
    
    return arg3
# WAF cookies 缓存 (按URL维护)
waf_cookies_cache = {}  # {url: {cookies: {}, timestamp: 0}}
WAF_COOKIE_TTL = 300  # WAF cookie 有效期（秒）
MAX_RETRY_ATTEMPTS = 2  # 每次URL+Key组合的最大重试次数（减少以配合探针策略）

# ===== 智能重试策略配置 =====
# 默认探测模型（当无法从请求中提取时使用）
DEFAULT_PROBE_MODEL = "claude-sonnet-4-5-20250929"

def create_probe_body(model: str = None):
    """
    创建极短的探测请求体，用于测试 API 可用性
    优先使用用户请求中的模型，否则使用默认模型
    """
    probe_model = model if model else DEFAULT_PROBE_MODEL
    probe_body = {
        "model": probe_model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}]
    }
    return json.dumps(probe_body, separators=(',', ':')).encode('utf-8')


def extract_model_from_body(body: bytes) -> str:
    """从请求体中提取模型名称"""
    try:
        data = json.loads(body.decode('utf-8'))
        return data.get('model', DEFAULT_PROBE_MODEL)
    except:
        return DEFAULT_PROBE_MODEL


class RetryContext:
    """
    重试上下文管理器
    策略：前2次使用完整请求，之后使用探测请求测试连接
    """
    def __init__(self, original_body: bytes):
        self.original_body = original_body
        # 从原始请求中提取模型，用于探测请求
        self.user_model = extract_model_from_body(original_body)
        self.probe_body = create_probe_body(self.user_model)
        self.full_context_attempts = 0  # 完整请求尝试次数
        self.probe_attempts = 0  # 探测请求尝试次数
        self.probe_succeeded_but_full_failed = False
        self.last_working_url = None
        self.last_working_key = None
    
    def should_use_probe(self) -> bool:
        """判断是否应该使用探测请求（前2次完整，之后探测）"""
        return self.full_context_attempts >= 2
    
    def get_request_body(self) -> bytes:
        """获取当前应该使用的请求体"""
        if self.should_use_probe():
            return self.probe_body
        return self.original_body
    
    def record_attempt(self, is_probe: bool, success: bool, url: str = None, key: str = None):
        """记录尝试结果"""
        if is_probe:
            self.probe_attempts += 1
            if success:
                self.last_working_url = url
                self.last_working_key = key
        else:
            self.full_context_attempts += 1


# ===== 多URL和多Key容错配置 =====
# 从环境变量读取候选URL列表（逗号分隔）
# 默认值作为备用
_default_urls = "https://anyrouter.top,https://c.cspok.cn,https://pmpjfbhq.cn-nb1.rainapp.top,https://a-ocnfniawgw.cn-shanghai.fcapp.run"
CANDIDATE_URLS = [url.strip() for url in os.getenv("CANDIDATE_URLS", _default_urls).split(",") if url.strip()]

# 从环境变量读取候选API Key列表（逗号分隔）
# 注意：环境变量名为 API_KEYS（与.env文件保持一致）
_default_keys = ""
CANDIDATE_API_KEYS = [key.strip() for key in os.getenv("API_KEYS", _default_keys).split(",") if key.strip()]

# ===== Key 标识（避免明文落盘） =====
COOLDOWN_SCHEMA_VERSION = 2

def _key_id(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

KEY_ID_BY_KEY = {key: _key_id(key) for key in CANDIDATE_API_KEYS}

# ===== Key使用次数统计（智能轮换） =====
# 记录每个Key的使用次数，优先使用次数最少的Key
key_usage_count = {key: 0 for key in CANDIDATE_API_KEYS}

# ===== 冷静期持久化 =====
COOLDOWN_FILE = os.path.join(os.path.dirname(__file__), "cooldown_state.json")

def load_cooldowns():
    """从JSON文件加载冷静期状态"""
    global key_cooldown_until, url_cooldown_until
    try:
        if os.path.exists(COOLDOWN_FILE):
            with open(COOLDOWN_FILE, 'r') as f:
                data = json.load(f)
                raw_keys = data.get("keys", {})
                url_cooldown_until = data.get("urls", {})
                schema_version = data.get("schema_version", 1)
                # 兼容旧格式：{明文key: ts} -> {key_id: ts}
                if isinstance(raw_keys, dict):
                    normalized = {}
                    for k, v in raw_keys.items():
                        if not isinstance(k, str):
                            continue
                        if schema_version >= 2 and re.fullmatch(r"[0-9a-f]{16}", k):
                            kid = k
                        elif k in KEY_ID_BY_KEY:
                            kid = KEY_ID_BY_KEY[k]
                        else:
                            continue
                        normalized[kid] = v
                    key_cooldown_until = normalized
                else:
                    key_cooldown_until = {}
                # 清理过期的冷静期
                import time
                now = time.time()
                key_cooldown_until = {k: v for k, v in key_cooldown_until.items() if v > now}
                url_cooldown_until = {k: v for k, v in url_cooldown_until.items() if v > now}
                if key_cooldown_until or url_cooldown_until:
                    print(f"[Cooldown] 加载持久化冷静期: {len(key_cooldown_until)} Keys, {len(url_cooldown_until)} URLs")
    except Exception as e:
        print(f"[Cooldown] 加载冷静期失败: {e}")

def save_cooldowns():
    """保存冷静期状态到JSON文件"""
    try:
        data = {
            "schema_version": COOLDOWN_SCHEMA_VERSION,
            "keys": key_cooldown_until,
            "urls": url_cooldown_until
        }
        with open(COOLDOWN_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        if DEBUG_MODE:
            print(f"[Cooldown] 保存冷静期失败: {e}")

# ===== Key冷静期机制 =====
# 记录问题Key的冷静期结束时间（72小时）
KEY_COOLDOWN_HOURS = 72
key_cooldown_until = {}  # {key_id: timestamp} - Key冷静期结束的时间戳

# URL冷静期
URL_COOLDOWN_HOURS = 72
url_cooldown_until = {}  # {url: timestamp} - URL冷静期结束的时间戳

# 启动时加载持久化冷静期
load_cooldowns()

# 待设置冷静期的Key列表（每个URL一个列表）
# 只有当同一URL上有Key成功后，才批量设置这些Key的冷静期
pending_cooldown_keys = {}  # {url: [key1, key2, ...]

# 是否启用非必要流量禁用（全部失败后自动设置）
# 内部逻辑使用布尔值，初始化时从环境变量读取
nonessential_traffic_disabled = os.getenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "0") == "1"

def add_pending_cooldown(url, key):
    """添加Key到待冷静期列表（暂不设置，等待成功确认）"""
    if url not in pending_cooldown_keys:
        pending_cooldown_keys[url] = []
    kid = KEY_ID_BY_KEY.get(key) or _key_id(key)
    if kid not in pending_cooldown_keys[url]:
        pending_cooldown_keys[url].append(kid)
        print(f"[Key Pending] Key {kid[:8]}… 加入待冷静期列表（等待URL确认）")

def confirm_cooldown_for_url(url):
    """确认某URL有Key成功，批量设置该URL下待冷静期的Key"""
    if url not in pending_cooldown_keys or not pending_cooldown_keys[url]:
        return
    
    import time
    for kid in pending_cooldown_keys[url]:
        cooldown_end = time.time() + (KEY_COOLDOWN_HOURS * 3600)
        key_cooldown_until[kid] = cooldown_end
        print(f"[Key Cooldown] ⚠ Key {kid[:8]}… 进入 {KEY_COOLDOWN_HOURS}小时 冷静期（URL已确认有效）")
    
    # 清空该URL的待冷静期列表
    pending_cooldown_keys[url] = []
    save_cooldowns()  # 持久化

def clear_pending_cooldown(url):
    """清空某URL的待冷静期列表（URL失效时调用，不设置冷静期）"""
    if url in pending_cooldown_keys:
        count = len(pending_cooldown_keys[url])
        if count > 0:
            print(f"[Key Pending] URL {url} 可能失效，{count}个Key不设置冷静期")
        pending_cooldown_keys[url] = []

def set_key_cooldown(key, url=None):
    """
    直接设置Key冷静期（用于无需确认的场景）
    """
    import time
    cooldown_end = time.time() + (KEY_COOLDOWN_HOURS * 3600)
    kid = KEY_ID_BY_KEY.get(key) or _key_id(key)
    key_cooldown_until[kid] = cooldown_end
    print(f"[Key Cooldown] ⚠ Key {kid[:8]}… 进入 {KEY_COOLDOWN_HOURS}小时 冷静期")
    save_cooldowns()  # 持久化
    return True

def is_key_in_cooldown(key):
    """检查Key是否在冷静期内"""
    import time
    kid = KEY_ID_BY_KEY.get(key) or _key_id(key)
    if kid not in key_cooldown_until:
        return False
    if time.time() >= key_cooldown_until[kid]:
        # 冷静期已过，移除记录
        del key_cooldown_until[kid]
        save_cooldowns()
        return False
    return True

def get_available_keys():
    """获取不在冷静期内的可用Key列表"""
    return [k for k in CANDIDATE_API_KEYS if not is_key_in_cooldown(k)]

# ===== URL冷静期机制 =====
# 与Key冷静期逻辑一致
URL_COOLDOWN_HOURS = 72
url_cooldown_until = {}  # {url: timestamp} - URL冷静期结束的时间戳

def set_url_cooldown(url):
    """设置URL进入72小时冷静期"""
    import time
    cooldown_end = time.time() + (URL_COOLDOWN_HOURS * 3600)
    url_cooldown_until[url] = cooldown_end
    print(f"[URL Cooldown] ⚠ URL {url} 进入 {URL_COOLDOWN_HOURS}小时 冷静期")
    save_cooldowns()  # 持久化

def is_url_in_cooldown(url):
    """检查URL是否在冷静期内"""
    import time
    if url not in url_cooldown_until:
        return False
    if time.time() >= url_cooldown_until[url]:
        # 冷静期已过，移除记录
        del url_cooldown_until[url]
        return False
    return True

def get_available_urls():
    """获取不在冷静期内的可用URL列表"""
    available = [u for u in CANDIDATE_URLS if not is_url_in_cooldown(u)]
    # 安全机制：至少保留一个URL
    if not available and CANDIDATE_URLS:
        # 返回冷静期最早结束的URL
        return [min(CANDIDATE_URLS, key=lambda u: url_cooldown_until.get(u, 0))]
    return available if available else CANDIDATE_URLS

def get_least_used_key():
    """获取使用次数最少的可用Key（排除冷静期内的Key）"""
    available_keys = get_available_keys()
    if not available_keys:
        # 如果所有Key都在冷静期，返回冷静期最早结束的Key
        if CANDIDATE_API_KEYS:
            return min(CANDIDATE_API_KEYS, key=lambda k: key_cooldown_until.get(KEY_ID_BY_KEY.get(k) or _key_id(k), 0))
        return None
    # 按使用次数排序，返回最少使用的
    sorted_keys = sorted(available_keys, key=lambda k: key_usage_count.get(k, 0))
    return sorted_keys[0]

def get_keys_sorted_by_usage():
    """获取按使用次数排序的可用Key列表（从少到多，排除冷静期）"""
    available_keys = get_available_keys()
    if not available_keys:
        # 如果所有Key都在冷静期，返回所有Key（按冷静期结束时间排序）
        return sorted(CANDIDATE_API_KEYS, key=lambda k: key_cooldown_until.get(KEY_ID_BY_KEY.get(k) or _key_id(k), 0))
    return sorted(available_keys, key=lambda k: key_usage_count.get(k, 0))

def increment_key_usage(key):
    """增加Key的使用次数"""
    if key in key_usage_count:
        key_usage_count[key] += 1
    else:
        key_usage_count[key] = 1

def get_waf_cookies(base_url):
    """获取指定URL的WAF cookies缓存"""
    if base_url not in waf_cookies_cache:
        waf_cookies_cache[base_url] = {"cookies": {}, "timestamp": 0}
    return waf_cookies_cache[base_url]

async def fetch_waf_cookie(client, base_url, force=False):
    """异步获取 WAF cookie（增强版，支持多URL）"""
    import time
    current_time = time.time()
    
    cache = get_waf_cookies(base_url)
    waf_cookies = cache["cookies"]
    waf_cookie_timestamp = cache["timestamp"]
    
    # 检查是否需要刷新（除非强制刷新）
    if not force and waf_cookies and (current_time - waf_cookie_timestamp) < WAF_COOKIE_TTL:
        print(f"[WAF] 使用缓存的 cookie (剩余有效期: {int(WAF_COOKIE_TTL - (current_time - waf_cookie_timestamp))}秒)")
        return True
    
    try:
        print(f"[WAF] {'强制刷新' if force else '获取'} {base_url} 的 WAF cookie...")
        resp = await client.get(base_url + "/", timeout=15)
        
        if 'acw_sc__v2' in resp.text or "var arg1=" in resp.text:
            cookie_value = solve_acw_challenge(resp.text)
            if cookie_value:
                waf_cookies['acw_sc__v2'] = cookie_value
                # 同时保留服务器返回的其他cookies
                for name, value in resp.cookies.items():
                    waf_cookies[name] = value
                cache["timestamp"] = current_time
                print(f"[WAF] 成功获取 cookie: acw_sc__v2={cookie_value[:20]}...")
                print(f"[WAF] 额外 cookies: {[k for k in waf_cookies.keys() if k != 'acw_sc__v2']}")
                return True
            else:
                print(f"[WAF] 无法从挑战页面提取 cookie")
                return False
        else:
            print(f"[WAF] {base_url} 无需 WAF 挑战")
            # 保存现有 cookies
            for name, value in resp.cookies.items():
                waf_cookies[name] = value
            cache["timestamp"] = current_time
            return True
    except Exception as e:
        print(f"[WAF] 获取 cookie 失败: {type(e).__name__}: {e}")
        return False

def get_next_url():
    """获取下一个候选URL"""
    global current_url_index
    url = CANDIDATE_URLS[current_url_index]
    current_url_index = (current_url_index + 1) % len(CANDIDATE_URLS)
    return url

def get_next_api_key():
    """获取下一个候选API Key"""
    global current_key_index
    key = CANDIDATE_API_KEYS[current_key_index]
    current_key_index = (current_key_index + 1) % len(CANDIDATE_API_KEYS)
    return key

def reset_indices():
    """重置轮换索引"""
    global current_url_index, current_key_index
    current_url_index = 0
    current_key_index = 0

# Shared HTTP client for connection pooling and proper lifecycle management
http_client: httpx.AsyncClient = None  # type: ignore


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Manage application lifespan events"""
    global http_client

    # 输出应用配置信息（只在 worker 进程启动时输出一次）
    print("=" * 60)
    print("Application Configuration:")
    print(f"  Base URL: {TARGET_BASE_URL}")
    print(f"  Candidate URLs: {len(CANDIDATE_URLS)} configured")
    for i, url in enumerate(CANDIDATE_URLS):
        print(f"    [{i+1}] {url}")
    print(f"  Candidate API Keys: {len(CANDIDATE_API_KEYS)} configured (dynamic rotation)")
    print(f"  Key Cooldown: {KEY_COOLDOWN_HOURS} hours for problematic keys")
    print(f"  System Prompt Replacement: {SYSTEM_PROMPT_REPLACEMENT}")
    print(f"  System Prompt Insert Mode: {SYSTEM_PROMPT_BLOCK_INSERT_IF_NOT_EXIST}")
    print(f"  Server Port: {PORT}")
    print(f"  Custom Headers: {len(CUSTOM_HEADERS)} headers loaded")
    if CUSTOM_HEADERS:
        print(f"  Custom Headers Keys: {list(CUSTOM_HEADERS.keys())}")
    print(f"  Debug Mode: {DEBUG_MODE}")
    print(f"  Hot Reload: {DEBUG_MODE}")
    print("=" * 60)

    # 读取代理配置
    http_proxy = os.getenv("HTTP_PROXY")
    https_proxy = os.getenv("HTTPS_PROXY")

    # 构建 mounts 配置（httpx 0.28.0+ 的新语法）
    mounts = {}

    if http_proxy:
        # 确保代理 URL 包含协议
        if "://" not in http_proxy:
            http_proxy = f"http://{http_proxy}"
        mounts["http://"] = httpx.AsyncHTTPTransport(proxy=http_proxy)
        print(f"HTTP Proxy configured: {http_proxy}")

    if https_proxy:
        # 注意：HTTPS 代理通常也使用 http:// 协议（这不是错误！）
        if "://" not in https_proxy:
            https_proxy = f"http://{https_proxy}"
        mounts["https://"] = httpx.AsyncHTTPTransport(proxy=https_proxy)
        print(f"HTTPS Proxy configured: {https_proxy}")

    try:
        # 使用新的 mounts 参数初始化客户端
        if mounts:
            http_client = httpx.AsyncClient(
                follow_redirects=False,
                timeout=60.0,
                mounts=mounts
            )
            print(f"HTTP client initialized with proxy mounts: {list(mounts.keys())}")
        else:
            http_client = httpx.AsyncClient(
                follow_redirects=False,
                timeout=60.0
            )
            print("HTTP client initialized without proxy")
    except Exception as e:
        print(f"Failed to initialize HTTP client: {e}")
        raise

    # 预获取 WAF cookie (异步)
    if 'anyrouter' in TARGET_BASE_URL or 'cspok' in TARGET_BASE_URL:
        await fetch_waf_cookie(http_client, TARGET_BASE_URL, force=True)

    print("=" * 60)

    yield

    # Shutdown: Close HTTP client
    await http_client.aclose()

# ===== 基础配置 =====
# 主站：https://anyrouter.top
load_dotenv()
# 可选：从 .secrets 加载敏感配置（API_KEYS 等），并覆盖 .env
_secrets_path = os.path.join(os.path.dirname(__file__), ".secrets")
if os.path.exists(_secrets_path):
    load_dotenv(dotenv_path=_secrets_path, override=True)
TARGET_BASE_URL = os.getenv("API_BASE_URL", "https://anyrouter.top")
PRESERVE_HOST = False  # 是否保留原始 Host

# System prompt 替换配置
# 设置为字符串以替换请求体中 system 数组的第一个元素的 text 内容
# 设置为 None 则保持原样不修改
# 通过环境变量 SYSTEM_PROMPT_REPLACEMENT 配置，默认为 None
SYSTEM_PROMPT_REPLACEMENT = os.getenv("SYSTEM_PROMPT_REPLACEMENT")  # 例如: "你是一个有用的AI助手"

# System prompt 插入模式配置
# 设置为 true/1/yes 时，启用插入模式而非替换模式
# 通过环境变量 SYSTEM_PROMPT_BLOCK_INSERT_IF_NOT_EXIST 配置，默认为 false
SYSTEM_PROMPT_BLOCK_INSERT_IF_NOT_EXIST = os.getenv("SYSTEM_PROMPT_BLOCK_INSERT_IF_NOT_EXIST", "false").lower() in ("true", "1", "yes")

# 关键字常量定义
# 用于判断是否需要执行替换操作
CLAUDE_CODE_KEYWORD = "Claude Code"

# 调试模式配置
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() in ("true", "1", "yes")

# 服务端口配置
PORT = int(os.getenv("PORT", "8088"))

app = FastAPI(
    title="Anthropic Transparent Proxy",
    version="1.2",  # Updated version
    lifespan=lifespan
)

# 自定义 Header 配置
# 从 env/.env.headers.json 文件加载，如果文件不存在或解析失败，则使用空字典 {}
def load_custom_headers() -> dict:
    """
    从 env/.env.headers.json 文件加载自定义请求头配置
    """
    try:
        headers_file = os.path.join(os.path.dirname(__file__), "env", ".env.headers.json")
        with open(headers_file, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

CUSTOM_HEADERS = load_custom_headers()


# ===== 请求头过滤 =====

# 定义需要过滤掉的逐跳头（Hop-by-Hop Headers）
REQUEST_HOP_BY_HOP = frozenset({
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'te', 'trailers', 'transfer-encoding', 'upgrade', 'host'
})

def filter_request_headers(incoming_headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    """
    过滤请求头，移除逐跳头和 host 头
    """
    return {k: v for k, v in incoming_headers if k.lower() not in REQUEST_HOP_BY_HOP}


# 定义需要过滤掉的响应逐跳头（Hop-by-Hop Headers）
RESPONSE_HOP_BY_HOP = frozenset({
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'te', 'trailers', 'transfer-encoding', 'upgrade',
    # 这些也需要过滤，否则 FastAPI 可能会报错
    'content-encoding', 'content-length',
})

def filter_response_headers(resp_headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    """
    过滤响应头，移除逐跳头
    """
    return {k: v for k, v in resp_headers if k.lower() not in RESPONSE_HOP_BY_HOP}


# ===== 请求体处理 =====

def process_request_body(body: bytes) -> bytes:
    """
    处理请求体，替换 system prompt
    """
    # 如果未配置替换内容，直接返回
    if SYSTEM_PROMPT_REPLACEMENT is None:
        return body

    # 尝试解析 JSON
    try:
        data = json.loads(body.decode('utf-8'))
        print("[System Replacement] Successfully parsed JSON body")
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"[System Replacement] Failed to parse JSON: {e}, keeping original body")
        return body

    # 检查 system 字段是否存在且为列表
    if "system" not in data:
        print("[System Replacement] No 'system' field found, keeping original body")
        return body

    if not isinstance(data["system"], list):
        print(f"[System Replacement] 'system' field is not a list (type: {type(data['system'])}), keeping original body")
        return body

    if len(data["system"]) == 0:
        print("[System Replacement] 'system' array is empty, keeping original body")
        return body

    # 获取第一个元素
    first_element = data["system"][0]

    # 检查第一个元素是否有 'text' 字段
    if not isinstance(first_element, dict) or "text" not in first_element:
        print(f"[System Replacement] First element doesn't have 'text' field, keeping original body")
        return body

    # 记录原始内容
    original_text = first_element["text"]
    print(f"[System Replacement] Original system[0].text: {original_text[:100]}..." if len(original_text) > 100 else f"[System Replacement] Original system[0].text: {original_text}")

    # 判断是否启用插入模式
    if SYSTEM_PROMPT_BLOCK_INSERT_IF_NOT_EXIST:
        # 插入模式：检查是否包含关键字（忽略大小写）
        if CLAUDE_CODE_KEYWORD.lower() in original_text.lower():
            # 包含关键字：执行替换
            first_element["text"] = SYSTEM_PROMPT_REPLACEMENT
            print(f"[System Replacement] Found '{CLAUDE_CODE_KEYWORD}', replacing with: {SYSTEM_PROMPT_REPLACEMENT[:100]}..." if len(SYSTEM_PROMPT_REPLACEMENT) > 100 else f"[System Replacement] Found '{CLAUDE_CODE_KEYWORD}', replacing with: {SYSTEM_PROMPT_REPLACEMENT}")
        else:
            # 不包含关键字：执行插入
            new_element = {
                "type": "text",
                "text": SYSTEM_PROMPT_REPLACEMENT,
                "cache_control": {
                    "type": "ephemeral"
                }
            }
            data["system"].insert(0, new_element)
            print(f"[System Replacement] '{CLAUDE_CODE_KEYWORD}' not found, inserting at position 0: {SYSTEM_PROMPT_REPLACEMENT[:100]}..." if len(SYSTEM_PROMPT_REPLACEMENT) > 100 else f"[System Replacement] '{CLAUDE_CODE_KEYWORD}' not found, inserting at position 0: {SYSTEM_PROMPT_REPLACEMENT}")
            print(f"[System Replacement] Array length changed: {len(data['system'])-1} -> {len(data['system'])}")
    else:
        # 原始模式：直接替换
        first_element["text"] = SYSTEM_PROMPT_REPLACEMENT
        print(f"[System Replacement] Replaced with: {SYSTEM_PROMPT_REPLACEMENT[:100]}..." if len(SYSTEM_PROMPT_REPLACEMENT) > 100 else f"[System Replacement] Replaced with: {SYSTEM_PROMPT_REPLACEMENT}")

    print(f"[System Replacement] original_text == SYSTEM_PROMPT_REPLACEMENT:{SYSTEM_PROMPT_REPLACEMENT == original_text}")

    # 转换回 JSON bytes
    try:
        # 这里必须加 separators 压缩空格，我也不知道为什么有空格不行。。。
        modified_body = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        print(f"[System Replacement] Successfully modified body (original size: {len(body)} bytes, new size: {len(modified_body)} bytes)")
        return modified_body
    except Exception as e:
        print(f"[System Replacement] Failed to serialize modified JSON: {e}, keeping original body")
        return body


# ===== 健康检查端点 =====

@app.get("/health")
async def health_check():
    """
    健康检查端点，用于容器健康检查和服务状态监控
    不依赖上游服务，仅检查代理服务本身是否正常运行
    """
    return {
        "status": "healthy",
        "service": "anthropic-transparent-proxy"
    }


# ===== 主代理逻辑 =====

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy(path: str, request: Request):
    """
    主代理逻辑：支持多URL和多Key轮换重试
    遇到错误时自动切换URL和API Key，直到成功或全部失败
    """
    
    # 读取 body
    body = await request.body()
    query = request.url.query
    
    # 仅测试环境打印详细日志
    if DEBUG_MODE:
        try:
            data = json.loads(body.decode('utf-8'))
            print(f"[Proxy] Original body ({len(body)} bytes): {json.dumps(data, indent=4)}")
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"[Proxy] Failed to parse JSON: {e}")
    else:
        print(f"[Proxy] Request: {request.method} {path}")
        print(f"[Proxy] Original body ({len(body)} bytes): {body[:200]}..." if len(body) > 200 else f"[Proxy] Original body: {body}")

    # 处理请求体（替换 system prompt）
    # 仅在路由为 /v1/messages 时执行处理
    print(f"[Proxy] Processing request for path: {path}")
    if path == "v1/messages" or path == "v1/messages/":
        body = process_request_body(body)

    # 复制并过滤请求头
    incoming_headers = list(request.headers.items())
    original_forward_headers = filter_request_headers(incoming_headers)

    # 添加 X-Forwarded-For
    client_host = request.client.host if request.client else None
    if client_host:
        existing = original_forward_headers.get("X-Forwarded-For")
        original_forward_headers["X-Forwarded-For"] = f"{existing}, {client_host}" if existing else client_host

    # 注入自定义 Header
    for k, v in CUSTOM_HEADERS.items():
        original_forward_headers[k] = v

    # ===== 智能重试策略 =====
    # 策略：前2次使用完整请求，之后使用探测请求测试连接
    global nonessential_traffic_disabled
    all_errors = []
    total_urls = len(CANDIDATE_URLS)
    total_keys = len(CANDIDATE_API_KEYS)
    
    # 初始化重试上下文
    retry_ctx = RetryContext(body)
    
    # 按使用次数排序的Key列表（从少到多）
    sorted_keys = get_keys_sorted_by_usage()
    
    # 获取可用URL列表（跳过冷静期中的URL）
    available_urls = get_available_urls()
    total_available_urls = len(available_urls)
    
    # 尝试所有可用URL
    for url_idx, current_base_url in enumerate(available_urls):
        print(f"\n[Failover] 尝试 URL {url_idx + 1}/{total_available_urls}: {current_base_url}")
        
        # 获取该URL的WAF cookies
        waf_cache = get_waf_cookies(current_base_url)
        waf_cookies = waf_cache["cookies"]
        
        # 预获取WAF cookie（如果需要）
        if 'anyrouter' in current_base_url or 'cspok' in current_base_url:
            await fetch_waf_cookie(http_client, current_base_url, force=False)
            waf_cookies = waf_cache["cookies"]
        
        # 尝试所有Key（按使用次数从少到多）
        for key_idx, current_api_key in enumerate(sorted_keys):
            key_preview = f"{current_api_key[:10]}...{current_api_key[-4:]}"
            
            # 判断使用哪种请求体
            is_probe = retry_ctx.should_use_probe()
            current_body = retry_ctx.get_request_body()
            
            if is_probe:
                print(f"[Probe] 使用探测请求测试 Key {key_idx + 1}/{total_keys}: {key_preview}")
            else:
                usage_count = key_usage_count.get(current_api_key, 0)
                print(f"[Failover] 尝试 Key {key_idx + 1}/{total_keys}: {key_preview} (完整请求, 使用次数: {usage_count})")
            
            # 记录Key使用
            increment_key_usage(current_api_key)
            
            # 构建本次请求的headers（需要复制，避免污染）
            forward_headers = dict(original_forward_headers)
            
            # 设置 Host
            if not PRESERVE_HOST:
                parsed = urlparse(current_base_url)
                forward_headers["Host"] = parsed.netloc
            
            # 注入当前API Key（覆盖请求中的x-api-key）
            forward_headers["x-api-key"] = current_api_key
            
            # 添加 WAF cookies
            if waf_cookies:
                cookie_str = "; ".join([f"{k}={v}" for k, v in waf_cookies.items()])
                forward_headers["Cookie"] = cookie_str
                if DEBUG_MODE:
                    print(f"[Proxy] Injected WAF cookies: {list(waf_cookies.keys())}")
            
            # 构造目标 URL
            target_url = f"{current_base_url}/{path}"
            if query:
                target_url += f"?{query}"
            
            # 单个URL+Key的重试逻辑（最多2次内部重试）
            for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
                try:
                    # 在每次尝试前确保 WAF cookie 有效
                    if attempt > 1:
                        print(f"[Proxy] 第 {attempt}/{MAX_RETRY_ATTEMPTS} 次重试...")
                        await fetch_waf_cookie(http_client, current_base_url, force=True)
                        waf_cookies = waf_cache["cookies"]
                        if waf_cookies:
                            forward_headers["Cookie"] = "; ".join([f"{k}={v}" for k, v in waf_cookies.items()])
                    
                    # 构建请求
                    req = http_client.build_request(
                        method=request.method,
                        url=target_url,
                        headers=forward_headers,
                        content=current_body,
                    )

                    # 发送请求并开启流式模式
                    resp = await http_client.send(req, stream=True)

                    # ===== 成功响应处理 (2xx, 3xx) =====
                    if resp.status_code < 400:
                        retry_ctx.record_attempt(is_probe, True, current_base_url, current_api_key)
                        
                        if is_probe:
                            # 探测成功，需要重发完整请求
                            print(f"[Probe] ✓ 探测成功，重发完整请求...")
                            await resp.aclose()
                            
                            # 重发完整请求
                            full_req = http_client.build_request(
                                method=request.method,
                                url=target_url,
                                headers=forward_headers,
                                content=retry_ctx.original_body,
                            )
                            full_resp = await http_client.send(full_req, stream=True)
                            
                            if full_resp.status_code >= 400:
                                # 探测成功但完整请求失败
                                retry_ctx.probe_succeeded_but_full_failed = True
                                resp_text = (await full_resp.aread()).decode('utf-8', errors='ignore')
                                await full_resp.aclose()
                                
                                print(f"[Probe] ⚠ 探测成功但完整请求失败 (HTTP {full_resp.status_code})")
                                
                                # 检查是否是内容相关的错误
                                try:
                                    error_data = json.loads(resp_text)
                                    error_type = error_data.get('error', {}).get('type', '')
                                    if error_type in ('invalid_request_error', 'content_policy_violation', 'request_too_large'):
                                        # 内容问题，返回错误给用户
                                        return Response(
                                            content=json.dumps({
                                                "error": {
                                                    "type": "content_error_after_probe",
                                                    "message": "API 连接正常，但请求内容被拒绝。请检查对话内容或缩短上下文。",
                                                    "original_error": error_data.get('error', {})
                                                }
                                            }, ensure_ascii=False),
                                            status_code=full_resp.status_code,
                                            media_type="application/json"
                                        )
                                except:
                                    pass
                                
                                # 其他错误，记录并继续下一个 Key
                                all_errors.append(f"Key={key_preview}: Probe OK but full request failed ({full_resp.status_code})")
                                break
                            
                            # 完整请求也成功，使用 full_resp
                            resp = full_resp
                        
                        # 返回成功响应
                        response_headers = filter_response_headers(resp.headers.items())
                        confirm_cooldown_for_url(current_base_url)
                        print(f"[Proxy] ✓ 请求成功 (URL: {current_base_url}, Key: {key_preview})")

                        async def iter_response():
                            try:
                                async for chunk in resp.aiter_bytes():
                                    yield chunk
                            except Exception as e:
                                if DEBUG_MODE:
                                    print(f"[Stream Error] {e}")

                        return StreamingResponse(
                            iter_response(),
                            status_code=resp.status_code,
                            headers=response_headers,
                            background=BackgroundTask(resp.aclose),
                        )

                    # ===== 错误响应处理 =====
                    resp_body = await resp.aread()
                    resp_text = resp_body.decode('utf-8', errors='ignore')
                    status_code = resp.status_code
                    await resp.aclose()
                    
                    retry_ctx.record_attempt(is_probe, False)
                    
                    # 检测 WAF 挑战
                    if 'var arg1=' in resp_text:
                        print(f"[WAF] 检测到 WAF 挑战 (状态码: {status_code})，刷新 cookie...")
                        new_cookie = solve_acw_challenge(resp_text)
                        if new_cookie:
                            waf_cookies['acw_sc__v2'] = new_cookie
                            print(f"[WAF] 刷新 cookie 成功: {new_cookie[:20]}...")
                        
                        if attempt < MAX_RETRY_ATTEMPTS:
                            await asyncio.sleep(0.5)
                            continue
                        all_errors.append(f"URL={current_base_url}: WAF challenge failed")
                        break
                    
                    # 5xx 错误处理
                    if status_code >= 500:
                        # 检测上游 API 错误
                        if 'application/json' in resp.headers.get('content-type', ''):
                            try:
                                error_data = json.loads(resp_text)
                                error_msg = error_data.get('error', {}).get('message', '')
                                error_type = error_data.get('error', {}).get('type', '')
                                
                                if '负载' in error_msg or 'overload' in error_msg.lower():
                                    print(f"[Proxy] 上游 API 负载已满: {error_msg[:100]}")
                                    all_errors.append(f"URL={current_base_url}: Overloaded")
                                    break
                                
                                if error_type in ('authentication_error', 'invalid_api_key', 'permission_error'):
                                    print(f"[Proxy] API Key 无效: {error_msg[:100]}")
                                    
                                    # 保留 DISABLE_NONESSENTIAL 重试（仅一次，仅在非探测模式）
                                    if not nonessential_traffic_disabled and not is_probe:
                                        nonessential_traffic_disabled = True
                                        os.environ["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
                                        print(f"[Proxy] ⟳ 设置 DISABLE_NONESSENTIAL，重试当前Key...")
                                        retry_ctx.full_context_attempts -= 1  # 这次不计入完整请求次数
                                        continue
                                    
                                    add_pending_cooldown(current_base_url, current_api_key)
                                    all_errors.append(f"Key={key_preview}: Invalid")
                                    break
                            except:
                                pass
                        
                        if attempt < MAX_RETRY_ATTEMPTS:
                            print(f"[Proxy] 上游返回 {status_code}，准备重试...")
                            await asyncio.sleep(0.5)
                            continue
                        
                        all_errors.append(f"URL={current_base_url}: HTTP {status_code}")
                        break

                    # 4xx 错误处理
                    if status_code >= 400:
                        if status_code in (401, 403):
                            try:
                                error_data = json.loads(resp_text)
                                error_type = error_data.get('error', {}).get('type', '')
                                if error_type in ('authentication_error', 'invalid_api_key', 'permission_error'):
                                    print(f"[Proxy] API Key 认证失败，切换下一个Key...")
                                    
                                    # 保留 DISABLE_NONESSENTIAL 重试（仅一次，仅在非探测模式）
                                    if not nonessential_traffic_disabled and not is_probe:
                                        nonessential_traffic_disabled = True
                                        os.environ["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
                                        print(f"[Proxy] ⟳ 设置 DISABLE_NONESSENTIAL，重试当前Key...")
                                        retry_ctx.full_context_attempts -= 1
                                        continue
                                    
                                    add_pending_cooldown(current_base_url, current_api_key)
                                    all_errors.append(f"Key={key_preview}: Auth failed ({status_code})")
                                    break
                            except:
                                pass
                        
                        # 其他 4xx 错误，直接返回给客户端
                        print(f"[Proxy] ✓ 请求完成 (状态码: {status_code})")
                        return Response(
                            content=resp_text,
                            status_code=status_code,
                            headers=dict(filter_response_headers(resp.headers.items()))
                        )

                except httpx.RequestError as e:
                    retry_ctx.record_attempt(is_probe, False)
                    print(f"[Proxy] 请求失败 (尝试 {attempt}/{MAX_RETRY_ATTEMPTS}): {type(e).__name__}: {e}")
                    
                    if attempt < MAX_RETRY_ATTEMPTS:
                        await asyncio.sleep(0.5)
                        continue
                    
                    all_errors.append(f"URL={current_base_url}, Key={key_preview}: {type(e).__name__}")
                    break
    
    # ===== 全部失败 =====
    error_summary = "; ".join(all_errors[-5:]) if all_errors else "Unknown error"
    
    # 特殊提示：探测成功但完整请求失败
    if retry_ctx.probe_succeeded_but_full_failed:
        print(f"[Proxy] ✗ 探测成功但完整请求失败")
        return Response(
            content=json.dumps({
                "error": {
                    "type": "probe_success_full_failed",
                    "message": "API 连接测试成功，但完整请求失败。可能是请求内容过大或触发了内容策略。",
                    "suggestion": "请尝试缩短对话上下文或检查消息内容",
                    "details": all_errors
                }
            }, ensure_ascii=False),
            status_code=502,
            media_type="application/json"
        )
    
    error_msg = f"All {total_urls} URLs and {total_keys} Keys exhausted. Recent errors: {error_summary}"
    print(f"[Proxy] ✗ {error_msg}")
    return Response(
        content=json.dumps({
            "error": {
                "type": "failover_exhausted",
                "message": error_msg,
                "details": all_errors
            }
        }),
        status_code=502,
        media_type="application/json"
    )


if __name__ == "__main__":
    import uvicorn
    # 开发模式启用热重载，生产模式禁用（通过 DEBUG_MODE 环境变量控制）
    uvicorn.run("app:app", host="0.0.0.0", port=PORT, reload=DEBUG_MODE)
