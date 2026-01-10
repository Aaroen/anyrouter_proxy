#!/usr/bin/env python3
"""
Claude Code Wrapper - 配置文件版
支持从 .env 文件读取配置，提高可移植性和安全性
"""
import os
import sys
import subprocess
import requests
import random
import time
import socket
import signal
import atexit
import json
import re
import hashlib
from pathlib import Path

# ==========================================
# 配置加载
# ==========================================

# 获取脚本所在目录（支持相对路径）
SCRIPT_DIR = Path(__file__).parent.absolute()

# 尝试加载 .env 文件（非敏感配置）
try:
    from dotenv import load_dotenv
    env_file = SCRIPT_DIR / ".env"
    if env_file.exists():
        load_dotenv(env_file)
        print(f"\033[90m[Config] 从 {env_file} 加载配置\033[0m")
    else:
        print(f"\033[90m[Config] 未找到 .env 文件，使用默认配置\033[0m")
except ImportError:
    print("\033[90m[Config] python-dotenv 未安装，使用默认配置\033[0m")

def _load_kv_file_into_environ(path: Path, override: bool = True) -> None:
    """
    以“KEY=VALUE”格式读取配置并写入环境变量。
    用于加载 .secrets 这类敏感文件，避免把 Key 写进可提交的配置文件。
    """
    if not path.exists():
        return
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            key = k.strip()
            value = v.strip()
            if not key:
                continue
            if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]
            if not override and key in os.environ:
                continue
            os.environ[key] = value
        print(f"\033[90m[Config] 从 {path} 加载敏感配置\033[0m")
    except Exception as e:
        if os.getenv("DEBUG_MODE", "false").lower() in ("true", "1", "yes"):
            print(f"\033[90m[Config] 加载 {path} 失败: {e}\033[0m")

# 加载敏感配置（优先级高于 .env）
_load_kv_file_into_environ(SCRIPT_DIR / ".secrets", override=True)

# ==========================================
# 配置项（优先从环境变量读取，否则使用默认值）
# ==========================================

# 项目目录（默认为脚本所在目录）
PROXY_DIR = os.getenv("PROXY_DIR", str(SCRIPT_DIR))
PROXY_APP_MODULE = "app:app"

# API 上游 URLs（从环境变量读取，支持逗号分隔）
CANDIDATE_URLS_STR = os.getenv(
    "CANDIDATE_URLS",
    "https://anyrouter.top"  # 默认仅使用 anyrouter.top
)
CANDIDATE_URLS = [url.strip() for url in CANDIDATE_URLS_STR.split(",") if url.strip()]

# API Keys（从环境变量读取，支持逗号分隔）
KEYS_STR = os.getenv(
    "API_KEYS",
    ""  # 不提供默认 Key，避免泄露风险；请在 .secrets 或环境变量中配置
)
KEYS = [key.strip() for key in KEYS_STR.split(",") if key.strip()]

# Key 轮询配置
ENABLE_KEY_ROTATION = os.getenv("ENABLE_KEY_ROTATION", "true").lower() in ("true", "1", "yes")
KEY_USAGE_STATS_FILE = SCRIPT_DIR / ".key_usage_stats.json"

def _key_id(key: str) -> str:
    # 使用短 hash 作为持久化标识，避免把明文 key 写入磁盘
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

def _normalize_key_stats(raw: dict) -> dict[str, int]:
    """
    将历史统计数据规范化为 {key_id: count}。
    兼容旧格式（直接用明文 key 作为 dict key）。
    """
    normalized: dict[str, int] = {}
    if not isinstance(raw, dict):
        return normalized
    for k, v in raw.items():
        try:
            count = int(v)
        except Exception:
            continue
        if isinstance(k, str) and k in KEYS:
            kid = _key_id(k)
        elif isinstance(k, str) and re.fullmatch(r"[0-9a-f]{16}", k):
            kid = k
        else:
            continue
        normalized[kid] = normalized.get(kid, 0) + max(0, count)
    return normalized

# 可执行文件路径
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "/home/aroen/.npm-global/bin/claude")
PYTHON_BIN = os.getenv("PYTHON_BIN", sys.executable)
UVICORN_BIN = os.getenv("UVICORN_BIN", "/home/aroen/miniconda3/bin/uvicorn")

# Clash 配置
CLASH_API = os.getenv("CLASH_API", "http://127.0.0.1:9090")
CLASH_PROXY_ADDR = os.getenv("CLASH_PROXY_ADDR", "http://127.0.0.1:7890")

