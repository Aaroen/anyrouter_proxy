#!/bin/bash
# ==========================================
# AnyRouter Proxy 增强版一键部署脚本
# 功能：自动检测环境、Clash 代理、生成配置
# ==========================================

set -Eeuo pipefail  # 遇到错误立即退出（并捕获管道错误）
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

on_error() {
    local exit_code=$?
    local line_no=${1:-"?"}
    echo ""
    echo -e "${RED:-}[Error] 部署脚本执行失败 (exit=${exit_code}, line=${line_no})${NC:-}" >&2
    exit "${exit_code}"
}
trap 'on_error $LINENO' ERR

echo "========================================"
echo "  AnyRouter Proxy 增强版部署脚本"
echo "========================================"
echo ""

# ==========================================
# 辅助函数
# ==========================================

# 检测命令是否存在
command_exists() {
    command -v "$1" &> /dev/null
}

# 检测端口是否监听
check_port() {
    local port=$1
    if command_exists nc; then
        nc -z 127.0.0.1 $port 2>/dev/null
    elif command_exists netstat; then
        netstat -tuln 2>/dev/null | grep -q ":$port "
    else
        # 使用 bash 内置功能
        if command_exists timeout; then
            timeout 1 bash -c "cat < /dev/null > /dev/tcp/127.0.0.1/$port" 2>/dev/null
        else
            bash -c "cat < /dev/null > /dev/tcp/127.0.0.1/$port" 2>/dev/null
        fi
    fi
}

# 查找可执行文件路径
find_executable() {
    local name=$1
    local result=""

    # 方法1: 使用 which
    if command_exists which; then
        result=$(which $name 2>/dev/null || echo "")
        if [ -n "$result" ]; then
            echo "$result"
            return 0
        fi
    fi

    # 方法2: 搜索常见路径
    local common_paths=(
        "$HOME/.npm-global/bin/$name"
        "$HOME/.local/bin/$name"
        "$HOME/miniconda3/bin/$name"
        "$HOME/anaconda3/bin/$name"
        "/usr/local/bin/$name"
        "/usr/bin/$name"
    )

    for path in "${common_paths[@]}"; do
        if [ -x "$path" ]; then
            echo "$path"
            return 0
        fi
    done

    return 1
}

# 检测包管理器
detect_package_manager() {
    if command_exists apt-get; then
        echo "apt"
    elif command_exists dnf; then
        echo "dnf"
    elif command_exists yum; then
        echo "yum"
    elif command_exists pacman; then
        echo "pacman"
    elif command_exists zypper; then
        echo "zypper"
    elif command_exists apk; then
        echo "apk"
    else
        echo "unknown"
    fi
}

# sudo 兼容：root 环境下不需要 sudo
SUDO=""
if [ "$(id -u)" -ne 0 ] && command_exists sudo; then
    SUDO="sudo"
fi

install_system_deps() {
    local pkg_manager="$1"
    shift
    local deps=("$@")

    if [ "${#deps[@]}" -eq 0 ]; then
        return 0
    fi

    if [ "$(id -u)" -ne 0 ] && [ -z "$SUDO" ]; then
        echo -e "${YELLOW}⚠ 需要 root 权限安装系统依赖，但当前没有 sudo；将跳过系统依赖安装${NC}"
        return 1
    fi

    case "$pkg_manager" in
        apt)
            DEBIAN_FRONTEND=noninteractive $SUDO apt-get update -qq > /dev/null 2>&1 || true
            DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y --no-install-recommends "${deps[@]}" > /dev/null 2>&1
            ;;
        dnf)
            $SUDO dnf install -y "${deps[@]}" > /dev/null 2>&1
            ;;
        yum)
            $SUDO yum install -y "${deps[@]}" > /dev/null 2>&1
            ;;
        pacman)
            $SUDO pacman -S --noconfirm "${deps[@]}" > /dev/null 2>&1
            ;;
        zypper)
            $SUDO zypper install -y "${deps[@]}" > /dev/null 2>&1
            ;;
        apk)
            $SUDO apk add --no-cache "${deps[@]}" > /dev/null 2>&1
            ;;
        *)
            return 1
            ;;
    esac
}

