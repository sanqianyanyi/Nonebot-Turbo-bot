from __future__ import annotations

import os
import json
import pyodbc
from pathlib import Path
from dotenv import load_dotenv
from typing import Any, Dict, List, Tuple

import httpx
from nonebot import on_command, on_regex
from nonebot.plugin import PluginMetadata
from nonebot.params import CommandArg, RegexGroup
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent

__plugin_meta__ = PluginMetadata(
    name="TurboNET maimai 机器人（BotKey 版）",
    description=(
        "使用 https://api.sys-allnet.com 的 TurboNET API 查询 maimai 数据、跑图券、机厅信息，"
        "通过 /bind 绑定每个 QQ 的 BotKey。"
    ),
    usage=(
        "/bind <botToken>       绑定你的机器人（内部会调 /bot/bind 获取 BotKey）\n"
        "/mai <TurboNET用户名>  查询 maimai 总览（需要先 /bind）\n"
        "/mai_status            查询机厅网络状态（需要先 /bind）\n"
        "/run X                 设置 X 倍跑图卷（需要先 /bind）\n"
        "/go                    相当于 /run 6（6 倍跑图卷）\n"
        "/norun                 取消跑图卷（需要先 /bind）\n"
        "/getrun                查看当前跑图卷倍率（需要先 /bind）\n"
        "发送 <机厅代号>j"
    ),
)

# 加载上级目录的 .env 文件
current_dir = Path(__file__).parent
parent_dir = current_dir.parent
env_path = parent_dir / '.env'

# 加载环境变量，如果.env文件中有变量与系统环境变量冲突，会用.env文件中的值覆盖[2,4](@ref)
load_dotenv(dotenv_path=env_path, override=True)

API_BASE = os.getenv("SYSALLNET_API_BASE")

# ======================= SQL Server 持久化 QQ -> botKey =======================

#从env文件中加载数据库配置信息
SQLSERVER_HOST = os.getenv("SQLSERVER_HOST")
SQLSERVER_PORT = os.getenv("SQLSERVER_PORT")
SQLSERVER_DATABASE = os.getenv("SQLSERVER_DATABASE")
SQLSERVER_USER = os.getenv("SQLSERVER_USER")
SQLSERVER_PASSWORD = os.getenv("SQLSERVER_PASSWORD")

# SQL Server 连接字符串
SQLSERVER_CONNECTION_STRING = f'DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={SQLSERVER_HOST},{SQLSERVER_PORT};DATABASE={SQLSERVER_DATABASE};UID={SQLSERVER_USER};PWD={SQLSERVER_PASSWORD}'

def _init_db() -> None:
    """初始化 SQL Server 数据库和表（如果不存在）。"""
    conn = pyodbc.connect(SQLSERVER_CONNECTION_STRING)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='user_bind' AND xtype='U')
            CREATE TABLE user_bind (
                qq_id   VARCHAR(255) PRIMARY KEY,
                bot_key VARCHAR(255) NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

_init_db()

def set_user_bot_key(qq_id: str, bot_key: str) -> None:
    """为某个 QQ 号绑定 / 更新 botKey。"""
    conn = pyodbc.connect(SQLSERVER_CONNECTION_STRING)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "IF EXISTS (SELECT * FROM user_bind WHERE qq_id = ?) "
            "UPDATE user_bind SET bot_key = ? WHERE qq_id = ? "
            "ELSE "
            "INSERT INTO user_bind(qq_id, bot_key) VALUES (?, ?)",
            (qq_id, bot_key, qq_id, qq_id, bot_key)
        )
        conn.commit()
    finally:
        conn.close()