# System Prompt 替换
SYSTEM_PROMPT_REPLACEMENT = os.getenv(
    "SYSTEM_PROMPT_REPLACEMENT",
    "You are Claude Code, Anthropic's official CLI for Claude."
)

# 是否启用 Clash 优选
ENABLE_CLASH_OPTIMIZATION = os.getenv("ENABLE_CLASH_OPTIMIZATION", "true").lower() in ("true", "1", "yes")

# 调试模式
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() in ("true", "1", "yes")

# ==========================================
# 配置验证和调试输出
# ==========================================

if DEBUG_MODE:
    print("\n" + "=" * 60)
    print("配置信息:")
    print(f"  PROXY_DIR: {PROXY_DIR}")
    print(f"  CANDIDATE_URLS: {len(CANDIDATE_URLS)} 个")
    for i, url in enumerate(CANDIDATE_URLS, 1):
        print(f"    {i}. {url}")
    print(f"  API_KEYS: {len(KEYS)} 个")
    print(f"  CLAUDE_BIN: {CLAUDE_BIN}")
    print(f"  PYTHON_BIN: {PYTHON_BIN}")
    print(f"  UVICORN_BIN: {UVICORN_BIN}")
    print(f"  CLASH_API: {CLASH_API}")
    print(f"  CLASH_PROXY_ADDR: {CLASH_PROXY_ADDR}")
    print(f"  ENABLE_CLASH_OPTIMIZATION: {ENABLE_CLASH_OPTIMIZATION}")
    print("=" * 60 + "\n")

# 验证关键配置
if not KEYS:
    print("\033[31m[Error] 未配置 API_KEYS，请检查 .env 文件\033[0m")
    sys.exit(1)

if not CANDIDATE_URLS:
    print("\033[31m[Error] 未配置 CANDIDATE_URLS，请检查 .env 文件\033[0m")
    sys.exit(1)

# 全局进程引用，用于清理
proxy_process = None

def cleanup():
    """清理后台代理进程"""
    if proxy_process:
        try:
            os.killpg(os.getpgid(proxy_process.pid), signal.SIGTERM)
        except:
            pass

atexit.register(cleanup)


def load_key_usage_stats():
    """加载 Key 使用统计"""
    try:
        if KEY_USAGE_STATS_FILE.exists():
            with open(KEY_USAGE_STATS_FILE, 'r') as f:
                import json
                raw = json.load(f)
                return _normalize_key_stats(raw)
        return {}
    except:
        return {}


def save_key_usage_stats(stats):
    """保存 Key 使用统计"""
    try:
        import json
        # 持久化时只保存 key_id，避免泄露明文 key
        with open(KEY_USAGE_STATS_FILE, 'w') as f:
            json.dump(stats, f, indent=2)
    except Exception as e:
        if DEBUG_MODE:
            print(f"[Key Stats] 保存统计失败: {e}")


def display_key_usage_stats(stats):
    """显示 Key 使用统计"""
    print("\n" + "=" * 60)
    print("API Key 使用统计:")

    # 为每个配置的 key 生成显示信息
    for i, key in enumerate(KEYS, 1):
        kid = _key_id(key)
        count = stats.get(kid, 0)
        print(f"  Key #{i}: {kid}  使用次数: {count}")

    print("=" * 60 + "\n")


def get_next_key():
    """
    基于使用次数轮询获取下一个 API Key
    策略：优先选择使用次数最少的 Key，实现负载均衡
    """
    if not ENABLE_KEY_ROTATION:
        # 随机模式（禁用轮询）
        return random.choice(KEYS)

    # 加载使用统计
    stats = load_key_usage_stats()

    # 不再显示统计信息，app.py已实现动态轮换
    if DEBUG_MODE:
        display_key_usage_stats(stats)

    # 找到使用次数最少的 key
    min_count = float('inf')
    selected_key = KEYS[0]

    for key in KEYS:
        count = stats.get(_key_id(key), 0)
        if count < min_count:
            min_count = count
            selected_key = key

    # 更新统计
    selected_key_id = _key_id(selected_key)
    stats[selected_key_id] = stats.get(selected_key_id, 0) + 1

    # 保存统计
    save_key_usage_stats(stats)

    # 仅在调试模式下显示选择信息
    if DEBUG_MODE:
        key_index = KEYS.index(selected_key) + 1
        print(f"\033[32m[Key Selection] 选择 Key #{key_index} ({selected_key_id}, 使用次数: {min_count} -> {stats[selected_key_id]})\033[0m\n")

    return selected_key


