# MoviePilot 插件：OpenRouter 免费模型助手

自动从 **OpenRouter** 拉取免费（零价）模型清单，并把选中的模型**一键填充到 MoviePilot 智能助手（LLM）配置**。

- 插件 ID：`OpenRouterHelper`
- 版本：`v1.0.0`
- 依赖：`requests`、`python-dotenv`（MoviePilot 均已自带）

---

## 1. 目录结构

```
MoviePilot-OpenRouterPlugin/
├── install.sh / install.ps1                 # 一键安装脚本（Linux/NAS · Windows）
├── package.v2.json                          # 插件市场索引（MoviePilot v2）
├── package.v3.json                          # 插件市场索引（MoviePilot v3）
├── icons/
│   └── openrouterhelper.png                 # 插件图标
├── plugins.v2/openrouterhelper/             # v2 插件本体
│   ├── __init__.py                          #   目录名 = 类名小写
│   └── requirements.txt
├── plugins.v3/openrouterhelper/             # v3 插件本体（内容同上，导入路径兼容）
├── dist/
│   ├── openrouterhelper-1.0.0.zip           # 手动安装包（解压即用）
│   └── MoviePilot-OpenRouterPlugin-repo.zip # 整仓库包（可直接推 GitHub）
└── dev/                                     # 开发自检（不部署）
    ├── selftest.py                          # 43 项离线自检
    ├── or_models.sample.json                # 真实抓取的 OpenRouter 目录快照
    └── _stubs/                              # 模拟 MoviePilot 宿主的桩模块
```

> **关键路径（源码核实）**：MoviePilot 从 `{ROOT_PATH}/app/plugins/{插件类名小写}/` 加载插件
> （见 `app/helper/plugin.py` 的 `install()` 与 `app/core/plugin.py` 的 `is_plugin_exists()`）。
> Docker 部署下 ROOT_PATH 为 `/app`，即 **`/app/app/plugins/openrouterhelper/`**——
> 注意是 `app` 两层。目录名必须是类名 `OpenRouterHelper` 的小写形式，大小写不符会加载失败。

---

## 2. 安装

三条路任选一条。**方式 A 最快**，方式 B 适合不想跑脚本的，方式 C 适合长期维护。

### 方式 A：一键脚本（推荐）

把整个项目目录拷到**能执行 docker 命令的机器**（跑 MoviePilot 容器的宿主机 / NAS），然后：

```bash
# Linux / NAS / Unraid
cd MoviePilot-OpenRouterPlugin
chmod +x install.sh
./install.sh                # 默认容器名 moviepilot
./install.sh my-mp          # 容器名不同就传进去
```

```powershell
# Windows（Docker Desktop / WSL2），在 PowerShell 中执行
cd MoviePilot-OpenRouterPlugin
.\install.ps1                       # 默认容器名 moviepilot
.\install.ps1 -Container my-mp
# 若报「禁止运行脚本」，先执行：
#   Set-ExecutionPolicy -Scope Process Bypass
```

脚本会：定位插件源 → 备份旧版本 → 写入容器 `/app/app/plugins/openrouterhelper/`
→ 图标同时写入 `plugins/icons/` 与插件目录内 → 校验文件已就位 → 重启容器。
**可重复执行**（幂等），也用来升级。源码部署改用 `--local`：`./install.sh --local /path/to/MoviePilot`。

### 方式 B：手动复制

```bash
# 1) 解压安装包，得到 openrouterhelper/ 目录
unzip dist/openrouterhelper-1.0.0.zip

# 2) Docker：拷进容器（注意是 app 两层）
docker cp openrouterhelper <容器名>:/app/app/plugins/
docker cp icons/openrouterhelper.png <容器名>:/app/app/plugins/icons/
docker restart <容器名>

# 3) 源码 / CLI 部署：直接放进项目目录
cp -r openrouterhelper /path/to/MoviePilot/app/plugins/
cp icons/openrouterhelper.png /path/to/MoviePilot/app/plugins/icons/
```

> 用 NAS 的「文件管理器 + 容器终端」也一样：先把 `openrouterhelper` 上传到 MoviePilot
> 挂载的持久目录 `/config`，再进容器终端执行
> `mv /config/openrouterhelper /app/app/plugins/`，最后重启容器。

### 方式 C：作为插件市场（长期维护 / 自动更新）

1. 把项目推到 GitHub 仓库（用 `dist/MoviePilot-OpenRouterPlugin-repo.zip` 解压上传，或用 git）。
   仓库根要有 `package.v2.json`（v2）或 `package.v3.json`（v3），且与 `plugins.v2/`、`icons/` 同级。
2. MoviePilot → **设置 → 插件 → 插件市场 → 右上角齿轮 → 添加仓库**，
   填 `https://github.com/<你的用户名>/<仓库名>`，刷新插件列表。
3. 搜到「OpenRouter 免费模型助手」点安装。