http_get() {
    local url="$1"
    local timeout_s="${2:-2}"
    if command_exists curl; then
        curl -fsS -m "$timeout_s" "$url" 2>/dev/null || true
    elif command_exists wget; then
        wget -q -T "$timeout_s" -O - "$url" 2>/dev/null || true
    else
        return 1
    fi
}

# 参数
FORCE_ENV=0
SKIP_ENV=0
NO_ALIAS=0
USE_SYSTEM_PYTHON=0
VENV_DIR_DEFAULT="$PROJECT_ROOT/env/.venv"
VENV_DIR="$VENV_DIR_DEFAULT"

usage() {
    echo "用法: $0 [选项]"
    echo ""
    echo "选项:"
    echo "  --force-env, -f     覆盖已存在的 .env（默认不覆盖）"
    echo "  --skip-env          跳过生成 .env"
    echo "  --no-alias          不修改 shell rc（默认会写入 alias anyrouter=...）"
    echo "  --system-python     不创建 venv，直接使用系统 Python（不推荐）"
    echo "  --venv <path>       指定 venv 目录（默认: $VENV_DIR_DEFAULT）"
    echo "  --help, -h          显示帮助"
}

while [ $# -gt 0 ]; do
    case "${1:-}" in
        --force-env|-f) FORCE_ENV=1 ;;
        --skip-env) SKIP_ENV=1 ;;
        --no-alias) NO_ALIAS=1 ;;
        --system-python) USE_SYSTEM_PYTHON=1 ;;
        --venv)
            shift
            VENV_DIR="${1:-}"
            if [ -z "$VENV_DIR" ]; then
                echo -e "${RED}✗ --venv 需要提供路径${NC}"
                exit 1
            fi
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo -e "${RED}✗ 未知参数: ${1}${NC}"
            echo ""
            usage
            exit 1
            ;;
    esac
    shift
done

# ==========================================
# 步骤 1: 环境检查
# ==========================================
echo -e "${CYAN}[1/6] 检查运行环境...${NC}"
echo ""

# 检查 Python
PKG_MANAGER="$(detect_package_manager)"
echo -e "${GREEN}✓${NC} 包管理器: ${PKG_MANAGER}"

SYSTEM_DEPS=()
if ! command_exists python3; then
    SYSTEM_DEPS+=("python3")
fi

# Debian/Ubuntu 常见：venv/pip 需要单独安装
if [ "$PKG_MANAGER" = "apt" ]; then
    if [ "$USE_SYSTEM_PYTHON" -ne 1 ]; then
        SYSTEM_DEPS+=("python3-venv")
    fi
    SYSTEM_DEPS+=("python3-pip")
fi

# Clash 探测/节点优选需要 curl/jq（缺少则降级）
if ! command_exists curl && ! command_exists wget; then
    SYSTEM_DEPS+=("curl")
fi
if ! command_exists jq; then
    SYSTEM_DEPS+=("jq")
fi

if [ "${#SYSTEM_DEPS[@]}" -gt 0 ] && [ "$PKG_MANAGER" != "unknown" ]; then
    echo -e "${YELLOW}⚠ 尝试安装系统依赖: ${SYSTEM_DEPS[*]}${NC}"
    install_system_deps "$PKG_MANAGER" "${SYSTEM_DEPS[@]}" || true
fi

if ! command_exists python3; then
    echo -e "${RED}✗ 错误：未找到 python3${NC}"
    exit 1
fi

PYTHON_SYS_PATH="$(find_executable python3)"
echo -e "${GREEN}✓${NC} Python3: $(python3 --version 2>&1)"
echo -e "  系统路径: $PYTHON_SYS_PATH"

mkdir -p "$PROJECT_ROOT/env"

