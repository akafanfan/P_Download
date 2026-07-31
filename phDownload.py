#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pornhub Downloader
1. requests + BeautifulSoup 解析列表
2. 当前页面视频反序下载，带序号
3. interval 增量过滤：publish_date > interval 才下载；all 则不过滤
4. 从第1页跑完后，把 interval 回写为当日零点
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

# 配置日志
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
        "interval": "all",  # "all" 不过滤；否则为时间字符串，如 "2026-01-01 00:00:00"
    }
    for k, v in defaults.items():
        cfg.setdefault(k, v)

    if not cfg["target"]:
        logger.error("target 链接不能为空")
        sys.exit(1)

    cfg["model_start_page"] = int(cfg["model_start_page"])
    cfg["model_end_page"] = int(cfg["model_end_page"])
    if cfg["model_start_page"] < 1:
        logger.error("model_start_page >= 1")
        sys.exit(1)

    return cfg


def update_config_interval(config_path: str, new_interval: str) -> None:
    """把配置文件中的 interval 改成新值并写回"""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        cfg["interval"] = new_interval
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
        logger.info(f"已回写配置 interval = {new_interval}")
    except Exception as e:
        logger.error(f"回写配置文件 interval 失败: {e}")


def parse_datetime(value) -> datetime | None:
    """尽量把 publish_date / interval 解析成 datetime"""
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
    # 尝试只取前 19/10 位
    try:
        if len(s) >= 19:
            return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        if len(s) >= 10:
            return datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        pass
    return None


def sanitize_filename(name: str) -> str:
    """清理文件名非法字符"""
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:180]


def file_already_exists(save_dir: Path, full_name: str) -> bool:
    target_file = save_dir / full_name
    return target_file.exists()


async def safe_download_video(
    client: Client,
    url: str,
    cfg: dict,
    index: int = 0,
    total: int = 0,
) -> str:
    """
    返回值:
        "skipped" - 已存在 / 日期不满足 interval，跳过
        "success" - 下载成功
        "failed"  - 下载失败
    """
    prefix = f"[{index}/{total}] " if index and total else ""
    logger.info(f"{prefix}正在获取: {url}")
    try:
        video = await client.get_video(url, load_html=True, load_api=True)
    except Exception as e:
        logger.error(f"{prefix}获取视频信息失败: {e}")
        return "failed"

    logger.info(f"{prefix}{video.title} 发布于 {video.publish_date}")

    # ---------- interval 过滤 ----------
    interval_raw = str(cfg.get("interval", "all")).strip()
    if interval_raw.lower() != "all":
        interval_dt = parse_datetime(interval_raw)
        publish_dt = parse_datetime(getattr(video, "publish_date", None))
        if interval_dt is None:
            logger.warning(f"{prefix}interval 无法解析: {interval_raw}，本次跳过日期过滤")
        elif publish_dt is None:
            logger.warning(f"{prefix}无法解析 publish_date={video.publish_date}，跳过该视频")
            return "skipped"
        elif publish_dt <= interval_dt:
            logger.info(
                f"{prefix}发布日期 {publish_dt} <= interval {interval_dt}，跳过"
            )
            return "skipped"
    # -----------------------------------

    raw_title = video.title or video.video_id or "untitled"
    safe_title = sanitize_filename(raw_title)
    output_filename = f"{safe_title}.mp4"

    save_dir = Path(cfg["save_path"])
    save_dir.mkdir(parents=True, exist_ok=True)

    if (save_dir / output_filename).exists():
        logger.info(f"{prefix}文件已存在，跳过：{output_filename}")
        return "skipped"

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
                    f"{prefix}下载超时 {attempt}/{cfg['download_max_retry']}，等待{cfg['retry_sleep']}s重试..."
                )
                await asyncio.sleep(cfg["retry_sleep"])
            else:
                logger.error(f"{prefix}下载异常: {err_msg}")
                return "failed"

    logger.error(f"{prefix}失败: {output_filename} 达到最大重试次数")
    return "failed"


# ========= 同步requests + BeautifulSoup 解析页面 =========
def sync_fetch_video_links(page_url: str, proxy_str: str, retry_times=3):
    link_list = []
    proxies = {}
    if proxy_str:
        proxies = {
            "http": proxy_str,
            "https": proxy_str
        }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    for tr in range(retry_times):
        try:
            response = requests.get(page_url, headers=headers, proxies=proxies, timeout=20)
            response.raise_for_status()
            html_content = response.text
            soup = BeautifulSoup(html_content, 'html.parser')
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
    result = await asyncio.to_thread(sync_fetch_video_links, page_url, proxy_str, retry_times)
    return result


async def download_model_videos(client: Client, raw_model_url: str, cfg: dict):
    save_dir = Path(cfg["save_path"])
    save_dir.mkdir(parents=True, exist_ok=True)

    start_page = cfg["model_start_page"]
    end_page = cfg["model_end_page"]
    proxy = cfg.get("proxy", "")
    success = skipped = failed = 0

    if "/videos" in raw_model_url:
        base_model_url = raw_model_url.split("/videos")[0].rstrip("/")
    else:
        base_model_url = raw_model_url.rstrip("/")

    logger.info(f"Model批量 基础地址：{base_model_url}")
    logger.info(f"interval 过滤: {cfg.get('interval', 'all')}")
    if end_page <= 0:
        logger.info(f"分页范围 起始页:{start_page} → 无上限")
    else:
        logger.info(f"分页范围 起始页:{start_page} → 终止页:{end_page}")

    current_page = start_page
    while True:
        target_page_url = f"{base_model_url}/videos?page={current_page}"
        logger.info(f"===== 处理第{current_page}页 | {target_page_url} =====")

        video_urls = await fetch_model_video_links(target_page_url, proxy)
        if not video_urls:
            logger.info(f"第{current_page}页无视频链接，判定尾页，停止爬取")
            break

        video_urls_reversed = list(reversed(video_urls))
        total = len(video_urls_reversed)
        logger.info(f"本页原生视频数量：{total}，启用反序下载")

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
            await asyncio.sleep(float(cfg["request_delay"]))

        current_page += 1
        if end_page > 0 and current_page > end_page:
            logger.info(f"终止：到达设定终止页码 {end_page}")
            break
        await asyncio.sleep(float(cfg["request_delay"]))

    logger.info("========== 任务汇总 ==========")
    logger.info(f"成功: {success} | 跳过: {skipped} | 失败: {failed}")

    # 仅当本次从第 1 页开始跑完时，把 interval 回写为当日零点
    if start_page == 1:
        today_zero = datetime.now().strftime("%Y-%m-%d 00:00:00")
        update_config_interval(CONFIG_PATH, today_zero)
    else:
        logger.info(f"起始页为 {start_page}（非第1页），不回写 interval")


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
    target_url = cfg["target"].strip()
    logger.info(f"运行模式: {mode}")
    logger.info(f"目标链接: {target_url}")
    logger.info(f"保存目录: {cfg['save_path']}")
    logger.info(f"interval: {cfg.get('interval', 'all')}")

    if mode == "single":
        await safe_download_video(client, target_url, cfg)
    elif mode == "model":
        await download_model_videos(client, target_url, cfg)
    else:
        logger.error("mode仅支持 single / model")
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("用户主动中断，程序退出")