> `package.v2.json` 的 `"icon"` 支持纯文件名（官方插件即如此），也支持绝对地址。
> 若你的环境取不到图标，改成
> `https://raw.githubusercontent.com/<你>/<仓库名>/main/icons/openrouterhelper.png` 即可。

#### 推到 GitHub 的两条路

本机直连 `github.com` 的 HTTPS 会被重置（TLS 层阻断），所以直接用 `https://` 推会失败。
已实测可用的替代通道是 **SSH over 443**，仓库里备好了一键脚本 `push-to-github.sh`：

```bash
# 一次性准备（浏览器里各点一下）
#   1) 添加公钥： https://github.com/settings/ssh/new
#   2) 建空仓库： https://github.com/new   （名字 MoviePilot-OpenRouterPlugin，别勾 README）
# 然后：
./push-to-github.sh <你的GitHub用户名>
```

脚本依赖 `~/.ssh/config` 里已配好的重定向（把 `github.com` 转到 `ssh.github.com:443`）：

```
Host github.com
    HostName ssh.github.com
    Port 443
    User git
    IdentityFile ~/.ssh/id_ed25519
```

如果机器上装了可用代理，也可以直接走 HTTPS，不用 SSH：

```bash
git remote add origin https://github.com/<你>/MoviePilot-OpenRouterPlugin.git
git -c http.proxy=http://127.0.0.1:7890 push -u origin main
```

### 装完的三个检查点

| 检查 | 命令 / 位置 | 期望 |
|------|------------|------|
| 文件到位 | `docker exec <容器名> ls /app/app/plugins/openrouterhelper/` | 有 `__init__.py` |
| 界面可开 | 设置 → 插件 → OpenRouter 免费模型助手 | 配置页能打开，模型下拉有内容 |
| 加载日志 | `docker logs <容器名> 2>&1 \| grep -i openrouter` | 有插件加载记录 |

### 关于「重启 / 升级镜像后插件消失」

MoviePilot 官方就是从 `/app/app/plugins/` 加载插件，该目录位于**容器可写层**，
镜像升级或容器重建后会回到镜像初始状态，第三方插件随之消失——这是官方机制，非本插件问题。
两种应对：

- **省事**：升级后重跑一次 `install.sh`（幂等，几秒）。
- **持久**：给容器加挂载。注意 `/app/app/plugins` 里有镜像自带插件，
  **直接挂空目录会让内置插件消失**，先把镜像内容导出来再挂：

  ```bash
  mkdir -p /volume1/docker/moviepilot/plugins
  docker cp <容器名>:/app/app/plugins/. /volume1/docker/moviepilot/plugins/
  ```

  然后在 docker-compose.yml 的 `volumes:` 里加：
  `- /volume1/docker/moviepilot/plugins:/app/app/plugins`
  再 `docker compose up -d` 重建容器。

---

## 3. 配置项

| 字段 | 说明 | 默认 |
|------|------|------|
| 启用插件 | 总开关 | 关 |
| 保存时自动填充 | 保存配置即写回智能助手 | 关 |
| 写入 app.env 持久化 | 落盘到配置文件，重启后仍生效 | 关 |
| **OpenRouter API Key** | `https://openrouter.ai/keys` 创建。**调免费模型也必须填** | 空 |
| 同时启用智能助手 | 一并把 `AI_AGENT_ENABLE` 置为 true | 关 |
| API 地址 | 写入 `LLM_BASE_URL` | `https://openrouter.ai/api/v1` |
| 模型目录接口 | 拉取清单用，无需鉴权 | `https://openrouter.ai/api/v1/models` |
| 免费模型 | 写入 `LLM_MODEL`，下拉从目录动态生成 | 空 |
| 提供商 | 写入 `LLM_PROVIDER` | `openrouter` |
| 接口协议 | 写入 `LLM_API_PROTOCOL` | `chat_completions` |
| 自动刷新间隔(小时) | 定时刷新目录，`0` 关闭 | `12` |
| 走系统代理 | 使用 MoviePilot 的 `PROXY_HOST` | 关 |
| 仅显示支持工具调用 | 过滤掉不支持 function calling 的免费模型 | **开** |
| 应用地址 / 应用名称 | OpenRouter 归属头 `HTTP-Referer` / `X-Title`（可选） | 空 / `MoviePilot` |

**填充后写回智能助手的键：**
`LLM_PROVIDER`、`LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`、`LLM_API_PROTOCOL`（可选加 `AI_AGENT_ENABLE`）。

---

## 4. 使用流程

1. 到 <https://openrouter.ai/keys> 注册并创建一个 API Key（免费模型也要求鉴权）。
2. 插件里填入 API Key，打开「启用插件」，保存 → 配置页会自动拉取一次模型目录。
3. 在「免费模型」下拉里挑一个（标题形如 `NVIDIA: Nemotron 3 Ultra (free) · nvidia/...:free [977K/工具]`）。
4. 打开「保存时自动填充」+「写入 app.env 持久化」，再保存一次即完成。
5. 到 **设定 → 系统 → 基础设置 → 智能助手** 确认 `LLM 提供商` 已是 `openrouter`、模型已填入。