if [ "$USE_SYSTEM_PYTHON" -ne 1 ]; then
    echo ""
    echo "正在准备 Python venv..."
    if [ ! -x "$VENV_DIR/bin/python" ]; then
        if ! python3 -m venv "$VENV_DIR" 2>/dev/null; then
            if [ "$PKG_MANAGER" = "apt" ]; then
                echo -e "${YELLOW}⚠ venv 创建失败，尝试安装 python3-venv...${NC}"
                install_system_deps "$PKG_MANAGER" python3-venv || true
            fi
            python3 -m venv "$VENV_DIR"
        fi
    fi

    PYTHON_PATH="$VENV_DIR/bin/python"
    PIP_PATH="$VENV_DIR/bin/pip"
    UVICORN_PATH="$VENV_DIR/bin/uvicorn"

    echo -e "${GREEN}✓${NC} venv: $VENV_DIR"
    echo "安装/更新 Python 依赖（requirements.txt）..."
    "$PIP_PATH" -q install -U pip setuptools wheel
    "$PIP_PATH" -q install -r "$PROJECT_ROOT/requirements.txt"

    # 快速导入测试
    "$PYTHON_PATH" -c "import fastapi,uvicorn,httpx,dotenv,requests" 2>/dev/null
    echo -e "${GREEN}✓${NC} Python 依赖安装完成"
else
    PYTHON_PATH="$PYTHON_SYS_PATH"
    UVICORN_PATH="$(find_executable uvicorn 2>/dev/null || echo "")"
    if [ -z "$UVICORN_PATH" ]; then
        echo -e "${YELLOW}⚠ 未找到 uvicorn（建议不使用 --system-python，改用 venv）${NC}"
    fi
fi

# 检查 Claude CLI
echo ""
CLAUDE_PATH=""
if command_exists claude; then
    CLAUDE_PATH=$(find_executable claude)
    echo -e "${GREEN}✓${NC} Claude CLI: $CLAUDE_PATH"
else
    echo -e "${YELLOW}⚠ 未找到 claude 命令${NC}"
    echo "  将在配置文件中使用默认路径"
    CLAUDE_PATH="/home/$(whoami)/.npm-global/bin/claude"
fi

echo ""
if [ -n "${UVICORN_PATH:-}" ] && [ -x "${UVICORN_PATH:-}" ]; then
    echo -e "${GREEN}✓${NC} Uvicorn: $UVICORN_PATH"
else
    echo -e "${YELLOW}⚠ 未找到可用的 uvicorn 可执行文件${NC}"
    if [ "$USE_SYSTEM_PYTHON" -ne 1 ]; then
        echo -e "${YELLOW}提示：请确认 venv 已正确创建并安装依赖（$VENV_DIR）${NC}"
    fi
fi

echo ""
echo -e "${GREEN}环境检查完成${NC}"
echo ""

# ==========================================
# 步骤 2: Clash 代理检测
# ==========================================
echo -e "${CYAN}[2/6] 检测 Clash 代理...${NC}"
echo ""

CLASH_DETECTED=false
CLASH_API_URL=""
CLASH_PROXY_URL=""
CLASH_NODES=()

# 常见 Clash API 端口
COMMON_CLASH_PORTS=(9090 9091 7890 9097)

for port in "${COMMON_CLASH_PORTS[@]}"; do
    if check_port $port; then
        # 尝试访问 Clash API
        response="$(http_get "http://127.0.0.1:$port/proxies" 1 || true)"
        if echo "$response" | grep -q "proxies"; then
            CLASH_API_URL="http://127.0.0.1:$port"
            echo -e "${GREEN}✓${NC} 检测到 Clash API: $CLASH_API_URL"
            CLASH_DETECTED=true
            break
        fi
    fi
done