def get_free_port():
    """获取一个随机空闲端口"""
    with socket.socket() as s:
        s.bind(('', 0))
        return s.getsockname()[1]

def wait_for_port(port, timeout=5):
    """等待端口 ready"""
    start_time = time.time()
    while time.time() - start_time < timeout:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('127.0.0.1', port)) == 0:
                return True
        time.sleep(0.1)
    return False

def optimize_clash():
    """Clash 优选逻辑"""
    if not ENABLE_CLASH_OPTIMIZATION:
        return

    print("\033[90m[Wrapper] 正在优化网络线路 (Clash)...\033[0m")
    try:
        resp = requests.get(f"{CLASH_API}/proxies", timeout=1)
        if resp.status_code != 200: return
        proxies = resp.json().get('proxies', {})

        selector = next((n for n in ["Proxy", "GLOBAL", "节点选择"] if n in proxies), None)
        if not selector: return

        candidates = proxies[selector]['all']
        best_node = None
        min_delay = 99999

        for node in candidates:
            if node not in proxies or proxies[node]['type'] in ['Selector', 'URLTest']: continue
            history = proxies[node].get('history', [])
            delay = history[-1]['delay'] if history else 0
            if 0 < delay < min_delay:
                min_delay = delay
                best_node = node

        if best_node and proxies[selector].get('now') != best_node:
            requests.put(f"{CLASH_API}/proxies/{selector}", json={"name": best_node}, timeout=1)
            print(f"\033[32m[Wrapper] 已切换至极速节点: {best_node} ({min_delay}ms)\033[0m")
    except:
        pass

def solve_acw_challenge(html_content):
    """
    从阿里云 WAF 挑战页面中计算 acw_sc__v2 cookie 值
    复制自 app.py
    """
    import base64
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


def get_waf_cookie(url):
    """获取 WAF cookie（阿里云 WAF 挑战求解）"""
    try:
        resp = requests.get(
            url + "/",
            proxies={"http": CLASH_PROXY_ADDR, "https": CLASH_PROXY_ADDR},
            timeout=10
        )
        
        if 'acw_sc__v2' in resp.text or "var arg1=" in resp.text:
            cookie_value = solve_acw_challenge(resp.text)
            if cookie_value:
                cookies = dict(resp.cookies)
                cookies['acw_sc__v2'] = cookie_value
                return cookies
        else:
            # 无需 WAF 挑战
            return dict(resp.cookies) if resp.cookies else {}
    except Exception as e:
        if DEBUG_MODE:
            print(f"  [WAF] 获取 cookie 失败: {e}")
        return None


def test_url(url, test_key):
    """
    测试单个URL的可用性和延迟 (带 WAF 支持)
    
    测试策略：
    1. 先获取 WAF cookie（如果需要）
    2. 发送测试请求
    3. 检查响应是否为有效 JSON（排除 WAF 挑战页面）
    4. 能返回有效 JSON 就认为服务可用（包括临时性错误如模型负载过高）
    """
    try:
        # 获取 WAF cookie
        cookies = {}
        if 'anyrouter' in url or 'cspok' in url:
            waf_cookies = get_waf_cookie(url)
            if waf_cookies:
                cookies = waf_cookies
        
        start_time = time.time()
        resp = requests.post(
            f"{url.rstrip('/')}/v1/messages",
            headers={
                "x-api-key": test_key, 
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json"
            },
            # 使用与实际对话相同的模型测试
            json={"model": "claude-sonnet-4-5-20250929", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]},
            cookies=cookies,
            proxies={"http": CLASH_PROXY_ADDR, "https": CLASH_PROXY_ADDR},
            timeout=10
        )
        
        latency = (time.time() - start_time) * 1000
        
        # 检查是否收到 WAF 挑战页面（HTML 而非 JSON）
        content_type = resp.headers.get('content-type', '')
        if 'text/html' in content_type:
            # 收到 HTML 页面，可能是 WAF 挑战
            if 'acw_sc__v2' in resp.text or 'var arg1=' in resp.text:
                return None, "WAF challenge"
            # 其他 HTML 页面也视为异常
            return None, "HTML response"
        
        # 尝试解析 JSON 验证响应格式
        try:
            resp_data = resp.json()
            # 有效的 JSON 响应表示服务在线（无论状态码）
            # 包括：
            # - 200: 正常响应
            # - 400: 请求格式错误（服务在线）
            # - 401: 认证失败（服务在线，可能 key 过期）
            # - 403: 禁止访问（服务在线）
            # - 429: 速率限制（服务在线，暂时限流）
            # - 500 + JSON: 模型负载过高等临时错误（服务在线）
            return latency, None
        except:
            # 无法解析 JSON
            if resp.status_code >= 500:
                return None, f"Server Error {resp.status_code}"
            return None, "Invalid JSON"
            
    except requests.exceptions.Timeout:
        return None, "Timeout"
    except requests.exceptions.ConnectionError:
        return None, "Connection failed"
    except Exception as e:
        return None, str(e)[:30]