def get_user_bot_key(qq_id: str) -> str | None:
    """获取某个 QQ 号绑定的 botKey，没有则返回 None。"""
    conn = pyodbc.connect(SQLSERVER_CONNECTION_STRING)
    try:
        cur = conn.execute(
            "SELECT bot_key FROM user_bind WHERE qq_id = ?",
            (qq_id,),
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _build_auth_headers(qq_id: str) -> Dict[str, str]:
    """
    根据 QQ 号构造授权头：
      Authorization: BotKey <botKey>

    若尚未绑定，返回空 dict。
    """
    headers: Dict[str, str] = {}
    bot_key = get_user_bot_key(qq_id)
    if bot_key:
        headers["Authorization"] = f"BotKey {bot_key}"
    return headers


def _fmt_number(v: Any, nd: int = 2, suffix: str | None = None) -> str:
    if v is None:
        return "未知"
    try:
        num = float(v)
    except Exception:
        return str(v)
    s = f"{num:.{nd}f}"
    if suffix:
        s += suffix
    return s


def _to_int(v: Any, default: int = 0) -> int:
    """安全转换为 int，用于人类可读输出。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ======================= 指令 0：/bind 绑定 BotKey =======================

bind_cmd = on_command(
    "bind",
    block=True,
    priority=5,
)


@bind_cmd.handle()
async def handle_bind(
    bot: Bot,
    event: MessageEvent,
    args: Message = CommandArg(),
):
    """
    绑定机器人：
      /bind <botToken>

    实现流程：
      1. 调用 POST /bot/bind，Body: { "botToken": <用户传入>, "botName": "yibot" }
      2. 从返回中提取 { "botId": "...", "botKey": "..." }
      3. 把 botKey 绑定到当前 QQ 号
    """
    text = args.extract_plain_text().strip()
    if not text:
        await bind_cmd.finish(
            "用法：/bind <你的 botToken>\n"
            "机器人会调用 /bot/bind 获取专属 BotKey 并与当前 QQ 绑定。\n"
            "注意：你只需要提供 botToken，不需要也拿不到 botKey。"
        )

    bot_token = text.split()[0]
    qq_id = str(event.user_id)

    url = f"{API_BASE}/bot/bind"
    json_data = {"botToken": bot_token, "botName": "yibot"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=json_data)
    except httpx.RequestError as e:
        await bind_cmd.finish(f"请求 /bot/bind 失败：{e}")

    if resp.status_code != 200:
        try:
            err = resp.json()
            msg = err.get("message") or str(err)
        except Exception:
            msg = resp.text
        await bind_cmd.finish(
            f"/bot/bind 返回异常（HTTP {resp.status_code}）：{msg}\n"
            "请确认 botToken 是否正确。"
        )

    try:
        data: Dict[str, Any] = resp.json()
    except Exception:
        await bind_cmd.finish("解析 /bot/bind 返回内容失败，请稍后重试。")

    # 正确格式示例：
    # { "botId": "botId_DObT1uoj", "botKey": "4861ae6c-c4f1-49ab-b58a-76f1280f6200" }
    bot_key = data.get("botKey")
    bot_id = data.get("botId")

    if not bot_key:
        await bind_cmd.finish(
            "绑定失败：未在返回中找到 botKey 字段。\n"
            "可能是 botToken 无效或服务器异常，请检查后重试。"
        )

    set_user_bot_key(qq_id, bot_key)

    lines: List[str] = []
    lines.append("绑定成功！")
    if bot_id:
        lines.append(f"你的 botId：{bot_id}")
    lines.append("已为当前 QQ 绑定专属 BotKey。")
    lines.append("后续所有查询与操作都会使用：")
    lines.append("Authorization: BotKey <你的botKey>")

    await bind_cmd.finish("\n".join(lines))


# ======================= 指令 1：/mai 查询用户信息 =======================

mai_cmd = on_command(
    "mai",
    aliases={"查分", "maimai"},
    block=True,
    priority=10,
)


@mai_cmd.handle()
async def handle_mai(
    bot: Bot,
    event: MessageEvent,
    args: Message = CommandArg(),
):
    """
    用法：
      /mai <TurboNET用户名>
    比如：
      /mai AAA_BBB

    必须先 /bind 绑定 BotKey。
    """
    text = args.extract_plain_text().strip()
    if not text:
        await mai_cmd.finish(
            "用法：/mai <TurboNET用户名>\n例如：/mai AAA_BBB"
        )

    turbo_name = text.split()[0]
    qq_id = str(event.user_id)

    headers = _build_auth_headers(qq_id)
    if not headers:
        await mai_cmd.finish(
            "你还没有绑定 BotKey，无法查询。\n"
            "请先使用：/bind <你的botToken>"
        )

    # requesterId 文档是一个字符串，这里仍使用 QQ 号
    requester_id = qq_id

    url = f"{API_BASE}/web/user"
    params = {"requesterId": requester_id}
    json_data = {"turboName": turbo_name}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                params=params,
                json=json_data,
                headers=headers,
            )
    except httpx.RequestError as e:
        await mai_cmd.finish(f"请求 /web/user 失败：{e}")

    if resp.status_code != 200:
        try:
            err = resp.json()
            msg = err.get("message") or str(err)
        except Exception:
            msg = resp.text
        await mai_cmd.finish(
            f"/web/user 返回异常（HTTP {resp.status_code}）：{msg}"
        )

    try:
        data: Dict[str, Any] = resp.json()
    except Exception:
        await mai_cmd.finish("解析 /web/user 返回内容失败，请稍后再试。")

    # ========== 解析返回数据 ==========
    user_name = data.get("turboName") or turbo_name
    maimai_name = data.get("maimaiName") or "（未设置）"
    qq_number = data.get("qqNumber")
    is_me = data.get("isMe")
    permission = data.get("permission")
    warning_times = data.get("warningTimes")
    warning_msg = data.get("warningMessage")
    is_banned = data.get("isBanned")
    banned_msg = data.get("bannedMessage")

    stats: Dict[str, Any] = data.get("maiStatistics") or {}
    play: Dict[str, Any] = data.get("playActivity") or {}
    best35: List[Dict[str, Any]] = data.get("best35") or []
    recent: List[Dict[str, Any]] = data.get("recentScores") or []

    lines: List[str] = []

    # ===== 基本信息 =====
    lines.append(f"TurboNET 用户：{user_name}")
    lines.append(f"maimai 名称：{maimai_name}")
    if qq_number:
        lines.append(f"绑定 QQ：{qq_number}")
    if is_me is not None:
        lines.append(f"是否本人：{'是' if is_me else '否'}")
    if permission:
        lines.append(f"权限等级：{permission}")
    if warning_times:
        lines.append(f"警告次数：{warning_times}")
    if warning_msg:
        lines.append(f"最近警告：{warning_msg}")
    if is_banned:
        lines.append(f"封禁状态：已封禁（{banned_msg or '无说明'}）")

    # ===== 综合数据 =====
    lines.append("")
    lines.append("=== 综合数据 ===")
    dr = stats.get("deluxRating")
    server_rank = stats.get("serverRanking")
    avg_acc = stats.get("averageAccuracy")
    max_combo = stats.get("maxCombo")
    full_combo = stats.get("fullCombo")
    all_perfect = stats.get("allPerfect")
    total_scores = stats.get("totalScores")

    lines.append(f"DX Rating：{_fmt_number(dr, 0)}")
    if server_rank is not None:
        lines.append(f"服务器排名：#{server_rank}")
    if avg_acc is not None:
        lines.append(f"平均达成率：{_fmt_number(avg_acc, 2, '%')}")
    if max_combo is not None:
        lines.append(f"历史最大连击：{max_combo}")
    if full_combo is not None:
        lines.append(f"Full Combo 数：{full_combo}")
    if all_perfect is not None:
        lines.append(f"All Perfect 数：{all_perfect}")
    if total_scores is not None:
        lines.append(f"总成绩数：{total_scores}")

    # ===== 游玩情况 =====
    lines.append("")
    lines.append("=== 游玩情况 ===")
    play_count = play.get("playCount")
    play_time = play.get("playTime")
    first_play = play.get("firstPlay")
    last_play = play.get("lastPlay")
    play_version = play.get("playVersion")

    if play_count is not None:
        lines.append(f"总游玩次数：{play_count}")
    if play_time is not None:
        lines.append(f"总游玩时长：{_fmt_number(play_time, 1)} 小时")
    if first_play:
        lines.append(f"首次游玩：{first_play}")
    if last_play:
        if play_version:
            lines.append(f"最近游玩：{last_play}（版本：{play_version}）")
        else:
            lines.append(f"最近游玩：{last_play}")

    # ===== Best 35 Top 3 =====
    if best35:
        lines.append("")
        lines.append("=== Best 35 Top 3 ===")
        for i, song in enumerate(best35[:3], start=1):
            name = song.get("musicName") or "未知曲目"
            level = song.get("level")
            diff = song.get("diff")
            score_rank = song.get("scoreRank")
            achv = song.get("achievement")
            score = song.get("score")

            head_parts = [f"{i}. {name}"]
            if level is not None:
                head_parts.append(f"[{_fmt_number(level, 1)}]")
            if diff is not None:
                head_parts.append(f"(Diff {diff})")
            detail = " ".join(head_parts)

            extra_parts: List[str] = []
            if achv is not None:
                extra_parts.append(f"达成率 {_fmt_number(achv, 4, '%')}")
            if score is not None:
                extra_parts.append(f"分数 {score}")
            if score_rank:
                extra_parts.append(f"评级 {score_rank}")

            if extra_parts:
                detail += " | " + " / ".join(extra_parts)

            lines.append(detail)

    # ===== 最近游玩 Top 3 =====
    if recent:
        lines.append("")
        lines.append("=== 最近游玩 Top 3 ===")
        for i, song in enumerate(recent[:3], start=1):
            name = song.get("musicName") or "未知曲目"
            level = song.get("level")
            score_rank = song.get("scoreRank")
            achv = song.get("achievement")

            head_parts = [f"{i}. {name}"]
            if level is not None:
                head_parts.append(f"[{_fmt_number(level, 1)}]")
            detail = " ".join(head_parts)

            extra_parts: List[str] = []
            if achv is not None:
                extra_parts.append(f"达成率 {_fmt_number(achv, 4, '%')}")
            if score_rank:
                extra_parts.append(f"评级 {score_rank}")

            if extra_parts:
                detail += " | " + " / ".join(extra_parts)

            lines.append(detail)

    await mai_cmd.finish("\n".join(lines))


# ======================= 指令 2：机厅网络状态 =======================

status_cmd = on_command(
    "mai_status",
    aliases={"网厅状态", "网络状况"},
    block=True,
    priority=11,
)


@status_cmd.handle()
async def handle_status(bot: Bot, event: MessageEvent):
    """
    查询 TurboNET 机厅网络状态：
      /mai_status
      网厅状态

    使用当前 QQ 绑定的 BotKey。
    """
    qq_id = str(event.user_id)
    headers = _build_auth_headers(qq_id)
    if not headers:
        await status_cmd.finish(
            "你还没有绑定 BotKey，无法查询机厅网络状态。\n"
            "请先使用：/bind <你的botToken>"
        )

    url = f"{API_BASE}/web/showNetworkStatus"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                url,
                headers=headers,
            )
    except httpx.RequestError as e:
        await status_cmd.finish(f"请求 /web/showNetworkStatus 失败：{e}")

    if resp.status_code != 200:
        try:
            err = resp.json()
            msg = err.get("message") or str(err)
        except Exception:
            msg = resp.text
        await status_cmd.finish(
            f"/web/showNetworkStatus 返回异常（HTTP {resp.status_code}）：{msg}"
        )

    try:
        data: List[Dict[str, Any]] = resp.json()
    except Exception:
        await status_cmd.finish("解析网络状态返回内容失败。")

    if not data:
        await status_cmd.finish("当前没有机厅状态数据。")

    lines: List[str] = []
    lines.append("当前 TurboNET 机厅网络状态（最多显示前 5 条）：")

    status_translate = {
        "WORKING": "正常",
        "WARNING": "警告",
        "ERROR": "错误",
        "UNKNOWN": "未知",
    }

    for arcade in data[:5]:
        name = arcade.get("arcadeName") or "未知机厅"
        atype = arcade.get("arcadeType") or "未知类型"
        ws = arcade.get("workingStatus") or "UNKNOWN"
        ws_cn = status_translate.get(ws, ws)
        last = arcade.get("lastHeartbeatSecond") or "未知时间"

        lines.append(f"- {name} [{atype}] 状态：{ws_cn}（最后心跳：{last}）")

    await status_cmd.finish("\n".join(lines))


# ======================= 跑图卷功能：/run /go /norun /getrun =======================

run_cmd = on_command(
    "run",
    block=True,
    priority=12,
)


@run_cmd.handle()
async def handle_run(
    bot: Bot,
    event: MessageEvent,
    args: Message = CommandArg(),
):
    """
    设置跑图卷：
      /run X    （例如 /run 2 /run 3）

    提取 X 为整数，作为 ticketId 调用：
      POST /web/setTickets
      { "ticketId": X }

    成功后提示「绑定X倍跑图卷成功」。
    """
    qq_id = str(event.user_id)
    headers = _build_auth_headers(qq_id)
    if not headers:
        await run_cmd.finish(
            "你还没有绑定 BotKey，无法设置跑图卷。\n"
            "请先使用：/bind <你的botToken>"
        )

    text = args.extract_plain_text().strip()
    if not text:
        await run_cmd.finish("用法：/run X\n例如：/run 2 或 /run 3")

    try:
        ticket_id = int(text.split()[0])
    except ValueError:
        await run_cmd.finish("跑图卷倍率必须是整数，例如 /run 2。")

    url = f"{API_BASE}/web/setTickets"
    json_data = {"ticketId": ticket_id}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                json=json_data,
                headers=headers,
            )
    except httpx.RequestError as e:
        await run_cmd.finish(f"设置跑图卷失败：{e}")

    if resp.status_code != 200:
        try:
            err = resp.json()
            msg = err.get("message") or str(err)
        except Exception:
            msg = resp.text
        await run_cmd.finish(
            f"/web/setTickets 返回异常（HTTP {resp.status_code}）：{msg}"
        )

    await run_cmd.finish(f"绑定{ticket_id}倍跑图卷成功")


go_cmd = on_command(
    "go",
    block=True,
    priority=13,
)


@go_cmd.handle()
async def handle_go(bot: Bot, event: MessageEvent):
    """
    快捷设置 6 倍跑图卷：
      /go   等价于 /run 6
    """
    qq_id = str(event.user_id)
    headers = _build_auth_headers(qq_id)
    if not headers:
        await go_cmd.finish(
            "你还没有绑定 BotKey，无法设置跑图卷。\n"
            "请先使用：/bind <你的botToken>"
        )

    ticket_id = 6
    url = f"{API_BASE}/web/setTickets"
    json_data = {"ticketId": ticket_id}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                json=json_data,
                headers=headers,
            )
    except httpx.RequestError as e:
        await go_cmd.finish(f"设置跑图卷失败：{e}")

    if resp.status_code != 200:
        try:
            err = resp.json()
            msg = err.get("message") or str(err)
        except Exception:
            msg = resp.text
        await go_cmd.finish(
            f"/web/setTickets 返回异常（HTTP {resp.status_code}）：{msg}"
        )

    await go_cmd.finish("绑定6倍跑图卷成功")


norun_cmd = on_command(
    "norun",
    block=True,
    priority=14,
)


@norun_cmd.handle()
async def handle_norun(bot: Bot, event: MessageEvent):
    """
    取消跑图卷：
      /norun

    调用：
      POST /web/resetTickets
    成功后提示「取消跑图卷成功」。
    """
    qq_id = str(event.user_id)
    headers = _build_auth_headers(qq_id)
    if not headers:
        await norun_cmd.finish(
            "你还没有绑定 BotKey，无法取消跑图卷。\n"
            "请先使用：/bind <你的botToken>"
        )

    url = f"{API_BASE}/web/resetTickets"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                headers=headers,
            )
    except httpx.RequestError as e:
        await norun_cmd.finish(f"取消跑图卷失败：{e}")

    if resp.status_code != 200:
        try:
            err = resp.json()
            msg = err.get("message") or str(err)
        except Exception:
            msg = resp.text
        await norun_cmd.finish(
            f"/web/resetTickets 返回异常（HTTP {resp.status_code}）：{msg}"
        )

    await norun_cmd.finish("取消跑图卷成功")


getrun_cmd = on_command(
    "getrun",
    block=True,
    priority=15,
)


@getrun_cmd.handle()
async def handle_getrun(bot: Bot, event: MessageEvent):
    """
    查看当前跑图卷倍率：
      /getrun

    调用：
      GET /web/currentTickets

    根据返回：
      { "turboTicket": { "isEnable": true, "ticketId": 3 }, ... }

    提取 ticketId 数字并提示：
      当前跑图卷倍率为3
    """
    qq_id = str(event.user_id)
    headers = _build_auth_headers(qq_id)
    if not headers:
        await getrun_cmd.finish(
            "你还没有绑定 BotKey，无法查看跑图卷。\n"
            "请先使用：/bind <你的botToken>"
        )

    url = f"{API_BASE}/web/currentTickets"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                url,
                headers=headers,
            )
    except httpx.RequestError as e:
        await getrun_cmd.finish(f"获取跑图卷信息失败：{e}")

    if resp.status_code != 200:
        try:
            err = resp.json()
            msg = err.get("message") or str(err)
        except Exception:
            msg = resp.text
        await getrun_cmd.finish(
            f"/web/currentTickets 返回异常（HTTP {resp.status_code}）：{msg}"
        )

    try:
        data: Dict[str, Any] = resp.json()
    except Exception:
        await getrun_cmd.finish("解析跑图卷信息返回内容失败。")

    turbo_ticket = data.get("turboTicket") or {}
    is_enable = turbo_ticket.get("isEnable")
    ticket_id = turbo_ticket.get("ticketId")

    if not is_enable or ticket_id is None:
        await getrun_cmd.finish("当前未启用跑图卷")

    await getrun_cmd.finish(f"当前跑图卷倍率为{_to_int(ticket_id)}")


# ======================= 自然语言触发：<机厅代号>j => /web/arcadeInfoDetail =======================

arcade_detail_matcher = on_regex(
    r"^(?!/)(\S+)j$",
    priority=20,
    block=True,
)


@arcade_detail_matcher.handle()
async def handle_arcade_detail(
    bot: Bot,
    event: MessageEvent,
    groups: Tuple[str, ...] = RegexGroup(),
):
    """
    当用户发送「XXj」时，如：
      fsj
    将提取「fs」作为 arcadeName 调用：
      GET /web/arcadeInfoDetail?arcadeName=fs

    使用管理员 QQ 绑定的 BotKey（来自数据库），并按指定格式回复例如：
      四川成都FS COMICS动漫
      30分钟内共游玩了0PC
      一小时内共游玩了0PC
      两小时内共游玩了1PC
      今日店内共1PC
      网络概况：今日网络请求78次中，缓存命中33次，错误修复次数0次。
    """
    if not groups:
        return

    arcade_code = groups[0]  # 例如 "fs"
    qq_id = str(event.user_id)

    # 机厅代号以 j 结尾（如 fsj）时：使用管理员 QQ 在数据库中绑定的 BotKey（无需用户自行绑定）
    admin_qq = os.getenv("SYSALLNET_ADMIN_QQ").strip()
    if not admin_qq:
        await arcade_detail_matcher.finish("管理员 QQ 未配置，请在 .env 中设置 SYSALLNET_ADMIN_QQ")

    headers = _build_auth_headers(admin_qq)
    if not headers:
        await arcade_detail_matcher.finish(
            "管理员尚未绑定 BotKey，无法查询机厅详情。\n"
            "请管理员先使用：/bind <管理员botToken>"
        )

    url = f"{API_BASE}/web/arcadeInfoDetail"
    params = {"arcadeName": arcade_code}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                url,
                params=params,
                headers=headers,
            )
    except httpx.RequestError as e:
        await arcade_detail_matcher.finish(f"请求 /web/arcadeInfoDetail 失败：{e}")

    if resp.status_code != 200:
        try:
            err = resp.json()
            msg = err.get("message") or str(err)
        except Exception:
            msg = resp.text
        await arcade_detail_matcher.finish(
            f"/web/arcadeInfoDetail 返回异常（HTTP {resp.status_code}）：{msg}"
        )

    try:
        data: Dict[str, Any] = resp.json()
    except Exception:
        await arcade_detail_matcher.finish("解析机厅详情返回内容失败。")

    # 示例返回结构（简化注释）：
    # {
    #   "arcadeInfo": {
    #     "arcadeName": "四川成都FS COMICS动漫",
    #     "arcadeType": "TURBO",
    #     "arcadePlayCount": 1,
    #     "arcadeRequested": 78,
    #     "arcadeCachedRequest": 33,
    #     "arcadeFixedRequest": 0,
    #     ...
    #   },
    #   "thirtyMinutesPlayer": 0,
    #   "oneHourPlayer": 0,
    #   "twoHoursPlayer": 1,
    #   ...
    # }
    arcade_info = data.get("arcadeInfo") or {}

    arcade_name = arcade_info.get("arcadeName") or arcade_code
    thirty_minutes_player = _to_int(data.get("thirtyMinutesPlayer"), 0)
    one_hour_player = _to_int(data.get("oneHourPlayer"), 0)
    two_hours_player = _to_int(data.get("twoHoursPlayer"), 0)
    arcade_play_count = _to_int(arcade_info.get("arcadePlayCount"), 0)
    arcade_requested = _to_int(arcade_info.get("arcadeRequested"), 0)
    arcade_cached_request = _to_int(arcade_info.get("arcadeCachedRequest"), 0)
    arcade_fixed_request = _to_int(arcade_info.get("arcadeFixedRequest"), 0)

    lines: List[str] = []
    lines.append(str(arcade_name))
    lines.append(f"30分钟内共游玩了{thirty_minutes_player}PC")
    lines.append(f"一小时内共游玩了{one_hour_player}PC")
    lines.append(f"两小时内共游玩了{two_hours_player}PC")
    lines.append(f"今日店内共{arcade_play_count}PC")

    # 新增：玩家列表（只取 playerList 的 maimaiName 与 playdate）
    player_list = data.get("playerList") or []
    lines.append("当前店内玩家列表：")
    if not player_list:
        lines.append("暂无")
    else:
        for p in player_list:
            name = p.get("maimaiName") or "未知玩家"
            playdate = p.get("playdate") or ""
            lines.append(f"{name} 上机时间{playdate}")

    lines.append(
        f"网络概况：今日网络请求{arcade_requested}次中，缓存命中{arcade_cached_request}次，错误修复次数{arcade_fixed_request}次。"
    )

    await arcade_detail_matcher.finish("\n".join(lines))

# ======================= 指令：/net 网络状态（管理员 BotKey） =======================

net_cmd = on_command(
    "net",
    block=True,
    priority=11,
)

@net_cmd.handle()
async def handle_net(bot: Bot, event: MessageEvent):
    admin_qq = os.getenv("SYSALLNET_ADMIN_QQ").strip()
    if not admin_qq:
        await net_cmd.finish("管理员 QQ 未配置，请在 .env 中设置 SYSALLNET_ADMIN_QQ")

    headers = _build_auth_headers(admin_qq)
    if not headers:
        await net_cmd.finish(
            "管理员尚未绑定 BotKey，无法查询网络状态。\n"
            "请管理员先使用：/bind <管理员botToken>"
        )

    url = f"{API_BASE}/web/showNetworkStatus"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, headers=headers)
    resp.raise_for_status()
    data = resp.json()

    status_translate = {"WORKING": "工作中", "WARNING": "警告", "ERROR": "掉线啦！！！", "UNKNOWN": "未知"}

    out: List[str] = []
    for idx, arcade in enumerate(data, start=1):
        name = arcade.get("arcadeName") or "未知机厅"
        ws = arcade.get("workingStatus") or "UNKNOWN"
        ws_cn = status_translate.get(ws, ws)
        last = arcade.get("lastHeartbeatSecond") or "未知时间"
        out.append(f"{idx}.{name} 状态：{ws_cn} 最后返回心跳包时间：{last}")

    await net_cmd.finish("\n".join(out))