if [ "$CLASH_DETECTED" = true ]; then
    # 获取代理端口（通常是 7890）
    if check_port 7890; then
        CLASH_PROXY_URL="http://127.0.0.1:7890"
    elif check_port 7891; then
        CLASH_PROXY_URL="http://127.0.0.1:7891"
    else
        CLASH_PROXY_URL="http://127.0.0.1:7890"  # 默认值
    fi
    echo -e "${GREEN}✓${NC} Clash 代理地址: $CLASH_PROXY_URL"

    # 获取可用节点列表
    echo ""
    echo "正在获取可用节点..."

    if command_exists curl && command_exists jq; then
        # 获取所有代理节点
        proxies_json="$(http_get "$CLASH_API_URL/proxies" 2)"
        proxies_json="${proxies_json:-{}}"

        # 查找选择器（通常是 Proxy、GLOBAL 或 节点选择）
        selector=""
        for name in "Proxy" "GLOBAL" "节点选择" "proxy" "select"; do
            if echo "$proxies_json" | jq -e ".proxies[\"$name\"]" > /dev/null 2>&1; then
                selector="$name"
                break
            fi
        done

        if [ -n "$selector" ]; then
            echo -e "${GREEN}✓${NC} 找到节点选择器: $selector"

            # 获取当前节点
            current_node=$(echo "$proxies_json" | jq -r ".proxies[\"$selector\"].now" 2>/dev/null || echo "")
            if [ -n "$current_node" ] && [ "$current_node" != "null" ]; then
                echo -e "  当前节点: ${BLUE}$current_node${NC}"
            fi

            # 获取所有可用节点
            mapfile -t CLASH_NODES < <(echo "$proxies_json" | jq -r ".proxies[\"$selector\"].all[]" 2>/dev/null)

            if [ ${#CLASH_NODES[@]} -gt 0 ]; then
                echo -e "${GREEN}✓${NC} 找到 ${#CLASH_NODES[@]} 个可用节点"
                echo ""
                echo "正在分析节点延迟..."

                # 自动选择最快节点
                best_node=""
                min_delay=99999

                for node in "${CLASH_NODES[@]}"; do
                    # 跳过选择器类型的节点
                    node_type=$(echo "$proxies_json" | jq -r ".proxies[\"$node\"].type" 2>/dev/null || echo "")
                    if [ "$node_type" = "Selector" ] || [ "$node_type" = "URLTest" ]; then
                        continue
                    fi

                    # 获取节点延迟
                    delay=$(echo "$proxies_json" | jq -r ".proxies[\"$node\"].history[-1].delay" 2>/dev/null || echo "0")

                    # 验证延迟是否有效
                    if [ -n "$delay" ] && [ "$delay" != "null" ] && [ "$delay" != "0" ]; then
                        echo "  $node: ${delay}ms"

                        # 更新最小延迟
                        if [ "$delay" -lt "$min_delay" ]; then
                            min_delay=$delay
                            best_node="$node"
                        fi
                    fi
                done

                # 切换到最快节点
                if [ -n "$best_node" ] && [ "$best_node" != "$current_node" ]; then
                    echo ""
                    echo -e "最快节点: ${BLUE}$best_node${NC} (${GREEN}${min_delay}ms${NC})"
                    echo "正在自动切换..."

                    switch_result=$(curl -s -X PUT "$CLASH_API_URL/proxies/$selector" \
                        -H "Content-Type: application/json" \
                        -d "{\"name\":\"$best_node\"}" 2>/dev/null || echo "")

                    if [ $? -eq 0 ]; then
                        echo -e "${GREEN}✓${NC} 已切换至极速节点: $best_node (${min_delay}ms)"
                    else
                        echo -e "${YELLOW}⚠${NC} 节点切换失败，将使用当前节点"
                    fi
                elif [ -n "$best_node" ] && [ "$best_node" = "$current_node" ]; then
                    echo ""
                    echo -e "${GREEN}✓${NC} 当前已是最快节点: $best_node (${min_delay}ms)"
                else
                    echo ""
                    echo -e "${YELLOW}⚠${NC} 无法获取有效节点延迟，保持当前配置"
                fi
            fi
        fi
    elif command_exists curl; then
        echo -e "${YELLOW}⚠ 未安装 jq，无法解析节点信息${NC}"
        echo "  提示: 安装 jq 可获得更好的节点管理体验"
        echo "  安装命令: sudo apt-get install jq"
    fi

    echo ""
    echo -e "${GREEN}Clash 配置完成${NC}"
else
    echo -e "${YELLOW}⚠ 未检测到 Clash 代理${NC}"
    echo "  如果您使用其他代理，请稍后手动编辑 .env 文件"
    CLASH_API_URL="http://127.0.0.1:9090"
    CLASH_PROXY_URL="http://127.0.0.1:7890"
fi

echo ""

# ==========================================
# 步骤 3: 生成配置文件
# ==========================================
echo -e "${CYAN}[3/6] 生成配置文件...${NC}"
echo ""

ENV_FILE="$PROJECT_ROOT/.env"
SECRETS_FILE="$PROJECT_ROOT/.secrets"

# 从 .secrets 文件读取敏感配置
SECRETS_API_KEYS=""
SECRETS_URLS=""

if [ -f "$SECRETS_FILE" ]; then
    echo -e "${GREEN}✓${NC} 检测到 .secrets 文件，读取敏感配置..."
    # 安全读取（仅支持 KEY=VALUE，不执行任何脚本）
    load_secrets_kv() {
        local file="$1"
        local line key value
        while IFS= read -r line || [ -n "$line" ]; do
            line="${line%$'\r'}"
            case "$line" in
                ""|\#*) continue ;;
            esac
            case "$line" in
                API_KEYS=*|CANDIDATE_URLS=*)
                    key="${line%%=*}"
                    value="${line#*=}"
                    value="${value#[[:space:]]}"
                    value="${value%[[:space:]]}"
                    if [[ ( "$value" == \"*\" && "$value" == *\" ) || ( "$value" == \'*\' && "$value" == *\' ) ]]; then
                        value="${value:1:${#value}-2}"
                    fi
                    export "$key=$value"
                    ;;
            esac
        done < "$file"
    }
    load_secrets_kv "$SECRETS_FILE"
    SECRETS_API_KEYS="${API_KEYS:-}"
    SECRETS_URLS="${CANDIDATE_URLS:-}"
    if [ -n "$SECRETS_API_KEYS" ]; then
        KEY_COUNT=$(echo "$SECRETS_API_KEYS" | tr ',' '\n' | wc -l)
        echo -e "  已加载 ${GREEN}${KEY_COUNT}${NC} 个 API Keys"
    fi
    if [ -n "$SECRETS_URLS" ]; then
        URL_COUNT=$(echo "$SECRETS_URLS" | tr ',' '\n' | wc -l)
        echo -e "  已加载 ${GREEN}${URL_COUNT}${NC} 个 URLs"
    fi
else
    echo -e "${YELLOW}⚠ 未找到 .secrets 文件${NC}"
    echo "  将自动创建示例文件（请手动填写 API_KEYS / CANDIDATE_URLS）"
    if [ -f "$PROJECT_ROOT/.secrets.example" ]; then
        cp "$PROJECT_ROOT/.secrets.example" "$SECRETS_FILE"
    else
        cat > "$SECRETS_FILE" <<EOF
# 仅用于本机部署，请勿提交到 Git
CANDIDATE_URLS=https://anyrouter.top
API_KEYS=
EOF
    fi
    chmod 600 "$SECRETS_FILE" 2>/dev/null || true
    echo "  已创建: $SECRETS_FILE"
    # 使用默认值（仅 URLs，不包含 Keys）
    SECRETS_URLS="https://anyrouter.top"
    SECRETS_API_KEYS=""
fi

SKIP_CONFIG_GENERATION=false
if [ "$SKIP_ENV" -eq 1 ]; then
    SKIP_CONFIG_GENERATION=true
    echo -e "${YELLOW}⚠ 已指定 --skip-env：跳过生成 .env${NC}"
elif [ -f "$ENV_FILE" ] && [ "$FORCE_ENV" -ne 1 ]; then
    SKIP_CONFIG_GENERATION=true
    echo -e "${YELLOW}⚠ .env 已存在，将保留原文件（如需覆盖请使用 --force-env）${NC}"
elif [ -f "$ENV_FILE" ] && [ "$FORCE_ENV" -eq 1 ]; then
    echo -e "${YELLOW}⚠ 将覆盖已存在的 .env（--force-env）${NC}"
fi

if [ "$SKIP_CONFIG_GENERATION" = false ]; then
    echo "正在生成配置文件..."

    # 创建 .env 文件
    cat > "$ENV_FILE" <<EOF
# ==========================================
# AnyRouter Proxy 配置文件
# 自动生成于: $(date '+%Y-%m-%d %H:%M:%S')
# ==========================================

# ===== 项目目录配置 =====
PROXY_DIR=$PROJECT_ROOT

# ===== API 上游配置 =====
# 多个 URL 用逗号分隔，系统会自动选择可用的上游
# 配置来源: .secrets 文件
CANDIDATE_URLS=$SECRETS_URLS

# ===== API Keys 配置 =====
# 多个 Key 用逗号分隔，系统会自动轮换使用
# 配置来源: .secrets 文件（建议不要把 Key 写入 .env）
API_KEYS=

# Key 轮询模式
ENABLE_KEY_ROTATION=true

# ===== 可执行文件路径配置 =====
# Claude Code CLI 路径（自动检测）
CLAUDE_BIN=$CLAUDE_PATH

# Python 解释器路径（自动检测）
PYTHON_BIN=$PYTHON_PATH

# Uvicorn 服务器路径（自动检测）
UVICORN_BIN=$UVICORN_PATH

# ===== Clash 代理配置 =====
# Clash API 地址（自动检测）
CLASH_API=$CLASH_API_URL

# Clash 代理地址（自动检测）
CLASH_PROXY_ADDR=$CLASH_PROXY_URL

# 是否启用 Clash 节点优选
ENABLE_CLASH_OPTIMIZATION=$CLASH_DETECTED

# ===== System Prompt 配置 =====
SYSTEM_PROMPT_REPLACEMENT="You are Claude Code, Anthropic's official CLI for Claude."
SYSTEM_PROMPT_BLOCK_INSERT_IF_NOT_EXIST=false

# ===== 调试配置 =====
DEBUG_MODE=false

# ===== 安全配置 =====
# 是否禁用 Claude Code 非必要流量
# 由 wrapper 自动设置，无需修改
# CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

# ==========================================
# 配置说明
# ==========================================
#
# 1. 必须修改的配置：
#    - API_KEYS: 建议放在 .secrets 文件中（避免写入 .env）
#
# 2. 可能需要修改的配置（根据实际环境）：
#    - CLAUDE_BIN: Claude CLI 安装路径
#    - UVICORN_BIN: Uvicorn 安装路径
#    - CLASH_PROXY_ADDR: 如果使用其他代理地址
#
# 3. 通常无需修改的配置：
#    - PROXY_DIR: 自动检测脚本所在目录
#    - PYTHON_BIN: 自动使用当前 Python
#    - CANDIDATE_URLS: 默认上游列表
#    - SYSTEM_PROMPT_REPLACEMENT: 推荐保持默认值
#
# 4. 快速开始：
#    # 创建并填写 .secrets（敏感信息）
#    编辑 .secrets  # 填写 API_KEYS / CANDIDATE_URLS
#    ./deploy.sh  # 执行部署
#
# 5. 验证配置：
#    # 启用调试模式查看加载的配置
#    DEBUG_MODE=true python3 strict_wrapper.py
#
# ==========================================
EOF

    chmod 600 "$ENV_FILE"
    echo -e "${GREEN}✓${NC} 配置文件已生成: $ENV_FILE"

    # 显示配置摘要
    echo ""
    echo "配置摘要:"
    echo "  项目目录: $PROJECT_ROOT"
    echo "  Claude CLI: $CLAUDE_PATH"
    echo "  Python: $PYTHON_PATH"
    echo "  Uvicorn: $UVICORN_PATH"
    if [ "$CLASH_DETECTED" = true ]; then
        echo "  Clash API: $CLASH_API_URL"
        echo "  Clash 代理: $CLASH_PROXY_URL"
    fi
fi

# 加载配置
if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
fi

echo ""

# ==========================================
# 步骤 4: 目录结构检查
# ==========================================
echo -e "${CYAN}[4/6] 检查目录结构...${NC}"
echo ""

mkdir -p "$PROJECT_ROOT/env"
mkdir -p "$PROJECT_ROOT/tests"
mkdir -p "$PROJECT_ROOT/deprecated"
mkdir -p "$PROJECT_ROOT/logs"

# headers 文件为可选配置：不存在则自动创建空 JSON
HEADERS_FILE="$PROJECT_ROOT/env/.env.headers.json"
if [ ! -f "$HEADERS_FILE" ]; then
    cat > "$HEADERS_FILE" <<EOF
{}
EOF
    chmod 600 "$HEADERS_FILE" 2>/dev/null || true
    echo -e "${YELLOW}⚠${NC} 未找到 env/.env.headers.json，已自动创建空配置"
fi

echo -e "${GREEN}✓${NC} 目录结构完整"
echo ""

# ==========================================
# 步骤 5: 设置 Bash Alias
# ==========================================
echo -e "${CYAN}[5/6] 配置 anyrouter 命令别名...${NC}"
echo ""

if [ "$NO_ALIAS" -eq 1 ]; then
    echo -e "${YELLOW}⚠ 已指定 --no-alias：跳过写入 shell rc${NC}"
else
    SHELL_RC="$HOME/.bashrc"
    if [ -n "${SHELL:-}" ] && echo "$SHELL" | grep -qi "zsh"; then
        SHELL_RC="$HOME/.zshrc"
    fi
    touch "$SHELL_RC" 2>/dev/null || true

    # 优先使用本次部署计算得到的 PYTHON_PATH（venv），避免 .env 已存在时指向系统 Python
    ALIAS_PY="${PYTHON_PATH:-${PYTHON_BIN:-python3}}"
    ALIAS_LINE="alias anyrouter=\"${ALIAS_PY} $PROJECT_ROOT/strict_wrapper.py\""

    ALIAS_BEGIN="# >>> anyrouter_proxy (managed) >>>"
    ALIAS_END="# <<< anyrouter_proxy (managed) <<<"

    # 清理旧配置块
    if grep -q "$ALIAS_BEGIN" "$SHELL_RC" 2>/dev/null; then
        sed -i.bak "/$ALIAS_BEGIN/,/$ALIAS_END/d" "$SHELL_RC" 2>/dev/null || true
    fi

    {
        echo ""
        echo "$ALIAS_BEGIN"
        echo "$ALIAS_LINE"
        echo "$ALIAS_END"
    } >> "$SHELL_RC"

    echo -e "${GREEN}✓${NC} 已添加 anyrouter 别名到 $SHELL_RC"
    echo "  执行 'source $SHELL_RC' 或重启终端使其生效"
fi

echo ""

# ==========================================
# 步骤 6: 验证部署
# ==========================================
echo -e "${CYAN}[6/6] 验证部署...${NC}"
echo ""

# 检查核心文件
CORE_FILES=("app.py" "strict_wrapper.py")
OPTIONAL_FILES=("env/.env.headers.json")
ALL_FILES_OK=true

for file in "${CORE_FILES[@]}"; do
    if [ ! -f "$PROJECT_ROOT/$file" ]; then
        echo -e "${RED}✗${NC} 缺少核心文件: $file"
        ALL_FILES_OK=false
    else
        echo -e "${GREEN}✓${NC} 核心文件存在: $file"
    fi
done

for file in "${OPTIONAL_FILES[@]}"; do
    if [ ! -f "$PROJECT_ROOT/$file" ]; then
        echo -e "${YELLOW}⚠${NC} 可选文件缺失（可忽略）: $file"
    else
        echo -e "${GREEN}✓${NC} 可选文件存在: $file"
    fi
done

if [ "$ALL_FILES_OK" = false ]; then
    echo ""
    echo -e "${RED}错误：部分核心文件缺失${NC}"
    exit 1
fi

echo ""
echo "========================================"
echo -e "${GREEN}✓ 部署完成！${NC}"
echo "========================================"
echo ""
echo "下一步操作:"
echo ""
echo "1. 激活配置:"
echo "   source ~/.bashrc  # 或 source ~/.zshrc"
echo ""
echo "2. (可选) 编辑配置文件添加更多 API Keys:"
echo "   $SECRETS_FILE"
echo ""
echo "3. 启动 Claude Code:"
echo "   claude"
echo ""
echo "测试命令:"
echo "   cd $PROJECT_ROOT/tests"
echo "   python3 verify_integration.py"
echo ""
echo "配置文件位置:"
echo "   $ENV_FILE"
echo ""
echo "日志文件位置:"
echo "   $PROJECT_ROOT/logs/"
echo ""

if [ -z "${SECRETS_API_KEYS:-}" ] || [ -z "${SECRETS_URLS:-}" ]; then
    echo -e "${YELLOW}⚠ 未检测到完整配置（API_KEYS/CANDIDATE_URLS）${NC}"
    echo "  1) 编辑: $SECRETS_FILE"
    echo "  2) 填写: API_KEYS=..., CANDIDATE_URLS=..."
    echo "  3) 重新运行: $PROJECT_ROOT/deploy.sh"
    echo ""
fi

if [ "$CLASH_DETECTED" = true ]; then
    echo -e "${CYAN}Clash 代理状态: 已启用${NC}"
    echo "  API: $CLASH_API_URL"
    echo "  代理: $CLASH_PROXY_URL"
    echo ""
fi

echo "========================================"
echo ""