def select_best_upstream():
    """
    测试并排序所有 API 上游（按延迟从小到大）
    加载冷静期文件，跳过冷静期中的URL
    """
    test_key = KEYS[0] if KEYS else None
    if not test_key:
        return CANDIDATE_URLS, False
    
    # 加载冷静期文件
    cooldown_file = SCRIPT_DIR / "cooldown_state.json"
    url_cooldowns = {}
    try:
        if cooldown_file.exists():
            with open(cooldown_file, 'r') as f:
                data = json.load(f)
                url_cooldowns = data.get("urls", {})
    except:
        pass
    
    now = time.time()
    
    # 过滤可用URL（不在冷静期）
    available_urls = []
    skipped_urls = []
    for url in CANDIDATE_URLS:
        if url in url_cooldowns and url_cooldowns[url] > now:
            remaining_hours = (url_cooldowns[url] - now) / 3600
            skipped_urls.append((url, remaining_hours))
        else:
            available_urls.append(url)
    
    print(f"\n[Wrapper] 正在测试 {len(available_urls)}/{len(CANDIDATE_URLS)} 个上游 API...")
    
    # 显示冷静期中的URL
    for url, hours in skipped_urls:
        print(f"  - {url} (暂不可用)")
    
    # 如果所有URL都在冷静期，清空冷静期并重新测试所有URL
    if not available_urls:
        print(f"  ⚠ 所有URL均在冷静期，正在清空冷静期并重新测试...")
        # 清空冷静期
        url_cooldowns = {}
        try:
            cooldown_file = SCRIPT_DIR / "cooldown_state.json"
            existing_data = {"keys": {}, "urls": {}}
            if cooldown_file.exists():
                with open(cooldown_file, 'r') as f:
                    existing_data = json.load(f)
            existing_data["urls"] = {}  # 清空URL冷静期
            with open(cooldown_file, 'w') as f:
                json.dump(existing_data, f, indent=2)
            print(f"  ✓ 已清空所有URL冷静期")
        except Exception as e:
            print(f"  ✗ 清空冷静期失败: {e}")
        # 重新测试所有URL
        available_urls = CANDIDATE_URLS.copy()
        skipped_urls = []  # 清空跳过列表
    
    results = []  # [(latency, url, need_disable), ...]
    failed_urls = []  # 测试失败的URL
    
    for url in available_urls:
        # 第一次测试
        latency, error = test_url(url, test_key)
        
        if latency is not None:
            results.append((latency, url, False))
            print(f"  ✓ {latency:.0f}ms\t{url}")
        else:
            # 重试一次（静默）
            latency2, error2 = test_url(url, test_key)
            
            if latency2 is not None:
                results.append((latency2, url, True))
                print(f"  ✓ {latency2:.0f}ms\t{url} (重试成功)")
            else:
                print(f"  ✗ {url}")
                results.append((999999, url, False))
                failed_urls.append(url)  # 记录失败的URL
    
    # 保存失败URL的冷静期
    if failed_urls:
        URL_COOLDOWN_HOURS = 72
        cooldown_end = now + (URL_COOLDOWN_HOURS * 3600)
        for url in failed_urls:
            url_cooldowns[url] = cooldown_end
        # 保存到文件
        try:
            # 先读取现有数据
            existing_data = {"keys": {}, "urls": {}}
            if cooldown_file.exists():
                with open(cooldown_file, 'r') as f:
                    existing_data = json.load(f)
            # 更新URL冷静期
            existing_data["urls"].update(url_cooldowns)
            with open(cooldown_file, 'w') as f:
                json.dump(existing_data, f, indent=2)
        except:
            pass
    
    # 按延迟排序，但 anyrouter 优先
    # 排序规则：(不是anyrouter, 延迟) - anyrouter 会排在前面
    results.sort(key=lambda x: (0 if 'anyrouter' in x[1] else 1, x[0]))
    sorted_urls = [r[1] for r in results]
    
    # 将跳过的URL追加到末尾（也优先 anyrouter）
    skipped_sorted = sorted(skipped_urls, key=lambda x: (0 if 'anyrouter' in x[0] else 1, x[1]))
    for url, _ in skipped_sorted:
        if url not in sorted_urls:
            sorted_urls.append(url)
    
    best_url = sorted_urls[0] if sorted_urls else CANDIDATE_URLS[0]
    best_latency = results[0][0] if results else 999999
    best_need_disable = results[0][2] if results else False
    
    if best_latency < 999999:
        anyrouter_note = " (anyrouter 优先)" if 'anyrouter' in best_url else ""
        print(f"[Wrapper] 最佳上游: {best_url} ({best_latency:.0f}ms){anyrouter_note}")
        if best_need_disable:
            print(f"[Wrapper] ⚠ 需要设置 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1")
    else:
        print(f"[Wrapper] 所有上游均测试失败，使用默认顺序")
    
    print()
    return sorted_urls, best_need_disable

