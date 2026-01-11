#!/bin/bash
# ==========================================
# AnyRouter Proxy 增强版一键部署脚本
# 功能：自动检测环境、Clash 代理、生成配置
# ==========================================

set -e  # 遇到错误立即退出

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

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
        timeout 1 bash -c "cat < /dev/null > /dev/tcp/127.0.0.1/$port" 2>/dev/null
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

# ==========================================
# 步骤 1: 环境检查
# ==========================================
echo -e "${CYAN}[1/6] 检查运行环境...${NC}"
echo ""

# 检查 Python
if ! command_exists python3; then
    echo -e "${RED}✗ 错误：未找到 python3${NC}"
    exit 1
fi
PYTHON_PATH=$(find_executable python3)
PYTHON_VERSION=$(python3 --version 2>&1)
echo -e "${GREEN}✓${NC} Python3: $PYTHON_VERSION"
echo -e "  路径: $PYTHON_PATH"

# 检查 Python 依赖
echo ""
echo "检查 Python 依赖包..."
REQUIRED_PACKAGES="fastapi uvicorn httpx python-dotenv requests"
MISSING_PACKAGES=""

for pkg in $REQUIRED_PACKAGES; do
    if ! python3 -c "import ${pkg//-/_}" 2>/dev/null; then
        MISSING_PACKAGES="$MISSING_PACKAGES $pkg"
    fi
done

if [ -n "$MISSING_PACKAGES" ]; then
    echo -e "${YELLOW}⚠ 缺少依赖包:$MISSING_PACKAGES${NC}"
    echo "自动安装中..."
    # 尝试多种安装方式
    if command_exists pip3; then
        pip3 install $MISSING_PACKAGES
    elif command_exists pip; then
        pip install $MISSING_PACKAGES
    else
        python3 -m pip install $MISSING_PACKAGES
    fi
    echo -e "${GREEN}✓${NC} 依赖包安装完成"
else
    echo -e "${GREEN}✓${NC} 所有依赖包已安装"
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

# 检查 Uvicorn
echo ""
UVICORN_PATH=""
if command_exists uvicorn; then
    UVICORN_PATH=$(find_executable uvicorn)
    echo -e "${GREEN}✓${NC} Uvicorn: $UVICORN_PATH"
else
    echo -e "${YELLOW}⚠ 未找到 uvicorn${NC}"
    UVICORN_PATH="$HOME/miniconda3/bin/uvicorn"
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
        if command_exists curl; then
            response=$(curl -s -m 1 "http://127.0.0.1:$port/proxies" 2>/dev/null || echo "")
            if echo "$response" | grep -q "proxies"; then
                CLASH_API_URL="http://127.0.0.1:$port"
                echo -e "${GREEN}✓${NC} 检测到 Clash API: $CLASH_API_URL"
                CLASH_DETECTED=true
                break
            fi
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
        proxies_json=$(curl -s "$CLASH_API_URL/proxies" 2>/dev/null || echo "{}")

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
    echo "  请创建 $SECRETS_FILE 并填写 API_KEYS 和 CANDIDATE_URLS"
    echo "  示例:"
    echo "    CANDIDATE_URLS=https://anyrouter.top,https://other.api.com"
    echo "    API_KEYS=sk-xxx,sk-yyy"
    # 使用默认值（仅 URLs，不包含 Keys）
    SECRETS_URLS="https://anyrouter.top"
    SECRETS_API_KEYS=""
fi

if [ -f "$ENV_FILE" ]; then
    echo -e "${YELLOW}⚠ .env 文件已存在，自动覆盖...${NC}"
fi
SKIP_CONFIG_GENERATION=false

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
#    nano .secrets  # 填写 API_KEYS / CANDIDATE_URLS
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

BASHRC="$HOME/.bashrc"
ALIAS_LINE="alias anyrouter=\"${PYTHON_BIN:-python3} $PROJECT_ROOT/strict_wrapper.py\""

# 检查是否已存在 alias
if grep -q "alias anyrouter=" "$BASHRC" 2>/dev/null; then
    echo "移除旧的 anyrouter alias..."
    sed -i.bak '/alias anyrouter=/d' "$BASHRC"
fi

# 添加新的 alias
echo "$ALIAS_LINE" >> "$BASHRC"
echo -e "${GREEN}✓${NC} 已添加 anyrouter 别名到 $BASHRC"
echo "  执行 'source ~/.bashrc' 或重启终端使其生效"

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
echo "   source ~/.bashrc"
echo ""
echo "2. (可选) 编辑配置文件添加更多 API Keys:"
echo "   nano $SECRETS_FILE"
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
