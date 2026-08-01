#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pornhub Downloader
1. requests + BeautifulSoup 解析列表
2. 当前页面视频反序下载，带序号（废弃）
3. interval 增量过滤：publish_date > interval 才下载；all 则不过滤
4. 从第1页跑完后，把对应 user 的 interval 回写为当日零点
5. 支持 userlist 多 model 循环
"""
import asyncio
import logging
import os
import re
import sys
import time
from datetime import datetime
import requests
from urllib.parse import urljoin
from pathlib import Path
from bs4 import BeautifulSoup
import yaml
from base_api import BaseCore, DownloadConfigHLS
from base_api.modules.config import RuntimeConfig
from pornhub_api import Client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

CONFIG_PATH = "config.yml"


def load_config(config_path: str = CONFIG_PATH) -> dict:
    if not os.path.exists(config_path):
        logger.error(f"找不到配置文件: {config_path}")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    defaults = {
        "mode": "single",
        "target": "",
        "save_path": "./downloads",
        "quality": "480",
        "proxy": "",
        "proxy_auth": "",
        "model_end_page": 20,
        "model_start_page": 1,
        "remux": True,
        "request_delay": 4,
        "download_max_retry": 3,
        "retry_sleep": 3,
        "interval": "all",
        "userlist": [],          # 新增
    }
    for k, v in defaults.items():
        cfg.setdefault(k, v)

    # 兼容旧配置：没有 userlist 时用全局 target
    if not cfg.get("userlist") and not cfg.get("target"):
        logger.error("target 或 userlist 不能同时为空")
        sys.exit(1)

    cfg["model_start_page"] = int(cfg["model_start_page"])
    cfg["model_end_page"] = int(cfg["model_end_page"])
    if cfg["model_start_page"] < 1:
        logger.error("model_start_page >= 1")
        sys.exit(1)

    return cfg


def update_user_interval(config_path: str, user_name: str, new_interval: str) -> None:
    """回写指定 user 的 interval"""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

        updated = False
        for user in cfg.get("userlist", []):
            if user.get("name") == user_name:
                user["interval"] = new_interval
                updated = True
                break

        # 兼容旧单 target 写法
        if not updated and not cfg.get("userlist"):
            cfg["interval"] = new_interval
            updated = True

        if updated:
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
            logger.info(f"已回写 [{user_name or 'global'}] interval = {new_interval}")
        else:
            logger.warning(f"未找到 name={user_name} 的用户，interval 未回写")
    except Exception as e:
        logger.error(f"回写 interval 失败: {e}")


def parse_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    if not s or s.lower() == "all":
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            return datetime.strptime(s[:26].replace("Z", ""), fmt)
        except ValueError:
            continue
    try:
        if len(s) >= 19:
            return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        if len(s) >= 10:
            return datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        pass
    return None


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:180]


async def safe_download_video(
    client: Client,
    url: str,
    cfg: dict,
    index: int = 0,
    total: int = 0,
) -> str:
    prefix = f"[{index}/{total}] " if index and total else ""
    logger.info(f"{prefix}正在获取: {url}")
    try:
        video = await client.get_video(url, load_html=True, load_api=True)
    except Exception as e:
        logger.error(f"{prefix}获取视频信息失败: {e} | URL: {url}")
        return "failed"

    logger.info(f"{prefix}{video.title} 发布于 {video.publish_date}")

    # interval 过滤
    interval_raw = str(cfg.get("interval", "all")).strip()
    if interval_raw.lower() != "all":
        interval_dt = parse_datetime(interval_raw)
        publish_dt = parse_datetime(getattr(video, "publish_date", None))
        if interval_dt is None:
            logger.warning(f"{prefix}interval 无法解析: {interval_raw}，本次跳过日期过滤")
        elif publish_dt is None:
            logger.warning(f"{prefix}无法解析 publish_date={video.publish_date}，跳过该视频 | URL: {url}")
            return "skipped"
        elif publish_dt <= interval_dt:
            logger.info(f"{prefix}发布日期 {publish_dt} <= interval {interval_dt}，跳过")
            return "skipped"

    raw_title = video.title or video.video_id or "untitled"
    safe_title = sanitize_filename(raw_title)
    output_filename = f"{safe_title}.mp4"

    save_dir = Path(cfg["save_path"])
    save_dir.mkdir(parents=True, exist_ok=True)

    if (save_dir / output_filename).exists():
        logger.info(f"{prefix}文件已存在，跳过：{output_filename}")
        return "skipped"

    # 强制安全文件名，避免 Windows Errno 22
    video.title = safe_title

    dl_config = DownloadConfigHLS(
        quality=str(cfg["quality"]),
        path=str(save_dir),
        remux=bool(cfg.get("remux", True)),
    )

    logger.info(f"{prefix}开始下载: {output_filename}")
    for attempt in range(1, cfg["download_max_retry"] + 1):
        try:
            await video.download(configuration=dl_config)
            logger.info(f"{prefix}完成: {output_filename}")
            return "success"
        except Exception as e:
            err_msg = str(e)
            if "curl: (28)" in err_msg or "Timeout" in err_msg or "timeout" in err_msg.lower():
                logger.warning(
                    f"{prefix}下载超时 {attempt}/{cfg['download_max_retry']}，"
                    f"等待{cfg['retry_sleep']}s重试... | URL: {url}"
                )
                await asyncio.sleep(cfg["retry_sleep"])
            else:
                logger.error(f"{prefix}下载异常: {err_msg} | URL: {url}")
                return "failed"

    logger.error(f"{prefix}失败: {output_filename} 达到最大重试次数 | URL: {url}")
    return "failed"


def sync_fetch_video_links(page_url: str, proxy_str: str, retry_times=3):
    link_list = []
    proxies = {}
    if proxy_str:
        proxies = {"http": proxy_str, "https": proxy_str}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    for tr in range(retry_times):
        try:
            response = requests.get(page_url, headers=headers, proxies=proxies, timeout=20)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            a_tags = soup.select('ul.full-row-thumbs a[href*="/view_video.php?viewkey="]')
            if not a_tags:
                return []
            for a in a_tags:
                href = a.get("href")
                full_url = urljoin(page_url, href)
                link_list.append(full_url)

            seen = set()
            unique_links = []
            for link in link_list:
                if link not in seen:
                    seen.add(link)
                    unique_links.append(link)
            return unique_links
        except Exception as e:
            logger.error(f"页面请求失败 {tr+1}/{retry_times} | {page_url} | 错误:{e}")
            time.sleep(3)
    return []


async def fetch_model_video_links(page_url: str, proxy_str: str, retry_times=3):
    return await asyncio.to_thread(sync_fetch_video_links, page_url, proxy_str, retry_times)


async def download_model_videos(client: Client, raw_model_url: str, cfg: dict, user_name: str = ""):
    save_dir = Path(cfg["save_path"])
    save_dir.mkdir(parents=True, exist_ok=True)

    start_page = int(cfg["model_start_page"])
    end_page = int(cfg["model_end_page"])
    proxy = cfg.get("proxy", "")
    success = skipped = failed = 0
    failed_urls: list[str] = []

    if "/videos" in raw_model_url:
        base_model_url = raw_model_url.split("/videos")[0].rstrip("/")
    else:
        base_model_url = raw_model_url.rstrip("/")

    logger.info(f"===== 开始处理用户: {user_name or base_model_url} =====")
    logger.info(f"基础地址：{base_model_url}")
    logger.info(f"保存目录：{save_dir}")
    logger.info(f"interval：{cfg.get('interval', 'all')}")
    if end_page <= 0:
        logger.info(f"分页范围 起始页:{start_page} → 无上限")
    else:
        logger.info(f"分页范围 起始页:{start_page} → 终止页:{end_page}")

    current_page = start_page
    while True:
        target_page_url = f"{base_model_url}/videos?page={current_page}"
        logger.info(f"----- 处理第{current_page}页 | {target_page_url} -----")

        video_urls = await fetch_model_video_links(target_page_url, proxy)
        if not video_urls:
            logger.info(f"第{current_page}页无视频链接，判定尾页，停止爬取")
            break

        # video_urls_reversed = list(reversed(video_urls))
        video_urls_reversed = video_urls
        total = len(video_urls_reversed)
        logger.info(f"本页视频数量：{total}")

        for idx, video_link in enumerate(video_urls_reversed, start=1):
            result = await safe_download_video(
                client, video_link, cfg, index=idx, total=total
            )
            if result == "success":
                success += 1
            elif result == "skipped":
                skipped += 1
            else:
                failed += 1
                failed_urls.append(video_link)
            await asyncio.sleep(float(cfg["request_delay"]))

        current_page += 1
        if end_page > 0 and current_page > end_page:
            logger.info(f"到达设定终止页码 {end_page}")
            break
        await asyncio.sleep(float(cfg["request_delay"]))

    logger.info(f"========== [{user_name or base_model_url}] 任务汇总 ==========")
    logger.info(f"成功: {success} | 跳过: {skipped} | 失败: {failed}")

    if failed_urls:
        logger.error("---------- 失败 URL 列表 ----------")
        for i, u in enumerate(failed_urls, 1):
            logger.error(f"  [{i}] {u}")
        logger.error("----------------------------------")
    else:
        logger.info("无失败 URL")

    # 仅从第1页跑完时回写该用户的 interval
    if start_page == 1:
        today_zero = datetime.now().strftime("%Y-%m-%d 00:00:00")
        update_user_interval(CONFIG_PATH, user_name, today_zero)
    else:
        logger.info(f"起始页为 {start_page}（非第1页），不回写 interval")


def merge_user_cfg(global_cfg: dict, user: dict) -> dict:
    """全局配置 + 单个用户配置合并"""
    merged = global_cfg.copy()
    for key in ("target", "save_path", "interval", "model_start_page", "model_end_page", "name"):
        if key in user and user[key] is not None:
            merged[key] = user[key]
    # 保证数字类型
    merged["model_start_page"] = int(merged.get("model_start_page", 1))
    merged["model_end_page"] = int(merged.get("model_end_page", 20))
    return merged


async def main():
    cfg = load_config(CONFIG_PATH)
    runtime = RuntimeConfig()
    runtime.request_delay = float(cfg["request_delay"])
    runtime.timeout = 60
    runtime.max_retries = 5

    proxy = cfg.get("proxy", "").strip()
    if proxy:
        runtime.proxies = {"http": proxy, "https": proxy}
        logger.info(f"代理已启用: {proxy}")
        if cfg.get("proxy_auth"):
            runtime.proxy_auth = cfg["proxy_auth"]
    else:
        logger.info("未配置代理")

    core = BaseCore(configuration=runtime)
    client = Client(core=core)

    mode = cfg["mode"].lower()
    logger.info(f"运行模式: {mode}")

    if mode == "single":
        target_url = cfg["target"].strip()
        if not target_url:
            logger.error("single 模式需要 target")
            sys.exit(1)
        result = await safe_download_video(client, target_url, cfg)
        if result == "failed":
            logger.error(f"single 模式下载失败 | URL: {target_url}")

    elif mode == "model":
        userlist = cfg.get("userlist") or []

        if userlist:
            logger.info(f"共 {len(userlist)} 个用户，开始依次处理")
            for idx, user in enumerate(userlist, 1):
                user_cfg = merge_user_cfg(cfg, user)
                name = user_cfg.get("name") or f"user_{idx}"
                target = user_cfg.get("target", "").strip()
                if not target:
                    logger.warning(f"[{name}] 缺少 target，跳过")
                    continue
                logger.info(f"\n########## [{idx}/{len(userlist)}] 处理用户: {name} ##########")
                await download_model_videos(client, target, user_cfg, user_name=name)
        else:
            # 兼容旧单 target 写法
            target_url = cfg["target"].strip()
            if not target_url:
                logger.error("model 模式需要 target 或 userlist")
                sys.exit(1)
            await download_model_videos(client, target_url, cfg, user_name="")

    else:
        logger.error("mode 仅支持 single / model")
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("用户主动中断，程序退出")