def main():
    global proxy_process

    # 1. 优化 Clash
    optimize_clash()

    # 2. 测试所有上游并排序
    sorted_urls, need_disable_nonessential = select_best_upstream()
    best_url = sorted_urls[0]
    
    # 更新候选URL列表字符串（排序后）
    sorted_urls_str = ",".join(sorted_urls)

    proxy_port = get_free_port()

    # 准备给子进程 (Proxy) 的环境变量
    proxy_env = os.environ.copy()
    proxy_env["API_BASE_URL"] = best_url
    proxy_env["CANDIDATE_URLS"] = sorted_urls_str  # 传递排序后的候选列表给 app.py
    proxy_env["PORT"] = str(proxy_port)
    proxy_env["HTTP_PROXY"] = CLASH_PROXY_ADDR
    proxy_env["HTTPS_PROXY"] = CLASH_PROXY_ADDR
    proxy_env["SYSTEM_PROMPT_REPLACEMENT"] = SYSTEM_PROMPT_REPLACEMENT
    
    # 如果需要禁用非必要流量（第二轮测试才成功），自动设置环境变量
    if need_disable_nonessential:
        proxy_env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        print(f"\033[33m[Wrapper] 已自动设置 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1\033[0m")

    # 3. 启动原版 app.py (通过 uvicorn)
    print(f"\033[90m[Wrapper] 启动本地透明代理 (Port {proxy_port})...\033[0m")

    # 优先使用配置的 uvicorn，否则尝试自动查找
    uvicorn_cmd = UVICORN_BIN
    if not os.path.exists(uvicorn_cmd):
        # 尝试从 PATH 查找
        uvicorn_in_path = subprocess.run(
            ["which", "uvicorn"],
            capture_output=True,
            text=True
        ).stdout.strip()

        if uvicorn_in_path:
            uvicorn_cmd = uvicorn_in_path
        else:
            print("\033[31m[Error] 未找到 uvicorn，请检查 UVICORN_BIN 配置\033[0m")
            sys.exit(1)

    cmd = [
        uvicorn_cmd,
        "app:app",
        "--host", "127.0.0.1",
        "--port", str(proxy_port),
        "--log-level", "error"  # 减少噪音
    ]

    proxy_process = subprocess.Popen(
        cmd,
        cwd=PROXY_DIR,
        env=proxy_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid
    )

    if not wait_for_port(proxy_port):
        print("\033[31m[Error] Failed to start local proxy server.\033[0m")
        sys.exit(1)

    print("\033[32m[Wrapper] 代理就绪。启动 Claude Code...\033[0m")
    print("-" * 50)

    # 4. 启动 Claude
    claude_env = os.environ.copy()
    claude_env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{proxy_port}"
    claude_env["ANTHROPIC_API_KEY"] = get_next_key()  # 使用智能 Key 轮询
    claude_env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"  # 禁用非必要流量

    # 清理旧 Token 变量
    if "ANTHROPIC_AUTH_TOKEN" in claude_env:
        del claude_env["ANTHROPIC_AUTH_TOKEN"]

    # 检查 Claude CLI 是否存在
    if not os.path.exists(CLAUDE_BIN):
        print(f"\033[31m[Error] 未找到 Claude CLI: {CLAUDE_BIN}\033[0m")
        print("请检查 CLAUDE_BIN 配置或安装 Claude Code CLI")
        cleanup()
        sys.exit(1)

    claude_cmd = [CLAUDE_BIN] + sys.argv[1:]

    try:
        # 移交前台控制权给 Claude
        p = subprocess.Popen(claude_cmd, env=claude_env)
        p.wait()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Error: {e}")
    finally:
        cleanup()

if __name__ == "__main__":
    main()
