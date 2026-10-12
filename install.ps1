# ============================================================
#  OpenRouter 免费模型助手 —— MoviePilot 插件一键安装脚本 (Windows)
#  适用：Windows 上的 Docker Desktop / WSL2 里的 MoviePilot
#
#  用法（在 PowerShell 中，于本文件所在目录执行）：
#     .\install.ps1                      # 默认容器名 moviepilot
#     .\install.ps1 -Container my-mp     # 指定容器名
#     .\install.ps1 -LocalRoot D:\MoviePilot   # 源码部署，不用 Docker
#
#  首次执行如提示脚本被禁止，先运行：
#     Set-ExecutionPolicy -Scope Process Bypass
#
#  可重复执行（幂等）：重复运行 = 覆盖安装 / 升级
# ============================================================
param(
    [string]$Container = "moviepilot",
    [string]$LocalRoot = ""
)

$ErrorActionPreference = "Stop"

$PluginId      = "OpenRouterHelper"
$PluginDirName = "openrouterhelper"      # 必须是插件类名的小写形式
$IconName      = "openrouterhelper.png"
$ContainerPlugins = "/app/app/plugins"   # MoviePilot 容器内真实加载路径
$ScriptDir     = Split-Path -Parent $MyInvocation.MyCommand.Path

# ---------- 定位插件源（自动适配 v2 / v3 目录结构）----------
$Src = $null
foreach ($cand in @("plugins.v2\$PluginDirName", "plugins.v3\$PluginDirName", "plugins\$PluginDirName")) {
    if (Test-Path (Join-Path $ScriptDir "$cand\__init__.py")) {
        $Src = Join-Path $ScriptDir $cand
        break
    }
}
if (-not $Src -and (Test-Path (Join-Path $ScriptDir "$PluginDirName\__init__.py"))) {
    $Src = Join-Path $ScriptDir $PluginDirName
}
if (-not $Src) {
    Write-Error "在 $ScriptDir 下找不到 $PluginDirName\__init__.py，请把脚本与插件目录放在一起。"
    exit 1
}
$Icon = Join-Path $ScriptDir "icons\$IconName"
Write-Host "==> 插件源目录：$Src"

function Install-Local([string]$root) {
    if (-not (Test-Path (Join-Path $root "app"))) {
        Write-Error "$root 看起来不是 MoviePilot 源码根目录（缺少 app\ 目录）"
        exit 1
    }
    $target = Join-Path $root "app\plugins\$PluginDirName"
    $pluginsRoot = Join-Path $root "app\plugins"
    if (-not (Test-Path $pluginsRoot)) { New-Item -ItemType Directory -Path $pluginsRoot | Out-Null }
    if (Test-Path $target) { Remove-Item $target -Recurse -Force }
    Copy-Item $Src $target -Recurse -Force
    Remove-Item (Join-Path $target "__pycache__") -Recurse -Force -ErrorAction SilentlyContinue
    $iconsDir = Join-Path $pluginsRoot "icons"
    if (-not (Test-Path $iconsDir)) { New-Item -ItemType Directory -Path $iconsDir | Out-Null }
    if (Test-Path $Icon) { Copy-Item $Icon (Join-Path $iconsDir $IconName) -Force }
    if (Test-Path $Icon) { Copy-Item $Icon (Join-Path $target $IconName) -Force }
    Write-Host "==> 已安装到：$target"
    Write-Host "==> 请重启 MoviePilot 服务后到 [设置 → 插件] 中启用「OpenRouter 免费模型助手」"
}

function Install-Docker([string]$c) {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Write-Error "未找到 docker 命令。若为源码部署请用：.\install.ps1 -LocalRoot D:\MoviePilot"
        exit 1
    }
    docker inspect $c 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Error "容器 [$c] 不存在。用 docker ps 查看实际名称。"
        exit 1
    }
    $running = (docker inspect -f '{{.State.Running}}' $c)
    Write-Host "==> 目标容器：$c (running=$running)"

    # 备份已有版本（可回滚）
    docker exec $c test -d "$ContainerPlugins/$PluginDirName" 2>$null
    if ($LASTEXITCODE -eq 0) {
        $stamp = Get-Date -Format "yyyyMMddHHmmss"
        $bak = "$ContainerPlugins/$PluginDirName.bak.$stamp"
        Write-Host "==> 检测到已安装，先备份到：$bak"
        docker exec $c sh -c "rm -rf '$bak'; cp -r '$ContainerPlugins/$PluginDirName' '$bak'"
        docker exec $c rm -rf "$ContainerPlugins/$PluginDirName"
    }

    docker exec $c mkdir -p $ContainerPlugins "$ContainerPlugins/icons"
    docker cp $Src "${c}:$ContainerPlugins/"
    docker exec $c rm -rf "$ContainerPlugins/$PluginDirName/__pycache__"

    if (Test-Path $Icon) {
        docker cp $Icon "${c}:$ContainerPlugins/icons/$IconName"
        docker cp $Icon "${c}:$ContainerPlugins/$PluginDirName/$IconName"
    }

    docker exec $c test -f "$ContainerPlugins/$PluginDirName/__init__.py"
    if ($LASTEXITCODE -eq 0) {
        Write-Host "==> 校验通过：$ContainerPlugins/$PluginDirName/__init__.py 已存在"
    } else {
        Write-Warning "写入后未在容器内找到 __init__.py，请手动确认路径。"
    }

    Write-Host "==> 重启容器 $c ..."
    docker restart $c | Out-Null
    Write-Host "==> 完成。请稍等 20~60 秒待服务启动，然后："
    Write-Host "    1) 打开 MoviePilot → 设置 → 插件，搜索「OpenRouter 免费模型助手」"
    Write-Host "    2) 填入 OpenRouter API Key，打开「启用插件」并保存"
    Write-Host "    3) 打开「仅显示支持工具调用」（默认已开），选择模型后保存"
    Write-Host "    4) 如需重启后仍生效，再打开「写入 app.env 持久化」并保存一次"
    Write-Host ""
    Write-Host "    注意：升级 MoviePilot 镜像后 /app 会被重建，插件会消失，重跑本脚本即可。"
}

if ($LocalRoot -ne "") {
    Install-Local $LocalRoot
} else {
    Install-Docker $Container
}