> 下拉标题里的 `[977K/工具]` 表示上下文长度约 977K token、且该模型**支持 tool calling**。
> 智能助手靠工具调用来查下载、搜资源，**建议保持「仅显示支持工具调用」开启**。

---

## 5. 插件 API

注册在 `/api/v1/plugin/OpenRouterHelper/<path>`，认证方式 `bear`：

| 路径 | 作用 |
|------|------|
| `GET /models` | 返回缓存的免费模型列表 |
| `GET /refresh` | 强制重新拉取模型目录 |
| `GET /apply` | 把当前选择写入智能助手配置 |
| `GET /test` | 向所选模型发一条测试消息，验证 Key 与模型可用性 |

---

## 6. 实测结论（2026-10-12）

拉取 `https://openrouter.ai/api/v1/models`（**无需鉴权**）：共 **458** 个模型。

- 零价模型 **19** 个；剔除音频生成模型后，零价且**纯文本输出**的 **17** 个。
- 其中支持 tool calling 的 **16** 个：

| 模型 id | 上下文 |
|---------|--------|
| `thinkingmachines/inkling-small:free` | 1024K |
| `thinkingmachines/inkling:free` | 1024K |
| `nvidia/nemotron-3-ultra-550b-a55b:free` | 977K |
| `nvidia/nemotron-3.5-lightning:free` | 977K |
| `dots-studio/dots-3-note-preview:free` | 500K |
| `apodex/apodex-1.1-mini:free` | 256K |
| `google/gemma-4-26b-a4b-it:free` | 256K |
| `google/gemma-4-31b-it:free` | 256K |
| `inclusionai/ling-3.1-flash` | 256K |
| `nvidia/nemotron-3-super-120b-a12b:free` | 256K |
| `poolside/laguna-s-2.1:free` | 256K |
| `poolside/laguna-xs-2.1:free` | 256K |
| `cohere/north-mini-code:free` | 250K |
| `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` | 250K |
| `openrouter/free` | 195K |
| `liquid/lfm-2.5-2.6b:free` | 64K |

被排除的两个 `google/lyria-3-*` 是**音乐生成**模型（`output_modalities` = `["text","audio"]`）；
`nvidia/nemotron-3.5-content-safety:free` 是内容安全判别模型，**不支持 tool calling**。

### 判定规则（不是看 `:free` 后缀）

- 免费 = `pricing.prompt == 0` 且 `pricing.completion == 0`
  （`openrouter/free`、`inclusionai/ling-3.1-flash` 是零价但**不带** `:free` 后缀）
- 可对话 = `architecture.output_modalities` 含 `text` 且不含 `audio`/`image`
- 可当智能助手 = `supported_parameters` 含 `tools`

### 已知限制

- 调 `:free` 模型**必须带 API Key**，否则 401。
- 免费额度按账号计：未充值约 **50 次/天**，累计充值 ≥ 10 美元后约 **1000 次/天**；超限返回 429。
- 模型目录会变（下架 / 转付费）。插件在刷新时若发现当前选中的模型已不在免费列表，会在日志里提示重新选择。
- MoviePilot 配置优先级为 **环境变量 > app.env ≈ WEB 界面**。若 `LLM_*` 已由 Docker 环境变量注入，写 `app.env` 不会覆盖它，此时请改用环境变量或去掉对应 `-e`。

---

## 7. 开发自检

不装 MoviePilot 也能验证插件逻辑（用桩模块模拟宿主 + 真实目录样本回放）：

```bash
python dev/selftest.py
```

覆盖：免费模型判定 / tool calling 标注 / 目录拉取过滤 / 写回 `settings` / 模块热重载 /
`app.env` 持久化（含目录自动创建）/ 幂等（无变化不重载）/ 表单与详情页结构 /
API 的成功·401·429 分支 / 定时任务注册 / 选中模型下架兜底。当前 **43 项全部通过**。

---

## 8. 实现要点

- 宿主基类与配置对象做了双路径兼容导入（`app.plugins` / `app.sdk.plugin.base`、`app.core.config` / `app.runtime.config`）。
- 写回配置采用 MoviePilot 官方 ConfigCenter 插件的同款方式：
  `setattr(settings, key, value)` 运行时生效 → `dotenv.set_key` 落 `app.env` → `ModuleManager().stop()/load_modules()` 热重载。
- `apply_to_agent()` 幂等：目标值已一致时不写文件、不重载模块，避免每次启动都触发全量模块重载。
- 模型清单带 TTL 缓存（`refresh_hours`），启动时不发网络请求，只在打开配置页/定时任务/手动刷新时拉取。
